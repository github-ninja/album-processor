"""
ReplayGain 2.0 tags, as foobar2000 writes them: REPLAYGAIN_TRACK_GAIN/PEAK
and REPLAYGAIN_ALBUM_GAIN/PEAK, gains relative to −18 LUFS (EBU R128).
The peaks are true peaks (4× oversampled), so a player's clipping
prevention also covers the overs between samples.

FFmpeg's ebur128 filter (bundled with PyAV) reports the loudness of each
400 ms block every 100 ms; the track and album loudness are gated from these
blocks as BS.1770 prescribes — the album from the blocks of all its tracks.
"""

import logging
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import av
import av.filter
import numpy as np
from mutagen.flac import FLAC

log = logging.getLogger(__name__)

REFERENCE_LUFS = -18.0
_ABSOLUTE_GATE = -70.0  # LUFS
_RELATIVE_GATE = -10.0  # LU below the absolute-gated loudness
FIELDS = ("replaygain_track_gain", "replaygain_track_peak", "replaygain_album_gain", "replaygain_album_peak")


@dataclass
class Measurement:
    blocks: np.ndarray  # momentary loudness (LUFS) of the 400 ms blocks, 100 ms apart
    peak: float         # true peak, linear


def measure(path: Path) -> Measurement:
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        graph = av.filter.Graph()
        source = graph.add_abuffer(template=stream)
        meter = graph.add("ebur128", "metadata=1:peak=true")
        sink = graph.add("abuffersink")
        source.link_to(meter)
        meter.link_to(sink)
        graph.configure()

        blocks: list[float] = []
        peak = 0.0

        def drain() -> None:
            nonlocal peak
            while True:
                try:
                    frame = graph.pull()
                except (av.error.BlockingIOError, av.error.EOFError):
                    return
                if frame.metadata:
                    blocks.append(float(frame.metadata["lavfi.r128.M"]))
                    peak = float(frame.metadata["lavfi.r128.true_peak"])

        for frame in container.decode(stream):
            graph.push(frame)
            drain()
        graph.push(None)
        drain()
    return Measurement(np.array(blocks), peak)


def integrated_loudness(blocks: np.ndarray) -> Optional[float]:
    """BS.1770 gated loudness of *blocks*; None for silence."""
    blocks = blocks[blocks > _ABSOLUTE_GATE]
    if not len(blocks):
        return None
    energy = 10 ** ((blocks + 0.691) / 10)
    threshold = -0.691 + 10 * np.log10(energy.mean()) + _RELATIVE_GATE
    return float(-0.691 + 10 * np.log10(energy[blocks > threshold].mean()))


def has_replaygain(files: list[Path]) -> bool:
    return all("replaygain_album_gain" in FLAC(f) for f in files if f.suffix.lower() == ".flac")


def tag_album(files: list[Path]) -> None:
    """Measure the album's FLAC files (in parallel) and write their ReplayGain tags."""
    files = [f for f in files if f.suffix.lower() == ".flac"]
    if not files:
        return
    # Not fork: a forked worker can inherit a lock held by another thread and hang
    with ProcessPoolExecutor(max_workers=min(len(files), os.cpu_count() or 1),
                             mp_context=multiprocessing.get_context("forkserver")) as pool:
        measured = list(pool.map(measure, files))

    album = integrated_loudness(np.concatenate([m.blocks for m in measured]))
    album_peak = max(m.peak for m in measured)
    for path, m in zip(files, measured):
        track = integrated_loudness(m.blocks)
        audio = FLAC(path)
        for field in FIELDS:
            audio.pop(field, None)
        if track is not None:
            audio["replaygain_track_gain"] = f"{REFERENCE_LUFS - track:.2f} dB"
            audio["replaygain_track_peak"] = f"{m.peak:.6f}"
        if album is not None:
            audio["replaygain_album_gain"] = f"{REFERENCE_LUFS - album:.2f} dB"
            audio["replaygain_album_peak"] = f"{album_peak:.6f}"
        audio.save()
    if album is not None:
        log.info("  🔊 ReplayGain: album %+.2f dB, true peak %.4f", REFERENCE_LUFS - album, album_peak)
