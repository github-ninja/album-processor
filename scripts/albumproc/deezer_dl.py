"""
Downloading albums through deezer-downloader (github.com/kmille/deezer-downloader).

Its web UI's HTTP endpoints are used: POST /search, POST /download, GET /queue.
They are not a documented API — if an update of deezer-downloader changes
them, this module is the one place to adapt.

deezer-downloader writes albums to <its base>/albums/<Artist - Album>/; its
base must be visible to Anton too: DEEZER_DOWNLOADER_DIR (by default the
"deezer" folder in downloads).  /newalbum takes albums from there as well,
but not while deezer-downloader is still downloading.
"""

import ast
import html
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from . import config
from .files import library_album_dirs
from .names import album_identity, name_key, sanitize_path_component

log = logging.getLogger(__name__)

_TIMEOUT = 20
_POLL_SECONDS = 5
_DOWNLOAD_TIMEOUT = 30 * 60
_RETRY_PAUSES = [15, 45]  # seconds before the 2nd and 3rd attempt


class DeezerError(RuntimeError):
    """A problem worth telling the user about in plain words."""


def _url(path: str) -> str:
    return config.DEEZER_DOWNLOADER_URL.rstrip("/") + path


def _call(method: str, path: str, **kwargs):
    if not config.DEEZER_DOWNLOADER_URL:
        raise DeezerError("не задан DEEZER_DOWNLOADER_URL")
    try:
        resp = requests.request(method, _url(path), timeout=_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise DeezerError(f"deezer-downloader недоступен ({config.DEEZER_DOWNLOADER_URL}): {exc}") from exc
    if resp.status_code != 200:
        raise DeezerError(f"deezer-downloader: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise DeezerError("deezer-downloader вернул некорректный JSON") from exc


def _queue() -> list[dict]:
    return _call("GET", "/queue")


def busy() -> bool:
    """Whether downloading may be in progress; an unreachable queue is not idle."""
    try:
        return any(task.get("state") in ("waiting", "active") for task in _queue())
    except DeezerError:
        log.warning("Cannot check the download queue — albums are left untouched.")
        return True


def _owned() -> dict[str, set[str]]:
    """Library albums by artist folder (case aside): identities like "hybrid theory"."""
    owned: dict[str, set[str]] = {}
    for album_dir in library_album_dirs():
        owned.setdefault(name_key(album_dir.parent.name), set()).add(album_identity(album_dir.name))
    return owned


def search(query: str, limit: int = 8) -> list[dict]:
    """Albums on Deezer: [{"id", "artist", "title", "in_library"}]."""
    owned = _owned()
    results = []
    for item in _call("POST", "/search", json={"type": "album", "query": query})[:limit]:
        artist, title = item.get("artist", ""), item.get("album", "")
        identity = album_identity(f"0000 - {sanitize_path_component(title)}")  # as folders spell it
        results.append({"id": int(item["album_id"]), "artist": artist, "title": title,
                        "in_library": identity in owned.get(name_key(artist), set())})
    return results


def _downloaded_files(result: str) -> list[str]:
    """A finished task's result: an (HTML-escaped) list of the files written, relative to its base."""
    try:
        files = ast.literal_eval(html.unescape(result))
    except (ValueError, SyntaxError):
        return []
    return files if isinstance(files, list) and all(isinstance(f, str) for f in files) else []


def _existing_tracks(files: list[str]) -> list[str]:
    """Validate task paths and count distinct audio files that actually exist."""
    base = config.DEEZER_DOWNLOADER_DIR.resolve()
    albums = set()
    existing = set()
    for name in files:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 3 or relative.parts[0] != "albums":
            raise DeezerError("задача вернула путь вне папки альбомов")
        path = base / relative
        if not path.resolve().is_relative_to(base / "albums"):
            raise DeezerError("путь скачанного файла выходит за пределы папки альбомов")
        albums.add(relative.parts[1])
        if path.is_file() and path.suffix.lower() in config.AUDIO_EXTENSIONS:
            existing.add(name)
    if len(albums) > 1:
        raise DeezerError("задача вернула файлы нескольких альбомов")
    return sorted(existing)


def _track_count(album_id: int) -> Optional[int]:
    """The album's number of tracks per Deezer's public API, None if it can't tell."""
    try:
        response = requests.get(f"https://api.deezer.com/album/{album_id}", timeout=_TIMEOUT)
        response.raise_for_status()
        total = response.json().get("nb_tracks")
        return total if isinstance(total, int) and total > 0 else None
    except (requests.RequestException, ValueError):
        return None


def _run_task(album_id: int, deadline: float) -> list[str]:
    """Queue the album once and wait for the task: the files it has (downloaded or already there)."""
    if time.monotonic() >= deadline:
        raise DeezerError("истекло время ожидания скачивания")
    task_id = _call("POST", "/download", json={"type": "album", "music_id": album_id,
                                               "add_to_playlist": False, "create_zip": False})["task_id"]
    log.info("⬇️  Deezer album %s queued (task %s)", album_id, task_id)
    while time.monotonic() < deadline:
        task = next((t for t in _queue() if t.get("id") == task_id), None)
        if task is None:
            raise DeezerError("задача пропала из очереди deezer-downloader (перезапуск?)")
        if task.get("state") == "failed":
            raise DeezerError(f"deezer-downloader не смог скачать альбом: {html.unescape(task.get('exception', ''))}")
        if task.get("state") == "mission accomplished":
            # deezer-downloader skips tracks it cannot get and still calls the task done
            return _downloaded_files(task.get("result", ""))
        time.sleep(_POLL_SECONDS)
    raise DeezerError("скачивание не закончилось за 30 минут")


def download(album_id: int, timeout: float = _DOWNLOAD_TIMEOUT) -> Path:
    """
    Queue the album, wait until deezer-downloader has it, return its folder.
    Deezer refuses tracks now and then and gives them a minute later: a short
    album is queued again (deezer-downloader keeps the tracks it already has
    and fetches only the missing ones), up to _ATTEMPTS times in all.
    """
    deadline = time.monotonic() + timeout
    total = _track_count(album_id)
    for attempt, pause in enumerate(_RETRY_PAUSES + [None], start=1):
        files = _existing_tracks(_run_task(album_id, deadline))
        if files and (not total or len(files) >= total):
            break
        if pause is None:
            break
        log.info("🔁 Deezer gave %d of %s tracks — trying again in %d s (attempt %d of %d)",
                 len(files), total or "?", pause, attempt + 1, len(_RETRY_PAUSES) + 1)
        time.sleep(min(pause, max(0, deadline - time.monotonic())))

    if not files:
        raise DeezerError(f"Deezer не отдал ни одного трека ни с одной из {len(_RETRY_PAUSES) + 1} попыток — "
                          "альбом недоступен в вашей стране или в нужном качестве")
    album_dir = config.DEEZER_DOWNLOADER_DIR / "albums" / Path(files[0]).parts[1]
    if not album_dir.is_dir():
        raise DeezerError(f"скачано, но папка альбома не найдена: {album_dir} (проверьте DEEZER_DOWNLOADER_DIR)")
    if total and len(files) < total:
        # Out of the albums folder, or /newalbum would file it as if it were whole
        aside = config.DEEZER_DOWNLOADER_DIR / "incomplete" / album_dir.name
        aside.parent.mkdir(exist_ok=True)
        if aside.exists():
            aside = aside.with_name(f"{aside.name}-{uuid.uuid4().hex[:12]}")
        shutil.move(str(album_dir), str(aside))
        raise DeezerError(f"после {len(_RETRY_PAUSES) + 1} попыток скачано только {len(files)} из {total} треков — "
                          f"часть альбома недоступна на Deezer. Неполный альбом не обработан, он в "
                          f"{aside.relative_to(config.DOWNLOADS_PATH)}")
    stale = config.DEEZER_DOWNLOADER_DIR / "incomplete" / album_dir.name
    if stale.is_dir():
        shutil.rmtree(stale)  # an earlier incomplete try: the album is whole now
        log.info("🗑️  Removed the earlier incomplete copy: %s", stale.name)
    return album_dir
