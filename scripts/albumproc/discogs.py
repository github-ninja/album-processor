"""
Discogs lookups: canonical album title, original year, genre, tracklist,
and canonical artist spelling.
"""

import logging
from collections import Counter
import re
import ssl
import time
from typing import Optional

import requests

from . import config
from .names import (
    TITLE_MIN_SIMILARITY, clean_folder_name, clean_text, is_various_artists, strip_artist_prefix, title_similarity,
)

log = logging.getLogger(__name__)

_API = "https://api.discogs.com"
_SEARCH_URL = f"{_API}/database/search"
_TIMEOUT = 10  # seconds
_RETRIES = 3
_HEADERS = {"User-Agent": "AlbumProcessor/4.0"}
if config.DISCOGS_TOKEN:
    _HEADERS["Authorization"] = f"Discogs token={config.DISCOGS_TOKEN}"


class _DiscogsAdapter(requests.adapters.HTTPAdapter):
    """Use a compact X25519 handshake on paths that stall with hybrid key shares."""

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        context = ssl.create_default_context()
        context.set_ecdh_curve("X25519")
        kwargs["ssl_context"] = context
        super().init_poolmanager(connections, maxsize, block=block, **kwargs)


_SESSION = requests.Session()
_SESSION.mount(f"{_API}/", _DiscogsAdapter())

# Pre-release copies often predate the actual release year
_PREVIEW_FORMATS = {"Promo", "Test Pressing", "White Label", "Advance"}
# Discogs disambiguates same-named artists with a numeric suffix: "Nico (4)"
_DISAMBIGUATION_RE = re.compile(r"\s*\(\d+\)\s*$")
# Another recording of the album rather than another edition of it: Discogs
# files "I Am Legion (Instrumentals)" under the master "I Am Legion"
_VERSION_GROUP_RE = re.compile(
    r"[\(\[][^\)\]]*\b(?:instrumentals?|remix(?:es|ed)?|acapellas?|a cappella|dubs)\b[^\)\]]*[\)\]]", re.IGNORECASE)


def api_get(url: str, **kwargs) -> Optional[dict]:
    """GET a Discogs API URL; waits and retries when rate-limited."""
    for attempt in range(_RETRIES + 1):
        resp = _SESSION.get(url, headers=_HEADERS, timeout=_TIMEOUT, **kwargs)
        if resp.status_code != 429 or attempt == _RETRIES:
            # Slow down before the per-minute quota runs out
            remaining = resp.headers.get("X-Discogs-Ratelimit-Remaining", "")
            if remaining.isdigit() and int(remaining) < 3:
                time.sleep(10)
            break
        delay = int(resp.headers.get("Retry-After") or 0) or 30
        log.info("  ⏳ Discogs rate limit — waiting %d s…", delay)
        time.sleep(delay)
    resp.raise_for_status()
    return resp.json()


def search(**params) -> list[dict]:
    return (api_get(_SEARCH_URL, params=params) or {}).get("results", [])


def strip_disambiguation(name: str) -> str:
    return _DISAMBIGUATION_RE.sub("", name).strip()


def join_artists(artists: list[dict]) -> str:
    """
    "; "-joined artist names from a Discogs ``artists`` list.  Discogs' own
    join tokens ("&", "feat.") are dropped — the tags keep one value per
    artist — and disambiguation suffixes ("New Order (2)") are stripped.
    Repeated entries (some releases list the same artist twice) are kept once.
    """
    names: dict[str, str] = {}
    for entry in artists:
        name = strip_disambiguation(entry.get("name", ""))
        if name:
            names.setdefault(name.lower(), name)
    return "; ".join(names.values())


def clean_title(title: str) -> str:
    """
    Tidy a Discogs title: drop invisible characters (zero-width spaces are
    common in Bandcamp imports), and keep only the first variant of
    multi-language titles ("Tekkonkinkreet OST = 鉄コン筋クリート OST").
    """
    return clean_text(clean_text(title).split(" = ")[0])


def _pick_result(results: list[dict], album_title: str) -> Optional[dict]:
    """
    The search result whose title best matches *album_title*, or None when
    nothing is close enough — a wrong album is never used.  Results are
    titled "Artist - Title"; ties go to results that belong to a master
    release, and singles lose ties to albums ("Do You Know Squarepusher?").
    """
    best, best_score = None, 0.0
    for result in results:
        result_title = result.get("title", "")
        if " - " in result_title:
            result_title = result_title.split(" - ", 1)[1]
        score = (title_similarity(album_title, result_title)
                 + (0.01 if result.get("master_id") else 0)
                 - (0.02 if "Single" in (result.get("format") or []) else 0))
        if score > best_score:
            best, best_score = result, score
    return best if best is not None and best_score >= TITLE_MIN_SIMILARITY else None


def fetch_album(artist: Optional[str], raw_title: str) -> Optional[dict]:
    """
    Look up an album by *artist* and a (possibly messy) folder name or title.

    The title and year come from the master release: it carries the
    canonical title ("Lemodie", not "Lemodie (15th Anniversary Edition)") and
    the original release year (earliest non-promo version).  Tracklist,
    artists and genre come from the matched release.  Costs 2-4 requests.

    Returns {"release_id", "master_id", "title", "artist", "year", "genre",
    "release_types", "tracklist"} or None when
    Discogs is unreachable or has no confident match.
    """
    cleaned = clean_folder_name(raw_title)
    # "Artist - Title" folder names would otherwise search for "Artist Artist - Title"
    if artist:
        cleaned = strip_artist_prefix(cleaned, artist)

    searches = []
    if artist:
        searches.append({"artist": artist, "release_title": cleaned})
    searches.append({"q": f"{artist} {cleaned}" if artist else cleaned})
    if artist:
        # Last resort for typos in the downloaded title ("Beats & Peaces II"):
        # all of the artist's releases, matched by title similarity
        searches.append({"artist": artist, "per_page": 100})

    try:
        match, results = None, []
        for params in searches:
            log.info("🔍 Querying Discogs: %s", ", ".join(f"{k}={v!r}" for k, v in params.items()))
            results = search(**{"type": "release", "per_page": 10, **params})
            match = _pick_result(results, cleaned)
            if match:
                break
        if not match or not match.get("resource_url"):
            log.info("  ℹ️  No matching Discogs release found.")
            return None

        log.info("  📥 Fetching release details…")
        release = api_get(match["resource_url"]) or {}
        title = clean_title(release.get("title", ""))
        year = release.get("year") or ""

        master_id = release.get("master_id")
        if master_id:
            log.info("  📥 Fetching master release…")
            master = api_get(f"{_API}/masters/{master_id}") or {}
            master_title = clean_title(master.get("title", ""))
            if master_title and title_similarity(master_title, title) < TITLE_MIN_SIMILARITY:
                # Mis-linked master in Discogs data — trust the release itself
                log.info("  ℹ️  Master release %r doesn't match — ignored.", master_title)
            else:
                if _VERSION_GROUP_RE.search(title) and not _VERSION_GROUP_RE.search(master_title):
                    log.info("  ℹ️  %r is another version of %r — its own title kept.", title, master_title)
                else:
                    title = master_title or title
                # The master's own year is occasionally a later edition's; the
                # earliest version seen in the search results is more reliable
                years = [
                    int(r["year"]) for r in results
                    if r.get("master_id") == master_id and str(r.get("year", "")).isdigit()
                    and not _PREVIEW_FORMATS & set(r.get("format") or [])
                ]
                if master.get("year"):
                    years.append(int(master["year"]))
                year = min(years) if years else year

        return {
            "release_id": release.get("id"),
            "master_id": master_id,
            "title": title,
            "artist": join_artists(release.get("artists", [])),
            "year": str(year) if year else "",
            "genre": "; ".join(release.get("styles") or release.get("genres") or []),
            "release_types": release_types(release),
            # Heading / index rows are not tracks
            "tracklist": [
                {"position": t.get("position", ""), "title": t.get("title", ""),
                 "artists": join_artists(t.get("artists", [])),
                 "featuring": _credited(t, "Featuring"),
                 "remixers": _credited(t, "Remix")}
                for t in release.get("tracklist", [])
                if t.get("type_", "track") == "track"
            ],
        }
    except requests.Timeout:
        log.warning("⚠️  Discogs request timed out.")
    except requests.HTTPError as exc:
        log.warning("⚠️  Discogs HTTP error: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️  Discogs error: %s", exc)
    return None


def edition_history(release_id: int, master_id: Optional[int]) -> dict:
    """
    What Discogs knows about an album's editions, for Anton's opinion: the
    matched release's labels, formats, mastering credits and notes, and every
    version under its master.  Costs 2-3 requests.
    """
    release = api_get(f"{_API}/releases/{release_id}") or {}
    credits = [f"{e.get('role')}: {strip_disambiguation(e.get('name', ''))}"
               for e in release.get("extraartists", [])
               if re.search(r"master|lacquer|remaster|mix", e.get("role", ""), re.IGNORECASE)]
    history = {
        "release": {
            "title": release.get("title"),
            "year": release.get("year"),
            "labels": [f"{l.get('name')} {l.get('catno', '')}".strip() for l in release.get("labels", [])],
            "formats": [", ".join([f.get("name", "")] + f.get("descriptions", [])) for f in release.get("formats", [])],
            "credits": credits,
            "notes": (release.get("notes") or "")[:1500],
        },
        "versions": [],
    }
    if master_id:
        versions, page, pages = [], 1, 1
        while page <= min(pages, _MAX_VERSION_PAGES):
            reply = api_get(f"{_API}/masters/{master_id}/versions", params={"per_page": 100, "page": page}) or {}
            versions += reply.get("versions", [])
            pages = reply.get("pagination", {}).get("pages", 1)
            page += 1
        history["editions"] = edition_summary(versions)
        history["editions_total"] = len(versions)
    return history


_MAX_VERSION_PAGES = 5  # 500 versions: more than any album has that is worth reading
_EDITION_MARKS = ("Remastered", "Reissue", "Promo", "Test Pressing")
_FILE_FORMATS = ("FLAC", "ALAC", "WAV", "AIFF", "AAC", "MP3")
# Never the source of a lossless library: left out of the summary, only counted
_NOT_A_SOURCE = {"Cassette", "Minidisc", "8-Track Cartridge", "Reel-To-Reel", "VHS"}


def edition_summary(versions: list[dict]) -> list[str]:
    """
    Every version of a master in a few readable lines, oldest first: the
    pressings of one edition (countries, catalogue numbers) collapse into
    "2016 · CD · Caroline International · Reissue ×3".  Bootlegs and tape
    formats are only counted.
    """
    titles = Counter(v.get("title", "") for v in versions)
    main_title = titles.most_common(1)[0][0] if titles else ""
    groups: Counter = Counter()
    bootlegs = tapes = 0
    for v in versions:
        description = v.get("format", "")
        media = v.get("major_formats") or []
        if "unofficial release" in description.lower():
            bootlegs += 1
            continue
        if media and set(media) <= _NOT_A_SOURCE:
            tapes += 1
            continue
        medium = "/".join(media) or "?"
        if medium == "File":
            kinds = [k for k in _FILE_FORMATS if k.lower() in description.lower()]
            medium = f"File ({', '.join(kinds)})" if kinds else "File"
        marks = ", ".join(m for m in _EDITION_MARKS if m.lower() in description.lower())
        year = (v.get("released") or "")[:4]
        title = v.get("title", "")
        groups[(year if year.strip("0") else "год ?", medium, strip_disambiguation(v.get("label", "")), marks,
                title if title != main_title else "")] += 1
    lines = []
    for (year, medium, label, marks, title), count in sorted(groups.items()):
        line = " · ".join(part for part in (year, medium, label, marks, f"«{title}»" if title else "") if part)
        lines.append(line + (f" ×{count}" if count > 1 else ""))
    if bootlegs or tapes:
        lines.append(f"(не показаны: неофициальных изданий {bootlegs}, кассет и прочих плёнок {tapes})")
    return lines


def canonical_artist_name(name: str) -> Optional[str]:
    """
    Spelling of *name* as a Discogs artist entity ("Arms and Sleepers" →
    "Arms And Sleepers"), or None if Discogs has no artist with exactly that
    name (ignoring case) — e.g. for ad-hoc collaborations like "Nico & Rukkus".
    """
    try:
        for result in search(q=name, type="artist", per_page=10):
            candidate = strip_disambiguation(result.get("title", ""))
            if candidate.lower() == name.lower():
                return candidate
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️  Discogs error: %s", exc)
    return None


def guess_release_types(track_count: int) -> list[str]:
    """RELEASETYPE by length alone, as MusicBrainz counts it: up to 3 tracks a single, up to 6 an EP."""
    return ["single" if track_count <= 3 else "ep" if track_count <= 6 else "album"]


def album_release_types(discogs_types: list[str], track_count: int, album_artist: str) -> list[str]:
    """RELEASETYPE of an album: Discogs' when known, else by its length; Various Artists is a compilation."""
    types = list(discogs_types) or guess_release_types(track_count)
    if is_various_artists(album_artist) and "compilation" not in types:
        types.append("compilation")
    return types


def release_types(release: dict) -> list[str]:
    """
    RELEASETYPE values (MusicBrainz vocabulary, which Navidrome groups by) of
    a Discogs release: the primary type from its format descriptions, else
    from its length; then compilation, soundtrack, DJ mix and remix albums.
    """
    descriptions = {d.lower() for f in release.get("formats", []) for d in f.get("descriptions", [])}
    styles = {s.lower() for s in release.get("styles", []) + release.get("genres", [])}
    if descriptions & {"single", "maxi-single"}:
        types = ["single"]
    elif descriptions & {"ep", "mini-album"}:
        types = ["ep"]
    elif descriptions & {"album", "lp"}:
        types = ["album"]
    else:
        types = guess_release_types(sum(t.get("type_", "track") == "track" for t in release.get("tracklist", [])))
    if "compilation" in descriptions:
        types.append("compilation")
    if "soundtrack" in styles:
        types.append("soundtrack")
    if "mixed" in descriptions:
        types.append("dj-mix")
    if re.search(r"\bremix(?:es|ed)\b", release.get("title", ""), re.IGNORECASE):
        types.append("remix")
    return types


def _credited(track: dict, role: str) -> list[str]:
    """Names credited on a Discogs track with *role* ("Featuring", "Remix" — also "Remix [Additional]")."""
    return [strip_disambiguation(e.get("name", "")) for e in track.get("extraartists", [])
            if role.lower() in e.get("role", "").lower() and e.get("name")]


def _position(position: str) -> tuple[int, Optional[int]]:
    """(disc, track) of a Discogs position: "2-03"/"CD2-3"/"2.3" → (2, 3), "A1"/"7" → (1, 1)/(1, 7)."""
    m = re.match(r"^(?:CD)?(\d+)[-.](\d+)$", position.strip(), re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    digits = re.sub(r"[^0-9]", "", position)
    return 1, int(digits) if digits else None


def resolve_track_title(tracklist: list[dict], track_num: int, fallback_index: int,
                        disc: int = 1) -> Optional[str]:
    """
    Discogs title for *track_num* on *disc* ("2-03" positions; "A1"/"B2" use
    their digits) or, failing that, the *fallback_index*-th (0-based) entry.
    """
    for entry in tracklist:
        if _position(entry.get("position", "")) == (disc, track_num):
            return entry.get("title") or None
    if 0 <= fallback_index < len(tracklist):
        return tracklist[fallback_index].get("title") or None
    return None
