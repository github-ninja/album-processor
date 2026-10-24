"""
Detection of lossy→lossless upconverts by spectral analysis.

Lossy encoders (MP3, AAC, Vorbis) low-pass the signal before encoding:
LAME uses ~16 kHz at 128 kbps, ~19 kHz at 192 kbps and ~20 kHz at 320 kbps.
Converting such a file back to FLAC cannot restore the removed band, so the
spectrum shows a brick-wall cutoff well below Nyquist.  Genuine CD-quality
audio usually extends to ~21-22 kHz, and natural roll-off is gradual.

(Fake hi-res is not checked: everything is converted to 16/44.1 anyway.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config
from .files import album_audio_files

try:
    import numpy as np
    import soundfile as sf
    _SPECTRAL_AVAILABLE = True
except ImportError:
    _SPECTRAL_AVAILABLE = False

log = logging.getLogger(__name__)

_SPEC_NFFT = 8192
_SPEC_SEGMENTS = 6            # excerpts analysed per track
_SPEC_SEGMENT_SECONDS = 10
_SPEC_BAND_HZ = 100           # spectrum is averaged into bands of this width
_CUTOFF_MIN_DROP_DB = 25      # level difference across the cutoff edge
_CUTOFF_EDGE_HZ = 500         # width of the windows compared across the edge
_LOSSY_CUTOFF_HZ = 20_500     # cutoff below this → lossy source
_LOSSY_SURE_CUTOFF_HZ = 19_600  # cutoff below this → lossy source, high confidence
_SILENCE_DB = -90             # tracks quieter than this are not analysed


@dataclass
class SpectrumVerdict:
    file: str
    cutoff_hz: Optional[float]  # None when no brick-wall cutoff was found
    verdict: str                # "ok" | "lossy" | "unknown"


@dataclass
class UpconvertReport:
    verdict: str                # "ok" | "lossy" | "unknown"
    confident: bool
    cutoff_hz: Optional[float]
    flagged: int
    analysed: int
    tracks: list[SpectrumVerdict] = field(default_factory=list)


def _average_spectrum(file_path: Path) -> tuple[np.ndarray, int]:
    """
    Return the mean power spectrum (linear) of several excerpts spread across
    the track, mixed down to mono, plus the sample rate.
    """
    window = np.hanning(_SPEC_NFFT).astype(np.float32)
    hop = _SPEC_NFFT // 2
    power = np.zeros(_SPEC_NFFT // 2 + 1, dtype=np.float64)
    frames_used = 0

    with sf.SoundFile(str(file_path)) as snd:
        sr = snd.samplerate
        total = snd.frames
        seg_len = min(total, sr * _SPEC_SEGMENT_SECONDS)
        if total <= seg_len * _SPEC_SEGMENTS:
            starts = range(0, max(total - _SPEC_NFFT, 1), seg_len)
        else:
            # Skip the first/last 5% (intros, fades, silence)
            lo, hi = int(total * 0.05), int(total * 0.95) - seg_len
            starts = np.linspace(lo, hi, _SPEC_SEGMENTS).astype(int)

        for start in starts:
            try:
                snd.seek(int(start))
                block = snd.read(seg_len, dtype="float32", always_2d=True)
            except Exception:  # noqa: BLE001 — broken header/seektable; use what we have
                break
            mono = block.mean(axis=1)
            if len(mono) < _SPEC_NFFT:
                continue
            n = (len(mono) - _SPEC_NFFT) // hop + 1
            idx = np.arange(_SPEC_NFFT)[None, :] + hop * np.arange(n)[:, None]
            spec = np.fft.rfft(mono[idx] * window, axis=1)
            power += (np.abs(spec) ** 2).sum(axis=0)
            frames_used += n

    if frames_used:
        power /= frames_used
    return power, sr


def _band_levels_db(power: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """Collapse a linear power spectrum into fixed-width bands, in dB."""
    freqs = np.fft.rfftfreq(_SPEC_NFFT, 1 / sr)
    n_bands = int(freqs[-1] // _SPEC_BAND_HZ)
    band_idx = np.minimum((freqs // _SPEC_BAND_HZ).astype(int), n_bands - 1)
    sums = np.bincount(band_idx, weights=power, minlength=n_bands)
    counts = np.bincount(band_idx, minlength=n_bands)
    levels = 10 * np.log10(sums / np.maximum(counts, 1) + 1e-20)
    centres = (np.arange(n_bands) + 0.5) * _SPEC_BAND_HZ
    return centres, levels


def _find_cutoff(centres: np.ndarray, levels: np.ndarray, sr: int) -> Optional[float]:
    """
    Locate a brick-wall cutoff: the frequency with the largest level drop
    between the windows just below and just above it, provided the band above
    stays quiet all the way to Nyquist.  Returns None for a natural spectrum.
    """
    edge = _CUTOFF_EDGE_HZ // _SPEC_BAND_HZ
    top = len(levels) - 2  # ignore the anti-alias region right at Nyquist
    lo = int(10_000 // _SPEC_BAND_HZ)

    best_drop, best_i = 0.0, None
    for i in range(max(lo, edge), top - edge):
        below = levels[i - edge:i].mean()
        above = levels[i + 1:i + 1 + edge].mean()
        drop = below - above
        if drop > best_drop:
            best_drop, best_i = drop, i

    if best_i is None or best_drop < _CUTOFF_MIN_DROP_DB:
        return None
    below = levels[best_i - edge:best_i].mean()
    # Real cutoffs stay quiet above the edge (resampler images may leave small
    # bumps); a dip followed by recovery to full level does not count
    above = levels[best_i + 1:top]
    if above.mean() > below - _CUTOFF_MIN_DROP_DB or above.max() > below - 6:
        return None
    return float(centres[best_i])


def analyse_track_spectrum(file_path: Path) -> SpectrumVerdict:
    """Classify a single lossless file as ok / lossy / unknown."""
    try:
        power, sr = _average_spectrum(file_path)
    except Exception as exc:  # noqa: BLE001
        log.debug("  Could not decode %s: %s", file_path.name, exc)
        return SpectrumVerdict(file_path.name, None, "unknown")

    centres, levels = _band_levels_db(power, sr)
    reference = np.median(levels[(centres >= 1_000) & (centres <= 5_000)])
    if reference < _SILENCE_DB:
        return SpectrumVerdict(file_path.name, None, "unknown")

    cutoff = _find_cutoff(centres, levels, sr)
    if cutoff is not None and cutoff < _LOSSY_CUTOFF_HZ:
        return SpectrumVerdict(file_path.name, cutoff, "lossy")

    # No cutoff, or one above 20.5 kHz (e.g. a CD-mastering filter)
    return SpectrumVerdict(file_path.name, cutoff, "ok")


def check_upconvert(album_path: Path) -> Optional[UpconvertReport]:
    """
    Run the spectral check on every lossless track in *album_path* and return
    an album-level verdict.  Returns None if there is nothing to analyse or
    the required libraries are missing.
    """
    files = [f for f in album_audio_files(album_path) if f.suffix.lower() in config.LOSSLESS_EXTENSIONS]
    if not files:
        return None
    if not _SPECTRAL_AVAILABLE:
        log.warning("⚠️  numpy/soundfile not installed — upconvert check skipped.")
        return None

    log.info("🔬 Checking %d lossless track(s) for upconvert…", len(files))
    tracks = [analyse_track_spectrum(f) for f in files]
    for track, f in zip(tracks, files):
        track.file = str(f.relative_to(album_path))  # disc sub-folders included
    for t in tracks:
        cutoff = f"{t.cutoff_hz / 1000:.1f} kHz" if t.cutoff_hz else "none"
        log.debug("  %s: cutoff %s → %s", t.file, cutoff, t.verdict)

    analysed = [t for t in tracks if t.verdict != "unknown"]
    if not analysed:
        return UpconvertReport("unknown", False, None, 0, 0, tracks)

    # Album verdict: lossy when at least half of the tracks are
    flagged = [t for t in analysed if t.verdict == "lossy"]
    if len(flagged) * 2 >= len(analysed):
        cutoff = float(np.median([t.cutoff_hz for t in flagged]))
        return UpconvertReport("lossy", cutoff < _LOSSY_SURE_CUTOFF_HZ, cutoff,
                               len(flagged), len(analysed), tracks)

    flagged = sum(t.verdict != "ok" for t in analysed)
    return UpconvertReport("ok", True, None, flagged, len(analysed), tracks)


def guess_lossy_source(cutoff_hz: float) -> str:
    """Rough guess of the original MP3 bitrate from the LAME low-pass frequency."""
    if cutoff_hz < 15_000:
        return "≤96 kbps"
    if cutoff_hz < 17_200:
        return "~128 kbps"
    if cutoff_hz < 18_000:
        return "~160 kbps"
    if cutoff_hz < 19_300:
        return "~192 kbps"
    if cutoff_hz < 19_900:
        return "~256 kbps / V0"
    return "~320 kbps"


def log_upconvert_report(report: UpconvertReport) -> None:
    if report.verdict == "lossy":
        level = "Вероятно" if report.confident else "Возможно"
        log.warning(
            "  🚩 %s апконверт из MP3/AAC: срез на %.1f kHz (%s) в %d/%d трек(ах).",
            level, report.cutoff_hz / 1000, guess_lossy_source(report.cutoff_hz),
            report.flagged, report.analysed,
        )
    elif report.verdict == "ok":
        if not report.flagged:
            log.info("  ✅ Спектр в порядке — признаков апконверта нет.")
            return
        log.info("  ✅ Альбом в целом в порядке, но есть подозрительные треки (%d/%d):",
                 report.flagged, report.analysed)
        for t in report.tracks:
            if t.verdict == "lossy":
                log.info("    • %s — срез %.1f kHz", t.file, t.cutoff_hz / 1000)
    else:
        log.info("  ℹ️  Не удалось проанализировать спектр.")


def describe(report: UpconvertReport) -> str:
    """Short description of a flagged album for summaries and the bot."""
    return f"MP3/AAC, срез {report.cutoff_hz / 1000:.1f} kHz"


def deferred_entry(album_path: Path, report: UpconvertReport) -> dict:
    """Record of an album left for the user to decide on (read by the Telegram bot)."""
    return {
        "kind": "upconvert",
        "path": str(album_path),
        "verdict": report.verdict,
        "confident": report.confident,
        "summary": describe(report),
        "flagged": report.flagged,
        "analysed": report.analysed,
        "tracks": [
            {"path": str(album_path / t.file), "cutoff_hz": t.cutoff_hz}
            for t in report.tracks if t.verdict == report.verdict
        ],
    }
