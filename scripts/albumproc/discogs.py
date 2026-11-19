"""
Discogs lookups: canonical album title, original year, genre, tracklist,
and canonical artist spelling.
"""

import logging
from collections import Counter
from collections.abc import Callable
from functools import cache
import re
import ssl
import time
from typing import Optional

import requests

from . import config
from .names import (
    TITLE_MIN_SIMILARITY, clean_folder_name, clean_text, is_generic_title, is_various_artists, normalize_title, strip_artist_prefix, title_similarity,
)

log = logging.getLogger(__name__)

_API = "https://api.discogs.com"
_SEARCH_URL = f"{_API}/database/search"
_TIMEOUT = 10  # seconds
_RETRIES = 3
_ALBUM_MIN_SIMILARITY = 0.8
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
        try:
            resp = _SESSION.get(url, headers=_HEADERS, timeout=_TIMEOUT, **kwargs)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == _RETRIES:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code not in {429, 500, 502, 503, 504} or attempt == _RETRIES:
            # Slow down before the per-minute quota runs out
            remaining = resp.headers.get("X-Discogs-Ratelimit-Remaining", "")
            if remaining.isdigit() and int(remaining) < 3:
                time.sleep(10)
            break
        retry_after = resp.headers.get("Retry-After", "")
        delay = (min(int(retry_after), 60) if retry_after.isdigit() else 0) or (
            30 if resp.status_code == 429 else 2 ** attempt)
        log.info("  ⏳ Discogs HTTP %s — retrying in %d s…", resp.status_code, delay)
        resp.close()
        time.sleep(delay)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Discogs response is not a JSON object")
    return data


def search(**params) -> list[dict]:
    results = (api_get(_SEARCH_URL, params=params) or {}).get("results", [])
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise ValueError("Discogs search response has an invalid results list")
    return results


def strip_disambiguation(name: str) -> str:
    return _DISAMBIGUATION_RE.sub("", name).strip()


def _artist_name(entry: dict, *, as_credited: bool = False) -> str:
    if as_credited and (variation := clean_text(entry.get("anv") or "")):
        return variation
    return strip_disambiguation(entry.get("name") or "")


def join_artists(artists: list[dict], *, as_credited: bool = False) -> str:
    """
    "; "-joined artist names from a Discogs ``artists`` list.  Discogs' own
    join tokens ("&", "feat.") are dropped — the tags keep one value per
    artist — and disambiguation suffixes ("New Order (2)") are stripped.
    Repeated names are kept once. Track credits use the edition's name variation.
    """
    names: dict[str, str] = {}
    for entry in artists:
        name = _artist_name(entry, as_credited=as_credited)
        if name:
            names.setdefault(name.lower(), name)
    return "; ".join(names.values())


def canonical_credited_artist(name: str, album: dict) -> str:
    """Resolve a release's explicitly credited artist variation."""
    variations = album.get("artist_variations") or {}
    return next((canonical for variation, canonical in variations.items()
                 if variation.casefold() == name.casefold()), name)


def clean_title(title: str) -> str:
    """
    Tidy a Discogs title: drop invisible characters (zero-width spaces are
    common in Bandcamp imports), and keep only the first variant of
    multi-language titles ("Tekkonkinkreet OST = 鉄コン筋クリート OST").
    """
    return clean_text(clean_text(title).split(" = ")[0])


def _rank_results(results: list[dict], album_title: str, artist: str = "") -> list[dict]:
    """Rank viable title matches; edition preferences only break title ties."""
    ranked = []
    for result in results:
        raw = result.get("title") or ""
        stripped = strip_artist_prefix(raw, artist) if artist else raw
        title = stripped if stripped != raw else raw.split(" - ", 1)[-1]
        if bool(_VERSION_GROUP_RE.search(album_title)) != bool(_VERSION_GROUP_RE.search(title)):
            continue
        similarity = title_similarity(album_title, title)
        if similarity >= _ALBUM_MIN_SIMILARITY:
            rank = (normalize_title(album_title) == normalize_title(title), similarity,
                    "Single" not in (result.get("format") or []), bool(result.get("master_id")))
            ranked.append((rank, result))
    return [result for _, result in sorted(ranked, key=lambda pair: pair[0], reverse=True)]


def _pick_result(results: list[dict], album_title: str, artist: str = "") -> Optional[dict]:
    return next(iter(_rank_results(results, album_title, artist)), None)


def _audio_version(version: dict) -> bool:
    """Reject video editions before looking at their tracklists."""
    formats = " ".join([version.get("format", ""), *(version.get("major_formats") or [])]).casefold()
    return not any(word in formats for word in ("dvd", "blu-ray", "video", "vhs", "laserdisc"))


def _release_is_audio(release: dict) -> bool:
    formats = " ".join(f.get("name", "") for f in (release.get("formats") or [])).casefold()
    return not any(word in formats for word in ("dvd", "blu-ray", "video", "vhs", "laserdisc"))


def tracklist_score(local_titles: list[str], tracklist: list[dict]) -> float:
    """Score overlap and order, so a shuffled edition cannot win on its set alone."""
    overlap, ordered = tracklist_agreement(local_titles, tracklist)
    return 0.4 * overlap + 0.6 * ordered


def tracklist_agreement(local_titles: list[str], tracklist: list[dict]) -> tuple[float, float]:
    """Return unordered overlap and ordered LCS agreement, each in 0..1."""
    entries = [t for t in tracklist if t.get("type_", "track") == "track" and t.get("title")]
    remote = [t["title"] for t in entries]
    local = [t for t in local_titles if t]
    if not local or not remote:
        return 0.0, 0.0

    # API order is not guaranteed. Sort complete numeric/disc or vinyl-side
    # positions before measuring sequence agreement.
    positions = [(t.get("position") or "").strip() for t in entries]
    sides = vinyl_track_order(entries)
    if sides:
        remote = [entries[index]["title"] for index in sides]
    else:
        numeric_positions = [_position(p) for p in positions]
        if all(track is not None for _, track in numeric_positions):
            remote = [title for _, title in sorted(zip(numeric_positions, remote), key=lambda item: item[0])]
    matches = [[index for index, title in enumerate(remote)
                if title_similarity(local_title, title) >= TITLE_MIN_SIMILARITY]
               for local_title in local]
    assigned: dict[int, int] = {}

    def assign(index: int, visited: set[int]) -> bool:
        for candidate in matches[index]:
            if candidate in visited:
                continue
            visited.add(candidate)
            if candidate not in assigned or assign(assigned[candidate], visited):
                assigned[candidate] = index
                return True
        return False

    # Match each remote track at most once, even with repeated or ambiguous titles.
    for index in range(len(local)):
        assign(index, set())
    overlap = 2 * len(assigned) / (len(local) + len(remote))

    # Longest common subsequence preserves order while allowing bonus tracks.
    rows = [0] * (len(remote) + 1)
    for matches_for_track in matches:
        previous = 0
        matching = set(matches_for_track)
        for index in range(1, len(rows)):
            saved = rows[index]
            if index - 1 in matching:
                rows[index] = previous + 1
            else:
                rows[index] = max(rows[index], rows[index - 1])
            previous = saved
    ordered = rows[-1] / min(len(local), len(remote))
    return overlap, ordered


def _track_entries(release: dict) -> list[dict]:
    return [
        {"position": t.get("position") or "", "title": t.get("title") or "",
         "duration": t.get("duration", ""),
         "artists": join_artists(t.get("artists") or [], as_credited=True),
         "featuring": _credited(t, "Featuring"), "remixers": _credited(t, "Remix")}
        for t in (release.get("tracklist") or []) if t.get("type_", "track") == "track"
    ]


def _album_from_release(release: dict, results: list[dict], track_titles: list[str],
                        get: Callable) -> Optional[dict]:
    title = clean_title(release.get("title", ""))
    year = release.get("year") or ""

    master_id = release.get("master_id")
    master = {}
    if master_id:
        log.info("  📥 Fetching master release…")
        master = get(f"{_API}/masters/{master_id}") or {}
        master_title = clean_title(master.get("title", ""))
        if master_title and title_similarity(master_title, title) < TITLE_MIN_SIMILARITY:
            # Mis-linked master in Discogs data — trust the release itself
            log.info("  ℹ️  Master release %r doesn't match — ignored.", master_title)
            master, master_id = {}, None
        elif _VERSION_GROUP_RE.search(title) and not _VERSION_GROUP_RE.search(master_title):
            log.info("  ℹ️  %r is another version of %r — its own title and year kept.", title, master_title)
            master, master_id = {}, None
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

    track_release = release if _release_is_audio(release) else {}
    best_score = tracklist_score(track_titles, (release.get("tracklist") or [])) if track_release else -1.0
    if master_id and best_score < 1 and (track_titles or not track_release):
        versions = get(f"{_API}/masters/{master_id}/versions", per_page=100, page=1) or {}
        candidates = [v for v in versions.get("versions", []) if _audio_version(v) and v.get("id")]
        candidates.sort(key=lambda v: (0 if any(x in " ".join(v.get("major_formats") or []).casefold()
                                                      for x in ("cd", "file")) else 1,
                                       0 if "album" in v.get("format", "").casefold() else 1))
        for version in candidates[:5]:
            if str(version.get("id")) == str(release.get("id")):
                continue
            candidate = get(f"{_API}/releases/{version['id']}") or {}
            if not candidate or not _release_is_audio(candidate):
                continue
            score = tracklist_score(track_titles, (candidate.get("tracklist") or []))
            if score > best_score:
                best_score, track_release = score, candidate
            if best_score == 1:
                break

    if not title or not track_release:
        return None
    artist_entries = master.get("artists") or release.get("artists") or []
    artist_variations = {clean_text(entry.get("anv") or ""): _artist_name(entry)
                         for entry in artist_entries if entry.get("anv") and _artist_name(entry)}
    return {
        "release_id": track_release.get("id"),
        "master_id": master_id,
        "title": title,
        "artist": join_artists(artist_entries),
        "artist_variations": artist_variations,
        "year": str(year) if year else "",
        "genre": "; ".join(master.get("styles") or master.get("genres")
                              or track_release.get("styles") or track_release.get("genres") or []),
        "release_types": release_types(track_release),
        "tracklist": _track_entries(track_release),
        "master_tracklist": _track_entries(master),
    }


def fetch_album(artist: Optional[str], raw_title: str, *, diagnostics: Optional[dict] = None,
                track_titles: Optional[list[str]] = None,
                accept: Callable[[dict], bool] | None = None) -> Optional[dict]:
    """Try distinct ranked candidates until an acceptable audio edition is found."""
    cleaned = clean_folder_name(raw_title)
    if artist:
        cleaned = clean_folder_name(strip_artist_prefix(cleaned, artist))
    searches = ([{"artist": artist, "release_title": cleaned}] if artist else [])
    searches.append({"q": f"{artist} {cleaned}" if artist else cleaned})
    fallback = [{"artist": artist, "per_page": 100}] if artist else []
    @cache
    def get(url: str, **params):
        return api_get(url, params=params) if params else api_get(url)

    seen = set()
    rejected = unavailable = False

    def check(url: str, results: list[dict]) -> Optional[dict]:
        nonlocal rejected, unavailable
        if url in seen:
            return None
        seen.add(url)
        try:
            release = get(url) or {}
            album = _album_from_release(release, results, track_titles or [], get)
            if album is not None and (accept is None or accept(album)):
                return album
            rejected = True
        except (requests.RequestException, ValueError, AttributeError, TypeError, KeyError) as exc:
            unavailable = True
            log.warning("Discogs could not load candidate details: %s", exc)
        return None

    album = None
    best = -1.0
    informative = [title for title in track_titles or [] if not is_generic_title(title)]
    for params in searches + fallback:
        try:
            results = search(**{"type": "release", "per_page": 20, **params})
            ranked = _rank_results(results, cleaned, artist or "")
        except (requests.RequestException, ValueError, AttributeError, TypeError) as exc:
            unavailable = True
            log.warning("Discogs release search failed: %s", exc)
            break
        # Repeated hits from a more specific query do not use up this query's budget.
        urls = list(dict.fromkeys(r["resource_url"] for r in ranked
                                  if r.get("resource_url") and r["resource_url"] not in seen))
        for url in urls[:5]:
            candidate = check(url, results)
            if candidate is None:
                continue
            score = tracklist_score(informative, candidate["tracklist"]) if informative else 1.0
            if score > best:
                album, best = candidate, score
            if best == 1:
                break
        if best > 0:
            break

    # A release links directly to its master. Search masters only if the release
    # queries could not provide an acceptable candidate.
    if album is None and not unavailable:
        for params in searches:
            try:
                match = _pick_result(search(**{"type": "master", "per_page": 10, **params}),
                                     cleaned, artist or "")
                if not match or not match.get("id"):
                    continue
                master = get(f"{_API}/masters/{match['id']}") or {}
                if release_id := master.get("main_release"):
                    album = check(f"{_API}/releases/{release_id}", [])
                if album:
                    break
            except (requests.RequestException, ValueError, AttributeError, TypeError, KeyError) as exc:
                unavailable = True
                log.warning("Discogs main release lookup failed: %s", exc)
                break

    if diagnostics is not None:
        diagnostics["status"] = ("matched" if album else "unavailable" if unavailable else
                                 "rejected" if rejected else "unmatched")
    return album


def lookup_album(artist: Optional[str], title: str, *, track_titles: Optional[list[str]] = None,
                 accept: Callable[[dict], bool] | None = None) -> tuple[Optional[dict], str]:
    diagnostics = {}
    album = fetch_album(artist, title, diagnostics=diagnostics, track_titles=track_titles, accept=accept)
    return album, diagnostics.get('status', 'matched' if album else 'unmatched')


def edition_history(release_id: int, master_id: Optional[int]) -> dict:
    """
    What Discogs knows about an album's editions, for Anton's opinion: the
    matched release's labels, formats, mastering credits and notes, and every
    version under its master.  Costs 2-3 requests.
    """
    release = api_get(f"{_API}/releases/{release_id}") or {}
    credits = [f"{e.get('role')}: {strip_disambiguation(e.get('name', ''))}"
               for e in (release.get("extraartists") or [])
               if re.search(r"master|lacquer|remaster|mix", e.get("role", ""), re.IGNORECASE)]
    history = {
        "release": {
            "title": release.get("title"),
            "year": release.get("year"),
            "labels": [f"{l.get('name')} {l.get('catno', '')}".strip() for l in (release.get("labels") or [])],
            "formats": [", ".join([f.get("name", "")] + (f.get("descriptions") or [])) for f in (release.get("formats") or [])],
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
    descriptions = {d.lower() for f in (release.get("formats") or []) for d in (f.get("descriptions") or [])}
    styles = {s.lower() for s in (release.get("styles") or []) + (release.get("genres") or [])}
    if descriptions & {"single", "maxi-single"}:
        types = ["single"]
    elif descriptions & {"ep", "mini-album"}:
        types = ["ep"]
    elif descriptions & {"album", "lp"}:
        types = ["album"]
    else:
        types = guess_release_types(sum(t.get("type_", "track") == "track" for t in (release.get("tracklist") or [])))
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
    return [name for entry in (track.get("extraartists") or [])
            if role.casefold() in (entry.get("role") or "").casefold()
            and (name := _artist_name(entry))]


def _position(position: str) -> tuple[int, int | None]:
    """Parse a numeric position, optionally prefixed by a disc number."""
    text = position.strip()
    match = re.fullmatch(r"(?:CD)?([1-9]\d*)[-.]0*([1-9]\d*)", text, re.I)
    if match:
        return int(match[1]), int(match[2])
    return 1, int(text) if re.fullmatch(r"0*[1-9]\d*", text) else None


def vinyl_track_order(tracklist: list[dict]) -> list[int]:
    """Sequential vinyl sides and tracks, or an empty list if ambiguous."""
    sides = {}
    for index, entry in enumerate(tracklist):
        match = re.fullmatch(r"([A-Z])([1-9]\d*)", entry.get("position", "").strip(), re.I)
        if not match:
            return []
        tracks = sides.setdefault(match[1].upper(), {})
        number = int(match[2])
        if number in tracks:
            return []
        tracks[number] = index
    order = []
    for index, (side, tracks) in enumerate(sorted(sides.items())):
        if side != chr(ord("A") + index) or sorted(tracks) != list(range(1, len(tracks) + 1)):
            return []
        order.extend(tracks[number] for number in sorted(tracks))
    return order


def tracklist_index_at_position(tracklist: list[dict], track_num: int, disc: int = 1) -> int | None:
    """Unique index for an explicit numeric Discogs position."""
    matches = [i for i, entry in enumerate(tracklist)
               if _position(entry.get("position", "")) == (disc, track_num)]
    return matches[0] if len(matches) == 1 else None


def resolve_track_title(tracklist: list[dict], track_num: int, fallback_index: int,
                        disc: int = 1, *, allow_unpositioned: bool = False) -> str | None:
    """Use explicit positions, or the sequence of a complete unpositioned list."""
    index = tracklist_index_at_position(tracklist, track_num, disc)
    if index is None and disc == 1:
        order = vinyl_track_order(tracklist)
        if order and 1 <= track_num <= len(order):
            index = order[track_num - 1]
        elif (allow_unpositioned and 0 <= fallback_index < len(tracklist)
              and all(not entry.get("position") for entry in tracklist)):
            index = fallback_index
    if index is None:
        return None
    return tracklist[index].get("title") or None
