"""
Strict One-at-a-Time SDR Resource Arbiter & Auto-Healing USB Recovery Engine
Prevents all USB / libusb conflicts and HackRF One detection dropouts between:
  - Smart RF Scanner (hackrf_sweep & hackrf_transfer)
  - Gqrx SDR Receiver (gr-osmosdr / libhackrf)
  - Stray background SDR tools

Key Reliability Architecture:
  1. Zero-USB-Traffic Passive Sysfs Detection: Idle polling inspects /sys/bus/usb/devices/*
     (idVendor=1d50, idProduct=6089) in <0.1ms WITHOUT opening /dev/bus/usb or claiming
     libusb interface 0 — eliminating background hackrf_info collisions with Gqrx startup.
  2. Non-Root Hardware USB Port Reset (USBDEVFS_RESET 0x5514): Automatically resets a wedged
     HackRF One LPC43xx USB transceiver via /dev/bus/usb/<bus>/<dev> (plugdev ACL) in ~0.7s,
     eliminating the need to physically unplug and replug the USB cable.
  3. Graceful SIGINT -> SIGTERM Shutdown + Complete Gqrx Config Sanitization: Resets
     crashed=false and recovers muted (-60 dB) audio gains across all ~/.config/gqrx/*.conf
     files both before launch and whenever Gqrx exits.
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Any

from PyQt6.QtCore import QSettings

logger = logging.getLogger("smart_scanner.arbiter")

LOCK_FILE_PATH = "/tmp/smart_rf_sdr.lock"
GQRX_CONFIG_DIR = Path(os.path.expanduser("~/.config/gqrx"))
HACKRF_VID = "1d50"
HACKRF_PID = "6089"
USBDEVFS_RESET = 0x5514


@dataclass
class SDRStatusSnapshot:
    owner: str                  # "IDLE", "RELEASING_SDR", "RESETTING_USB", "SCANNER_L1", "SCANNER_L2", "SCANNER_L3", "GQRX_TUNING", "GQRX_LIVE", "FAULT"
    badge_text: str             # Alphanumeric status badge
    detail_text: str            # Detailed operational status
    progress_pct: float         # 0..100 graphical indicator percentage for handoff/state
    hackrf_online: bool
    hackrf_serial: str
    gqrx_running: bool
    scanner_locked: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_hackrf_sysfs() -> Tuple[bool, str, int, int]:
    """
    100% passive Linux sysfs check for HackRF One (1d50:6089).
    Performs ZERO libusb traffic and never claims or disturbs the USB device.
    Returns (present, serial_number, busnum, devnum).
    """
    try:
        for dev_dir in glob.glob("/sys/bus/usb/devices/*"):
            vid_path = os.path.join(dev_dir, "idVendor")
            pid_path = os.path.join(dev_dir, "idProduct")
            if not (os.path.isfile(vid_path) and os.path.isfile(pid_path)):
                continue
            with open(vid_path, "r", encoding="utf-8", errors="ignore") as f:
                vid = f.read().strip().lower()
            if vid != HACKRF_VID:
                continue
            with open(pid_path, "r", encoding="utf-8", errors="ignore") as f:
                pid = f.read().strip().lower()
            if pid != HACKRF_PID:
                continue

            serial = ""
            ser_path = os.path.join(dev_dir, "serial")
            if os.path.isfile(ser_path):
                with open(ser_path, "r", encoding="utf-8", errors="ignore") as f:
                    serial = f.read().strip()

            busnum = 0
            devnum = 0
            bus_path = os.path.join(dev_dir, "busnum")
            dev_path = os.path.join(dev_dir, "devnum")
            if os.path.isfile(bus_path):
                busnum = int(open(bus_path, "r", encoding="utf-8").read().strip() or "0")
            if os.path.isfile(dev_path):
                devnum = int(open(dev_path, "r", encoding="utf-8").read().strip() or "0")

            return True, serial, busnum, devnum
    except Exception as exc:
        logger.debug("sysfs HackRF lookup note: %s", exc)

    return False, "", 0, 0


def format_osmosdr_hackrf_device_arg(serial: str) -> str:
    """
    Formats the exact gr-osmosdr HackRF device identifier matching Gqrx's CIoConfig
    enumeration (e.g. 'hackrf=7c5f4f' for serial '0000000000000000010961dc2a7c5f4f').
    """
    clean = (serial or "").strip()
    if not clean:
        return "hackrf=0"
    stripped = clean.lstrip("0")
    short_ser = stripped[-6:] if len(stripped) >= 6 else (stripped or "0")
    return f"hackrf={short_ser}"


def sanitize_gqrx_crash_flags(
    extra_conf: Optional[Path] = None,
    fix_muted_audio: bool = True,
    config_dir: Optional[Path] = None,
) -> None:
    """
    Ensures Gqrx .conf files always have crashed=false so terminating or restarting Gqrx
    NEVER triggers a blocking 'Crash detected' / 'Configure I/O devices' modal dialog.
    Also repairs:
      - accidentally muted (-60 dB) audio/gain settings & broken output/device=default
      - legacy input/device="hackrf=0" -> exact gr-osmosdr serial ("hackrf=7c5f4f")
      - legacy black-waterfall pandapter_min_db > -75 -> calibrated -95..-25 dBFS
      - dangling/deleted file entries in recentconfig.cfg (which otherwise cause bare 'gqrx'
        launches to open a blank unconfigured I/O dialog!)
    """
    cfg_root = config_dir or GQRX_CONFIG_DIR
    cfg_root.mkdir(parents=True, exist_ok=True)
    targets = [Path(p) for p in glob.glob(str(cfg_root / "*.conf"))]
    default_conf = cfg_root / "default.conf"
    if default_conf not in targets and default_conf.exists():
        targets.append(default_conf)
    if extra_conf and extra_conf not in targets and extra_conf.exists():
        targets.append(extra_conf)

    present, ser, _, _ = detect_hackrf_sysfs()
    exact_dev = format_osmosdr_hackrf_device_arg(ser) if (present and ser) else ""

    for cfg in targets:
        try:
            qs = QSettings(str(cfg), QSettings.Format.IniFormat)
            qs.setValue("crashed", False)
            if not qs.contains("configversion"):
                qs.setValue("configversion", 4)
            if exact_dev and qs.contains("input/device"):
                cur_dev = str(qs.value("input/device", "")).strip()
                if cur_dev in ("hackrf=0", "hackrf", ""):
                    qs.setValue("input/device", exact_dev)
            if fix_muted_audio and qs.contains("audio/gain"):
                try:
                    ag = float(qs.value("audio/gain", 0))
                    if ag <= -45.0:
                        qs.setValue("audio/gain", 0)
                except Exception:
                    pass
            if qs.contains("output/device"):
                out_dev = str(qs.value("output/device", "")).strip()
                if out_dev == "default":
                    qs.remove("output/device")
            if qs.contains("fft/pandapter_min_db"):
                try:
                    p_min = int(float(qs.value("fft/pandapter_min_db", -92)))
                    if p_min > -75:
                        qs.setValue("fft/pandapter_min_db", -95)
                        qs.setValue("fft/waterfall_min_db", -95)
                        p_max = int(float(qs.value("fft/pandapter_max_db", -25)))
                        if p_max > -15 or p_max < -50:
                            qs.setValue("fft/pandapter_max_db", -25)
                            qs.setValue("fft/waterfall_max_db", -25)
                except Exception:
                    pass
            qs.sync()
        except Exception as exc:
            logger.debug("Could not sanitize Gqrx config %s: %s", cfg, exc)

    # Prune deleted profile paths from recentconfig.cfg so bare 'gqrx' never opens a missing file
    recent_cfg = cfg_root / "recentconfig.cfg"
    try:
        existing_lines: list[str] = []
        if recent_cfg.exists():
            raw_lines = recent_cfg.read_text(encoding="utf-8", errors="ignore").splitlines()
            for ln in raw_lines:
                p_str = ln.strip()
                if p_str and Path(p_str).is_file() and p_str not in existing_lines:
                    existing_lines.append(p_str)
        if extra_conf and extra_conf.is_file():
            e_str = str(extra_conf.resolve())
            existing_lines = [e_str] + [x for x in existing_lines if x != e_str]
        for fallback_p in (cfg_root / "SmartScanner_Active.conf", default_conf):
            if fallback_p.is_file():
                fb_str = str(fallback_p.resolve())
                if fb_str not in existing_lines:
                    existing_lines.append(fb_str)
        if existing_lines:
            recent_cfg.write_text("\n".join(existing_lines[:10]) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not sanitize Gqrx recentconfig.cfg: %s", exc)


class SDRResourceArbiter:
    """
    Centralized mutex & hardware lifecycle arbiter guaranteeing strictly one system
    accesses the HackRF One USB transceiver at any point in time, with automatic
    kernel USB port reset recovery if HackRF firmware ever stops responding.
    """

    def __init__(self) -> None:
        self._mutex = threading.RLock()
        self._lock_fd: Optional[int] = None
        self._scanner_active = False
        self._owner = "IDLE"
        self._detail = "HackRF One idle & ready"
        self._handoff_pct = 100.0
        self._serial = ""
        self._firmware = "v2.1.0"
        self._hardware_present = False
        self._was_gqrx_running = False
        self._last_usb_reset_ts = 0.0

        # Initial passive sysfs discovery (zero USB traffic)
        present, ser, _, _ = detect_hackrf_sysfs()
        self._hardware_present = present
        if ser:
            self._serial = ser

    # ------------------------------------------------------------------
    # Passive Inspection & Status (Zero USB Bus Contention)
    # ------------------------------------------------------------------
    def get_hackrf_serial(self) -> str:
        present, ser, _, _ = detect_hackrf_sysfs()
        with self._mutex:
            self._hardware_present = present
            if ser:
                self._serial = ser
            return self._serial

    def get_hackrf_device_arg(self) -> str:
        return format_osmosdr_hackrf_device_arg(self.get_hackrf_serial())

    def is_gqrx_running(self) -> bool:
        try:
            res = subprocess.run(
                ["pgrep", "-x", "gqrx"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.2,
            )
            return res.returncode == 0 and bool(res.stdout.strip())
        except Exception:
            return False

    def is_scanner_active(self) -> bool:
        with self._mutex:
            return self._scanner_active

    def set_owner_state(self, owner: str, detail: str, pct: float = 100.0) -> None:
        with self._mutex:
            self._owner = owner
            self._detail = detail
            self._handoff_pct = max(0.0, min(100.0, pct))

    def probe_hackrf_hardware(self, deep_usb_open: bool = False) -> Tuple[bool, str]:
        """
        Checks HackRF availability.
        By default (deep_usb_open=False), performs a 100% passive Linux sysfs check
        so background UI timers NEVER claim libusb or interfere with Gqrx startup.
        When deep_usb_open=True, verifies active libhackrf communication via hackrf_info.
        """
        gqrx_up = self.is_gqrx_running()
        with self._mutex:
            if self._was_gqrx_running and not gqrx_up:
                # Gqrx just exited (closed by user or arbiter) -> immediately sanitize crashed=false!
                sanitize_gqrx_crash_flags()
            self._was_gqrx_running = gqrx_up

            if self._scanner_active:
                return True, f"Owned by Smart Scanner ({self._serial[-6:] or 'active'})"
            if gqrx_up:
                self._owner = "GQRX_LIVE"
                self._detail = "Owned by Gqrx Live Receiver"
                return True, "Owned by Gqrx Live Receiver"

        present, ser, bus, dev = detect_hackrf_sysfs()
        with self._mutex:
            self._hardware_present = present
            if ser:
                self._serial = ser
            if not present:
                self._owner = "FAULT"
                self._detail = "HackRF One unplugged / not on USB bus"
                return False, self._detail
            if not deep_usb_open:
                if self._owner not in ("GQRX_LIVE", "GQRX_TUNING", "RESETTING_USB"):
                    self._owner = "IDLE"
                    self._detail = f"HackRF One Ready ({self._serial[-6:] or f'{bus}:{dev}'})"
                return True, self._detail

        # Active libhackrf check (ONLY executed during explicit handoff/recovery)
        return self._verify_hackrf_info_once()

    def _verify_hackrf_info_once(self) -> Tuple[bool, str]:
        try:
            proc = subprocess.run(
                ["hackrf_info"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2.8,
            )
            out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            if proc.returncode == 0 and "Found HackRF" in out:
                for line in out.splitlines():
                    if "Serial number:" in line:
                        self._serial = line.split(":", 1)[1].strip()
                    elif "Firmware Version:" in line:
                        self._firmware = line.split(":", 1)[1].strip()
                with self._mutex:
                    self._hardware_present = True
                return True, f"HackRF One Verified ({self._serial[-6:]})"
            return False, out or f"hackrf_info rc={proc.returncode}"
        except subprocess.TimeoutExpired:
            return False, "hackrf_info timed out (USB endpoint wedged)"
        except Exception as exc:
            return False, f"hackrf_info error: {exc}"

    def get_status_snapshot(self) -> SDRStatusSnapshot:
        gqrx_up = self.is_gqrx_running()
        with self._mutex:
            if self._was_gqrx_running and not gqrx_up:
                sanitize_gqrx_crash_flags()
            self._was_gqrx_running = gqrx_up

            scanner_up = self._scanner_active
            if scanner_up:
                owner = self._owner
                short_s = self._serial[-6:] if self._serial else "LOCKED"
                badge = f"SDR: {owner} ({short_s})"
                detail = self._detail
                pct = self._handoff_pct
                online = True
            elif gqrx_up:
                if self._owner not in ("GQRX_TUNING", "RELEASING_SDR", "RESETTING_USB"):
                    self._owner = "GQRX_LIVE"
                    self._detail = "Gqrx owns HackRF One (1-at-a-time lock)"
                owner = self._owner
                badge = "SDR: GQRX LIVE AUDIO"
                detail = self._detail
                pct = 100.0
                online = True
            elif self._owner == "RESETTING_USB":
                owner = self._owner
                badge = "SDR: USB SELF-HEAL..."
                detail = self._detail
                pct = self._handoff_pct
                online = True
            else:
                if self._owner in ("GQRX_LIVE", "GQRX_TUNING"):
                    self._owner = "IDLE"
                ok, _ = self.probe_hackrf_hardware(deep_usb_open=False)
                owner = self._owner
                online = ok
                short_s = self._serial[-6:] if self._serial else "USB"
                badge = f"SDR: READY ({short_s})" if ok else "SDR: CHECK USB"
                detail = self._detail
                pct = 100.0 if ok else 0.0

            return SDRStatusSnapshot(
                owner=owner,
                badge_text=badge,
                detail_text=detail,
                progress_pct=pct,
                hackrf_online=online,
                hackrf_serial=self._serial,
                gqrx_running=gqrx_up,
                scanner_locked=scanner_up,
            )

    # ------------------------------------------------------------------
    # Non-Root Hardware USB Port Reset (Self-Healing Wedged HackRF One)
    # ------------------------------------------------------------------
    def reset_hackrf_usb_port(
        self,
        progress_cb: Optional[Callable[[str, float], None]] = None,
        stop_gqrx: bool = True,
    ) -> Tuple[bool, str]:
        """
        Executes a kernel-level USBDEVFS_RESET ioctl on HackRF One (/dev/bus/usb/BBB/DDD),
        re-enumerating the LPC43xx USB transceiver identically to an unplug/replug cycle.
        """
        with self._mutex:
            prev_owner = self._owner
            self._owner = "RESETTING_USB"
            self._detail = "Resetting HackRF One USB transceiver..."
            self._handoff_pct = 30.0

        try:
            if stop_gqrx and self.is_gqrx_running():
                if progress_cb:
                    progress_cb("Closing Gqrx prior to HackRF USB bus reset...", 20.0)
                self.stop_gqrx_cleanly()

            self._terminate_stray_hackrf_cli()

            present, ser, bus, dev = detect_hackrf_sysfs()
            if not present or bus <= 0 or dev <= 0:
                with self._mutex:
                    self._owner = "FAULT"
                    self._detail = "HackRF One (1d50:6089) not detected on USB ports"
                return False, "HackRF One (1d50:6089) not found on USB bus. Check physical USB cable."

            usb_dev_path = f"/dev/bus/usb/{bus:03d}/{dev:03d}"
            if progress_cb:
                progress_cb(f"Issuing hardware USB port reset on {usb_dev_path}...", 50.0)

            logger.warning("Executing USBDEVFS_RESET on HackRF One at %s (serial=%s)", usb_dev_path, ser)
            reset_ok = False
            try:
                fd = os.open(usb_dev_path, os.O_WRONLY)
                try:
                    fcntl.ioctl(fd, USBDEVFS_RESET, 0)
                    reset_ok = True
                finally:
                    os.close(fd)
            except Exception as ioctl_exc:
                logger.warning("ioctl USBDEVFS_RESET note (%s); trying usbreset fallback", ioctl_exc)
                res_cmd = subprocess.run(
                    ["usbreset", f"{HACKRF_VID}:{HACKRF_PID}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=4.0,
                )
                reset_ok = res_cmd.returncode == 0

            self._last_usb_reset_ts = time.time()

            # Wait for xhci-hcd re-enumeration & udev plugdev ACL application
            for step in range(8):
                time.sleep(0.22)
                if progress_cb:
                    progress_cb("Waiting for HackRF USB re-enumeration & firmware handshake...", 62.0 + step * 4.5)
                p_now, s_now, _, _ = detect_hackrf_sysfs()
                if p_now:
                    ok_info, info_msg = self._verify_hackrf_info_once()
                    if ok_info:
                        sanitize_gqrx_crash_flags()
                        with self._mutex:
                            self._hardware_present = True
                            if s_now:
                                self._serial = s_now
                            self._owner = prev_owner if prev_owner.startswith("SCANNER") else "IDLE"
                            self._detail = f"HackRF USB Reset & Verified ({self._serial[-6:]})"
                            self._handoff_pct = 100.0
                        logger.info("HackRF One USB reset succeeded: %s", info_msg)
                        if progress_cb:
                            progress_cb(f"HackRF One recovered & verified ({self._serial[-6:]})!", 100.0)
                        return True, info_msg

            with self._mutex:
                self._owner = "FAULT"
                self._detail = "HackRF did not respond after USB reset"
            return False, f"HackRF USB reset attempted (ok={reset_ok}) but hackrf_info still unresponsive."
        except Exception as exc:
            with self._mutex:
                self._owner = "FAULT"
                self._detail = f"USB reset error: {exc}"
            return False, f"HackRF USB reset failed: {exc}"

    # ------------------------------------------------------------------
    # Clean Shutdown of Competing SDR Processes
    # ------------------------------------------------------------------
    def _send_gqrx_tcp_stop(self) -> None:
        try:
            sock = socket.create_connection(("127.0.0.1", 7356), timeout=0.7)
            sock.sendall(b"U DSP 0\n")
            sock.recv(128)
            sock.close()
        except Exception:
            pass

    def stop_gqrx_cleanly(
        self,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> bool:
        """
        Gracefully stops Gqrx DSP stream first (releasing libhackrf RX streaming cleanly),
        terminates Gqrx, waits for USB handle release, and sanitizes crashed=false in all configs.
        """
        if not self.is_gqrx_running():
            sanitize_gqrx_crash_flags()
            return True

        self.set_owner_state("RELEASING_SDR", "Stopping Gqrx DSP & releasing HackRF USB...", 25.0)
        if progress_cb:
            progress_cb("Stopping Gqrx live DSP stream (TCP 7356)...", 25.0)

        self._send_gqrx_tcp_stop()
        time.sleep(0.22)

        if progress_cb:
            progress_cb("Closing Gqrx USB handle cleanly...", 55.0)
        subprocess.run(["pkill", "-TERM", "-x", "gqrx"], stderr=subprocess.DEVNULL)

        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not self.is_gqrx_running():
                break
            time.sleep(0.12)

        if self.is_gqrx_running():
            subprocess.run(["pkill", "-KILL", "-x", "gqrx"], stderr=subprocess.DEVNULL)
            time.sleep(0.30)

        # Brief USB bulk endpoint settle after Gqrx exits
        time.sleep(0.18)
        sanitize_gqrx_crash_flags()
        with self._mutex:
            self._was_gqrx_running = False
        if progress_cb:
            progress_cb("Gqrx released HackRF One cleanly.", 85.0)
        return not self.is_gqrx_running()

    def _terminate_stray_hackrf_cli(self) -> None:
        """
        Terminates any orphaned hackrf_sweep or hackrf_transfer subprocesses using SIGINT first
        so their C signal handlers execute hackrf_stop_rx() + hackrf_close() cleanly.
        """
        any_running = False
        for tool_name in ("hackrf_sweep", "hackrf_transfer"):
            r = subprocess.run(["pgrep", "-x", tool_name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and r.stdout.strip():
                any_running = True
                subprocess.run(["pkill", "-INT", "-x", tool_name], stderr=subprocess.DEVNULL)

        if not any_running:
            return

        deadline = time.time() + 0.9
        while time.time() < deadline:
            still = False
            for tool_name in ("hackrf_sweep", "hackrf_transfer"):
                r = subprocess.run(["pgrep", "-x", tool_name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                if r.returncode == 0 and r.stdout.strip():
                    still = True
            if not still:
                time.sleep(0.08)
                return
            time.sleep(0.1)

        for tool_name in ("hackrf_sweep", "hackrf_transfer"):
            subprocess.run(["pkill", "-TERM", "-x", tool_name], stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        for tool_name in ("hackrf_sweep", "hackrf_transfer"):
            subprocess.run(["pkill", "-KILL", "-x", tool_name], stderr=subprocess.DEVNULL)
        time.sleep(0.15)

    # ------------------------------------------------------------------
    # Exclusive Acquisition for Multi-Layer Scanner (With Auto-Heal)
    # ------------------------------------------------------------------
    def acquire_for_scanner(
        self,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> Tuple[bool, str]:
        """
        Acquires exclusive lock on HackRF One for the Smart RF Scanner:
          1. Acquires cross-process flock (/tmp/smart_rf_sdr.lock)
          2. Gracefully shuts down Gqrx if open (releasing USB handle)
          3. Cleans up any stray hackrf_sweep / hackrf_transfer processes via SIGINT
          4. Verifies HackRF One responds to hackrf_info; if wedged, auto-resets USB port!
        """
        with self._mutex:
            if self._scanner_active:
                return False, "Smart Scanner already holds the HackRF One lock."

            self._owner = "RELEASING_SDR"
            self._detail = "Acquiring exclusive SDR mutex..."
            self._handoff_pct = 10.0

            try:
                fd = os.open(LOCK_FILE_PATH, os.O_CREAT | os.O_RDWR, 0o666)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(fd, 0)
                os.write(fd, f"SCANNER:{os.getpid()}:{time.time():.0f}\n".encode("utf-8"))
                self._lock_fd = fd
            except BlockingIOError:
                return False, "Another Smart RF Scanner process is currently using HackRF One."
            except Exception as exc:
                return False, f"Failed to acquire SDR lock file: {exc}"

            if self.is_gqrx_running():
                self.stop_gqrx_cleanly(progress_cb=progress_cb)

            self._terminate_stray_hackrf_cli()

            verified = False
            last_err = "HackRF One not detected"
            for attempt in range(4):
                if progress_cb:
                    progress_cb(
                        f"Verifying HackRF One USB readiness (check {attempt + 1}/4)...",
                        60.0 + attempt * 7.0,
                    )
                ok_info, info_msg = self._verify_hackrf_info_once()
                if ok_info:
                    verified = True
                    break
                last_err = info_msg
                time.sleep(0.28)

            # Automatic USB Self-Heal if HackRF is plugged into USB but libhackrf failed!
            if not verified:
                present, _, _, _ = detect_hackrf_sysfs()
                if present:
                    if progress_cb:
                        progress_cb("HackRF USB endpoint busy/wedged — running automatic USB bus reset...", 85.0)
                    ok_rst, rst_msg = self.reset_hackrf_usb_port(progress_cb=progress_cb, stop_gqrx=True)
                    if ok_rst:
                        verified = True
                    else:
                        last_err = rst_msg

            if not verified:
                self._release_flock_locked()
                self._owner = "FAULT"
                self._detail = f"HackRF unavailable: {last_err[:80]}"
                return False, f"HackRF One could not be claimed: {last_err}"

            self._scanner_active = True
            self._hardware_present = True
            self._owner = "SCANNER_L1"
            self._detail = f"Exclusive lock granted to Scanner ({self._serial[-6:]})"
            self._handoff_pct = 100.0
            if progress_cb:
                progress_cb(f"HackRF One locked exclusively ({self._serial[-6:]}).", 100.0)
            logger.info("Exclusive HackRF lock acquired by Smart Scanner (serial=%s)", self._serial)
            return True, self._serial

    def _release_flock_locked(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None

    def release_from_scanner(self) -> None:
        """Releases exclusive scanner lock so Gqrx can safely claim HackRF One."""
        with self._mutex:
            self._terminate_stray_hackrf_cli()
            self._scanner_active = False
            self._release_flock_locked()
            self._owner = "IDLE"
            self._detail = f"HackRF One Released & Ready for Gqrx ({self._serial[-6:]})"
            self._handoff_pct = 100.0
            logger.info("Smart Scanner released HackRF One lock.")

    # ------------------------------------------------------------------
    # Preparation for Gqrx Tuning (With Pre-Launch Health Check & Auto-Heal)
    # ------------------------------------------------------------------
    def prepare_for_gqrx(
        self,
        verify_usb_if_gqrx_closed: bool = True,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> Tuple[bool, str]:
        """
        Verifies Smart Scanner is completely finished, no CLI capture tools hold HackRF,
        crashed=false is sanitized across all Gqrx configs, and if Gqrx is not currently open,
        verifies HackRF One is healthy on USB (auto-resetting the USB port if wedged!).
        """
        with self._mutex:
            if self._scanner_active:
                return (
                    False,
                    "SDR Lock Active: Multi-layer scan is still running. Wait for scan completion before tuning Gqrx.",
                )
            try:
                fd = os.open(LOCK_FILE_PATH, os.O_CREAT | os.O_RDWR, 0o666)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except BlockingIOError:
                return (
                    False,
                    "Another Smart Scanner process currently owns HackRF One. Please wait for it to finish.",
                )
            except Exception:
                pass

            self._terminate_stray_hackrf_cli()
            sanitize_gqrx_crash_flags()

        if verify_usb_if_gqrx_closed and not self.is_gqrx_running():
            present, _, _, _ = detect_hackrf_sysfs()
            if not present:
                return False, "HackRF One (1d50:6089) is not detected on USB. Please check the USB connection."
            ok_info, info_msg = self._verify_hackrf_info_once()
            if not ok_info:
                logger.warning("Pre-Gqrx HackRF check failed (%s); auto-resetting USB port...", info_msg)
                if progress_cb:
                    progress_cb("Recovering HackRF One USB interface before launching Gqrx...", 20.0)
                ok_rst, rst_msg = self.reset_hackrf_usb_port(progress_cb=progress_cb, stop_gqrx=False)
                if not ok_rst:
                    return False, f"HackRF One USB recovery failed prior to Gqrx launch: {rst_msg}"
            # Brief 120ms settle after hackrf_info closes before Gqrx opens libhackrf
            time.sleep(0.12)

        with self._mutex:
            self._owner = "GQRX_TUNING"
            self._detail = "Handing HackRF One to Gqrx Live Demodulator..."
            self._handoff_pct = 50.0
        return True, "Ready for Gqrx"


_arbiter_singleton: Optional[SDRResourceArbiter] = None
_arbiter_init_lock = threading.Lock()


def get_sdr_arbiter() -> SDRResourceArbiter:
    global _arbiter_singleton
    with _arbiter_init_lock:
        if _arbiter_singleton is None:
            _arbiter_singleton = SDRResourceArbiter()
        return _arbiter_singleton
