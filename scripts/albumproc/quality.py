"""
Mastering passport: dynamics and clipping of every album, measured once and
kept in the library's state folder (quality.json) — never in the tags.

  DR        dynamic range as the TT DR Meter computes it (dr.loudness-war.info):
            per channel, 3 s blocks; the second-highest block peak against the
            RMS of the loudest 20 % of blocks.  12+ open, 7–9 dense, ≤ 6 crushed.
  clips     flat tops: runs of ≥ 3 identical samples within 1 dB of the track's
            peak — what hard clipping leaves; reported per minute.
  loudness  and true peak come from the ReplayGain tags.

A passport is tied to the audio, not to file names: tracks are recognised by
the MD5 of their decoded audio that FLAC stores in STREAMINFO, so renames and
tag edits keep it and a new edition or a conversion invalidates it.
"""

import json
import logging
import math
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from . import config
from .files import album_audio_files, library_album_dirs

log = logging.getLogger(__name__)

_BLOCK_SECONDS = 3
_CLIP_WINDOW_DB = 1.0
# 🔴: crushed (DR ≤ 5), or heavily clipped without the dynamics to carry it;
# clipped transients on an open master (DR 11, peaks shaved) stay 🟡
_RED = {"dr": 5, "clips_per_minute": 100, "clipped_dr": 8}
_YELLOW = {"dr": 8, "clips_per_minute": 5}


def store_path() -> Path:
    return config.state_dir() / "quality.json"


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

def _flat_top_levels(channel: np.ndarray) -> np.ndarray:
    """Levels of the runs of ≥ 3 identical samples in *channel*."""
    same = np.diff(np.r_[0, (channel[1:] == channel[:-1]).astype(np.int8), 0])
    starts, ends = np.flatnonzero(same == 1), np.flatnonzero(same == -1)
    return np.abs(channel[starts[ends - starts >= 2]])


def audio_id(path: Path) -> str:
    """Identity of a file's audio: the decoded-audio MD5 from STREAMINFO (or length and size without one)."""
    info = FLAC(path).info
    return f"{info.md5_signature:032x}" if info.md5_signature else f"{info.total_samples}:{path.stat().st_size}"


def measure_track(path: Path) -> dict:
    """DR, flat tops and length of one FLAC file, read in blocks (DJ mixes fit in memory)."""
    full_scale = 2 ** 31
    rms, peaks, levels = [], [], []
    frames = 0
    peak = 0
    with sf.SoundFile(path) as audio:
        block = _BLOCK_SECONDS * audio.samplerate
        for chunk in audio.blocks(blocksize=block, dtype="int32", always_2d=True):
            frames += len(chunk)
            values = chunk.astype(np.int64)
            peak = max(peak, int(np.abs(values).max()))
            for channel in values.T:
                levels.append(_flat_top_levels(channel))
            if len(chunk) == block or not rms:  # TT DR skips the last partial block
                scaled = chunk / full_scale
                rms.append(np.sqrt(2 * np.mean(scaled ** 2, axis=0)))
                peaks.append(np.abs(scaled).max(axis=0))
        rate = audio.samplerate

    rms_blocks, peak_blocks = np.array(rms), np.array(peaks)
    channel_dr = []
    for ch in range(rms_blocks.shape[1]):
        loudest = np.sort(rms_blocks[:, ch])[-max(1, len(rms_blocks) // 5):]
        ch_peaks = np.sort(peak_blocks[:, ch])
        second = ch_peaks[-2] if len(ch_peaks) > 1 else ch_peaks[-1]
        loud_rms = math.sqrt(float(np.mean(loudest ** 2)))
        if second > 0 and loud_rms > 0:
            channel_dr.append(20 * math.log10(second / loud_rms))
    levels = np.concatenate(levels) if levels else np.array([])
    clips = int((levels >= peak * 10 ** (-_CLIP_WINDOW_DB / 20)).sum()) if peak else 0

    tags = FLAC(path)
    gain = (tags.get("replaygain_track_gain") or [""])[0].split()
    true_peak = (tags.get("replaygain_track_peak") or [""])[0]
    return {
        "file": path.name,
        "id": audio_id(path),
        "seconds": round(frames / rate, 1),
        "dr": round(float(np.mean(channel_dr)), 1) if channel_dr else None,
        "clips": clips,
        "lufs": round(-18 - float(gain[0]), 1) if gain else None,
        "true_peak": float(true_peak) if true_peak else None,
    }


def measure_album(album_dir: Path) -> dict:
    """Passport of an album: its tracks measured in parallel."""
    files = [f for f in album_audio_files(album_dir) if f.suffix.lower() == ".flac"]
    if not files:
        return {"tracks": []}
    with ProcessPoolExecutor(max_workers=min(len(files), os.cpu_count() or 1),
                             mp_context=multiprocessing.get_context("forkserver")) as pool:
        tracks = list(pool.map(measure_track, files))
    album_gain = (FLAC(files[0]).get("replaygain_album_gain") or [""])[0].split()
    return {
        "measured": time.strftime("%Y-%m-%d"),
        "lufs": round(-18 - float(album_gain[0]), 1) if album_gain else None,
        "tracks": tracks,
    }


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def album_dr(passport: dict) -> Optional[int]:
    """Album DR: the rounded mean of the tracks', as the DR database lists it."""
    values = [t["dr"] for t in passport["tracks"] if t["dr"] is not None]
    return round(sum(values) / len(values)) if values else None


def clips_per_minute(passport: dict) -> float:
    minutes = sum(t["seconds"] for t in passport["tracks"]) / 60
    return sum(t["clips"] for t in passport["tracks"]) / minutes if minutes else 0.0


def grade(passport: dict) -> str:
    dr, rate = album_dr(passport), clips_per_minute(passport)
    if dr is not None and (dr <= _RED["dr"] or (rate >= _RED["clips_per_minute"] and dr <= _RED["clipped_dr"])):
        return "🔴"
    if (dr is not None and dr <= _YELLOW["dr"]) or rate >= _YELLOW["clips_per_minute"]:
        return "🟡"
    return "🟢"


def _db(value: Optional[float]) -> str:
    return f"{20 * math.log10(value):+.1f} dB" if value else "—"


def summary(passport: dict) -> str:
    """One line: "🟢 DR 11 · −14.2 LUFS · true peak −0.3 dB · клиппинга нет"."""
    if not passport["tracks"]:
        return "—"
    dr = album_dr(passport)
    clips = sum(t["clips"] for t in passport["tracks"])
    peak = max((t["true_peak"] or 0) for t in passport["tracks"])
    parts = [f"{grade(passport)} DR {dr if dr is not None else '—'}"]
    if passport.get("lufs") is not None:
        parts.append(f"{passport['lufs']:.1f} LUFS")
    parts.append(f"true peak {_db(peak)}")
    parts.append(f"полок: {clips} ({clips_per_minute(passport):.0f}/мин)" if clips else "клиппинга нет")
    return " · ".join(parts)


def worst_track(passport: dict) -> Optional[dict]:
    clipped = [t for t in passport["tracks"] if t["clips"]]
    return max(clipped, key=lambda t: t["clips"] / max(t["seconds"], 1)) if clipped else None


def describe(passport: dict) -> list[str]:
    """Summary line plus a hint for a crushed master."""
    lines = [f"🎚 Мастеринг: {summary(passport)}"]
    if grade(passport) == "🔴":
        worst = worst_track(passport)
        lines.append("   ⚠️ громкий мастер" + (f", хуже всех «{Path(worst['file']).stem}»" if worst else "")
                     + " — возможно, есть издание получше")
    return lines


# ---------------------------------------------------------------------------
# Store: music/.album_processor/quality.json
# ---------------------------------------------------------------------------

def _load() -> dict[str, dict]:
    try:
        return json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(store: dict[str, dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _audio_key(album_dir: Path) -> list[str]:
    return sorted(audio_id(f) for f in album_audio_files(album_dir) if f.suffix.lower() == ".flac")


def _rel(album_dir: Path) -> str:
    return str(album_dir.relative_to(config.MUSIC_PATH))


def passport(album_dir: Path, store: Optional[dict] = None) -> dict:
    """
    The album's passport: from the store when its audio is unchanged (even if
    it was renamed or moved), else measured.  Library albums are (re)stored.
    """
    own_store = store is None
    store = _load() if own_store else store
    key = _audio_key(album_dir)
    found = next((p for p in store.values() if p.get("audio") == key), None)
    if found is None:
        found = {**measure_album(album_dir), "audio": key}
    if config.MUSIC_PATH in album_dir.parents:
        store = {path: p for path, p in store.items() if p.get("audio") != key}
        store[_rel(album_dir)] = found
        if own_store:
            _save(store)
    return found


def remember(album_dir: Path, measured: dict) -> None:
    """Store a passport measured in downloads under the album's library path."""
    store = {path: p for path, p in _load().items() if p.get("audio") != measured.get("audio")}
    store[_rel(album_dir)] = measured
    _save(store)


# ---------------------------------------------------------------------------
# /quality report
# ---------------------------------------------------------------------------

def library_report(query: str = "", limit: int = 30) -> list[str]:
    """
    Text for /quality: the library's most crushed albums first, or with
    *query* the passports of the matching albums track by track.  Albums
    without a passport are measured; passports of albums gone are dropped.
    """
    old = _load()
    store: dict[str, dict] = {}
    albums = library_album_dirs()
    missing = 0
    for album_dir in albums:
        rel = _rel(album_dir)
        if rel not in old or old[rel].get("audio") != _audio_key(album_dir):
            missing += 1
            log.info("📏 %s", rel)
        store[rel] = passport(album_dir, {**old, **store})
    _save(store)

    if query:
        matching = [rel for rel in sorted(store) if query.casefold() in rel.casefold()]
        if not matching:
            return [f"Не нашёл альбомов по запросу «{query}»."]
        lines = []
        for rel in matching[:5]:
            p = store[rel]
            lines += ["", rel.replace("/", " — ", 1), summary(p)]
            for t in p["tracks"]:
                rate = t["clips"] / max(t["seconds"] / 60, 1 / 60)
                clips = f"{t['clips']} пол. ({rate:.0f}/мин)" if t["clips"] else "—"
                lines.append(f"  DR {t['dr'] if t['dr'] is not None else '—':>4} · {_db(t['true_peak']):>8} · {clips:<18} {Path(t['file']).stem}")
        if len(matching) > 5:
            lines.append(f"\n…и ещё {len(matching) - 5}; уточните запрос.")
        return lines[1:]

    grades = {g: sum(grade(p) == g for p in store.values()) for g in ("🟢", "🟡", "🔴")}
    order = {"🔴": 0, "🟡": 1, "🟢": 2}
    ranked = sorted(store.items(), key=lambda item: (order[grade(item[1])], -clips_per_minute(item[1]),
                                                     album_dr(item[1]) or 0))
    lines = [f"Паспорт мастеринга: {len(store)} альб. — 🟢 {grades['🟢']} · 🟡 {grades['🟡']} · 🔴 {grades['🔴']}"
             + (f" (измерено новых: {missing})" if missing else ""), ""]
    flagged = [(rel, p) for rel, p in ranked if grade(p) != "🟢"]
    for rel, p in flagged[:limit]:
        lines.append(f"{grade(p)} DR {album_dr(p)} · {clips_per_minute(p):.0f} пол./мин · {rel.replace('/', ' — ', 1)}")
    if len(flagged) > limit:
        rest = flagged[limit:]
        lines.append(f"…и ещё 🔴 {sum(grade(p) == '🔴' for _, p in rest)} · 🟡 {sum(grade(p) == '🟡' for _, p in rest)}")
    lines.append("\nПодробно по альбому: /quality <часть названия>")
    return lines
