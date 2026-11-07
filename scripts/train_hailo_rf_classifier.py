#!/usr/bin/env python3
"""
scripts/train_hailo_rf_classifier.py
Generates a physics-grounded 10-class RF IQ/STFT spectrogram dataset (2,500 frames),
extracts 1000-D embeddings on the live Hailo-8 26 TOPS PCIe NPU (/dev/hailo0) fused
with 16-D scale-aware physical spectral morphology features (1016-D total), trains
both the Hailo-8 BatchNorm+MLP transfer head and the 16-D CPU fallback MLP head,
evaluates held-out validation accuracy & per-class metrics, and saves calibrated
weights to models/hailo_pictogram_head_weights.npz.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.hailo_pictogram import (
    HailoPictogramClassifier,
    PictogramROI,
    compute_roi_physical_features,
)
from core.pictogram_engine import AIPictogramClassifierManager

CLASSES: List[str] = [
    "adsb_pulse",
    "am_voice",
    "atsc_tv",
    "fsk_pager",
    "geometric",
    "glyph",
    "ism_telemetry",
    "nfm_voice",
    "noise_reject",
    "wfm_stereo",
]


def _iq_to_1mhz_stft_frame(
    iq: np.ndarray,
    rows: int = 64,
    cols: int = 128,
    c_bin_start: int = 96,
    c_bin_end: int = 224,
) -> np.ndarray:
    """Exact HackRF Layer 3 2.0 MSPS -> 256-pt Hann STFT -> 64x128 1.000 MHz dBFS pipeline."""
    # Quantize to 8-bit signed IQ like HackRF MAX5864 ADC
    peak = max(1.0, float(np.max(np.abs(iq.real))), float(np.max(np.abs(iq.imag))))
    iq_q = (
        np.clip(np.round((iq.real / peak) * 127.0), -128, 127).astype(np.float32) / 128.0
        + 1j * np.clip(np.round((iq.imag / peak) * 127.0), -128, 127).astype(np.float32) / 128.0
    )
    iq_q = iq_q - np.mean(iq_q)

    nfft = 256
    window = np.hanning(nfft).astype(np.float32)
    win_sum = float(np.sum(window))
    sub_rows = rows * 2
    starts = np.linspace(0, len(iq_q) - nfft, sub_rows, dtype=np.int64)
    idx_grid = starts[:, None] + np.arange(nfft, dtype=np.int64)[None, :]
    windowed = iq_q[idx_grid] * window[None, :]
    spectra = np.fft.fftshift(np.fft.fft(windowed, n=nfft, axis=1), axes=1)
    mag_sq = (np.abs(spectra) / win_sum) ** 2
    mag_sq[:, 128] = 0.5 * (mag_sq[:, 127] + mag_sq[:, 129])
    avg_mag_sq = 0.5 * (mag_sq[0::2, :] + mag_sq[1::2, :])
    dbfs_full = 10.0 * np.log10(avg_mag_sq + 1e-12)
    return dbfs_full[:rows, c_bin_start:c_bin_end].astype(np.float32)


def _lowpass_noise(rng: np.random.Generator, n: int, cutoff_frac: float) -> np.ndarray:
    """Band-limited random audio/symbol modulation waveform normalized to unit peak."""
    raw = rng.standard_normal(n).astype(np.float32)
    spec = np.fft.rfft(raw)
    freqs = np.linspace(0.0, 1.0, len(spec))
    spec *= np.exp(-0.5 * (freqs / max(1e-4, cutoff_frac)) ** 4)
    out = np.fft.irfft(spec, n=n).astype(np.float32)
    return out / (float(np.percentile(np.abs(out), 98)) + 1e-6)


def synthesize_class_frame(cls_name: str, rng: np.random.Generator) -> Tuple[np.ndarray, float]:
    """
    Synthesize a realistic 64x128 1.000 MHz STFT dBFS frame + optional reference floor
    matching real HackRF One captures across SNR 8..40 dB.
    """
    fs = 2_000_000.0
    n = 45_056  # ~22.5 ms at 2.0 MSPS -> 128 non-overlapping / 2x-averaged 64 STFT rows
    t = np.arange(n, dtype=np.float32) / fs

    noise_amp = float(rng.uniform(0.015, 0.055))
    awgn = (
        rng.normal(0.0, noise_amp, size=n).astype(np.float32)
        + 1j * rng.normal(0.0, noise_amp, size=n).astype(np.float32)
    )
    snr_db = float(rng.uniform(10.0, 38.0))
    sig_amp = min(0.88, noise_amp * (10.0 ** (snr_db / 20.0)) * 0.18)

    # Target centered around +250 kHz (+ small random tuning offset)
    f_off = 250_000.0 + float(rng.uniform(-35_000.0, 35_000.0))
    ref_floor = -84.0

    if cls_name == "wfm_stereo":
        # Stereo MPX: L+R (15 kHz) + 19 kHz pilot + 38 kHz L-R + 57 kHz RDS
        mono = _lowpass_noise(rng, n, 15_000.0 / (fs * 0.5))
        diff = _lowpass_noise(rng, n, 15_000.0 / (fs * 0.5))
        pilot = np.sin(2.0 * np.pi * 19_000.0 * t)
        stereo = diff * np.sin(2.0 * np.pi * 38_000.0 * t)
        mpx = 0.45 * mono + 0.10 * pilot + 0.40 * stereo
        max_dev = float(rng.uniform(52_000.0, 78_000.0))
        phase = 2.0 * np.pi * f_off * t + 2.0 * np.pi * max_dev * np.cumsum(mpx) / fs
        iq = awgn + sig_amp * 2.2 * np.exp(1j * phase)
        frame = _iq_to_1mhz_stft_frame(iq)
        return frame, float(np.percentile(frame, 12))

    if cls_name == "nfm_voice":
        # Narrowband FM (Fire/Sheriff/NOAA/Rail): +-2.5..5.0 kHz deviation + CTCSS
        voice = _lowpass_noise(rng, n, 3_200.0 / (fs * 0.5))
        ctcss = 0.15 * np.sin(2.0 * np.pi * float(rng.choice([100.0, 123.0, 151.4])) * t)
        max_dev = float(rng.uniform(2_200.0, 5_200.0))
        # Occasional PTT onset/release (continuous 75-100% of frame)
        env = np.ones(n, dtype=np.float32)
        if rng.random() < 0.25:
            cut = int(rng.uniform(0.75, 0.95) * n)
            if rng.random() < 0.5:
                env[cut:] = 0.0
            else:
                env[: n - cut] = 0.0
        phase = 2.0 * np.pi * f_off * t + 2.0 * np.pi * max_dev * np.cumsum(voice + ctcss) / fs
        iq = awgn + sig_amp * env * np.exp(1j * phase)
        frame = _iq_to_1mhz_stft_frame(iq)
        return frame, float(np.percentile(frame, 12))

    if cls_name == "am_voice":
        # AM Airband / CB voice: strong central carrier spine + symmetric +-4.5 kHz sidebands
        voice = _lowpass_noise(rng, n, float(rng.uniform(3_500.0, 6_000.0)) / (fs * 0.5))
        mod_depth = float(rng.uniform(0.45, 0.92))
        # Modulate sideband envelope in speech syllables so carrier spine stands out clearly
        syllable = 0.55 + 0.45 * np.sin(2.0 * np.pi * float(rng.uniform(60.0, 180.0)) * t + rng.uniform(0, 6.28))
        am_env = 1.0 + mod_depth * syllable * np.clip(voice, -1.0, 1.0)
        iq = awgn + sig_amp * am_env * np.exp(1j * 2.0 * np.pi * f_off * t)
        frame = _iq_to_1mhz_stft_frame(iq)
        return frame, float(np.percentile(frame, 12))

    if cls_name == "atsc_tv":
        # ATSC 8VSB TV channel: either band-edge capture [lo-150kHz .. lo+850kHz] with +309.44 kHz pilot
        # or mid-shelf capture with out-of-channel ref_floor
        frame = _iq_to_1mhz_stft_frame(awgn)
        floor_med = float(np.median(frame))
        shelf_snr = float(rng.uniform(9.0, 24.0))
        pilot_boost = float(rng.uniform(6.5, 11.5))
        edge_col = int(rng.integers(14, 26)) if rng.random() < 0.82 else 0
        shelf = rng.normal(floor_med + shelf_snr, 1.35, size=(64, 128 - edge_col)).astype(np.float32)
        # Smooth flat-top 8VSB raised-cosine rolloff at lower band edge
        if edge_col > 0:
            ramp = np.linspace(0.2, 1.0, min(5, 128 - edge_col), dtype=np.float32)
            shelf[:, : len(ramp)] = floor_med + (shelf[:, : len(ramp)] - floor_med) * ramp[None, :]
        frame[:, edge_col:] = np.maximum(frame[:, edge_col:], shelf)
        # Add +309.441 kHz 8VSB pilot carrier (~39.6 bins above band edge)
        pilot_col = min(124, edge_col + int(round(309_441.0 / 7_812.5)) + int(rng.integers(-2, 3)))
        for dc, att in ((-1, -4.5), (0, 0.0), (1, -4.5)):
            pc = np.clip(pilot_col + dc, 0, 127)
            frame[:, pc] = np.maximum(
                frame[:, pc],
                floor_med + shelf_snr + pilot_boost + att + rng.normal(0.0, 0.6, size=64).astype(np.float32),
            )
        return frame, floor_med

    if cls_name == "fsk_pager":
        # POCSAG / FLEX 2-FSK / 4-FSK bursty data (+-4.5 kHz / +-8 kHz shift, distinct twin peaks + bursty rows)
        baud = float(rng.choice([512.0, 1200.0, 2400.0]))
        sps = max(8, int(fs / baud))
        num_syms = (n // sps) + 2
        levels = rng.choice([-1.0, 1.0] if rng.random() < 0.7 else [-1.0, -0.33, 0.33, 1.0], size=num_syms)
        syms = np.repeat(levels, sps)[:n].astype(np.float32)
        shift_hz = float(rng.uniform(4_500.0, 14_000.0))
        # Burst envelope (1 to 3 packet bursts in frame)
        env = np.zeros(n, dtype=np.float32)
        n_bursts = int(rng.integers(1, 4))
        for _ in range(n_bursts):
            b_len = int(rng.uniform(0.18, 0.45) * n)
            b_start = int(rng.integers(0, max(1, n - b_len)))
            env[b_start : b_start + b_len] = 1.0
        phase = 2.0 * np.pi * (f_off + shift_hz * syms) * t
        # Continuous phase FSK
        inst_f = f_off + shift_hz * syms
        phase = 2.0 * np.pi * np.cumsum(inst_f) / fs
        iq = awgn + sig_amp * 1.4 * env * np.exp(1j * phase)
        frame = _iq_to_1mhz_stft_frame(iq)
        return frame, float(np.percentile(frame, 12))

    if cls_name == "ism_telemetry":
        # 433 / 915 MHz OOK / ASK / PWM sensor packet pulse train (narrow-to-medium width rapid on/off rows)
        frame = _iq_to_1mhz_stft_frame(awgn)
        floor_med = float(np.median(frame))
        c_mid = 64 + int(rng.integers(-5, 6))
        bw_bins = int(rng.integers(2, 7))
        n_pulses = int(rng.integers(5, 16))
        r_cursor = int(rng.integers(2, 10))
        pk = floor_med + float(rng.uniform(14.0, 32.0))
        for _ in range(n_pulses):
            plen = int(rng.integers(1, 4))
            gap = int(rng.integers(2, 5))
            if r_cursor + plen >= 63:
                break
            for c_off in range(-bw_bins // 2, bw_bins // 2 + 1):
                cc = np.clip(c_mid + c_off, 0, 127)
                att = 3.5 * abs(c_off)
                frame[r_cursor : r_cursor + plen, cc] = np.maximum(
                    frame[r_cursor : r_cursor + plen, cc],
                    pk - att + rng.normal(0.0, 0.8, size=plen).astype(np.float32),
                )
            r_cursor += plen + gap
        return frame, floor_med

    if cls_name == "adsb_pulse":
        # 1090 MHz Mode-S / ADS-B: ultrashort (1-2 STFT rows) wideband (350-850 kHz = 45-110 bins) pulses
        frame = _iq_to_1mhz_stft_frame(awgn)
        floor_med = float(np.median(frame))
        n_frames_adsb = int(rng.integers(1, 5))
        for _ in range(n_frames_adsb):
            r0 = int(rng.integers(3, 60))
            r_h = int(rng.integers(1, 3))
            w_bins = int(rng.integers(42, 108))
            c0 = max(2, min(126 - w_bins, 64 - w_bins // 2 + int(rng.integers(-8, 9))))
            pk = floor_med + float(rng.uniform(14.0, 34.0))
            # Sinc-squared main lobe across wide bandwidth
            x = np.linspace(-0.85, 0.85, w_bins, dtype=np.float32)
            sinc_env = 10.0 * np.log10(np.sinc(x) ** 2 + 0.08)
            pulse = pk + sinc_env[None, :] + rng.normal(0.0, 1.2, size=(r_h, w_bins)).astype(np.float32)
            frame[r0 : r0 + r_h, c0 : c0 + w_bins] = np.maximum(
                frame[r0 : r0 + r_h, c0 : c0 + w_bins], pulse
            )
        return frame, floor_med

    if cls_name == "glyph":
        # 2D Waterfall Alphanumeric / Symbol Glyph (Hellschreiber / spectrogram beacon)
        frame = _iq_to_1mhz_stft_frame(awgn)
        floor_med = float(np.median(frame))
        canvas = np.zeros((64, 128), dtype=np.uint8)
        chars = list("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#%*+@")
        txt = "".join(rng.choice(chars, size=int(rng.integers(1, 3))))
        scale = float(rng.uniform(0.9, 1.6))
        thick = int(rng.integers(2, 4))
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
        x0 = max(8, min(120 - tw, 64 - tw // 2 + int(rng.integers(-6, 7))))
        y0 = max(th + 4, min(60, 32 + th // 2 + int(rng.integers(-6, 7))))
        cv2.putText(canvas, txt, (x0, y0), cv2.FONT_HERSHEY_SIMPLEX, scale, 255, thick, cv2.LINE_AA)
        canvas_f = cv2.GaussianBlur(canvas.astype(np.float32) / 255.0, (3, 3), 0.7)
        pk_snr = float(rng.uniform(14.0, 32.0))
        frame = frame + canvas_f * pk_snr
        return frame, floor_med

    if cls_name == "geometric":
        # Swept LoRa chirp / FMCW diagonal sweep / V-chirp / polygon in waterfall
        frame = _iq_to_1mhz_stft_frame(awgn)
        floor_med = float(np.median(frame))
        canvas = np.zeros((64, 128), dtype=np.uint8)
        mode = int(rng.integers(0, 3))
        c_span = int(rng.integers(18, 52))
        c_mid = 64 + int(rng.integers(-8, 9))
        r0 = int(rng.integers(4, 16))
        r1 = int(rng.integers(46, 60))
        thick = int(rng.integers(2, 4))
        if mode == 0:
            # Diagonal upchirp or downchirp
            if rng.random() < 0.5:
                cv2.line(canvas, (c_mid - c_span // 2, r0), (c_mid + c_span // 2, r1), 255, thick, cv2.LINE_AA)
            else:
                cv2.line(canvas, (c_mid + c_span // 2, r0), (c_mid - c_span // 2, r1), 255, thick, cv2.LINE_AA)
        elif mode == 1:
            # V-sweep / sawtooth chirp
            rm = (r0 + r1) // 2
            cv2.line(canvas, (c_mid - c_span // 2, r0), (c_mid + c_span // 2, rm), 255, thick, cv2.LINE_AA)
            cv2.line(canvas, (c_mid + c_span // 2, rm), (c_mid - c_span // 2, r1), 255, thick, cv2.LINE_AA)
        else:
            # Multi-chirp LoRa preamble
            for k in range(3):
                rs = r0 + k * 15
                re = min(62, rs + 13)
                cv2.line(canvas, (c_mid - c_span // 2, rs), (c_mid + c_span // 2, re), 255, 2, cv2.LINE_AA)
        canvas_f = cv2.GaussianBlur(canvas.astype(np.float32) / 255.0, (3, 3), 0.6)
        frame = frame + canvas_f * float(rng.uniform(14.0, 32.0))
        return frame, floor_med

    # noise_reject: pure AWGN thermal floor, weak random speckle, or 1-bin clock spur
    frame = _iq_to_1mhz_stft_frame(awgn)
    floor_med = float(np.median(frame))
    sub_mode = int(rng.integers(0, 3))
    if sub_mode == 1:
        # Random weak thermal speckle
        for _ in range(int(rng.integers(2, 6))):
            rr = int(rng.integers(0, 62))
            cc = int(rng.integers(40, 88))
            frame[rr : rr + 2, cc : cc + 2] += float(rng.uniform(3.5, 6.5))
    elif sub_mode == 2:
        # Weak wideband impulse click (1 row) or faint 1-bin LO leakage
        rr = int(rng.integers(5, 58))
        frame[rr, 48:80] += float(rng.uniform(3.0, 5.8))
    return frame, floor_med


def train_mlp(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    hidden_dim: int,
    num_classes: int,
    epochs: int = 320,
    lr: float = 3e-3,
    weight_decay: float = 1.5e-4,
    seed: int = 42,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
    """Pure NumPy AdamW 2-layer ReLU MLP trainer with cosine LR decay and label smoothing."""
    rng = np.random.default_rng(seed)
    n_samples, in_dim = X_train.shape

    w1 = (rng.standard_normal((in_dim, hidden_dim)) * math.sqrt(2.0 / in_dim)).astype(np.float32)
    b1 = np.zeros(hidden_dim, dtype=np.float32)
    w2 = (rng.standard_normal((hidden_dim, num_classes)) * math.sqrt(2.0 / hidden_dim)).astype(np.float32)
    b2 = np.zeros(num_classes, dtype=np.float32)

    # Adam moment buffers
    mw1, vw1 = np.zeros_like(w1), np.zeros_like(w1)
    mb1, vb1 = np.zeros_like(b1), np.zeros_like(b1)
    mw2, vw2 = np.zeros_like(w2), np.zeros_like(w2)
    mb2, vb2 = np.zeros_like(b2), np.zeros_like(b2)

    smooth = 0.03
    y_onehot = np.full((n_samples, num_classes), smooth / num_classes, dtype=np.float32)
    y_onehot[np.arange(n_samples), y_train] += 1.0 - smooth

    best_acc = 0.0
    best_params = (w1.copy(), b1.copy(), w2.copy(), b2.copy())
    batch_size = 128
    step = 0

    for ep in range(epochs):
        perm = rng.permutation(n_samples)
        cur_lr = lr * 0.5 * (1.0 + math.cos(math.pi * ep / epochs))

        for i in range(0, n_samples, batch_size):
            step += 1
            idx = perm[i : i + batch_size]
            xb = X_train[idx]
            yb = y_onehot[idx]
            bsz = float(len(idx))

            # Forward
            z1 = xb @ w1 + b1
            h1 = np.maximum(0.0, z1)
            logits = h1 @ w2 + b2
            logits_shift = logits - np.max(logits, axis=1, keepdims=True)
            exp_l = np.exp(logits_shift)
            probs = exp_l / np.sum(exp_l, axis=1, keepdims=True)

            # Backward
            dlogits = (probs - yb) / bsz
            gw2 = h1.T @ dlogits + weight_decay * w2
            gb2 = np.sum(dlogits, axis=0)
            dh1 = dlogits @ w2.T
            dz1 = dh1 * (z1 > 0.0)
            gw1 = xb.T @ dz1 + weight_decay * w1
            gb1 = np.sum(dz1, axis=0)

            b1_corr = 1.0 - 0.9 ** step
            b2_corr = 1.0 - 0.999 ** step

            for param, grad, m, v in (
                (w1, gw1, mw1, vw1),
                (b1, gb1, mb1, vb1),
                (w2, gw2, mw2, vw2),
                (b2, gb2, mb2, vb2),
            ):
                m[:] = 0.9 * m + 0.1 * grad
                v[:] = 0.999 * v + 0.001 * (grad * grad)
                param -= cur_lr * (m / b1_corr) / (np.sqrt(v / b2_corr) + 1e-8)

        # Evaluate validation accuracy
        val_h1 = np.maximum(0.0, X_val @ w1 + b1)
        val_preds = np.argmax(val_h1 @ w2 + b2, axis=1)
        val_acc = float(np.mean(val_preds == y_val))
        if val_acc >= best_acc:
            best_acc = val_acc
            best_params = (w1.copy(), b1.copy(), w2.copy(), b2.copy())

    return (*best_params, best_acc)


def main() -> int:
    t0 = time.perf_counter()
    samples_per_class = 250  # 2,500 total frames (2,000 train / 500 val)
    rng = np.random.default_rng(20261107)

    clf = HailoPictogramClassifier(fallback_ok=False)
    hw_ok = clf.initialize()
    if not hw_ok or not clf.is_hardware_active:
        print("ERROR: Hailo-8 PCIe NPU (/dev/hailo0) is required to extract hardware embeddings!")
        return 1

    mgr = AIPictogramClassifierManager()
    mgr._roi_cls = PictogramROI

    deep_list: List[np.ndarray] = []
    phys_list: List[np.ndarray] = []
    labels_list: List[int] = []

    print(f"[1/4] Synthesizing {samples_per_class * len(CLASSES)} physics-grounded RF STFT frames & extracting Hailo-8 embeddings...")
    t_inf = 0.0
    for c_idx, cls_name in enumerate(CLASSES):
        for s_idx in range(samples_per_class):
            frame, ref_floor = synthesize_class_frame(cls_name, rng)
            roi, _ = mgr._build_spectral_frame_roi(
                frame=frame,
                center_freq_hz=150_000_000.0,
                bandwidth_hz=1_000_000.0,
                frame_idx=s_idx,
                ref_floor_db=ref_floor,
            )
            assert roi is not None
            ti0 = time.perf_counter()
            emb = clf.extract_hailo_embedding(roi.normalized_rgb)
            t_inf += time.perf_counter() - ti0
            assert emb is not None
            phys = compute_roi_physical_features(roi)

            deep_list.append(emb)
            phys_list.append(phys)
            labels_list.append(c_idx)

        print(f"      Class [{c_idx + 1:2d}/{len(CLASSES)}] {cls_name:15s}: {samples_per_class} embeddings extracted")

    clf.close()

    X_deep = np.vstack(deep_list).astype(np.float32)
    X_phys = np.vstack(phys_list).astype(np.float32)
    X_full = np.hstack([X_deep, X_phys]).astype(np.float32)
    y_all = np.array(labels_list, dtype=np.int64)

    avg_ms = (t_inf / len(y_all)) * 1000.0
    print(f"      Hailo-8 mean NPU inference latency: {avg_ms:.2f} ms/ROI ({1000.0 / max(0.01, avg_ms):.0f} FPS)")

    # Stratified 80/20 train/val split
    train_idx: List[int] = []
    val_idx: List[int] = []
    for c_idx in range(len(CLASSES)):
        c_rows = np.where(y_all == c_idx)[0]
        rng.shuffle(c_rows)
        n_tr = int(0.8 * len(c_rows))
        train_idx.extend(c_rows[:n_tr].tolist())
        val_idx.extend(c_rows[n_tr:].tolist())

    X_tr_raw, y_tr = X_full[train_idx], y_all[train_idx]
    X_va_raw, y_va = X_full[val_idx], y_all[val_idx]

    # Calibrated BatchNorm parameters over 1016-D fused vector
    bn_eps = 1e-5
    bn_mean = np.mean(X_tr_raw, axis=0).astype(np.float32)
    bn_var = np.var(X_tr_raw, axis=0).astype(np.float32)
    # Prevent zero-variance division on sparse ReLU/softmax channels
    bn_var = np.maximum(bn_var, 1e-4).astype(np.float32)
    bn_weight = np.ones_like(bn_mean, dtype=np.float32)
    # Upweight the 16 physical RF features slightly (1.6x) so physical scale anchors deep ResNet textures
    bn_weight[-16:] = 1.6
    bn_bias = np.zeros_like(bn_mean, dtype=np.float32)

    X_tr_norm = ((X_tr_raw - bn_mean) / np.sqrt(bn_var + bn_eps)) * bn_weight + bn_bias
    X_va_norm = ((X_va_raw - bn_mean) / np.sqrt(bn_var + bn_eps)) * bn_weight + bn_bias

    print(f"[2/4] Training 1016 -> 192 -> {len(CLASSES)} Hailo-8 + Physical RF Fusion MLP...")
    w1, b1, w2, b2, val_acc = train_mlp(
        X_tr_norm, y_tr, X_va_norm, y_va, hidden_dim=192, num_classes=len(CLASSES), epochs=340, lr=2.8e-3
    )
    print(f"      Held-out Hailo-8 Validation Accuracy: {val_acc * 100.0:.2f}% ({len(y_va)} unseen frames)")

    # Per-class validation report
    val_h1 = np.maximum(0.0, X_va_norm @ w1 + b1)
    val_preds = np.argmax(val_h1 @ w2 + b2, axis=1)
    for c_idx, cname in enumerate(CLASSES):
        mask = y_va == c_idx
        c_acc = float(np.mean(val_preds[mask] == c_idx))
        print(f"        - {cname:15s}: {c_acc * 100.0:6.2f}%")

    # Train 16-D CPU fallback MLP head
    print(f"[3/4] Training 16 -> 64 -> {len(CLASSES)} CPU Spectral Fallback MLP...")
    Xp_tr, Xp_va = X_phys[train_idx], X_phys[val_idx]
    cpu_mean = np.mean(Xp_tr, axis=0).astype(np.float32)
    cpu_std = np.maximum(np.std(Xp_tr, axis=0), 1e-4).astype(np.float32)
    Xp_tr_n = (Xp_tr - cpu_mean) / (cpu_std + 1e-5)
    Xp_va_n = (Xp_va - cpu_mean) / (cpu_std + 1e-5)
    cpu_w1, cpu_b1, cpu_w2, cpu_b2, cpu_acc = train_mlp(
        Xp_tr_n, y_tr, Xp_va_n, y_va, hidden_dim=64, num_classes=len(CLASSES), epochs=360, lr=4e-3
    )
    print(f"      Held-out CPU Fallback Validation Accuracy: {cpu_acc * 100.0:.2f}%")

    out_path = PROJECT_ROOT / "models" / "hailo_pictogram_head_weights.npz"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.stem + ".tmp.npz")
    np.savez(
        str(tmp_path),
        classes=np.array(CLASSES),
        bn_mean=bn_mean,
        bn_var=bn_var,
        bn_weight=bn_weight,
        bn_bias=bn_bias,
        bn_eps=np.float32(bn_eps),
        w1=w1,
        b1=b1,
        w2=w2,
        b2=b2,
        cpu_mean=cpu_mean,
        cpu_std=cpu_std,
        cpu_w1=cpu_w1,
        cpu_b1=cpu_b1,
        cpu_w2=cpu_w2,
        cpu_b2=cpu_b2,
        val_accuracy=np.float32(val_acc),
        cpu_val_accuracy=np.float32(cpu_acc),
    )
    os.replace(str(tmp_path), str(out_path))
    print(f"[4/4] Saved calibrated weights to {out_path} in {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
