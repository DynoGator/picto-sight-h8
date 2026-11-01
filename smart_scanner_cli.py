#!/usr/bin/env python3
"""
AI-Powered Smart RF Scanner & Pictogram Classifier — CLI & Desktop GUI Entrypoint
Supports:
  - Responsive GUI launch (--gui, default)
  - Desktop 1-click Location Profile Auto-Scan GUI (--auto-scan {quick,deep,custom})
  - Single-instance boot autostart guard (--single-instance)
  - Headless multi-layer scan & permanent Location Profile builder (--scan --mode {quick,deep,custom})
  - Saved Location Profile listing (--list-profiles)
  - Direct verified Gqrx tuning (--tune-freq <MHz>)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.bandplans import (
    SUPPORTED_DEMOD_MODES,
    classify_voice_broadcast,
    evaluate_signal_demod_feasibility,
)
from core.gps_engine import get_gps_manager
from core.gqrx_controller import GqrxController
from core.hackrf_engine import ScanConfig
from core.profile_organizer import ClassifiedSignalProfile, SignalProfileOrganizer
from core.scanner_pipeline import MultiLayerScannerPipeline, ScanProgressUpdate

GUI_INSTANCE_LOCK = "/tmp/smart_rf_scanner_gui.lock"
GUI_IPC_FILE = "/tmp/smart_rf_scanner_ipc.json"
_gui_lock_fd: Optional[int] = None


def acquire_single_gui_instance() -> bool:
    global _gui_lock_fd
    try:
        fd = os.open(GUI_INSTANCE_LOCK, os.O_CREAT | os.O_RDWR, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        _gui_lock_fd = fd
        return True
    except BlockingIOError:
        return False
    except Exception:
        return True


def dispatch_ipc_to_running_gui(auto_scan_mode: Optional[str] = None) -> None:
    """
    Sends an atomic raise / auto-scan request to the already-running GUI window so
    Desktop launcher icons never spawn duplicate competing GUI instances.
    """
    payload = {
        "ts": time.time(),
        "action": "AUTO_SCAN" if auto_scan_mode else "RAISE",
        "mode": (auto_scan_mode or "").upper().strip(),
    }
    tmp_ipc = f"{GUI_IPC_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp_ipc, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
        os.replace(tmp_ipc, GUI_IPC_FILE)
    except Exception:
        pass


def run_headless_scan(
    mode: str,
    min_mhz: int,
    max_mhz: int,
    snr_db: float,
    peak_dbfs: float,
    demod: str,
    frames: int,
    auto_tune_top: bool = False,
    voice_only: bool = False,
) -> int:
    cfg = ScanConfig.from_mode(
        mode=mode,
        freq_min_mhz=min_mhz,
        freq_max_mhz=max_mhz,
        min_snr_db=snr_db,
        min_peak_dbfs=peak_dbfs,
        demod_mode=demod,
        l3_frames_per_mhz=frames,
    )

    print("\n" + "=" * 88)
    print(f"AI-POWERED SMART RF SCANNER & PICTOGRAM CLASSIFIER [{cfg.mode} MODE]")
    print(
        f"Range: {cfg.freq_min_mhz}-{cfg.freq_max_mhz} MHz | Min SNR: +{cfg.min_snr_db:.1f} dB | "
        f"Min Peak: {cfg.min_peak_dbfs:.0f} dBFS | Demod: {cfg.demod_mode} | L3 Frames/MHz: {cfg.l3_frames_per_mhz}"
    )
    if voice_only:
        print("Filter: VOICE BROADCAST ONLY (--voice-only)")
    print("=" * 88)

    gps = get_gps_manager().refresh_now()
    print(
        f"Locality: {gps.locality_name} ({gps.maidenhead_grid}) | "
        f"Approx Time: {gps.local_datetime_approx} [Source: {gps.source}]"
    )

    def _cli_progress(u: ScanProgressUpdate) -> None:
        bar_len = 24
        filled = int(bar_len * (u.percent_complete / 100.0))
        bar = "█" * filled + "░" * (bar_len - filled)
        line = (
            f"\r[{bar}] {u.percent_complete:5.1f}% (ETA {u.eta_s:4.1f}s) | "
            f"{u.sdr_owner_badge:<22} | {u.message[:48]:<48}"
        )
        print(line, end="", flush=True)

    pipeline = MultiLayerScannerPipeline(callback=_cli_progress)
    t0 = time.perf_counter()
    try:
        profiles, saved = pipeline.run_scan_with_config(cfg)
    finally:
        pipeline.picto_mgr.close()

    elapsed = time.perf_counter() - t0
    vbc_count = sum(1 for p in profiles if getattr(p, "is_voice_broadcast", False))
    print(f"\n\nScan finished in {elapsed:.2f}s — {len(profiles)} viable signals ({vbc_count} Voice Broadcast) ordered.\n")
    if saved is not None:
        print(f"Permanent Location Profile : {saved.json_path}")
        print(f"Profile Bookmarks Snapshot : {saved.bookmarks_csv_path}")

    if voice_only:
        profiles = [p for p in profiles if getattr(p, "is_voice_broadcast", False)]
        print(f"\n[FILTERED TO {len(profiles)} VOICE BROADCAST FREQUENCIES ONLY]\n")

    print("-" * 140)
    print(
        f"{'FREQ (MHz)':<12} | {'TYPE / LABEL':<26} | {'DEMOD TOOL':<12} | {'SERVICE / IDENTITY & AI CLASS':<36} | "
        f"{'DEMOD':<12} | {'SNR / PEAK':<16} | {'OBW'}"
    )
    print("-" * 140)
    for p in profiles:
        sp = f"+{p.snr_db:4.1f}dB/{p.peak_dbm:5.1f}dB"
        bw = f"{p.bandwidth_hz / 1000.0:5.1f}kHz"
        v_lbl = getattr(p, "voice_label", "[📻 VOICE BROADCAST]" if getattr(p, "is_voice_broadcast", False) else "[📊 OTHER]")
        tool = getattr(p, "demod_tool", "gqrx")
        print(
            f"{p.frequency_mhz:<12.4f} | {v_lbl:<26} | {tool:<12} | {p.signal_name[:36]:<36} | {p.modulation[:12]:<12} | "
            f"{sp:<16} | {bw}"
        )
    print("-" * 128)

    if auto_tune_top and profiles:
        best = max(profiles, key=lambda x: x.snr_db)
        print(f"\nTuning strongest viable signal in Gqrx: {best.frequency_mhz:.4f} MHz ({best.modulation})...")
        ctrl = GqrxController()
        res = ctrl.tune_and_launch(best, all_profiles=profiles, organizer=pipeline.organizer)
        print(f"Gqrx Result: {res}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-Powered Smart RF Scanner & Pictogram Classifier")
    parser.add_argument("--gui", action="store_true", help="Launch Responsive PyQt6 GUI (default)")
    parser.add_argument(
        "--auto-scan",
        choices=["quick", "deep", "custom", "QUICK", "DEEP", "CUSTOM"],
        default=None,
        help="Open GUI and immediately initiate a Quick/Deep/Custom Location Profile scan",
    )
    parser.add_argument(
        "--single-instance",
        action="store_true",
        help="Exit quietly if a Smart Scanner GUI window is already running (used for boot autostart)",
    )
    parser.add_argument("--scan", action="store_true", help="Execute headless multi-layer scan in terminal")
    parser.add_argument("--mode", choices=["quick", "deep", "custom"], default="quick", help="Scan mode")
    parser.add_argument("--min", type=int, default=88, help="Start frequency in MHz")
    parser.add_argument("--max", type=int, default=137, help="Stop frequency in MHz")
    parser.add_argument("--snr", type=float, default=9.0, help="Minimum SNR threshold in dB")
    parser.add_argument("--peak", type=float, default=-72.0, help="Minimum Peak Signal Strength in dBFS")
    parser.add_argument("--demod", type=str, default="AUTO (Band + AI)", help="Demodulation mode or AUTO")
    parser.add_argument("--frames", type=int, default=3, help="Layer 3 spectrogram frames per 1 MHz (>=3)")
    parser.add_argument("--tune-top", action="store_true", help="Tune strongest signal in Gqrx after CLI scan")
    parser.add_argument("--tune-freq", type=float, default=None, help="Directly tune a frequency (MHz) in Gqrx")
    parser.add_argument(
        "--telemetry",
        type=float,
        default=None,
        help="Demodulate live IoT/weather/meter sensor telemetry at given frequency in MHz using rtl_433 and HackRF",
    )
    parser.add_argument("--voice-only", action="store_true", help="Filter output list to only voice broadcast stations (FM/AM/SW/NOAA)")
    parser.add_argument("--list-profiles", action="store_true", help="List permanently saved Location Profiles")

    args = parser.parse_args()

    if args.list_profiles:
        org = SignalProfileOrganizer()
        items = org.list_saved_location_profiles()
        print(f"Saved Location Profiles ({len(items)}):")
        for item in items:
            print(f"  - {item['display_title']} -> {item['json_path']}")
        return

    if args.telemetry is not None:
        import json
        from core.demod_engine import run_telemetry_decode
        freq_hz = int(round(args.telemetry * 1e6))
        print(f"\nInitiating live HackRF telemetry decode at {args.telemetry:.4f} MHz via rtl_433 (5s capture)...")
        res = run_telemetry_decode(freq_hz, duration_sec=5.0)
        if res["success"]:
            print(f"\n[+] Captured {res['count']} decoded telemetry packet(s):")
            for pkt in res["packets"]:
                print(json.dumps(pkt, indent=2))
            if not res["packets"]:
                print("No sensor bursts decoded during this 5s window (sensors transmit periodically).")
        else:
            print(f"[-] Telemetry demodulation note: {res.get('error')}")
        return

    if args.tune_freq is not None:
        freq_hz = int(round(args.tune_freq * 1e6))
        feas = evaluate_signal_demod_feasibility(
            freq_hz,
            snr_db=20.0,
            measured_bw_hz=180_000.0 if 87.9e6 <= freq_hz <= 108.0e6 else 16_000.0,
            demod_override=args.demod if args.demod in SUPPORTED_DEMOD_MODES else "AUTO (Band + AI)",
        )
        v_info = classify_voice_broadcast(
            freq_hz=freq_hz,
            demod=feas["demod"],
            band_name=feas["band_name"],
            service_type="",
            pic_label="DirectTune",
            profile_category=feas["profile_category"],
            obw_hz=float(feas["passband_hz"]),
        )
        prof = ClassifiedSignalProfile(
            frequency_hz=freq_hz,
            frequency_mhz=round(freq_hz / 1e6, 4),
            raw_measured_hz=float(freq_hz),
            carrier_offset_hz=0.0,
            signal_name=f"{v_info['voice_label']} {args.tune_freq:.4f} MHz Direct Tune ({feas['demod']})",
            profile_category=feas["profile_category"],
            modulation=feas["demod"],
            rigctl_mode=feas["rigctl_mode"],
            passband_hz=feas["passband_hz"],
            filter_low_cut=feas["filter_low_cut"],
            filter_high_cut=feas["filter_high_cut"],
            fm_maxdev=feas["fm_maxdev"],
            sql_level=feas["sql_level"],
            bandwidth_hz=float(feas["passband_hz"]),
            snr_db=20.0,
            peak_dbm=-45.0,
            noise_floor_dbm=-78.0,
            iq_mod_hint="WFM" if "WFM" in feas["demod"] else "NFM",
            duty_cycle=1.0,
            frames_evaluated=3,
            pictogram_category="DIRECT_TUNE",
            pictogram_neural_class="carrier_comb",
            pictogram_label="DirectTune",
            pictogram_confidence=1.0,
            hailo_accelerated=False,
            lna_gain=feas["lna_gain"],
            vga_gain=feas["vga_gain"],
            amp_gain=feas["amp_gain"],
            plot_min=float(feas["pandapter_min_db"]),
            plot_max=float(feas["pandapter_max_db"]),
            is_legal=feas["is_legal"],
            legal_notes=feas["legal_notes"],
            locality_ref="Direct CLI",
            timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            is_voice_broadcast=v_info["is_voice_broadcast"],
            is_any_voice=v_info["is_any_voice"],
            voice_label=v_info["voice_label"],
            demod_tool="gqrx",
            demod_cmd="gqrx",
            atsc_channel=None,
        )
        ctrl = GqrxController()
        res = ctrl.tune_and_launch(prof, all_profiles=[prof], organizer=SignalProfileOrganizer())
        print(f"Gqrx Tune Result: {res}")
        return

    if args.scan:
        rc = run_headless_scan(
            mode=args.mode,
            min_mhz=args.min,
            max_mhz=args.max,
            snr_db=args.snr,
            peak_dbfs=args.peak,
            demod=args.demod,
            frames=args.frames,
            auto_tune_top=args.tune_top,
            voice_only=args.voice_only,
        )
        os._exit(rc)

    # GUI Mode: strictly enforce single GUI instance to prevent duplicate hardware/TCP pollers
    if not acquire_single_gui_instance():
        if args.single_instance:
            print("AI Smart RF Scanner GUI is already active; skipping duplicate autostart launch.")
        else:
            dispatch_ipc_to_running_gui(auto_scan_mode=args.auto_scan)
            print("Forwarded launcher action to running AI Smart RF Scanner GUI.")
        return

    # Clear any stale IPC command left from before launch
    if os.path.exists(GUI_IPC_FILE):
        try:
            os.remove(GUI_IPC_FILE)
        except Exception:
            pass

    from ui.main_window import main as gui_main

    gui_main(auto_scan_mode=args.auto_scan)


if __name__ == "__main__":
    main()
