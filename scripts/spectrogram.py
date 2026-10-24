"""
Spectrogram rendering for suspected upconverts.

Produces a PNG with the spectrogram of a whole track (left) and its average
spectrum (right), with the detected cutoff marked — enough to judge by eye
whether a "lossless" file was made from an MP3.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Optional

# The container runs as a user without a writable home directory
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import numpy as np
import soundfile as sf
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

_NFFT = 4096
_COLUMNS = 1200        # time resolution of the image
_BLOCK = 1 << 18       # frames decoded per read
_DB_RANGE = 120


def _spectrogram(file_path: Path) -> tuple[np.ndarray, int, float]:
    """
    Return (power[columns, bins], sample rate, duration in seconds).

    Reads the file sequentially in blocks, so memory stays low even for long
    hi-res tracks and broken seek tables do not matter.
    """
    window = np.hanning(_NFFT).astype(np.float32)
    with sf.SoundFile(str(file_path)) as snd:
        sr, total = snd.samplerate, snd.frames
        positions = np.linspace(0, max(total - _NFFT, 0), _COLUMNS).astype(np.int64)
        power = np.zeros((_COLUMNS, _NFFT // 2 + 1), dtype=np.float32)

        buf = np.zeros(0, dtype=np.float32)
        buf_start = 0
        col = 0
        for block in snd.blocks(blocksize=_BLOCK, dtype="float32", always_2d=True):
            buf = np.concatenate([buf, block.mean(axis=1)])
            while col < _COLUMNS and positions[col] + _NFFT <= buf_start + len(buf):
                off = positions[col] - buf_start
                spec = np.fft.rfft(buf[off:off + _NFFT] * window)
                power[col] = np.abs(spec) ** 2
                col += 1
            keep_from = min(positions[col] - buf_start, len(buf)) if col < _COLUMNS else len(buf)
            buf = buf[keep_from:]
            buf_start += keep_from
            if col >= _COLUMNS:
                break

        if col < _COLUMNS and len(buf):  # track shorter than one FFT window
            seg = np.zeros(_NFFT, dtype=np.float32)
            seg[:len(buf[:_NFFT])] = buf[:_NFFT]
            power[col:] = np.abs(np.fft.rfft(seg * window)) ** 2

    return power, sr, total / sr if sr else 0.0


def render_spectrogram(file_path: Path, cutoff_hz: Optional[float] = None) -> bytes:
    """Render the spectrogram of *file_path* as PNG bytes."""
    power, sr, duration = _spectrogram(file_path)
    ref = max(float(power.max()), 1e-20)
    db = 10 * np.log10(power / ref + 1e-20)
    nyquist_khz = sr / 2000
    avg_power = np.convolve(power.mean(axis=0), np.ones(9) / 9, mode="same")
    avg_db = 10 * np.log10(avg_power / ref + 1e-20)
    freqs_khz = np.linspace(0, nyquist_khz, len(avg_db))

    fig = Figure(figsize=(12, 6), dpi=100, facecolor="#111")
    grid = fig.add_gridspec(1, 3, width_ratios=[6, 1.3, 0.15], wspace=0.06)
    ax = fig.add_subplot(grid[0])
    ax_avg = fig.add_subplot(grid[1], sharey=ax)
    ax_cbar = fig.add_subplot(grid[2])

    image = ax.imshow(
        db.T, origin="lower", aspect="auto", cmap="magma",
        extent=(0, duration, 0, nyquist_khz), vmin=-_DB_RANGE, vmax=0,
    )
    ax_avg.plot(avg_db, freqs_khz, color="#f5a623", linewidth=1)
    ax_avg.set_xlim(-_DB_RANGE, 0)

    if cutoff_hz:
        for a in (ax, ax_avg):
            a.axhline(cutoff_hz / 1000, color="#4fc3f7", linestyle="--", linewidth=1)
        ax.text(duration * 0.01, cutoff_hz / 1000 + nyquist_khz * 0.01,
                f"срез {cutoff_hz / 1000:.1f} kHz", color="#4fc3f7", fontsize=10)

    try:
        info = sf.info(str(file_path))
        fmt = f"{info.samplerate / 1000:g} kHz, {info.subtype_info}"
    except Exception:  # noqa: BLE001
        fmt = f"{sr / 1000:g} kHz"
    ax.set_title(f"{file_path.name}\n{fmt}", color="white", fontsize=11, loc="left")

    ax.set_xlabel("Время", color="#ccc")
    ax.set_ylabel("kHz", color="#ccc")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda s, _: f"{int(s) // 60}:{int(s) % 60:02d}"))
    ax_avg.set_xlabel("Средний, dB", color="#ccc")
    ax_avg.tick_params(labelleft=False)
    ax_avg.grid(True, color="#333", linewidth=0.5)
    for a in (ax, ax_avg, ax_cbar):
        a.set_facecolor("black")
        a.tick_params(colors="#ccc", labelsize=8)
        for spine in a.spines.values():
            spine.set_color("#555")
    cbar = fig.colorbar(image, cax=ax_cbar)
    cbar.set_label("dB", color="#ccc")

    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
    return out.getvalue()
