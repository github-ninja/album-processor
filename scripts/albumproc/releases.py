"""
New albums by the library's artists, from Deezer: release day, a record type
on every release (album / ep / single / compile), no key needed.

Only albums are reported — singles and EPs are noise for someone who waits
for the album — and not reissues, remix or live albums, nor albums already
in the library.  Namesakes cannot be told apart for sure — Deezer merges
same-named artists into one page ("Phace" the drum & bass producer and PHACE
the contemporary music ensemble) and its genres are loose — so an album is
reported as sure only in a genre of the artist's albums in the library, and
under "may be a namesake" otherwise: nothing is hidden, the doubtful is marked.  Checks are on demand; automatic checks run only after the
user schedules them (daily / weekly / monthly), and stay quiet without news.

State (music/.album_processor/releases.json):
  schedule     "daily" | "weekly" | "monthly" | null
  last_check   date of the last check (manual or scheduled)
  artists      library artist folder → {"id": Deezer artist id, "genres": Deezer genres
               of the artist's albums in the library ([] — none found, unverified)}
               (null: no artist of that name on Deezer)
  notified     Deezer ids of the albums already reported
"""

import datetime
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

from . import config
from .files import library_album_dirs
from .names import album_identity, clean_folder_name, is_various_artists, name_key, title_similarity

log = logging.getLogger(__name__)

_API = "https://api.deezer.com"
_PAUSE = 0.12                      # Deezer allows 50 requests per 5 s
_FIRST_LOOKBACK_DAYS = 90          # the very first check reports this far back
CHECK_HOUR = 10                    # scheduled checks run from this hour on (TZ)
SCHEDULES = ("daily", "weekly", "monthly")
# Not a new album: reissues, remix and live albums, collections
_NOT_NEW_RE = re.compile(
    r"\b(?:remix(?:es|ed)?|live|deluxe|remaster(?:ed)?|anniversary|expanded|edition|compilation|"
    r"collection|best of|greatest hits|instrumentals?|versions|rework(?:ed|s)?|reimagined)\b", re.IGNORECASE)


def state_path() -> Path:
    return config.state_dir() / "releases.json"


def load_state() -> dict:
    try:
        state = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return {"schedule": None, "last_check": None, "artists": {}, "notified": [], **state}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _get(url: str, **params) -> dict:
    time.sleep(_PAUSE)
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Deezer: {data['error']}")
    return data


# ---------------------------------------------------------------------------
# Library side
# ---------------------------------------------------------------------------

def library_artists() -> dict[str, list[str]]:
    """Artist folder → its album titles (lower case, no year); Various Artists left out."""
    artists: dict[str, list[str]] = {}
    for album_dir in library_album_dirs():
        if not is_various_artists(album_dir.parent.name):
            artists.setdefault(album_dir.parent.name, []).append(album_identity(album_dir.name))
    return artists


def _in_library(title: str, owned: list[str]) -> bool:
    """Whether a Deezer title is one of *owned*, edition tails aside ("… (15th Anniversary Edition)")."""
    bare = clean_folder_name(title).lower()
    return any(title_similarity(bare, own) >= 0.9 for own in owned)


# ---------------------------------------------------------------------------
# Deezer side
# ---------------------------------------------------------------------------

# Deezer's top genres, for the "another genre" note
GENRES = {
    132: "Pop", 116: "Rap/Hip Hop", 152: "Rock", 113: "Dance", 165: "R&B", 85: "Alternative", 106: "Electro",
    466: "Folk", 144: "Reggae", 129: "Jazz", 98: "Classical", 173: "Films/Games", 464: "Metal",
    169: "Soul & Funk", 2: "African", 16: "Asian", 153: "Blues", 75: "Brazilian", 81: "Indian",
    95: "Kids", 197: "Latin",
}


def albums_of(artist_id: int) -> list[dict]:
    """Every release on the artist's Deezer page (the list is not in date order)."""
    releases, url, params = [], f"{_API}/artist/{artist_id}/albums", {"limit": 100}
    while url:
        data = _get(url, **params)
        releases += data.get("data", [])
        url, params = data.get("next"), {}
    return releases


def owned_on_deezer(artist_id: int, name: str, owned: list[str], page: list[dict]) -> list[dict]:
    """
    The library's albums of an artist found on Deezer: on the artist's page,
    else by album search — the page misses some albums (Akira Kosemura's
    "Polaroid Piano" is on Deezer under his name but not on his page).
    """
    found = [r for r in page if _in_library(r.get("title", ""), owned)]
    if found:
        return found
    for title in owned[:3]:
        results = _get(f"{_API}/search/album", q=f'artist:"{name}" album:"{title}"', limit=10).get("data", [])
        found += [r for r in results if (r.get("artist") or {}).get("id") == artist_id
                  and _in_library(r.get("title", ""), owned)]
    return found


def find_artist(name: str, owned: list[str]) -> Optional[dict]:
    """
    The Deezer artist for a library artist — {"id", "genres"}: an exact name
    match, preferring (most followed first) one with an album of the library,
    whose genres then tell the artist's own albums from a namesake's.  With
    none verified, the most followed match with no genres; None without a match.
    """
    candidates = sorted((a for a in _get(f"{_API}/search/artist", q=name, limit=10).get("data", [])
                         if name_key(a.get("name", "")) == name_key(name)), key=lambda a: -a.get("nb_fan", 0))
    for candidate in candidates:
        owned_albums = owned_on_deezer(candidate["id"], name, owned, albums_of(candidate["id"]))
        if owned_albums:
            return {"id": candidate["id"], "genres": sorted({r.get("genre_id") for r in owned_albums})}
    return {"id": candidates[0]["id"], "genres": []} if candidates else None


def new_albums(releases: list[dict], owned: list[str], since: str, today: str, notified: set,
               genres: list) -> list[dict]:
    """
    Albums out from *since* to *today*, not reissues, not owned, not reported
    yet.  *since* is the last check's day, included: what Deezer added later
    that day is caught next time, and *notified* keeps the rest from repeating.
    An album is "sure" in one of *genres* (the artist's albums in the library);
    otherwise it may be a namesake's and carries the reason why.
    """
    found, seen = [], set()
    for r in sorted(releases, key=lambda r: r.get("release_date", "")):
        title, date = r.get("title", ""), r.get("release_date", "")
        key = name_key(title)
        if (r.get("record_type") != "album" or not since <= date <= today or r.get("id") in notified
                or key in seen or _NOT_NEW_RE.search(title) or _in_library(title, owned)):
            continue
        seen.add(key)  # explicit / clean and regional versions of one album
        if not genres:
            doubt = "на Deezer нет ваших альбомов, сверить не с чем"
        elif r.get("genre_id") not in genres:
            doubt = f"другой жанр: {GENRES.get(r.get('genre_id'), 'не указан')}"
        else:
            doubt = ""
        found.append({"id": r["id"], "title": title, "date": date, "doubt": doubt,
                      "link": r.get("link") or f"https://www.deezer.com/album/{r['id']}"})
    return found


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def check(today: Optional[datetime.date] = None) -> tuple[list[dict], dict]:
    """New albums of every library artist since the last check; updates the state."""
    today = today or datetime.date.today()
    state = load_state()
    since = state["last_check"] or (today - datetime.timedelta(days=_FIRST_LOOKBACK_DAYS)).isoformat()
    notified = set(state["notified"])
    artists = library_artists()
    news = []
    for name, owned in sorted(artists.items()):
        try:
            # Not found, or found but not verified yet: look again (new albums may appear on Deezer)
            if not (state["artists"].get(name) or {}).get("genres"):
                state["artists"][name] = find_artist(name, owned)
            artist = state["artists"][name]
            if artist is None:
                continue
            for album in new_albums(albums_of(artist["id"]), owned, since, today.isoformat(), notified,
                                    artist["genres"]):
                news.append({**album, "artist": name})
        except Exception as exc:  # noqa: BLE001 — one artist's failure must not end the check
            log.warning("⚠️  %s: %s", name, exc)
    state["artists"] = {name: state["artists"].get(name) for name in artists}
    state["notified"] = sorted(notified | {a["id"] for a in news})
    state["last_check"] = today.isoformat()
    save_state(state)
    return news, state


def is_due(state: dict, now: datetime.datetime) -> bool:
    """A scheduled check is due from CHECK_HOUR once per day / Monday-started week / month."""
    if state["schedule"] not in SCHEDULES or now.hour < CHECK_HOUR:
        return False
    today = now.date()
    period_start = {
        "daily": today,
        "weekly": today - datetime.timedelta(days=today.weekday()),
        "monthly": today.replace(day=1),
    }[state["schedule"]]
    return not state["last_check"] or state["last_check"] < period_start.isoformat()


def next_check(state: dict, now: datetime.datetime) -> Optional[datetime.datetime]:
    if state["schedule"] not in SCHEDULES:
        return None
    if is_due(state, now):
        return now
    day = now.date() if now.hour < CHECK_HOUR and is_due(state, now.replace(hour=CHECK_HOUR)) else None
    if day is None:
        today = now.date()
        day = {
            "daily": today + datetime.timedelta(days=1),
            "weekly": today + datetime.timedelta(days=7 - today.weekday()),
            "monthly": (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1),
        }[state["schedule"]]
    return datetime.datetime.combine(day, datetime.time(CHECK_HOUR))


# ---------------------------------------------------------------------------
# Text for the bot
# ---------------------------------------------------------------------------

_SCHEDULE_NAMES = {"daily": "каждый день", "weekly": "по понедельникам", "monthly": "1-го числа каждого месяца"}


def _line(album: dict) -> str:
    date = datetime.date.fromisoformat(album["date"]).strftime("%d.%m.%Y")
    line = f"• {album['artist']} — {album['title']} ({date})"
    if album["doubt"]:
        line += f" — {album['doubt']}"
    return f"{line}\n  {album['link']}"


def report(news: list[dict], state: dict, quiet: bool = False) -> str:
    """
    The news as a message: the sure albums, then the ones that may be a
    namesake's; without news an empty string when *quiet* (scheduled checks).
    """
    if not news:
        if quiet:
            return ""
        tracked = sum(1 for artist in state["artists"].values() if artist)
        return f"Новых альбомов нет (проверено исполнителей: {tracked})."
    ordered = sorted(news, key=lambda a: (a["date"], a["artist"]))
    sure = [_line(a) for a in ordered if not a["doubt"]]
    doubtful = [_line(a) for a in ordered if a["doubt"]]
    parts = []
    if sure:
        parts.append("🆕 Новые альбомы ваших исполнителей:\n" + "\n".join(sure))
    if doubtful:
        parts.append("❓ Возможно, тёзки — проверьте по ссылке:\n" + "\n".join(doubtful))
    return "\n\n".join(parts)


def status(state: dict, now: datetime.datetime) -> str:
    lines = []
    if state["schedule"] in SCHEDULES:
        nxt = next_check(state, now)
        lines.append(f"🔔 Автопроверка: {_SCHEDULE_NAMES[state['schedule']]} в {CHECK_HOUR}:00"
                     + (f", следующая — {nxt:%d.%m.%Y %H:%M}" if nxt else ""))
    else:
        lines.append("🔕 Автопроверка выключена")
    if state["last_check"]:
        lines.append(f"Последняя проверка: {datetime.date.fromisoformat(state['last_check']):%d.%m.%Y}")
    artists = state["artists"]
    missing = sorted(name for name, artist in artists.items() if not artist)
    unverified = sorted(name for name, artist in artists.items() if artist and not artist["genres"])
    if artists:
        lines.append(f"Отслеживается исполнителей: {len(artists) - len(missing)}")
    if unverified:
        lines.append("Без сверки (на Deezer нет их альбомов из медиатеки, новинки идут в «❓»): " + ", ".join(unverified))
    if missing:
        lines.append("Не нашлись на Deezer, не отслеживаются: " + ", ".join(missing))
    return "\n".join(lines)


def set_schedule(schedule: Optional[str]) -> dict:
    state = load_state()
    state["schedule"] = schedule
    save_state(state)
    return state
