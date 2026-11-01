#!/usr/bin/env python3
"""
core/demod_engine.py - Advanced Demodulation & Signal Decoder Engine
====================================================================
Integrates multi-tool demodulation for the AI-Powered Smart Scanner:
  1. Audio & Voice Broadcasts: Gqrx 2.17.6 (WFM, NFM, AM, USB, LSB) with DSP & bookmarks
  2. IoT / ISM Telemetry: rtl_433 (sensors, weather stations, TPMS, smart meters at 433/915/315 MHz)
  3. Digital Broadcast Television: ATSC 1.0 / ATSC 3.0 channel mapping & w_scan_cpp / GNU Radio
  4. Aviation ADS-B Surveillance: readsb / dump1090 Mode-S decoder at 1090 MHz
  5. Pager & Weather Alert Data: multimon-ng (POCSAG512/1200/2400, FLEX, EAS alerts)
  6. Amateur Packet Telemetry: direwolf / multimon-ng (APRS at 144.390 MHz)
  7. Audio FSK Telemetry: minimodem (Bell103/202, RTTY, NOAA SAME)
"""

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("DemodEngine")


@dataclass
class DemodProfile:
    """Structured demodulation target metadata for a classified RF signal."""
    tool_id: str                   # 'gqrx', 'rtl_433', 'multimon_ng', 'readsb', 'direwolf', 'w_scan_cpp', 'minimodem'
    tool_name: str                 # Human-friendly tool name
    category: str                  # Voice_Broadcast, Broadcast_TV, ISM_Telemetry, Aviation_ADS_B, etc.
    demod_label: str               # Visual label with emoji badge (e.g. '[📻 VOICE BROADCAST]')
    description: str               # Detailed explanation of signal demodulation capability
    command: str                   # Direct headless execution command
    terminal_command: str          # GUI terminal launch command (lxterminal / x-terminal-emulator)
    is_installed: bool             # Whether required tool binary exists on host
    atsc_channel: Optional[int]    # ATSC channel number (2-36) if TV broadcast, else None
    demod_mode: str = ""           # Gqrx modulation string or sub-demodulator flag

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_installed_demod_tools() -> Dict[str, bool]:
    """Returns dictionary of supported demodulation tools and their availability."""
    return {
        "gqrx": bool(shutil.which("gqrx")),
        "rtl_433": bool(shutil.which("rtl_433")),
        "multimon_ng": bool(shutil.which("multimon-ng")),
        "direwolf": bool(shutil.which("direwolf")),
        "minimodem": bool(shutil.which("minimodem")),
        "readsb": bool(shutil.which("readsb")),
        "w_scan_cpp": bool(shutil.which("w_scan_cpp")),
        "sox": bool(shutil.which("sox")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "hackrf_transfer": bool(shutil.which("hackrf_transfer")),
    }


def identify_atsc_tv_channel(freq_hz: float) -> Optional[Tuple[int, float, float, float]]:
    """
    Determines if freq_hz falls within standard North American ATSC TV broadcast channels (6 MHz).
    Returns (channel_number, center_freq_hz, lower_edge_hz, pilot_carrier_hz) or None.
    """
    f = float(freq_hz)

    # VHF Low: Channels 2 to 6
    vhf_low = {
        2: (54_000_000, 60_000_000),
        3: (60_000_000, 66_000_000),
        4: (66_000_000, 72_000_000),
        5: (76_000_000, 82_000_000),
        6: (82_000_000, 88_000_000),
    }
    for ch, (lo, hi) in vhf_low.items():
        if lo <= f < hi:
            center = (lo + hi) / 2.0
            pilot = lo + 309_441.0
            return (ch, center, float(lo), pilot)

    # VHF High: Channels 7 to 13 (174 - 216 MHz)
    if 174_000_000 <= f < 216_000_000:
        ch = 7 + int((f - 174_000_000) / 6_000_000)
        lo = 174_000_000 + (ch - 7) * 6_000_000
        center = lo + 3_000_000.0
        pilot = lo + 309_441.0
        return (ch, center, float(lo), pilot)

    # UHF: Channels 14 to 36 (470 - 608 MHz)
    if 470_000_000 <= f < 608_000_000:
        ch = 14 + int((f - 470_000_000) / 6_000_000)
        lo = 470_000_000 + (ch - 14) * 6_000_000
        center = lo + 3_000_000.0
        pilot = lo + 309_441.0
        return (ch, center, float(lo), pilot)

    return None


def get_demod_profile_for_signal(
    freq_hz: float,
    demod: str = "",
    band_name: str = "",
    service_type: str = "",
    pic_label: str = "",
    profile_category: str = "",
    obw_hz: float = 0.0,
) -> DemodProfile:
    """
    Resolves the optimal demodulation tool, command pipeline, and metadata
    for a classified signal.
    """
    f_hz = float(freq_hz)
    f_mhz = f_hz / 1e6
    installed = get_installed_demod_tools()

    # 1. Aviation ADS-B Mode-S Transponder (1090 MHz)
    if (1_088_000_000 <= f_hz <= 1_092_000_000) or "ADS-B" in band_name.upper():
        is_inst = installed.get("readsb", False) and installed.get("hackrf_transfer", False)
        cmd = "hackrf_transfer -f 1090000000 -s 2000000 -a 1 -l 32 -g 40 -r - | readsb --device-type=ifile --ifile=- --interactive"
        term_cmd = f"lxterminal -t 'ADS-B 1090 MHz Live Aviation Transponders' -e bash -c '{cmd}'"
        return DemodProfile(
            tool_id="readsb",
            tool_name="readsb ADS-B Aviation Decoder",
            category="Aviation_Airband",
            demod_label="[✈️ ADS-B TELEMETRY]",
            description="Decodes Mode-S / ADS-B transponder position, altitude, squawk, and airspeed from aircraft in flight.",
            command=cmd,
            terminal_command=term_cmd,
            is_installed=is_inst,
            atsc_channel=None,
            demod_mode="Raw I/Q",
        )

    # 2. Digital Broadcast Television (ATSC 1.0 / ATSC 3.0)
    tv_info = identify_atsc_tv_channel(f_hz)
    if tv_info is not None:
        ch, center, lo, pilot = tv_info
        is_inst = installed.get("w_scan_cpp", False)
        cmd = f"w_scan_cpp -f a -A 1 -c US"
        term_cmd = (
            f"lxterminal -t 'ATSC Digital TV Broadcast Ch {ch} ({lo/1e6:.1f}-{(lo+6e6)/1e6:.1f} MHz)' "
            f"-e bash -c 'echo \"=== ATSC Channel {ch} ===\"; "
            f"echo \"Band: {lo/1e6:.2f} - {(lo+6e6)/1e6:.2f} MHz (Pilot Carrier: {pilot/1e6:.4f} MHz)\"; "
            f"echo \"Scanning DTV channels...\"; {cmd}; read -p \"Press Enter to close...\"'"
        )
        return DemodProfile(
            tool_id="w_scan_cpp",
            tool_name="w_scan_cpp ATSC TV Scanner",
            category="Broadcast_TV",
            demod_label=f"[📺 BROADCAST TV (Ch {ch})]",
            description=f"North American ATSC Digital Broadcast Television Channel {ch} (6 MHz 8VSB/OFDM multiplex; pilot @ {pilot/1e6:.3f} MHz).",
            command=cmd,
            terminal_command=term_cmd,
            is_installed=is_inst,
            atsc_channel=ch,
            demod_mode="Raw I/Q",
        )

    # 3. IoT / ISM Telemetry & Sensor Channels (433 MHz, 915 MHz, 315 MHz, 868 MHz)
    is_ism = (
        (433_000_000 <= f_hz <= 434_900_000)
        or (902_000_000 <= f_hz <= 928_000_000)
        or (314_500_000 <= f_hz <= 315_500_000)
        or (868_000_000 <= f_hz <= 868_600_000)
        or profile_category in ("ISM_Telemetry", "Telemetry")
        or any(k in band_name.lower() for k in ("ism", "telemetry", "sensor", "lora", "tpms"))
    )
    if is_ism:
        is_inst = installed.get("rtl_433", False)
        cmd = f"rtl_433 -d driver=hackrf -f {int(f_hz)} -M level"
        term_cmd = (
            f"lxterminal -t 'rtl_433 ISM Telemetry Decoder @ {f_mhz:.3f} MHz' "
            f"-e bash -c 'echo \"=== rtl_433 HackRF Telemetry Decoder @ {f_mhz:.3f} MHz ===\"; "
            f"{cmd}; read -p \"Press Enter to close...\"'"
        )
        return DemodProfile(
            tool_id="rtl_433",
            tool_name="rtl_433 IoT Telemetry Decoder",
            category="ISM_Telemetry",
            demod_label="[📊 TELEMETRY (rtl_433)]",
            description=f"Decodes over 200 IoT sensor protocols (weather stations, TPMS tire pressure, smart meters, remotes) on {f_mhz:.3f} MHz via HackRF.",
            command=cmd,
            terminal_command=term_cmd,
            is_installed=is_inst,
            atsc_channel=None,
            demod_mode="Narrow FM",
        )

    # 4. Amateur APRS Tactical Packet Radio Telemetry (144.390 MHz North America)
    if abs(f_hz - 144_390_000) <= 25_000 or "APRS" in band_name.upper():
        is_inst = installed.get("direwolf", False) or installed.get("multimon_ng", False)
        cmd = "multimon-ng -t raw -a AFSK1200 -A"
        term_cmd = (
            f"lxterminal -t 'APRS Packet Telemetry Decoder (144.390 MHz)' "
            f"-e bash -c 'echo \"Listening for AX.25 APRS tactical position/weather packets...\"; "
            f"gqrx & sleep 2; read -p \"Press Enter to close...\"'"
        )
        return DemodProfile(
            tool_id="direwolf",
            tool_name="direwolf / multimon-ng APRS Decoder",
            category="Amateur_Radio",
            demod_label="[📍 APRS TELEMETRY]",
            description="Decodes amateur radio 1200-baud AFSK AX.25 position reports, telemetry, and weather station beacons.",
            command=cmd,
            terminal_command=term_cmd,
            is_installed=is_inst,
            atsc_channel=None,
            demod_mode="Narrow FM",
        )

    # 5. Pager / POCSAG / FLEX Dispatch Channels
    if any(k in band_name.upper() for k in ("PAGER", "POCSAG", "FLEX")) or (
        profile_category == "Public_Safety" and any(k in service_type.upper() for k in ("PAGER", "DISPATCH DATA"))
    ):
        is_inst = installed.get("multimon_ng", False)
        cmd = "multimon-ng -a POCSAG512 -a POCSAG1200 -a POCSAG2400 -a FLEX -f auto"
        term_cmd = (
            f"lxterminal -t 'POCSAG / FLEX Pager Decoder @ {f_mhz:.3f} MHz' "
            f"-e bash -c 'echo \"=== multimon-ng Pager Decoder ===\"; "
            f"{cmd}; read -p \"Press Enter to close...\"'"
        )
        return DemodProfile(
            tool_id="multimon_ng",
            tool_name="multimon-ng Pager / Data Decoder",
            category="Public_Safety",
            demod_label="[📟 POCSAG / PAGER DATA]",
            description=f"Decodes POCSAG 512/1200/2400 and Motorola FLEX alphanumeric pager broadcasts.",
            command=cmd,
            terminal_command=term_cmd,
            is_installed=is_inst,
            atsc_channel=None,
            demod_mode="Narrow FM",
        )

    # 6. Commercial FM Broadcast Radio (87.5 - 108.0 MHz)
    if (87_500_000 <= f_hz <= 108_100_000) or ("WFM" in demod and obw_hz >= 75_000):
        is_inst = installed.get("gqrx", False)
        return DemodProfile(
            tool_id="gqrx",
            tool_name="Gqrx 2.17.6 SDR Receiver",
            category="Voice_Broadcast",
            demod_label="[📻 VOICE BROADCAST]",
            description=f"Commercial Wideband FM stereo broadcast with RDS subcarrier on {f_mhz:.2f} MHz.",
            command=f"gqrx",
            terminal_command="",
            is_installed=is_inst,
            atsc_channel=None,
            demod_mode="WFM (stereo)",
        )

    # 7. NOAA All-Hazards Emergency Weather Radio (162.400 - 162.550 MHz)
    if (162_385_000 <= f_hz <= 162_565_000) or "NOAA" in band_name.upper():
        is_inst = installed.get("gqrx", False)
        return DemodProfile(
            tool_id="gqrx",
            tool_name="Gqrx 2.17.6 SDR Receiver",
            category="Voice_Broadcast",
            demod_label="[🎙️ WX VOICE BROADCAST]",
            description=f"Continuous 24/7 automated NOAA All-Hazards voice weather broadcast & 1050 Hz / SAME alert tones.",
            command="gqrx",
            terminal_command="",
            is_installed=is_inst,
            atsc_channel=None,
            demod_mode="Narrow FM",
        )

    # 8. Civil & Military Aviation Voice (Air Traffic Control / ATIS)
    if (118_000_000 <= f_hz <= 136_975_000) or (225_000_000 <= f_hz <= 380_000_000 and "AM" in demod):
        is_atis = any(k in band_name.upper() or k in pic_label.upper() for k in ("ATIS", "AWOS", "ASOS"))
        label = "[🎙️ ATIS VOICE BROADCAST]" if is_atis else "[✈️ AIR VOICE (ATC)]"
        is_inst = installed.get("gqrx", False)
        return DemodProfile(
            tool_id="gqrx",
            tool_name="Gqrx 2.17.6 SDR Receiver",
            category="Voice_Broadcast" if is_atis else "Aviation_Airband",
            demod_label=label,
            description="Civil / Military aviation amplitude modulated (AM) voice transmission open for passive air-band monitoring.",
            command="gqrx",
            terminal_command="",
            is_installed=is_inst,
            atsc_channel=None,
            demod_mode="AM",
        )

    # 9. Default Fallback -> Gqrx 2.17.6
    effective_demod = demod or "Narrow FM"
    is_inst = installed.get("gqrx", False)
    return DemodProfile(
        tool_id="gqrx",
        tool_name="Gqrx 2.17.6 SDR Receiver",
        category=profile_category or "Public_Safety",
        demod_label=f"[{effective_demod.upper()}]",
        description=f"Demodulate live analog RF signal using Gqrx ({effective_demod}) on {f_mhz:.4f} MHz.",
        command="gqrx",
        terminal_command="",
        is_installed=is_inst,
        atsc_channel=None,
        demod_mode=effective_demod,
    )


def run_telemetry_decode(
    freq_hz: float,
    duration_sec: float = 3.0,
    sample_rate_hz: int = 2_000_000,
    gain_db: int = 32,
) -> Dict[str, Any]:
    """
    Executes a direct telemetry capture and JSON decode session via rtl_433 and HackRF.
    Safely releases and re-checks HackRF access.
    Returns:
      {
        "success": bool,
        "freq_hz": int,
        "count": int,
        "packets": List[Dict[str, Any]],
        "raw_lines": List[str],
        "error": Optional[str]
      }
    """
    f_hz = int(freq_hz)
    dur = max(1.0, min(15.0, float(duration_sec)))

    cmd = [
        "rtl_433",
        "-d", "driver=hackrf",
        "-f", str(f_hz),
        "-s", str(sample_rate_hz),
        "-T", str(dur),
        "-F", "json",
    ]

    packets: List[Dict[str, Any]] = []
    raw_lines: List[str] = []

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=dur + 5.0,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            raw_lines.append(line)
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                    packets.append(payload)
                except Exception:
                    pass

        return {
            "success": True,
            "freq_hz": f_hz,
            "count": len(packets),
            "packets": packets,
            "raw_lines": raw_lines,
            "error": None if proc.returncode == 0 else f"rtl_433 exit {proc.returncode}: {stderr[:200]}",
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "freq_hz": f_hz,
            "count": 0,
            "packets": [],
            "raw_lines": [],
            "error": f"rtl_433 timed out after {dur + 5.0:.1f}s",
        }
    except FileNotFoundError:
        return {
            "success": False,
            "freq_hz": f_hz,
            "count": 0,
            "packets": [],
            "raw_lines": [],
            "error": "rtl_433 binary not installed on host system",
        }
    except Exception as exc:
        return {
            "success": False,
            "freq_hz": f_hz,
            "count": 0,
            "packets": [],
            "raw_lines": [],
            "error": str(exc),
        }


def launch_in_gui_terminal(command_str: str, window_title: str = "Smart Scanner Demod") -> bool:
    """
    Launches an interactive command inside a standalone desktop terminal window
    (lxterminal or x-terminal-emulator).
    """
    term_bin = shutil.which("lxterminal") or shutil.which("x-terminal-emulator")
    if not term_bin:
        logger.error("No terminal emulator found (lxterminal / x-terminal-emulator)")
        return False

    shell_cmd = f"{command_str}; echo; read -p 'Press Enter to finish and close...'"
    args = [term_bin, "-t", window_title, "-e", f"bash -c \"{shell_cmd}\""]

    try:
        subprocess.Popen(args, start_new_session=True)
        return True
    except Exception as exc:
        logger.error(f"Failed to launch terminal demodulator: {exc}")
        return False


def fm_demodulate_iq_to_pcm(raw_iq_bytes: bytes, in_rate: int = 2_000_000, out_rate: int = 22_050) -> bytes:
    """
    Discriminator-based FM demodulation of interleaved 8-bit signed IQ bytes (I, Q, I, Q)
    decimated down to 16-bit mono PCM suitable for audio decoders (multimon-ng, direwolf).
    """
    samples = np.frombuffer(raw_iq_bytes, dtype=np.int8).astype(np.float32)
    if len(samples) < 8:
        return b""

    n = (len(samples) // 2) * 2
    i = samples[:n:2]
    q = samples[1:n:2]
    iq = i + 1j * q

    # Phase difference FM discriminator
    dphi = np.angle(iq[1:] * np.conj(iq[:-1]))

    # Decimate down to out_rate
    decim = max(1, int(round(in_rate / out_rate)))
    if decim > 1:
        pad = (len(dphi) // decim) * decim
        audio = np.mean(dphi[:pad].reshape(-1, decim), axis=1)
    else:
        audio = dphi

    # Normalize audio dynamic range to int16
    peak = float(np.percentile(np.abs(audio), 99)) if len(audio) > 0 else 1.0
    scale = 16000.0 / max(1e-4, peak)
    audio_int16 = np.clip(audio * scale, -32767, 32767).astype(np.int16)
    return audio_int16.tobytes()


def run_pager_data_decode(
    freq_hz: float,
    duration_sec: float = 3.0,
    sample_rate_hz: int = 2_000_000,
) -> Dict[str, Any]:
    """
    Captures live RF IQ from HackRF One, FM-demodulates in real time, and pipes into
    multimon-ng to decode POCSAG 512/1200/2400, Motorola FLEX, and EAS emergency alerts.
    """
    f_hz = int(freq_hz)
    dur = max(1.0, min(10.0, float(duration_sec)))
    n_samples = int(dur * sample_rate_hz * 2)  # 2 bytes per complex sample

    if not shutil.which("hackrf_transfer") or not shutil.which("multimon-ng"):
        return {
            "success": False,
            "freq_hz": f_hz,
            "count": 0,
            "messages": [],
            "raw_lines": [],
            "error": "hackrf_transfer or multimon-ng binary not installed",
        }

    cmd_hackrf = [
        "hackrf_transfer",
        "-r", "-",
        "-f", str(f_hz),
        "-s", str(sample_rate_hz),
        "-n", str(n_samples),
        "-a", "1",
        "-l", "32",
        "-g", "38",
    ]

    try:
        proc_hrf = subprocess.run(
            cmd_hackrf,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=dur + 4.0,
        )
        iq_bytes = proc_hrf.stdout or b""
        if len(iq_bytes) < 4096:
            return {
                "success": False,
                "freq_hz": f_hz,
                "count": 0,
                "messages": [],
                "raw_lines": [],
                "error": f"HackRF capture returned short data ({len(iq_bytes)} bytes)",
            }

        pcm_bytes = fm_demodulate_iq_to_pcm(iq_bytes, in_rate=sample_rate_hz, out_rate=22_050)

        cmd_multi = [
            "multimon-ng",
            "-t", "raw",
            "-a", "POCSAG512",
            "-a", "POCSAG1200",
            "-a", "POCSAG2400",
            "-a", "FLEX",
            "-a", "EAS",
            "-",
        ]
        proc_multi = subprocess.run(
            cmd_multi,
            input=pcm_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
        )

        lines: List[str] = []
        messages: List[str] = []
        for line in proc_multi.stdout.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            lines.append(line)
            if any(k in line for k in ("POCSAG", "FLEX", "EAS", "Alpha", "Numeric")):
                messages.append(line)

        return {
            "success": True,
            "freq_hz": f_hz,
            "count": len(messages),
            "messages": messages,
            "raw_lines": lines,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "freq_hz": f_hz,
            "count": 0,
            "messages": [],
            "raw_lines": [],
            "error": "Pager demodulation timed out",
        }
    except Exception as exc:
        return {
            "success": False,
            "freq_hz": f_hz,
            "count": 0,
            "messages": [],
            "raw_lines": [],
            "error": str(exc),
        }


def run_aprs_data_decode(
    freq_hz: float = 144_390_000,
    duration_sec: float = 4.0,
    sample_rate_hz: int = 2_000_000,
) -> Dict[str, Any]:
    """
    Captures live RF IQ from HackRF One at 144.390 MHz (or specified frequency),
    FM-demodulates, and decodes AX.25 tactical packet radio telemetry via multimon-ng or direwolf.
    """
    f_hz = int(freq_hz)
    dur = max(1.0, min(10.0, float(duration_sec)))
    n_samples = int(dur * sample_rate_hz * 2)

    if not shutil.which("hackrf_transfer") or not shutil.which("multimon-ng"):
        return {
            "success": False,
            "freq_hz": f_hz,
            "count": 0,
            "packets": [],
            "raw_lines": [],
            "error": "hackrf_transfer or multimon-ng binary not installed",
        }

    cmd_hackrf = [
        "hackrf_transfer",
        "-r", "-",
        "-f", str(f_hz),
        "-s", str(sample_rate_hz),
        "-n", str(n_samples),
        "-a", "1",
        "-l", "24",
        "-g", "32",
    ]

    try:
        proc_hrf = subprocess.run(cmd_hackrf, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=dur + 4.0)
        iq_bytes = proc_hrf.stdout or b""
        if len(iq_bytes) < 4096:
            return {
                "success": False,
                "freq_hz": f_hz,
                "count": 0,
                "packets": [],
                "raw_lines": [],
                "error": "HackRF capture returned short data",
            }

        pcm_bytes = fm_demodulate_iq_to_pcm(iq_bytes, in_rate=sample_rate_hz, out_rate=22_050)

        cmd_multi = ["multimon-ng", "-t", "raw", "-a", "AFSK1200", "-A", "-"]
        proc_multi = subprocess.run(cmd_multi, input=pcm_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5.0)

        lines: List[str] = []
        packets: List[str] = []
        for line in proc_multi.stdout.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            lines.append(line)
            if "APRS:" in line or ">" in line:
                packets.append(line)

        return {
            "success": True,
            "freq_hz": f_hz,
            "count": len(packets),
            "packets": packets,
            "raw_lines": lines,
            "error": None,
        }

    except Exception as exc:
        return {
            "success": False,
            "freq_hz": f_hz,
            "count": 0,
            "packets": [],
            "raw_lines": [],
            "error": str(exc),
        }


def run_atsc_tv_analysis(freq_hz: float) -> Dict[str, Any]:
    """
    Performs physical-layer ATSC 1.0 / ATSC 3.0 digital broadcast TV analysis
    for a given carrier frequency:
      - Maps to ATSC terrestrial channel number (2 - 36)
      - Computes 6.000 MHz channel boundaries and exact 8VSB pilot carrier frequency (+309.4 kHz)
      - Generates automated scanner command (w_scan_cpp) and Gqrx raw IQ visual pandapter settings.
    """
    f_hz = float(freq_hz)
    tv_info = identify_atsc_tv_channel(f_hz)

    if tv_info is None:
        return {
            "is_atsc_tv": False,
            "freq_hz": f_hz,
            "channel": None,
            "message": f"{f_hz / 1e6:.3f} MHz is outside North American ATSC broadcast TV allocations.",
        }

    ch, center, lo, pilot = tv_info
    hi = lo + 6_000_000.0
    offset_from_pilot = f_hz - pilot

    return {
        "is_atsc_tv": True,
        "channel": ch,
        "band_name": f"ATSC Digital Television Channel {ch}",
        "channel_range_mhz": f"{lo / 1e6:.3f} - {hi / 1e6:.3f} MHz",
        "center_freq_mhz": round(center / 1e6, 4),
        "pilot_carrier_mhz": round(pilot / 1e6, 4),
        "target_freq_mhz": round(f_hz / 1e6, 4),
        "offset_from_pilot_khz": round(offset_from_pilot / 1000.0, 1),
        "modulation": "8VSB (ATSC 1.0) / OFDM (ATSC 3.0)",
        "bandwidth_mhz": 6.0,
        "scanner_tool": "w_scan_cpp",
        "scanner_command": "w_scan_cpp -f a -A 1 -c US",
        "gqrx_pandapter_settings": {
            "mode": "Raw I/Q",
            "sample_rate_hz": 6_000_000,
            "center_freq_hz": int(center),
            "filter_bw_hz": 6_000_000,
        },
    }


def demodulate_signal(
    freq_hz: float,
    demod: str = "",
    band_name: str = "",
    service_type: str = "",
    pic_label: str = "",
    profile_category: str = "",
    duration_sec: float = 3.0,
) -> Dict[str, Any]:
    """
    High-level universal demodulator dispatcher.
    Directs the input signal to the optimal decoder (rtl_433, multimon_ng, w_scan_cpp, or Gqrx)
    and returns parsed telemetry, TV channel data, or digital messages.
    """
    prof = get_demod_profile_for_signal(
        freq_hz=freq_hz,
        demod=demod,
        band_name=band_name,
        service_type=service_type,
        pic_label=pic_label,
        profile_category=profile_category,
    )

    result = {
        "tool_id": prof.tool_id,
        "tool_name": prof.tool_name,
        "category": prof.category,
        "demod_label": prof.demod_label,
        "freq_hz": int(freq_hz),
        "freq_mhz": round(freq_hz / 1e6, 4),
        "command": prof.command,
        "demod_data": {},
    }

    # 1. IoT / ISM Telemetry
    if prof.tool_id == "rtl_433":
        res = run_telemetry_decode(freq_hz, duration_sec=duration_sec)
        result["demod_data"] = {
            "type": "TELEMETRY",
            "count": res["count"],
            "packets": res["packets"],
            "raw_lines": res["raw_lines"][:10],
            "error": res.get("error"),
        }
        return result

    # 2. Pager & Data Dispatch
    if prof.tool_id == "multimon_ng":
        res = run_pager_data_decode(freq_hz, duration_sec=duration_sec)
        result["demod_data"] = {
            "type": "DATA_PAGER",
            "count": res["count"],
            "messages": res["messages"],
            "raw_lines": res["raw_lines"][:10],
            "error": res.get("error"),
        }
        return result

    # 3. APRS Tactical Packet Telemetry
    if prof.tool_id == "direwolf" or abs(freq_hz - 144_390_000) <= 25_000:
        res = run_aprs_data_decode(freq_hz, duration_sec=duration_sec)
        result["demod_data"] = {
            "type": "TELEMETRY_APRS",
            "count": res["count"],
            "packets": res["packets"],
            "raw_lines": res["raw_lines"][:10],
            "error": res.get("error"),
        }
        return result

    # 4. Digital Broadcast Television
    if prof.tool_id == "w_scan_cpp" or prof.category == "Broadcast_TV":
        tv_data = run_atsc_tv_analysis(freq_hz)
        result["demod_data"] = {
            "type": "BROADCAST_TV",
            "tv_info": tv_data,
        }
        return result

    # 5. Voice / Audio Broadcast -> Gqrx SDR
    result["demod_data"] = {
        "type": "VOICE_AUDIO",
        "gqrx_mode": prof.demod_mode or "Narrow FM",
        "description": prof.description,
    }
    return result
