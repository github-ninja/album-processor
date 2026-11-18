"""Filesystem helpers and the change journal."""

import json
import logging
import os
import re
import shutil
import time
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from . import config
from .names import artist_folder_name

log = logging.getLogger(__name__)

# One folder per run for everything moved aside, not one per second
_RUN_STAMP = time.strftime("%Y%m%d-%H%M%S")


@contextmanager
def processor_lock():
    """One CLI writer per library, including commands started outside Telegram."""
    import fcntl

    if config.state_dir().is_symlink():
        raise ValueError("state directory must be inside the library")
    config.state_dir().mkdir(parents=True, exist_ok=True)
    with (config.state_dir() / "processor.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Обработчик уже работает с этой медиатекой; дождитесь завершения.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
        if sub.is_dir() and not sub.is_symlink() and disc_dir_number(sub) is not None:
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
    if not config.MUSIC_PATH.is_dir():
        return []
    return sorted(
        album
        for artist_dir in config.MUSIC_PATH.iterdir()
        if artist_dir.is_dir() and not artist_dir.is_symlink() and not artist_dir.name.startswith((".", "$"))
        for album in artist_dir.iterdir()
        if album.is_dir() and not album.is_symlink() and not album.name.startswith(".") and album_audio_files(album)
    )


def safe_rename(src: Path, dst: Path) -> Path:
    """Rename on the same filesystem without replacing an existing path."""
    if src == dst:
        return src
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"destination already exists: {dst}")
    src.rename(dst)
    return dst


def rename_files(pairs: list[tuple[Path, Path]]) -> None:
    """Rename tracks without overwriting, including swaps; roll back failed moves."""
    pairs = [(src, dst) for src, dst in pairs if src != dst]
    sources = {src for src, _ in pairs}
    targets = [dst for _, dst in pairs]
    if len(sources) != len(pairs) or len(set(targets)) != len(targets):
        raise ValueError("duplicate source or destination in track renames")
    for src, dst in pairs:
        if not src.is_file() or src.is_symlink():
            raise ValueError(f"not a regular track: {src}")
        if (dst.exists() or dst.is_symlink()) and dst not in sources:
            raise FileExistsError(f"destination already exists: {dst}")
    staged, published = [], []
    try:
        for src, dst in pairs:
            tmp = src.with_name(f".rename-{uuid.uuid4().hex}{src.suffix}")
            safe_rename(src, tmp)
            staged.append((src, tmp, dst))
        for src, tmp, dst in staged:
            dst.parent.mkdir(parents=True, exist_ok=True)
            safe_rename(tmp, dst)
            published.append((src, tmp, dst))
    except Exception:
        for src, tmp, dst in reversed(published):
            safe_rename(dst, tmp)
        for src, tmp, dst in reversed(staged):
            safe_rename(tmp, src)
        raise


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
    if not folder or folder.startswith((".", "$")):
        raise ValueError("artist does not have a usable library folder name")
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
    # An unrecognised folder may contain a second album: never discard its audio.
    for item in album_dir.iterdir():
        if item.is_symlink():
            raise ValueError(f"symbolic link in album: {item}")
        if item.is_dir() and disc_dir_number(item) is None and any(
                f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS for f in item.rglob("*")):
            raise ValueError(f"audio in an unsupported subfolder: {item}")
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


def archive_path(path: Path) -> Path:
    """Reserve a name under the library lock before moving a version aside."""
    target = config.state_dir() / "replaced" / _RUN_STAMP / path.relative_to(config.MUSIC_PATH)
    if target.exists() or target.is_symlink():
        target = target.with_name(f"{target.name}-{uuid.uuid4().hex}")
    if not target.parent.resolve().is_relative_to(config.MUSIC_PATH.resolve()):
        raise ValueError("archive path escapes the library")
    return target


def move_aside(path: Path) -> Path:
    target = archive_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return safe_rename(path, target)


def restore_backups(backups: list[list[str]]) -> None:
    for original, archive in reversed(backups):
        original, archive = Path(original), Path(archive)
        if archive.exists() or archive.is_symlink():
            safe_rename(archive, original)


def publish_album(work: Path, destination: Path, backups: list[list[str]]) -> None:
    """Publish a prepared directory; the caller saves the move list first."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    moved = []
    try:
        for original, archive in backups:
            archive = Path(archive)
            archive.parent.mkdir(parents=True, exist_ok=True)
            safe_rename(Path(original), archive)
            moved.append([original, str(archive)])
        safe_rename(work, destination)
    except Exception:
        restore_backups(moved)
        raise


def normalize_permissions(root: Path) -> None:
    """Give *root* and everything below it the library's standard permissions."""
    paths = [root, *root.rglob("*")] if root.is_dir() else [root]
    for path in paths:
        mode = config.DIR_MODE if path.is_dir() else config.FILE_MODE
        if path.stat().st_mode & 0o777 != mode:
            os.chmod(path, mode)


def write_json(path: Path, payload: dict, *, sort_keys: bool = False) -> None:
    """Replace a state file atomically after writing its complete JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(payload, stream, ensure_ascii=False, indent=1, sort_keys=sort_keys)
            stream.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def journal(entry: dict) -> None:
    """Append a change record (with the old values) to the library's journal."""
    path = config.state_dir() / "fixes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**entry, "applied": time.strftime("%Y-%m-%d %H:%M:%S")}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
