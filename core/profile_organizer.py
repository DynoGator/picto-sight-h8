"""
Locality Profile Organizer, Permanent Disk Persistence & Spec-Compliant Gqrx Bookmarks Engine
Organizes multi-layer RF + Hailo-8 AI classifications into:
  1. Numerically ordered ClassifiedSignalProfile lists
  2. Permanently saved, reboot-safe Location Profiles (with approximate local date/time, GPS/grid,
     scan mode, demod/strength settings, category summaries, and 3-frame spectrogram PNG strips)
  3. 100% Gqrx 2.17.6 two-table bookmarks.csv files (with Tag color palette and Active_Target marker)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional, Any

import cv2

from core.bandplans import (
    BOOKMARK_CATEGORY_COLORS,
    NOAA_WX_CHANNELS_HZ,
    evaluate_signal_demod_feasibility,
    normalize_gqrx_waterfall_bounds,
)
from core.gps_engine import GPSLocalityFix, format_approx_local_datetime
from core.gqrx_controller import GqrxController

logger = logging.getLogger("smart_scanner.organizer")

# Locality-specific FCC FM Broadcast reference stations for Denver / Colorado Front Range (Grid DM79)
DENVER_FM_CALLSIGNS_MHZ: Dict[float, str] = {
    88.1: "KVOD CPR Classical",
    88.5: "KGNU Community Radio",
    89.3: "KUVO Jazz",
    90.1: "KCFR Colorado Public Radio",
    91.1: "KLDV K-LOVE",
    92.5: "KKSE Altitude Sports",
    93.3: "KTCL Channel 93.3",
    94.7: "KRKS",
    95.7: "KPTT The Party / Wave",
    96.5: "KXPK La Tricolor",
    97.3: "KBCO World Class Rock",
    98.5: "KYGO Country",
    99.5: "KQMT The Mountain",
    100.3: "KIMN Mix 100",
    101.1: "KSIR / KOSI",
    101.5: "KJHM Jammin 101.5",
    102.3: "KCFR Indie 102.3",
    103.5: "KQKS The Fox",
    104.3: "KKFN The Fan",
    105.1: "KXKL Kool 105",
    105.9: "KALC Alice 105.9",
    106.7: "KWBL The Bull",
    107.1: "KFCO Flo 107.1",
    107.5: "KQKS KS107.5",
    107.9: "KBPI Rocks the Rockies",
}


@dataclass
class ClassifiedSignalProfile:
    frequency_hz: int
    frequency_mhz: float
    raw_measured_hz: float
    carrier_offset_hz: float
    signal_name: str
    profile_category: str
    modulation: str             # Exact Gqrx 2.17.6 UI demod string
    rigctl_mode: str            # Exact Gqrx TCP 7356 mode string
    passband_hz: int
    filter_low_cut: int
    filter_high_cut: int
    fm_maxdev: int
    sql_level: float
    bandwidth_hz: float
    snr_db: float
    peak_dbm: float             # Calibrated dBFS peak
    noise_floor_dbm: float      # Calibrated dBFS baseline floor
    iq_mod_hint: str
    duty_cycle: float
    frames_evaluated: int       # Always >= 3 in Layer 3
    pictogram_category: str
    pictogram_neural_class: str
    pictogram_label: str
    pictogram_confidence: float
    hailo_accelerated: bool
    lna_gain: int               # 0..40 step 8 (dB)
    vga_gain: int               # 0..62 step 2 (dB)
    amp_gain: int               # 0 or 14 (dB)
    plot_min: float             # Gqrx pandapter_min_db / waterfall_min_db
    plot_max: float             # Gqrx pandapter_max_db / waterfall_max_db
    is_legal: bool
    legal_notes: str
    locality_ref: str
    timestamp_iso: str
    thumbnail_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClassifiedSignalProfile":
        defaults = {
            "raw_measured_hz": float(data.get("frequency_hz", 0)),
            "carrier_offset_hz": 0.0,
            "rigctl_mode": "WFM_ST" if "WFM" in str(data.get("modulation", "")) else "FM",
            "passband_hz": int(data.get("bandwidth_hz", 16000)),
            "sql_level": -150.0,
            "iq_mod_hint": "NFM",
            "duty_cycle": 1.0,
            "frames_evaluated": 3,
            "pictogram_neural_class": "carrier_comb",
            "thumbnail_path": "",
        }
        merged = {**defaults, **data}
        valid_keys = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in merged.items() if k in valid_keys}
        wf_min, wf_max = normalize_gqrx_waterfall_bounds(
            float(filtered.get("plot_min", -92.0)),
            float(filtered.get("plot_max", -25.0)),
        )
        filtered["plot_min"] = float(wf_min)
        filtered["plot_max"] = float(wf_max)
        return cls(**filtered)


@dataclass
class SavedLocationProfile:
    profile_id: str
    display_title: str
    approx_datetime_local: str
    utc_timestamp_iso: str
    locality_name: str
    maidenhead_grid: str
    latitude: float
    longitude: float
    gps_source: str
    scan_mode: str              # "QUICK", "DEEP", "CUSTOM"
    freq_min_mhz: int
    freq_max_mhz: int
    min_snr_db: float
    min_peak_dbfs: float
    demod_mode: str
    squelch_mode: str
    scan_duration_s: float
    signal_count: int
    category_counts: Dict[str, int] = field(default_factory=dict)
    json_path: str = ""
    bookmarks_csv_path: str = ""
    signals: List[ClassifiedSignalProfile] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["signals"] = [s.to_dict() for s in self.signals]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SavedLocationProfile":
        sigs = [ClassifiedSignalProfile.from_dict(s) for s in data.get("signals", [])]
        sigs.sort(key=lambda p: p.frequency_hz)
        return cls(
            profile_id=str(data.get("profile_id", "profile")),
            display_title=str(data.get("display_title", "Saved Location Profile")),
            approx_datetime_local=str(data.get("approx_datetime_local", "")),
            utc_timestamp_iso=str(data.get("utc_timestamp_iso", "")),
            locality_name=str(data.get("locality_name", "Unknown Locality")),
            maidenhead_grid=str(data.get("maidenhead_grid", "DM79mr")),
            latitude=float(data.get("latitude", 39.7392)),
            longitude=float(data.get("longitude", -104.9903)),
            gps_source=str(data.get("gps_source", "LOCAL_DEFAULT")),
            scan_mode=str(data.get("scan_mode", "QUICK")),
            freq_min_mhz=int(data.get("freq_min_mhz", 88)),
            freq_max_mhz=int(data.get("freq_max_mhz", 137)),
            min_snr_db=float(data.get("min_snr_db", 8.0)),
            min_peak_dbfs=float(data.get("min_peak_dbfs", -75.0)),
            demod_mode=str(data.get("demod_mode", "AUTO (Band + AI)")),
            squelch_mode=str(data.get("squelch_mode", "OPEN")),
            scan_duration_s=float(data.get("scan_duration_s", 0.0)),
            signal_count=len(sigs),
            category_counts=dict(data.get("category_counts", {})),
            json_path=str(data.get("json_path", "")),
            bookmarks_csv_path=str(data.get("bookmarks_csv_path", "")),
            signals=sigs,
        )


def _atomic_write_text(target: Path, content: str) -> None:
    """Writes text atomically with fsync so saved profiles survive sudden power loss or reboot."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


class SignalProfileOrganizer:
    """
    Organizes multi-layer classified signals into permanent, reboot-persistent Location Profiles
    and spec-compliant Gqrx 2.17.6 bookmark sets.
    """

    def __init__(
        self,
        export_dir: Optional[str] = None,
        profiles_dir: Optional[str] = None,
    ) -> None:
        base = Path(__file__).resolve().parent.parent
        self.export_dir = Path(export_dir) if export_dir else (base / "export")
        self.profiles_dir = Path(profiles_dir) if profiles_dir else (base / "profiles")
        self.thumbs_dir = self.profiles_dir / "thumbnails"

        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)

    def organize_and_save_location_profile(
        self,
        layer3_signals: List[Dict[str, Any]],
        gps_fix: GPSLocalityFix,
        scan_config: Any,
        scan_duration_s: float = 0.0,
    ) -> Tuple[List[ClassifiedSignalProfile], SavedLocationProfile]:
        """
        Refines Layer 3 records into a numerically ordered signal list, builds a permanently saved
        Location Profile stamped with approximate date/time & GPS locality, saves 3-frame spectrogram
        thumbnails, and exports Gqrx 2.17.6 bookmarks.
        """
        approx_dt = format_approx_local_datetime()
        ts_slug = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
        loc_short = self._slugify_locality(gps_fix.locality_name)
        mode_str = getattr(scan_config, "mode", "QUICK").upper()
        profile_id = f"{loc_short}_{gps_fix.maidenhead_grid}_{ts_slug}_{mode_str}"

        demod_override = getattr(scan_config, "demod_mode", "AUTO (Band + AI)")
        squelch_mode = getattr(scan_config, "squelch_mode", "OPEN")
        only_legal = bool(getattr(scan_config, "only_legal_demod", True))
        min_snr_req = float(getattr(scan_config, "min_snr_db", 8.0))
        min_peak_req = float(getattr(scan_config, "min_peak_dbfs", -80.0))

        locality_str = f"{gps_fix.locality_name} ({gps_fix.maidenhead_grid})"
        profiles: List[ClassifiedSignalProfile] = []

        for s in layer3_signals:
            freq_hz = int(round(float(s.get("center_freq_hz", 0))))
            if freq_hz <= 0:
                continue

            snr_db = float(s.get("snr_db", 10.0))
            peak_dbfs = float(s.get("peak_dbm", -50.0))
            noise_dbfs = float(s.get("noise_floor_dbm", -82.0))
            bw_hz = float(s.get("estimated_bandwidth_hz", 16_000.0))
            iq_mod_hint = str(s.get("iq_mod_hint", "NFM"))
            duty_cycle = float(s.get("duty_cycle", 1.0))

            # Enforce user's viable signal strength thresholds
            if snr_db < (min_snr_req - 1.0) or peak_dbfs < (min_peak_req - 2.0):
                continue

            feasibility = evaluate_signal_demod_feasibility(
                freq_hz=freq_hz,
                snr_db=snr_db,
                measured_bw_hz=bw_hz,
                peak_dbfs=peak_dbfs,
                noise_floor_dbfs=noise_dbfs,
                iq_mod_hint=iq_mod_hint,
                demod_override=demod_override,
                squelch_mode=squelch_mode,
                region=gps_fix.region_code,
            )

            if only_legal and not feasibility["is_legal"]:
                continue

            pic_cat = str(s.get("picto_category", "NARROWBAND_FM"))
            pic_neural = str(s.get("picto_neural_class", "carrier_comb"))
            pic_label = str(s.get("picto_label", "NFM-Voice"))
            pic_conf = float(s.get("picto_confidence", 0.85))
            hailo_acc = bool(s.get("hailo_accelerated", False))
            num_frames = max(3, int(s.get("num_frames", 3)))

            category = feasibility["profile_category"]
            if pic_cat in ("PICTO_GLYPH", "PICTO_RASTER", "PICTO_GEOMETRIC"):
                category = "Pictogram_Exotic"

            name = self._generate_channel_name(
                freq_hz=freq_hz,
                band_name=feasibility["band_name"],
                demod=feasibility["demod"],
                pic_label=pic_label,
                gps_fix=gps_fix,
            )

            # Persist 3+ frame composite spectrogram PNG thumbnail on disk
            thumb_file = ""
            comp_rgb = s.get("composite_rgb")
            if comp_rgb is not None:
                try:
                    t_path = self.thumbs_dir / f"{profile_id}_{freq_hz}.png"
                    bgr = cv2.cvtColor(comp_rgb, cv2.COLOR_RGB2BGR)
                    cv2.imwrite(str(t_path), bgr)
                    thumb_file = str(t_path)
                except Exception as exc:
                    logger.debug("Thumbnail save note: %s", exc)

            prof = ClassifiedSignalProfile(
                frequency_hz=freq_hz,
                frequency_mhz=round(freq_hz / 1e6, 4),
                raw_measured_hz=float(s.get("raw_measured_hz", freq_hz)),
                carrier_offset_hz=float(s.get("carrier_offset_hz", 0.0)),
                signal_name=name,
                profile_category=category,
                modulation=feasibility["demod"],
                rigctl_mode=feasibility["rigctl_mode"],
                passband_hz=feasibility["passband_hz"],
                filter_low_cut=feasibility["filter_low_cut"],
                filter_high_cut=feasibility["filter_high_cut"],
                fm_maxdev=feasibility["fm_maxdev"],
                sql_level=feasibility["sql_level"],
                bandwidth_hz=round(bw_hz, 0),
                snr_db=round(snr_db, 1),
                peak_dbm=round(peak_dbfs, 1),
                noise_floor_dbm=round(noise_dbfs, 1),
                iq_mod_hint=iq_mod_hint,
                duty_cycle=round(duty_cycle, 2),
                frames_evaluated=num_frames,
                pictogram_category=pic_cat,
                pictogram_neural_class=pic_neural,
                pictogram_label=pic_label,
                pictogram_confidence=round(pic_conf, 2),
                hailo_accelerated=hailo_acc,
                lna_gain=feasibility["lna_gain"],
                vga_gain=feasibility["vga_gain"],
                amp_gain=feasibility["amp_gain"],
                plot_min=float(feasibility["pandapter_min_db"]),
                plot_max=float(feasibility["pandapter_max_db"]),
                is_legal=feasibility["is_legal"],
                legal_notes=feasibility["legal_notes"],
                locality_ref=locality_str,
                timestamp_iso=str(s.get("timestamp_iso", gps_fix.utc_timestamp_iso)),
                thumbnail_path=thumb_file,
            )
            profiles.append(prof)

        # STRICT REQUIREMENT: Numerical ordered list based on frequency (ascending)
        profiles.sort(key=lambda p: p.frequency_hz)

        cat_counts: Dict[str, int] = {}
        for p in profiles:
            cat_counts[p.profile_category] = cat_counts.get(p.profile_category, 0) + 1

        profile_json_path = self.profiles_dir / f"{profile_id}.json"
        profile_csv_path = self.profiles_dir / f"{profile_id}_bookmarks.csv"

        city_short = gps_fix.locality_name.split(",")[0].strip()
        f_min = int(getattr(scan_config, "freq_min_mhz", 88))
        f_max = int(getattr(scan_config, "freq_max_mhz", 137))
        display_title = (
            f"{city_short} ({gps_fix.maidenhead_grid}) • {approx_dt} • "
            f"{mode_str} {f_min}-{f_max}MHz ({len(profiles)} sigs)"
        )

        saved_loc = SavedLocationProfile(
            profile_id=profile_id,
            display_title=display_title,
            approx_datetime_local=approx_dt,
            utc_timestamp_iso=gps_fix.utc_timestamp_iso,
            locality_name=gps_fix.locality_name,
            maidenhead_grid=gps_fix.maidenhead_grid,
            latitude=gps_fix.latitude,
            longitude=gps_fix.longitude,
            gps_source=gps_fix.source,
            scan_mode=mode_str,
            freq_min_mhz=f_min,
            freq_max_mhz=f_max,
            min_snr_db=min_snr_req,
            min_peak_dbfs=min_peak_req,
            demod_mode=demod_override,
            squelch_mode=squelch_mode,
            scan_duration_s=round(scan_duration_s, 2),
            signal_count=len(profiles),
            category_counts=cat_counts,
            json_path=str(profile_json_path),
            bookmarks_csv_path=str(profile_csv_path),
            signals=profiles,
        )

        # Save permanent Location Profile JSON + latest pointer + bookmarks CSV
        json_payload = json.dumps(saved_loc.to_dict(), indent=2)
        _atomic_write_text(profile_json_path, json_payload)
        _atomic_write_text(self.profiles_dir / "latest_profile.json", json_payload)
        _atomic_write_text(self.export_dir / "classified_signals.json", json_payload)

        self.export_gqrx_bookmarks(
            profiles=profiles,
            active_freq_hz=None,
            extra_csv_path=profile_csv_path,
        )
        self._rebuild_profiles_index()

        logger.info(
            "Saved permanent Location Profile '%s' (%d signals) at %s",
            profile_id,
            len(profiles),
            profile_json_path,
        )
        return profiles, saved_loc

    # Backwards-compatible wrapper
    def organize_and_refine(
        self,
        layer3_signals: List[Dict[str, Any]],
        gps_fix: GPSLocalityFix,
    ) -> List[ClassifiedSignalProfile]:
        from core.hackrf_engine import ScanConfig

        profiles, _ = self.organize_and_save_location_profile(
            layer3_signals=layer3_signals,
            gps_fix=gps_fix,
            scan_config=ScanConfig(),
        )
        return profiles

    # ------------------------------------------------------------------
    # Permanent Location Profile Discovery & Reloading (Reboot-Safe)
    # ------------------------------------------------------------------
    def _rebuild_profiles_index(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for p_file in sorted(self.profiles_dir.glob("*.json"), reverse=True):
            if p_file.name in ("latest_profile.json", "profiles_index.json", "last_known_locality.json"):
                continue
            try:
                raw = json.loads(p_file.read_text(encoding="utf-8"))
                entries.append(
                    {
                        "profile_id": raw.get("profile_id", p_file.stem),
                        "display_title": raw.get("display_title", p_file.stem),
                        "approx_datetime_local": raw.get("approx_datetime_local", ""),
                        "utc_timestamp_iso": raw.get("utc_timestamp_iso", ""),
                        "locality_name": raw.get("locality_name", ""),
                        "maidenhead_grid": raw.get("maidenhead_grid", ""),
                        "scan_mode": raw.get("scan_mode", "QUICK"),
                        "freq_min_mhz": raw.get("freq_min_mhz", 88),
                        "freq_max_mhz": raw.get("freq_max_mhz", 137),
                        "signal_count": raw.get("signal_count", len(raw.get("signals", []))),
                        "json_path": str(p_file),
                        "bookmarks_csv_path": raw.get("bookmarks_csv_path", ""),
                    }
                )
            except Exception:
                continue

        entries.sort(key=lambda e: str(e.get("utc_timestamp_iso", "")), reverse=True)
        _atomic_write_text(self.profiles_dir / "profiles_index.json", json.dumps(entries, indent=2))
        return entries

    def list_saved_location_profiles(self) -> List[Dict[str, Any]]:
        return self._rebuild_profiles_index()

    def load_location_profile(self, path_or_id: str) -> Optional[SavedLocationProfile]:
        p = Path(path_or_id)
        if not p.exists():
            p = self.profiles_dir / f"{path_or_id}.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            prof = SavedLocationProfile.from_dict(data)
            # Immediately sync loaded profile's bookmarks into Gqrx
            self.export_gqrx_bookmarks(prof.signals)
            return prof
        except Exception as exc:
            logger.warning("Failed loading saved Location Profile %s: %s", p, exc)
            return None

    def load_latest_location_profile(self) -> Optional[SavedLocationProfile]:
        latest = self.profiles_dir / "latest_profile.json"
        if latest.exists():
            prof = self.load_location_profile(str(latest))
            if prof is not None:
                return prof
        all_entries = self.list_saved_location_profiles()
        if all_entries:
            return self.load_location_profile(all_entries[0]["json_path"])
        return None

    def delete_location_profile(self, path_or_id: str) -> bool:
        p = Path(path_or_id)
        if not p.exists():
            p = self.profiles_dir / f"{path_or_id}.json"
        if not p.exists() or p.name in ("latest_profile.json", "profiles_index.json"):
            return False
        try:
            pid = p.stem
            csv_p = self.profiles_dir / f"{pid}_bookmarks.csv"
            if csv_p.exists():
                csv_p.unlink()
            for th in self.thumbs_dir.glob(f"{pid}_*.png"):
                th.unlink()
            p.unlink()
            remaining = self._rebuild_profiles_index()
            if remaining:
                newest = Path(remaining[0]["json_path"])
                if newest.exists():
                    _atomic_write_text(self.profiles_dir / "latest_profile.json", newest.read_text(encoding="utf-8"))
            else:
                lp = self.profiles_dir / "latest_profile.json"
                if lp.exists():
                    lp.unlink()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Spec-Compliant Gqrx 2.17.6 Two-Table Bookmarks Exporter
    # ------------------------------------------------------------------
    def export_gqrx_bookmarks(
        self,
        profiles: List[ClassifiedSignalProfile],
        active_freq_hz: Optional[int] = None,
        extra_csv_path: Optional[Path] = None,
    ) -> str:
        """
        Writes 100% spec-compliant Gqrx 2.17.6 two-table bookmarks.csv:
          Section 1: # Tag name ; color
          Section 2: # Frequency ; Name ; Modulation ; Bandwidth ; Tags
        Embeds exact AI classification + demod label into Name so Gqrx's live pandapter
        markers (fft/markers=true) and Bookmarks dock actively display the classification.
        """
        local_csv = self.export_dir / "gqrx_bookmarks.csv"
        gqrx_cfg_dir = Path(os.path.expanduser("~/.config/gqrx"))
        gqrx_cfg_dir.mkdir(parents=True, exist_ok=True)
        system_csv = gqrx_cfg_dir / "bookmarks.csv"

        lines: List[str] = [
            "# Tag name ; color\n",
            "Untagged ; #c0c0c0\n",
        ]
        for tag_name, hex_color in BOOKMARK_CATEGORY_COLORS.items():
            lines.append(f"{tag_name} ; {hex_color}\n")

        lines.append("\n")
        lines.append("# Frequency ; Name ; Modulation ; Bandwidth ; Tags\n")

        ordered = sorted(profiles, key=lambda x: x.frequency_hz)
        for p in ordered:
            is_target = active_freq_hz is not None and abs(p.frequency_hz - active_freq_hz) <= 1_500
            prefix = "[ACTIVE] " if is_target else ""
            # Sanitize semicolons and commas cleanly for Gqrx's ';' parser
            base_name = p.signal_name.replace(";", "-").strip()
            ai_badge = f"AI:{p.pictogram_label} {int(round(p.pictogram_confidence * 100))}%"
            if p.pictogram_label not in base_name:
                full_name = f"{prefix}{base_name} [{ai_badge}]"
            else:
                full_name = f"{prefix}{base_name} ({int(round(p.pictogram_confidence * 100))}%)"
            full_name = full_name.replace(";", "-")

            # Exact Gqrx UI modulation name (WFM (stereo), Narrow FM, AM, USB, LSB, CW-U, Raw I/Q)
            gqrx_mod = p.modulation
            bw_int = max(500, int(round(p.passband_hz if p.passband_hz > 0 else p.bandwidth_hz)))

            tags = []
            if is_target:
                tags.append("Active_Target")
            cat_tag = p.profile_category.replace(";", "_").replace(",", "_")
            tags.append(cat_tag)
            tags.append("SmartScanner")
            tags_col = ",".join(tags)

            lines.append(f"{p.frequency_hz} ; {full_name} ; {gqrx_mod} ; {bw_int} ; {tags_col}\n")

        content = "".join(lines)
        _atomic_write_text(local_csv, content)
        if extra_csv_path is not None:
            _atomic_write_text(extra_csv_path, content)
        try:
            _atomic_write_text(system_csv, content)
            if active_freq_hz is None:
                GqrxController.mark_bookmarks_dirty()
        except Exception as exc:
            logger.warning("Could not write system Gqrx bookmarks.csv: %s", exc)

        return str(local_csv)

    # ------------------------------------------------------------------
    # Locality-Aware Signal Naming
    # ------------------------------------------------------------------
    @staticmethod
    def _slugify_locality(locality_name: str) -> str:
        head = locality_name.split(",")[0].strip() or "FieldSite"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", head).strip("_")
        return slug[:24] or "FieldSite"

    @staticmethod
    def _generate_channel_name(
        freq_hz: int,
        band_name: str,
        demod: str,
        pic_label: str,
        gps_fix: GPSLocalityFix,
    ) -> str:
        f_mhz = round(freq_hz / 1e6, 4)
        in_denver_grid = gps_fix.maidenhead_grid.upper().startswith("DM79") or "Denver" in gps_fix.locality_name

        # 1. FM Commercial Broadcast
        if 87_800_000 <= freq_hz <= 108_100_000:
            key_1 = round(freq_hz / 1e6, 1)
            if in_denver_grid and key_1 in DENVER_FM_CALLSIGNS_MHZ:
                return f"{key_1:.1f} MHz {DENVER_FM_CALLSIGNS_MHZ[key_1]} [{pic_label}]"
            return f"{f_mhz:.3f} MHz FM Broadcast [{pic_label}]"

        # 2. NOAA All-Hazards Weather Radio WX1..WX7
        if 162_385_000 <= freq_hz <= 162_565_000:
            wx_map = {
                162_550_000: "WX1",
                162_400_000: "WX2",
                162_475_000: "WX3",
                162_425_000: "WX4",
                162_450_000: "WX5",
                162_500_000: "WX6",
                162_525_000: "WX7",
            }
            nearest = min(NOAA_WX_CHANNELS_HZ, key=lambda c: abs(c - freq_hz))
            wx_id = wx_map.get(nearest, "WX")
            return f"{f_mhz:.3f} MHz NOAA Weather {wx_id} [{pic_label}]"

        # 3. Civil Aviation Airband
        if 118_000_000 <= freq_hz <= 136_975_000:
            if abs(freq_hz - 121_500_000) <= 5_000:
                return "121.500 MHz Airband Emergency Guard (AM)"
            if in_denver_grid:
                if abs(freq_hz - 118_750_000) <= 8_000:
                    return f"{f_mhz:.3f} MHz KDEN/KBJC Approach ATC [{pic_label}]"
                if abs(freq_hz - 125_600_000) <= 8_000:
                    return f"{f_mhz:.3f} MHz KDEN ATIS Weather [{pic_label}]"
            return f"{f_mhz:.3f} MHz Civil Airband ATC [{pic_label}]"

        # 4. Marine & Amateur Calling Channels
        if abs(freq_hz - 156_800_000) <= 8_000:
            return "156.800 MHz Marine VHF Ch 16 Distress"
        if abs(freq_hz - 146_520_000) <= 6_000:
            return "146.520 MHz 2m Amateur National Simplex"
        if abs(freq_hz - 446_000_000) <= 6_000:
            return "446.000 MHz 70cm Amateur National Simplex"

        # 5. FRS / GMRS Channels
        if 462_550_000 <= freq_hz <= 467_725_000:
            return f"{f_mhz:.4f} MHz FRS/GMRS Personal [{pic_label}]"

        return f"{f_mhz:.4f} MHz {band_name} [{pic_label}]"
