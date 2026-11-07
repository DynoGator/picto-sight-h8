#!/usr/bin/env python3
"""
scripts/doctor_check.py - Full-Stack Pre-Flight Doctor & Out-of-the-Box Gqrx Provisioner
========================================================================================
Performs comprehensive dependency, kernel/udev, SDR, Hailo-8 NPU, GNU Radio ATSC TV,
1090 MHz ADS-B, and Gqrx configuration verification on Raspberry Pi OS / Debian 13 (Trixie),
and provisions Gqrx 2.17+ (`default.conf`, `smart_scanner_active.conf`, `bookmarks.csv`,
`recentconfig.cfg`) so the entire stack works cleanly out of the box on first boot.
"""

from __future__ import annotations

import grp
import importlib
import json
import os
import platform
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import __version__, get_runtime_data_dir


def _read_os_release() -> Dict[str, str]:
    info: Dict[str, str] = {}
    os_rel = Path("/etc/os-release")
    if os_rel.exists():
        for line in os_rel.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip().strip('"')
    return info


def provision_gqrx_out_of_box(force_bookmarks: bool = False) -> Dict[str, Any]:
    """
    Generates a clean, ready-to-stream Gqrx 2.17+ configuration (`default.conf`,
    `smart_scanner_active.conf`, `recentconfig.cfg`, and starter `bookmarks.csv`)
    bound to the host's HackRF One (`hackrf=<serial_suffix>` or `hackrf=0`),
    PipeWire default audio (`gain=0`), TCP remote control (`127.0.0.1:7356`),
    and `crash=false`.
    """
    from core.bandplans import classify_voice_broadcast, evaluate_signal_demod_feasibility
    from core.gqrx_controller import GqrxController
    from core.profile_organizer import ClassifiedSignalProfile, SignalProfileOrganizer
    from core.sdr_arbiter import sanitize_gqrx_crash_flags

    ctrl = GqrxController()
    org = SignalProfileOrganizer()

    # Sanitize any stale crash=true flags or dangling recentconfig entries first
    sanitize_gqrx_crash_flags(ctrl.conf_path, config_dir=ctrl.CONFIG_DIR)

    # Build a clean default WFM Stereo starter profile @ 104.5 MHz (or 96.9 MHz)
    seed_freqs: List[Tuple[int, str, str]] = [
        (27_185_000, "CB Channel 19 Highway Calling", "AM"),
        (96_900_000, "Commercial FM Stereo Broadcast (96.9 MHz)", "WFM (stereo)"),
        (104_500_000, "Commercial FM Stereo Broadcast (104.5 MHz)", "WFM (stereo)"),
        (121_500_000, "VHF Aviation Guard / Emergency", "AM"),
        (122_800_000, "Unicom / CTAF Airfield Advisory (1V6)", "AM"),
        (144_390_000, "North America APRS 1200-Baud Packet", "Narrow FM"),
        (154_845_000, "VHF Public Safety / Sheriff Dispatch", "Narrow FM"),
        (155_160_000, "Mountain Search & Rescue MRA-1", "Narrow FM"),
        (155_880_000, "VHF Fire & EMS Tactical Dispatch", "Narrow FM"),
        (161_490_000, "Railroad AAR Ch 92 Road Operations", "Narrow FM"),
        (162_550_000, "NOAA All-Hazards Weather Radio WX", "Narrow FM"),
        (433_920_000, "ISM 433.92 MHz IoT Sensor Telemetry", "Narrow FM"),
        (539_000_000, "ATSC Ch 25 Digital TV (6 MHz 8VSB)", "Raw I/Q"),
    ]

    starter_profiles: List[ClassifiedSignalProfile] = []
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for f_hz, label, demod_hint in seed_freqs:
        bw_hint = 180_000.0 if "WFM" in demod_hint else (5_380_000.0 if demod_hint == "Raw I/Q" else 16_000.0)
        feas = evaluate_signal_demod_feasibility(
            f_hz,
            snr_db=22.0,
            measured_bw_hz=bw_hint,
            demod_override="AUTO (Band + AI)",
        )
        v_info = classify_voice_broadcast(
            freq_hz=f_hz,
            demod=feas["demod"],
            band_name=label,
            profile_category=feas["profile_category"],
            obw_hz=bw_hint,
        )
        starter_profiles.append(
            ClassifiedSignalProfile(
                frequency_hz=f_hz,
                frequency_mhz=round(f_hz / 1e6, 4),
                raw_measured_hz=float(f_hz),
                carrier_offset_hz=0.0,
                signal_name=f"{v_info['voice_label']} {label}",
                profile_category=feas["profile_category"],
                modulation=feas["demod"],
                rigctl_mode=feas["rigctl_mode"],
                passband_hz=feas["passband_hz"],
                filter_low_cut=feas["filter_low_cut"],
                filter_high_cut=feas["filter_high_cut"],
                fm_maxdev=feas["fm_maxdev"],
                sql_level=-150.0,
                bandwidth_hz=bw_hint,
                snr_db=22.0,
                peak_dbm=-44.0,
                noise_floor_dbm=-82.0,
                iq_mod_hint="WFM" if "WFM" in feas["demod"] else "NFM",
                duty_cycle=1.0,
                frames_evaluated=3,
                pictogram_category="PROVISIONED",
                pictogram_neural_class="wfm_stereo" if "WFM" in feas["demod"] else "nfm_voice",
                pictogram_label="StarterSeed",
                pictogram_confidence=1.0,
                hailo_accelerated=False,
                lna_gain=feas["lna_gain"],
                vga_gain=feas["vga_gain"],
                amp_gain=feas["amp_gain"],
                plot_min=float(feas["pandapter_min_db"]),
                plot_max=float(feas["pandapter_max_db"]),
                is_legal=True,
                legal_notes=feas["legal_notes"],
                locality_ref="Picto-Sight H8 Provisioned Seed",
                timestamp_iso=now_iso,
                is_voice_broadcast=v_info["is_voice_broadcast"],
                is_any_voice=v_info["is_any_voice"],
                voice_label=v_info["voice_label"],
                demod_tool="gqrx",
                demod_cmd="gqrx",
                atsc_channel=25 if f_hz == 539_000_000 else None,
            )
        )

    # Active starter target: 104.5 MHz WFM Stereo
    default_target = [p for p in starter_profiles if p.frequency_hz == 104_500_000][0]
    generated_conf = ctrl.generate_profile(default_target, compact_screen=True)
    sanitize_gqrx_crash_flags(generated_conf, config_dir=ctrl.CONFIG_DIR)

    gqrx_bookmarks = Path.home() / ".config" / "gqrx" / "bookmarks.csv"
    wrote_bookmarks = False
    if force_bookmarks or not gqrx_bookmarks.exists() or gqrx_bookmarks.stat().st_size < 64:
        org.export_gqrx_bookmarks(starter_profiles, active_freq_hz=104_500_000)
        wrote_bookmarks = True

    return {
        "ok": True,
        "device_arg": ctrl.arbiter.get_hackrf_device_arg(),
        "default_conf": str(ctrl.DEFAULT_CONF_PATH),
        "active_conf": str(ctrl.conf_path),
        "named_conf": str(generated_conf),
        "bookmarks_csv": str(gqrx_bookmarks),
        "bookmarks_seeded": wrote_bookmarks,
    }


def run_doctor_check() -> Dict[str, Any]:
    """
    Executes end-to-end environment, package, kernel/udev, NPU, SDR, and config checks.
    """
    os_info = _read_os_release()
    checks: List[Dict[str, Any]] = []

    def add_check(name: str, status: str, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "status": status, "detail": detail, "required": required})

    # 1. OS & Python Runtime
    pretty_os = os_info.get("PRETTY_NAME", platform.platform())
    codename = os_info.get("VERSION_CODENAME", "unknown")
    add_check(
        "Operating System",
        "PASS",
        f"{pretty_os} ({platform.machine()}, kernel {platform.release()})",
    )

    py_ver = sys.version.split()[0]
    py_ok = sys.version_info >= (3, 11)
    add_check(
        "Python Interpreter",
        "PASS" if py_ok else "FAIL",
        f"Python {py_ver} ({sys.executable})",
    )

    # 2. Required & Optional Python Bindings
    req_mods = [
        ("numpy", True, "Core DSP / FFT arrays"),
        ("cv2", True, "OpenCV spectrogram ROI segmentation"),
        ("PyQt6.QtWidgets", True, "Adaptive 5\" DSI / HDMI GUI"),
        ("serial", True, "u-blox USB GNSS NMEA serial"),
        ("gnuradio.dtv", True, "GNU Radio ATSC 1.0 8VSB TV demodulator"),
        ("osmosdr", True, "gr-osmosdr HackRF 8 MSPS TV source"),
        ("hailo_platform", False, "HailoRT Python PCIe NPU bindings"),
    ]
    for mod_name, is_req, purpose in req_mods:
        try:
            importlib.import_module(mod_name)
            add_check(f"Python Module: {mod_name}", "PASS", f"Available ({purpose})", required=is_req)
        except Exception as exc:
            st = "FAIL" if is_req else "WARN"
            add_check(
                f"Python Module: {mod_name}",
                st,
                f"Missing ({purpose}) — {exc}",
                required=is_req,
            )

    # 3. External RF / SDR / Video / Telemetry Binaries
    bins = [
        ("hackrf_sweep", True, "Wideband Layer 1 & Layer 2 RF sweeps"),
        ("hackrf_transfer", True, "Raw IQ capture (Layer 3 AI, ADS-B, NTSC, Pager)"),
        ("gqrx", True, "Live Stereo FM / NFM / AM / SSB audio receiver"),
        ("readsb", True, "Live 1090 MHz Mode-S / ADS-B aircraft decoder"),
        ("rtl_433", True, "ISM 315/433/915 MHz sensor telemetry decoder"),
        ("multimon-ng", True, "POCSAG 512/1200/2400, FLEX, EAS & APRS decoder"),
        ("mpv", True, "Low-latency MPEG-TS live ATSC TV video player"),
        ("direwolf", False, "Optional AX.25 packet modem"),
        ("w_scan_cpp", False, "Optional terrestrial broadcast table scanner"),
        ("gpsd", False, "GNSS background multiplexer"),
    ]
    for bin_name, is_req, purpose in bins:
        path = shutil.which(bin_name)
        if path:
            add_check(f"Binary: {bin_name}", "PASS", f"{path} ({purpose})", required=is_req)
        else:
            add_check(
                f"Binary: {bin_name}",
                "FAIL" if is_req else "WARN",
                f"Not found in PATH ({purpose})",
                required=is_req,
            )

    # 4. Conflicting Background SDR Daemon Check (`readsb.service`)
    try:
        r_active = subprocess.run(
            ["systemctl", "is-active", "readsb"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        ).stdout.strip()
        if r_active == "active":
            add_check(
                "SDR Daemon De-confliction",
                "WARN",
                "readsb.service is active in background and may hold SDR; run `sudo systemctl disable --now readsb`",
                required=False,
            )
        else:
            add_check(
                "SDR Daemon De-confliction",
                "PASS",
                f"readsb.service={r_active or 'inactive'} (HackRF free for 1-at-a-time arbiter)",
                required=False,
            )
    except Exception:
        pass

    # 5. User Group Membership & HackRF One USB / sysfs Permissions
    try:
        user_name = pwd.getpwuid(os.getuid()).pw_name
        user_groups = {g.gr_name for g in grp.getgrall() if user_name in g.gr_mem}
        primary_gid = pwd.getpwuid(os.getuid()).pw_gid
        user_groups.add(grp.getgrgid(primary_gid).gr_name)
        has_plugdev = "plugdev" in user_groups or os.getuid() == 0
        add_check(
            "USB Permissions (plugdev)",
            "PASS" if has_plugdev else "WARN",
            f"User '{user_name}' groups: {', '.join(sorted(user_groups))}",
            required=False,
        )
    except Exception:
        pass

    from core.sdr_arbiter import detect_hackrf_sysfs, format_osmosdr_hackrf_device_arg

    hrf_present, hrf_serial, bus_num, dev_num = detect_hackrf_sysfs()
    if hrf_present:
        dev_node = f"/dev/bus/usb/{bus_num:03d}/{dev_num:03d}" if (bus_num and dev_num) else ""
        writable = os.access(dev_node, os.R_OK | os.W_OK) if dev_node and os.path.exists(dev_node) else False
        osmo_arg = format_osmosdr_hackrf_device_arg(hrf_serial)
        add_check(
            "HackRF One Hardware",
            "PASS" if writable else "WARN",
            f"Detected ({osmo_arg}) at {dev_node} (USB reset RW={'OK' if writable else 'READ-ONLY'})",
            required=False,
        )
    else:
        add_check(
            "HackRF One Hardware",
            "WARN",
            "Not currently plugged into USB (plug in HackRF One 1d50:6089 before scanning)",
            required=False,
        )

    # 6. Hailo-8 NPU & 10-Class RF Classifier Weights
    weights_npz = PROJECT_ROOT / "models" / "hailo_pictogram_head_weights.npz"
    if weights_npz.exists():
        data = __import__("numpy").load(str(weights_npz), allow_pickle=False)
        classes = [str(x) for x in data["classes"].tolist()] if "classes" in data else []
        val_acc = float(__import__("numpy").asarray(data["val_accuracy"]).reshape(-1)[0]) * 100.0 if "val_accuracy" in data else 0.0
        add_check(
            "10-Class RF Model Weights",
            "PASS" if len(classes) == 10 else "FAIL",
            f"{weights_npz.name} ({len(classes)} classes, {val_acc:.1f}% held-out val accuracy)",
            required=True,
        )
    else:
        add_check("10-Class RF Model Weights", "FAIL", f"Missing {weights_npz}", required=True)

    hailo_dev = Path("/dev/hailo0")
    from core.hailo_pictogram import _resolve_default_hef

    hef_path = Path(_resolve_default_hef())
    if hailo_dev.exists() and hef_path.exists():
        add_check(
            "Hailo-8 PCIe NPU",
            "PASS",
            f"/dev/hailo0 online + HEF {hef_path}",
            required=False,
        )
    elif hailo_dev.exists():
        add_check(
            "Hailo-8 PCIe NPU",
            "WARN",
            f"/dev/hailo0 present, but HEF missing at {hef_path} (install `hailo-models` or `hailo-all`; CPU MLP fallback active)",
            required=False,
        )
    else:
        add_check(
            "Hailo-8 PCIe NPU",
            "WARN",
            "/dev/hailo0 not present — 16-D physical RF CPU MLP fallback (96.0% val accuracy) will run automatically",
            required=False,
        )

    # 7. Gqrx Configuration & Runtime Data Directory Health
    data_dir = get_runtime_data_dir()
    gqrx_default = Path.home() / ".config" / "gqrx" / "default.conf"
    gqrx_bmarks = Path.home() / ".config" / "gqrx" / "bookmarks.csv"
    if gqrx_default.exists():
        txt = gqrx_default.read_text(encoding="utf-8", errors="ignore")
        has_crash = "crash=true" in txt.lower()
        add_check(
            "Gqrx 2.17+ Configuration",
            "WARN" if has_crash else "PASS",
            f"{gqrx_default} (crash_clean={not has_crash}, bookmarks={'OK' if gqrx_bmarks.exists() else 'unseeded'}, data_dir={data_dir})",
            required=False,
        )
    else:
        add_check(
            "Gqrx 2.17+ Configuration",
            "WARN",
            "~/.config/gqrx/default.conf not yet provisioned (run `picto-sight --provision-gqrx`)",
            required=False,
        )

    req_failures = [c for c in checks if c["required"] and c["status"] == "FAIL"]
    return {
        "version": __version__,
        "os_codename": codename,
        "ready": len(req_failures) == 0,
        "required_failures": len(req_failures),
        "checks": checks,
    }


def print_doctor_report(report: Dict[str, Any]) -> int:
    print("\n" + "=" * 92)
    print(f"  PICTO-SIGHT H8 v{report['version']} — FULL-STACK SYSTEM DOCTOR & DEPENDENCY AUDIT")
    print("=" * 92)
    for chk in report["checks"]:
        st = chk["status"]
        badge = "[ PASS ]" if st == "PASS" else ("[ WARN ]" if st == "WARN" else "[ FAIL ]")
        print(f"  {badge} {chk['name']:<30} : {chk['detail']}")
    print("-" * 92)
    if report["ready"]:
        print("  VERDICT: ALL REQUIRED STACK DEPENDENCIES VERIFIED — READY FOR SPECTRUM OPS.")
        print("=" * 92 + "\n")
        return 0
    print(f"  VERDICT: {report['required_failures']} REQUIRED CHECK(S) FAILED — RUN `./install.sh` TO REMEDIATE.")
    print("=" * 92 + "\n")
    return 1


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Picto-Sight H8 System Doctor & Gqrx Provisioner")
    ap.add_argument("--provision-gqrx", action="store_true", help="Provision Gqrx configs & starter bookmarks")
    ap.add_argument("--json", action="store_true", help="Output JSON report")
    args = ap.parse_args()

    if args.provision_gqrx:
        res = provision_gqrx_out_of_box()
        print(json.dumps(res, indent=2))
        raise SystemExit(0)

    rep = run_doctor_check()
    if args.json:
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["ready"] else 1)
    raise SystemExit(print_doctor_report(rep))
