"""Filesystem helpers and the change journal."""

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from . import config
from .names import artist_folder_name

log = logging.getLogger(__name__)

# One folder per run for everything moved aside, not one per second
_RUN_STAMP = time.strftime("%Y%m%d-%H%M%S")


def get_audio_files(directory: Path) -> list[Path]:
    """Sorted audio files directly inside *directory*."""
    return sorted(
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
    )


def album_audio_files(album_dir: Path) -> list[Path]:
    """Audio files of an album, including disc sub-folders (CD1, CD2…)."""
    files = get_audio_files(album_dir)
    for sub in sorted(album_dir.iterdir()):
        if sub.is_dir() and not sub.name.startswith("."):
            files.extend(get_audio_files(sub))
    return files


def has_audio_files(directory: Path) -> bool:
    return any(f.suffix.lower() in config.AUDIO_EXTENSIONS for f in directory.iterdir() if f.is_file())


# Disc sub-folders of a download or library album: "CD1", "Disc 2", "CD 1 - Experience"
_DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk|диск)[\s._-]*(\d+)(?:\D.*)?$", re.IGNORECASE)


def disc_dir_number(directory: Path) -> Optional[int]:
    """Disc number of a disc sub-folder ("CD2" → 2), None for any other folder."""
    m = _DISC_DIR_RE.match(directory.name)
    return int(m.group(1)) if m else None


def is_album_dir(directory: Path) -> bool:
    """Audio directly inside, or in disc sub-folders (CD1, CD2…)."""
    return has_audio_files(directory) or any(
        sub.is_dir() and disc_dir_number(sub) is not None and has_audio_files(sub)
        for sub in directory.iterdir()
    )


def library_album_dirs() -> list[Path]:
    """
    All "<artist>/<album>" folders in the library that contain audio; hidden
    and system folders ("$RECYCLE.BIN" of a Windows share) are not artists.
    """
    return sorted(
        album
        for artist_dir in config.MUSIC_PATH.iterdir()
        if artist_dir.is_dir() and not artist_dir.name.startswith((".", "$"))
        for album in artist_dir.iterdir()
        if album.is_dir() and not album.name.startswith(".") and album_audio_files(album)
    )


def safe_rename(src: Path, dst: Path) -> Path:
    """Rename *src* → *dst*, falling back to copy+delete on PermissionError."""
    if src == dst:
        return src
    try:
        src.rename(dst)
    except PermissionError:
        shutil.copy2(src, dst)
        src.unlink()
    return dst


def find_dir_case_insensitive(parent: Path, name: str) -> Optional[Path]:
    exact = parent / name
    if exact.is_dir():
        return exact
    for entry in parent.iterdir():
        if entry.is_dir() and entry.name.lower() == name.lower():
            return entry
    return None


def find_or_create_artist_dir(artist: str) -> Path:
    """
    Library folder for an artist tag value.  An existing folder is matched
    case-insensitively, so "Arms and Sleepers" never gets a twin
    "Arms And Sleepers"; otherwise the (not yet created) new path is returned.
    """
    folder = artist_folder_name(artist)
    if config.MUSIC_PATH.exists():
        existing = find_dir_case_insensitive(config.MUSIC_PATH, folder)
        if existing:
            return existing
    return config.MUSIC_PATH / folder


def remove_non_music_files(album_dir: Path) -> None:
    """
    Delete everything in *album_dir* except audio files and the cover; disc
    sub-folders (CD1, CD2…) are cleaned the same way, other folders removed.
    """
    for item in list(album_dir.iterdir()):
        if item.is_file():
            if item.suffix.lower() in config.AUDIO_EXTENSIONS or item.name == config.COVER_NAME:
                continue
            item.unlink()
            log.info("  🗑️  Removed: %s", item.name)
        elif item.is_dir() and disc_dir_number(item) is not None and has_audio_files(item):
            remove_non_music_files(item)
        elif item.is_dir():
            shutil.rmtree(item)
            log.info("  🗑️  Removed directory: %s/", item.name)


def move_aside(path: Path) -> Path:
    """
    Move *path* out of the library into the hidden state folder instead of
    deleting it, so an overwritten album can still be recovered.
    """
    target = config.state_dir() / "replaced" / _RUN_STAMP / path.relative_to(config.MUSIC_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    return target


def normalize_permissions(root: Path) -> None:
    """Give *root* and everything below it the library's standard permissions."""
    paths = [root, *root.rglob("*")] if root.is_dir() else [root]
    for path in paths:
        mode = config.DIR_MODE if path.is_dir() else config.FILE_MODE
        if path.stat().st_mode & 0o777 != mode:
            os.chmod(path, mode)


def journal(entry: dict) -> None:
    """Append a change record (with the old values) to the library's journal."""
    path = config.state_dir() / "fixes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**entry, "applied": time.strftime("%Y-%m-%d %H:%M:%S")}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
