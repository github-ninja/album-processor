"""
Library maintenance: find everything that deviates from the conventions and
fix it.  Work happens in two steps so the user can review the changes:

  plan_library_fixes()  → list of per-album entries (JSON-serialisable, no changes)
  apply_library_fixes() → applies a plan, journaling the old values

Checks per album:
  - album title and year (Discogs, for albums whose names look off)
  - track credits: featured guests are artists, not part of the title;
    remixers are not artists (their "(X Remix)" stays in the title)
  - tag hygiene: invisible characters and stray whitespace, genre spelling
    and splitting, TRACKTOTAL/DISCTOTAL convention, disc numbers, missing
    album artist or genre, artist spelling consistent across the library
  - cover: Cover.jpg at COVER_SIZE², a copy in every disc folder (one placed
    there by hand is kept), the same image embedded in every track, no stray
    cover files; small covers are upgraded from Deezer when the
    same artwork exists there in a higher resolution
  - audio format: FLAC that isn't 16/44.1 is converted
  - ReplayGain tags (track and album) where missing or after a conversion
  - RELEASETYPE, COMPILATION and REMIXER where missing (Discogs, else by length)
  - duplicates: one album in two folders (another year or case) — the
    older copy is moved aside
  - track file names, album folder and artist folder derived from the tags
"""

import json
import logging
import re
import shutil
import unicodedata
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Optional

from mutagen import File as AudioFile
from mutagen.flac import FLAC

from . import audioformat, config, covers, discogs, jobs, replaygain
from .files import (
    album_audio_files, archive_path, find_dir_case_insensitive, journal,
    library_album_dirs, publish_album, rename_files, restore_backups, safe_rename, write_json,
)
from .names import (
    album_folder_name, album_identity, artist_folder_name, credit_track, has_credit_markers, artists_match, clean_folder_name, clean_text,
    is_various_artists, sanitize_path_component, split_genres, split_multi_value, strip_artist_prefix,
    is_generic_title, normalize_title, split_featuring, track_file_name, track_remixers,
)
from .tags import album_tag_values, embed_picture, flac_values as _flac_values, get_embedded_picture, read_tags, track_disc, update_album_tags

log = logging.getLogger(__name__)

_LIBRARY_DIR_RE = re.compile(r"^(\d{4}) - (.+)$")
_LOOSE_YEAR_DIR_RE = re.compile(r"^(\d{4})\s*[-–—]\s*(.+)$")

# Text fields whose values are cleaned of invisible characters and stray whitespace
_TEXT_FIELDS = ("title", "artist", "album", "albumartist", "genre", "date")
# Genre spellings that are the same genre (key → Discogs spelling)
_GENRE_ALIASES = {
    "drum and bass": "Drum n Bass", "drum & bass": "Drum n Bass", "drum'n'bass": "Drum n Bass",
    "drum 'n' bass": "Drum n Bass", "dnb": "Drum n Bass", "trip-hop": "Trip Hop",
    "fo-fi house": "Lo-Fi House",  # typo seen in the library
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _split_library_dir(name: str) -> tuple[str, str]:
    m = _LOOSE_YEAR_DIR_RE.match(name)
    return (m.group(1), m.group(2).strip()) if m else ("", name.strip())


def _positioned_tracks(album_dir: Path, files: list[Path], tracklist: list[dict]) -> dict[Path, dict]:
    """Map a complete Discogs list to files by numeric positions or vinyl sides."""
    if not tracklist or len(tracklist) != len(files):
        return {}
    local = []
    for f in files:
        number = read_tags(f)["track_number"]
        if not number.isdigit():
            return {}
        local.append((track_disc(f, album_dir), int(number), f))
    local.sort(key=lambda item: (item[0], item[1], str(item[2])))
    local_positions = [(disc, number) for disc, number, _ in local]
    if len(set(local_positions)) != len(files):
        return {}

    exact = [discogs.tracklist_index_at_position(tracklist, number, disc)
             for disc, number, _ in local]
    if (all(index is not None for index in exact) and len(set(exact)) == len(files)
            and set(exact) == set(range(len(tracklist)))
            and all(tracklist[index].get("title") for index in exact)):
        return {item[2]: tracklist[index] for item, index in zip(local, exact)}

    # Discogs uses A1/B1 for vinyl sides. Local files use sequential track
    # numbers, so derive their order from the side and track coordinates.
    if len({disc for disc, _, _ in local}) != 1 or [n for _, n, _ in local] != list(range(1, len(files) + 1)):
        return {}
    order = discogs.vinyl_track_order(tracklist)
    if not order or any(not entry.get("title") for entry in tracklist):
        return {}
    return {item[2]: tracklist[index] for item, index in zip(local, order)}


def _title_key(title: str) -> str:
    return normalize_title(split_featuring(title)[0])


def _comparable_title(local: str, remote: str) -> bool:
    """Accept formatting or a one-letter typo while preserving meaningful words."""
    left = _title_key(local).split()
    right = _title_key(remote).split()
    if not left or not right:
        return clean_text(local) == clean_text(remote)
    if left == right:
        return True
    if len(left) != len(right):
        return False
    mismatches = [(a, b) for a, b in zip(left, right) if a != b]
    if len(mismatches) != 1:
        return False
    a, b = sorted(mismatches[0], key=len)
    if len(a) < 4 or not (a.isalpha() and b.isalpha()):
        return False
    index = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), len(a))
    if len(a) == len(b):
        return a[index + 1:] == b[index + 1:] or (
            a[index:index + 2] == b[index:index + 2][::-1] and a[index + 2:] == b[index + 2:])
    return len(b) == len(a) + 1 and a[index:] == b[index + 1:]


def _edition_track_title(local: str, remote: str) -> str:
    """Keep established styling when Discogs only adds its standard capitals."""
    words = re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)*", remote)
    equivalent = unicodedata.normalize("NFKC", local).casefold() == unicodedata.normalize("NFKC", remote).casefold()
    if equivalent and words and all(word[0].isupper() for word in words):
        return local
    return remote


def _duration_delta(path: Path, value: str) -> float | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?::[0-5]\d){1,2}", value.strip()):
        return None
    expected = 0
    for part in value.split(":"):
        expected = expected * 60 + int(part)
    audio = AudioFile(path)
    actual = getattr(getattr(audio, "info", None), "length", 0)
    return abs(actual - expected) if actual and expected else None


def _confirmed_tracks(album_dir: Path, files: list[Path], tracklist: list[dict],
                      master_tracklist: list[dict]) -> dict[Path, dict]:
    """Return position-aligned metadata supported by one audio programme."""
    mapped = _positioned_tracks(album_dir, files, tracklist)
    confirmed = {}
    master_positions = None
    for path, track in mapped.items():
        local, remote = read_tags(path)["title"], track["title"]
        delta = _duration_delta(path, track.get("duration", ""))
        # Catalogue durations are rounded. A large difference suggests a different cut.
        if delta is not None and delta > 5:
            return {}
        local_key, remote_key = _title_key(local), _title_key(remote)
        if not is_generic_title(local):
            if not (local_key or remote_key) and clean_text(local) != clean_text(remote):
                return {}
            if local_key != remote_key:
                if not _comparable_title(local, remote):
                    return {}
                if master_positions is None:
                    master_positions = _positioned_tracks(album_dir, files, master_tracklist)
                master_title = master_positions.get(path, {}).get("title", "")
                # Timings link the recording. Agreement with the master guards
                # against misspellings introduced on an individual edition.
                if delta is None or delta > 2 or _title_key(master_title) != remote_key:
                    continue
        confirmed[path] = {**track, "title": _edition_track_title(local, remote)}
    return confirmed


def _disc_folder(file_path: Path, album_dir: Path, discs: list[int]) -> Path:
    """Relative folder a track belongs in: "CD<n>" on a multi-disc album, else the album itself."""
    return Path(f"CD{track_disc(file_path, album_dir)}") if len(discs) > 1 else Path(".")


def album_needs_review(album_dir: Path) -> bool:
    """
    Cheap local check for albums whose title or year probably need a Discogs
    lookup: junk in the title, folder/tag mismatch, missing year or tags, wrong dash.
    """
    album_tag, year_tag = album_tag_values(album_audio_files(album_dir))
    year, title = _split_library_dir(album_dir.name)
    return (
        not _LIBRARY_DIR_RE.match(album_dir.name)
        or clean_folder_name(title) != title
        or not album_tag
        or album_tag != album_tag.strip()
        or clean_folder_name(album_tag) != album_tag
        or sanitize_path_component(album_tag) != title
        or (year_tag and year_tag != year)
    )


def _tags_with(file_path: Path, overrides: Optional[dict] = None) -> dict[str, str]:
    """read_tags() with planned changes applied ({field: new values} in FLAC field names)."""
    tags = read_tags(file_path)
    for field, key in (("artist", "artist"), ("albumartist", "album_artist"), ("title", "title")):
        if overrides and field in overrides:
            tags[key] = "; ".join(overrides[field])
    return tags


def _expected_artist_folder(files: list[Path], overrides: Optional[dict] = None) -> Optional[str]:
    if not files:
        return None
    tags = _tags_with(files[0], overrides)
    value = tags["album_artist"] or tags["artist"]
    return artist_folder_name(value) if value else None


def expected_track_filename(file_path: Path, overrides: Optional[dict] = None) -> Optional[str]:
    """"NN. Artist - Title.ext" from the (planned) tags, or None when they are incomplete."""
    tags = _tags_with(file_path, overrides)
    number = tags["track_number"].split("/")[0]
    if not (number.isdigit() and tags["artist"] and tags["title"]):
        return None
    return track_file_name(int(number), tags["artist"], tags["title"], file_path.suffix)


# ---------------------------------------------------------------------------
# Library-wide spelling decisions
# ---------------------------------------------------------------------------

def _genre_key(genre: str) -> str:
    return re.sub(r"[\s\-]+", " ", genre.casefold()).strip()


def _canonical_genres(album_dirs: list[Path]) -> dict[str, str]:
    """Genre key → preferred spelling (an alias, else the most used spelling)."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for album_dir in album_dirs:
        for f in album_audio_files(album_dir):
            for genre in split_genres(read_tags(f)["genre"]):
                counts[_genre_key(clean_text(genre))][clean_text(genre)] += 1
    canonical = {key: spellings.most_common(1)[0][0] for key, spellings in counts.items()}
    for alias, target in _GENRE_ALIASES.items():
        canonical[_genre_key(alias)] = target
    return canonical


def _canonical_artists(album_dirs: list[Path], use_discogs: bool) -> dict[str, str]:
    """
    Artist spellings to unify: lower-case name → preferred spelling, only for
    names that appear in more than one spelling (Discogs decides, else the
    most used spelling).
    """
    counts: dict[str, Counter] = defaultdict(Counter)
    for album_dir in album_dirs:
        for f in album_audio_files(album_dir):
            tags = read_tags(f)
            for name in split_multi_value(tags["artist"]) + split_multi_value(tags["album_artist"]):
                counts[name.lower()][name] += 1
    canonical = {}
    variants = [(key, spellings) for key, spellings in counts.items() if len(spellings) > 1]
    for index, (key, spellings) in enumerate(variants, 1):
        if len(spellings) > 1:
            if use_discogs:
                log.info("🎤 [%d/%d] Проверяю написание исполнителя: %s",
                         index, len(variants), spellings.most_common(1)[0][0])
            found = discogs.canonical_artist_name(spellings.most_common(1)[0][0]) if use_discogs else None
            canonical[key] = found if found and found.lower() == key else spellings.most_common(1)[0][0]
    return canonical


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _plan_title(album_dir: Path, files: list[Path], check_all: bool) -> dict:
    """Album title, year and folder name — from Discogs for albums that look off."""
    artist = album_dir.parent.name
    old_album, old_year = album_tag_values(files)
    folder_year, folder_title = _split_library_dir(album_dir.name)
    result = {"old_album": old_album, "new_album": old_album, "old_year": old_year,
              "new_year": old_year, "new_folder": album_dir.name, "source": "local", "note": ""}
    if not (check_all or album_needs_review(album_dir)):
        cleaned = clean_text(old_album)
        if cleaned and cleaned != old_album:  # invisible characters / stray spaces only
            result.update(new_album=cleaned, new_folder=album_folder_name(folder_year or old_year, cleaned))
        return result

    log.info("\n📀 %s / %s", artist, album_dir.name)
    positions = []
    for path in files:
        tags = read_tags(path)
        number = tags["track_number"]
        positions.append((track_disc(path, album_dir), int(number) if number.isdigit() else 0, tags["title"]))
    local_titles = [title for _, _, title in sorted(positions, key=lambda entry: entry[:2])]

    def accepts(candidate: dict) -> bool:
        remote_artist = candidate.get("artist")
        if remote_artist and not artists_match(artist, remote_artist):
            result["note"] = f"Discogs нашёл другого артиста ({remote_artist}) — проигнорирован"
            return False
        tracklist = candidate.get("tracklist") or []
        overlap, ordered = discogs.tracklist_agreement(local_titles, tracklist)
        score = 0.4 * overlap + 0.6 * ordered
        same_identity = (album_identity(album_dir.name) == album_identity(candidate.get("title", ""))
                         and (not candidate.get("year") or candidate["year"] == (folder_year or old_year)))
        if (overlap >= 0.8 and ordered < 0.95) or (
                score < 0.75 and not (same_identity and _confirmed_tracks(
                    album_dir, files, tracklist, candidate.get("master_tracklist") or []))):
            result["note"] = (f"Discogs-трек-лист слабо совпадает с файлами ({score:.0%}) — "
                              "название и год оставлены без изменений")
            return False
        return True

    found = None
    # Tags usually carry punctuation missing from the folder name.
    for query in dict.fromkeys(q for q in (old_album.strip(), folder_title) if q):
        candidate = discogs.fetch_album(artist, query, track_titles=local_titles, accept=accepts)
        if candidate and accepts(candidate):
            found = candidate
            result["note"] = ""
            break

    if found and found.get("title"):
        result.update(new_album=found["title"], new_year=found.get("year") or folder_year or old_year,
                      source="discogs", _catalogue=found)
    else:
        inferred = clean_folder_name(folder_title)
        local_title = clean_text(old_album) or strip_artist_prefix(inferred, artist) or inferred
        result.update(new_album=local_title, new_year=old_year or folder_year)
        result["note"] = result["note"] or "нет совпадения в Discogs — только локальная чистка"
    result["new_folder"] = album_folder_name(result["new_year"], result["new_album"])
    return result


def _plan_tag_fixes(album_dir: Path, files: list[Path], genres: dict[str, str],
                    artists: dict[str, str], fill_genre: Optional[str],
                    tracklist: Optional[list[dict]] = None,
                    release_types: Optional[list[str]] = None,
                    canonical_tracklist: Optional[list[dict]] = None,
                    master_tracklist: Optional[list[dict]] = None) -> list[list]:
    """
    Per-field tag corrections: [[relative file, field, old values, new values], …]
    (an empty new value list removes the field).  With *release_types* the
    tracks also get RELEASETYPE, COMPILATION and REMIXER.
    """
    fixes = []
    discs = sorted({track_disc(f, album_dir) for f in files})
    tracks_per_disc = Counter(track_disc(f, album_dir) for f in files)
    current_by_file = {f: _flac_values(f) for f in files
              if f.suffix.lower() == ".flac" or audioformat.needs_conversion(f)}
    album_artists = {tuple(current.get("artist", [])) for current in current_by_file.values()}

    # Only the explicit full Discogs check supplies canonical_tracklist.
    canonical_by_file = _confirmed_tracks(album_dir, files, canonical_tracklist or [], master_tracklist or [])

    for f in files:
        if f not in current_by_file:
            continue
        rel = str(f.relative_to(album_dir))
        current = current_by_file[f]
        wanted = {k: list(v) for k, v in current.items()}

        for field in _TEXT_FIELDS:
            if field in wanted:
                wanted[field] = [clean_text(v) for v in wanted[field] if clean_text(v)]
        if "genre" in wanted or fill_genre:
            values = wanted.get("genre") or split_multi_value(fill_genre or "")
            split = [g for v in values for g in split_genres(v)]
            wanted["genre"] = list(dict.fromkeys(genres.get(_genre_key(g), g) for g in split))
        canonical = canonical_by_file.get(f)
        credited_tracks = [canonical] if canonical else tracklist if canonical_tracklist is None else None
        local_title = (wanted.get("title") or [""])[0]
        source_title = canonical["title"] if canonical else local_title
        if source_title:
            title, credited = credit_track(source_title, wanted.get("artist", []),
                                           wanted.get("albumartist") or [album_dir.parent.name], credited_tracks,
                                           previous_title=local_title)
            wanted["title"] = [title]
            if credited:
                wanted["artist"] = credited
        for field in ("artist", "albumartist"):
            if field in wanted:
                wanted[field] = list(dict.fromkeys(artists.get(v.lower(), v) for v in wanted[field]))
        if not wanted.get("albumartist") and len(album_artists) == 1 and next(iter(album_artists)):
            wanted["albumartist"] = [artists.get(v.lower(), v) for v in next(iter(album_artists))]

        if release_types is not None:
            wanted["releasetype"] = list(release_types)
            if is_various_artists("; ".join(wanted.get("albumartist", []))):
                wanted["compilation"] = ["1"]
            remixers = track_remixers((wanted.get("title") or [""])[0], credited_tracks)
            if remixers:
                wanted["remixer"] = remixers

        # Totals describe the actual discs/tracks, not stale source metadata.
        disc = track_disc(f, album_dir)
        wanted.pop("totaltracks", None)
        wanted.pop("totaldiscs", None)
        number = (wanted.get("tracknumber") or [""])[0]
        if "/" in number:  # "3/12"
            number, _, total = number.partition("/")
            wanted["tracknumber"] = [number]
            wanted.setdefault("tracktotal", [total])
        wanted["tracktotal"] = [str(tracks_per_disc[disc])]
        wanted["discnumber"] = [str(disc)]
        wanted["disctotal"] = [str(len(discs))]

        for field in sorted(set(current) | set(wanted)):
            old, new = current.get(field, []), wanted.get(field, [])
            if old != new:
                fixes.append([rel, field, old, new])
    return fixes


def _credit_markers(file_path: Path) -> bool:
    tags = FLAC(file_path) if file_path.suffix.lower() == ".flac" else None
    return bool(tags) and has_credit_markers((tags.get("title") or [""])[0], tags.get("artist") or [])


def _cover_changes(album_dir: Path, files: list[Path], final: bytes) -> tuple[bool, list[str], list[Path], list[Path]]:
    """
    (Cover.jpg needs rewriting, stray cover files, tracks whose embedded art
    differs, disc-folder copies to write: missing ones, and copies of the old
    Cover.jpg when it changes — a disc cover unlike it was placed by hand).
    """
    cover_file = album_dir / config.COVER_NAME
    current = cover_file.read_bytes() if cover_file.exists() else None
    rewrite = current != final
    disc_covers = covers.disc_cover_files(album_dir)
    stray = [str(f.relative_to(album_dir)) for f in covers.cover_like_files(album_dir)
             if f != cover_file and f not in disc_covers]
    stale = [f for f in files if get_embedded_picture(f) != final]
    copies = [f for f in disc_covers if not f.exists() or (rewrite and f.read_bytes() == current)]
    return rewrite, stray, stale, copies


def _plan_cover(album_dir: Path, files: list[Path], artist: str, album: str) -> Optional[dict]:
    """What to do about the album's artwork, or None when it is already right."""
    final, origin, size = covers.choose_cover(album_dir, files, artist, album)
    if final is None:
        return None
    rewrite, stray, stale, copies = _cover_changes(album_dir, files, final)
    if not (rewrite or stray or stale or copies):
        return None
    final_size = covers.image_size(final)
    return {
        "origin": origin,
        "sha256": sha256(final).hexdigest(),
        "from_size": list(size) if size else None,
        "to_size": list(final_size) if final_size else None,
        "rewrite_cover": rewrite,
        "embed": len(stale),
        "disc_copies": len(copies),
        "remove": stray,
    }


def _track_signature(album_dir: Path) -> tuple:
    """Track titles and positions that identify the same programme."""
    tracks = []
    for path in album_audio_files(album_dir):
        tags = read_tags(path)
        title, number = tags["title"], tags["track_number"]
        if is_generic_title(title) or not number.isdigit() or int(number) <= 0:
            return ()
        tracks.append((track_disc(path, album_dir), int(number), normalize_title(title)))
    if len({(disc, number) for disc, number, _ in tracks}) != len(tracks):
        return ()
    return tuple(sorted(tracks))


def _plan_duplicates(album_dirs: list[Path]) -> list[dict]:
    """
    Albums present twice under one artist ("2020 - Hybrid Theory" and
    "2000 - Hybrid Theory"): the most recently added copy is kept, the
    others are planned for moving aside.
    """
    groups: dict[tuple, list[Path]] = defaultdict(list)
    by_name: dict[tuple, list[Path]] = defaultdict(list)
    for album_dir in album_dirs:
        by_name[(album_dir.parent, album_identity(album_dir.name))].append(album_dir)
    for key, copies in by_name.items():
        if len(copies) < 2:
            continue
        for album_dir in copies:
            if signature := _track_signature(album_dir):
                groups[(*key, signature)].append(album_dir)
    plan = []
    for copies in groups.values():
        if len(copies) < 2:
            continue
        keep = max(copies, key=lambda d: d.stat().st_mtime)
        for album_dir in copies:
            if album_dir == keep:
                continue
            album, year = album_tag_values(album_audio_files(album_dir))
            artist = album_dir.parent.name
            plan.append({
                "path": str(album_dir), "artist": artist,
                "old_artist_folder": artist, "new_artist_folder": artist,
                "old_folder": album_dir.name, "new_folder": album_dir.name,
                "old_album": album, "new_album": album, "old_year": year, "new_year": year,
                "source": "local", "note": f"дубль: остаётся «{keep.name}», эта копия уйдёт в replaced/",
                "duplicate_of": str(keep),
                "manifest": jobs.manifest(album_dir),
                "duplicate_manifest": jobs.manifest(keep),
            })
            log.info("  ♊ %s / %s — дубль «%s»", artist, album_dir.name, keep.name)
    return plan


def plan_library_fixes(check_all: bool = False) -> list[dict]:
    """
    Work out "before → after" for every library album without touching
    anything.  *check_all* also looks up every album's title on Discogs
    (slow) and resolves artist spelling conflicts via Discogs.
    """
    album_dirs = library_album_dirs()
    log.info("🔎 Checking %d album(s)…", len(album_dirs))
    duplicates = _plan_duplicates(album_dirs)
    removed = {Path(e["path"]) for e in duplicates}
    album_dirs = [d for d in album_dirs if d not in removed]
    genres = _canonical_genres(album_dirs)
    artists = _canonical_artists(album_dirs, use_discogs=True)

    # Expected artist folder of every album, grouped by its current folder
    expected_folders: dict[Path, set[str]] = defaultdict(set)
    for album_dir in album_dirs:
        expected_folders[album_dir.parent].add(
            _expected_artist_folder(album_audio_files(album_dir)) or album_dir.parent.name
        )

    plan = duplicates
    for index, album_dir in enumerate(album_dirs, 1):
        snapshot = jobs.manifest(album_dir)
        files = album_audio_files(album_dir)
        artist = album_dir.parent.name
        log.info("🔎 [%d/%d] %s / %s", index, len(album_dirs), artist, album_dir.name)
        title = _plan_title(album_dir, files, check_all)
        found = title.pop("_catalogue", None)

        missing_genre = not any(read_tags(f)["genre"] for f in files)
        # Release type, compilation flag and remixers are filled in once, from Discogs
        missing_type = any("releasetype" not in _flac_values(f) for f in files)
        if found is None and not check_all and (missing_genre or missing_type or any(_credit_markers(f) for f in files)):
            found = discogs.fetch_album(artist, title["new_album"],
                                        track_titles=[read_tags(f)["title"] for f in files])
            if found:
                remote_artist = found.get("artist", "")
                local_titles = [read_tags(f)["title"] for f in files]
                tracks = found.get("tracklist", [])
                if (not artists_match(artist, remote_artist)
                        or (tracks and local_titles and discogs.tracklist_score(local_titles, tracks) < 0.75)):
                    found = None
        tracklist = found.get("tracklist") if found else None
        fill_genre = found.get("genre") if found and missing_genre else None
        album_artist = (read_tags(files[0])["album_artist"] or artist) if files else artist
        release_types = discogs.album_release_types(found.get("release_types", []) if found else [],
                                                    len(files), album_artist) if missing_type else None

        tag_fixes = _plan_tag_fixes(album_dir, files, genres, artists, fill_genre,
                                    tracklist, release_types,
                                    canonical_tracklist=(tracklist or []) if check_all else None,
                                    master_tracklist=found.get("master_tracklist") if found else None)
        cover = _plan_cover(album_dir, files, artist, title["new_album"])
        convert = [[str(f.relative_to(album_dir)), audioformat.describe_format(f)]
                   for f in files if audioformat.needs_conversion(f)]

        planned: dict[str, dict] = defaultdict(dict)  # relative file → {field: new values}
        for rel, field, _, new in tag_fixes:
            planned[rel][field] = new

        file_renames = []
        discs = sorted({track_disc(f, album_dir) for f in files})
        for f in files:
            rel = f.relative_to(album_dir)
            expected = expected_track_filename(f, planned.get(str(rel))) or f.name
            if audioformat.needs_conversion(f):
                expected = str(Path(expected).with_suffix(".flac"))
            # A flat multi-disc album (disc numbers only in tags) is split into CD<n> folders
            folder = _disc_folder(f, album_dir, discs) if f.parent == album_dir else rel.parent
            target = folder / expected
            if target != rel:
                file_renames.append([str(rel), str(target)])

        new_artist_folder = _expected_artist_folder(files, planned.get(str(files[0].relative_to(album_dir)))) or artist
        # A case-only rename applies to the whole artist folder, so only do it
        # when all of its albums agree on the spelling (otherwise it would flip-flop)
        spellings = {n for n in expected_folders[album_dir.parent] if n.lower() == artist.lower()}
        if new_artist_folder.lower() == artist.lower() and len(spellings) > 1:
            new_artist_folder = artist

        unchanged = (title["new_folder"], title["new_album"], title["new_year"], new_artist_folder) == \
            (album_dir.name, title["old_album"], title["old_year"], artist)
        gain = bool(convert) or not replaygain.has_replaygain(files)
        if unchanged and not (file_renames or tag_fixes or cover or convert or gain):
            continue

        entry = {
            "path": str(album_dir),
            "manifest": snapshot,
            "artist": artist,
            "old_artist_folder": artist,
            "new_artist_folder": new_artist_folder,
            "old_folder": album_dir.name,
            **title,
            "file_renames": file_renames,
            "tag_fixes": tag_fixes,
            "cover": cover,
            "convert": convert,
            "replaygain": gain,
        }
        plan.append(entry)
        extras = [f"{len(file_renames)} файл(ов)" if file_renames else "",
                  f"{len(tag_fixes)} тег(ов)" if tag_fixes else "",
                  f"обложка ({cover['origin']})" if cover else "",
                  f"{len(convert)} → 16/44.1" if convert else "",
                  "ReplayGain" if gain else ""]
        log.info("  ✏️  %s / %s → %s / %s %s%s", artist, album_dir.name, new_artist_folder, title["new_folder"],
                 ", ".join(e for e in extras if e), f"  ({title['note']})" if title["note"] else "")
    return plan


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def _apply_tag_fixes(album_dir: Path, fixes: list[list]) -> int:
    by_file: dict[str, list] = defaultdict(list)
    for rel, field, old, new in fixes:
        by_file[rel].append((field, new))
    for rel, changes in by_file.items():
        audio = FLAC(album_dir / rel)
        for field, new in changes:
            if new:
                audio[field] = new
            elif field in audio:
                del audio[field]
        audio.save()
    return len(by_file)


def _apply_cover(album_dir: Path, files: list[Path], artist: str, album: str,
                 expected_digest: str | None) -> Optional[str]:
    """Apply the approved artwork to the staged album."""
    final, origin, _ = covers.choose_cover(album_dir, files, artist, album)
    if expected_digest is not None and (final is None or sha256(final).hexdigest() != expected_digest):
        raise ValueError("approved cover changed or became unavailable; build a new plan")
    if final is None:
        return None
    rewrite, stray, stale, copies = _cover_changes(album_dir, files, final)
    cover_file = album_dir / config.COVER_NAME
    if rewrite:
        cover_file.write_bytes(final)
    for f in copies:
        f.write_bytes(final)
    for rel in stray:
        (album_dir / rel).unlink()
    for f in stale:
        embed_picture(f, final)
    size = "×".join(map(str, covers.image_size(final) or ()))
    return f"{origin}, {size}, встроена в {len(stale)} файл(ов)" + (
        f", копий в папках дисков: {len(copies)}" if copies else "")


def _maintenance_dir() -> Path:
    return config.state_dir() / "maintenance"


def recover_maintenance() -> None:
    """Finish publication bookkeeping or restore the original album after interruption."""
    pending = _maintenance_dir() / "pending.json"
    try:
        step = json.loads(pending.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    work, target = Path(step["work"]), Path(step["target"])
    root = config.MUSIC_PATH.resolve()
    if (not work.resolve().is_relative_to((_maintenance_dir() / "work").resolve())
            or not target.resolve().is_relative_to(root)):
        raise ValueError("invalid maintenance recovery paths")
    if work.is_dir() or not target.exists():
        restore_backups(step["backups"])
        journal({**step["entry"], "status": "restored"})
    elif target.is_dir() and jobs.manifest(target) == step["prepared_manifest"]:
        journal({**step["entry"], "new_path": str(target), "archive": step["backups"][0][1]})
    else:
        raise RuntimeError("maintenance publication could not be verified; inspect maintenance/pending.json")
    pending.unlink()


def _check_album_path(path: Path) -> None:
    relative = path.resolve().relative_to(config.MUSIC_PATH.resolve())
    if (len(relative.parts) != 2 or relative.parts[0].startswith((".", "$"))
            or path.is_symlink() or path.parent.is_symlink() or not path.is_dir()):
        raise ValueError("plan path is not a library album")
    if any(p.is_symlink() for p in path.rglob("*")):
        raise ValueError("symbolic links inside an album are not supported")


def _check_entry(album_dir: Path, entry: dict) -> list:
    _check_album_path(album_dir)
    if "manifest" in entry and jobs.manifest(album_dir) != entry["manifest"]:
        raise ValueError("album files changed after planning; build a new plan")
    if album_tag_values(album_audio_files(album_dir)) != (entry["old_album"], entry["old_year"]):
        raise ValueError("album changed after planning; build a new plan")
    for rel in ([r[0] for r in entry.get("tag_fixes", [])]
                + [r[0] for r in entry.get("convert", [])]
                + [p for pair in entry.get("file_renames", []) for p in pair]):
        if Path(rel).is_absolute() or ".." in Path(rel).parts or not Path(rel).parts:
            raise ValueError("track path escapes album")
    before_tags = {rel: _flac_values(album_dir / rel)
                   for rel in {r[0] for r in entry.get("tag_fixes", [])}}
    for rel, field, old, _ in entry.get("tag_fixes", []):
        if before_tags[rel].get(field, []) != old:
            raise ValueError(f"track changed after planning: {rel}; build a new plan")
    pairs = entry.get("file_renames", [])
    sources = {src for src, _ in pairs}
    if len({dst for _, dst in pairs}) != len(pairs):
        raise ValueError("duplicate track destinations in plan")
    for src, dst in pairs:
        if not (album_dir / src).is_file() or ((album_dir / dst).exists() and dst not in sources):
            raise FileExistsError(f"track rename conflicts with current files: {src} → {dst}")
    return pairs


def _apply_to_copy(work: Path, entry: dict, pairs: list) -> None:
    converted = {}
    for rel, _ in entry.get("convert", []):
        converted[rel] = str(audioformat.convert_to_library_format(work / rel).relative_to(work))
    if converted:
        log.info("  Converted to 16/44.1: %d track(s)", len(converted))
    if tag_fixes := entry.get("tag_fixes"):
        changed = _apply_tag_fixes(work, [[converted.get(rel, rel), field, old, new]
                                         for rel, field, old, new in tag_fixes])
        log.info("  Updated %d tag(s) across %d track(s)", len(tag_fixes), changed)

    files = album_audio_files(work)
    if (entry["new_album"], entry["new_year"]) != (entry["old_album"], entry["old_year"]):
        for f in files:
            update_album_tags(f, entry["new_album"], entry["new_year"])
    if entry.get("replaygain"):
        replaygain.tag_album(files)
    if cover := entry.get("cover"):
        result = _apply_cover(work, files, entry["artist"], entry["new_album"], cover.get("sha256"))
        if result:
            log.info("  🖼️  %s", result)

    rename_files([(work / converted.get(src, src), work / dst) for src, dst in pairs])
    covers.copy_to_discs(work)


def _apply_entry(album_dir: Path, entry: dict) -> None:
    pairs = _check_entry(album_dir, entry)
    if entry.get("duplicate_of"):
        keep = Path(entry["duplicate_of"])
        _check_album_path(keep)
        if (keep.parent != album_dir.parent or keep == album_dir
                or album_identity(keep.name) != album_identity(album_dir.name)
                or not _track_signature(keep) or _track_signature(keep) != _track_signature(album_dir)
                or ("duplicate_manifest" in entry and jobs.manifest(keep) != entry["duplicate_manifest"])):
            raise ValueError("the duplicate to keep changed after planning")
        archive = archive_path(album_dir)
        archive.parent.mkdir(parents=True, exist_ok=True)
        journal({**entry, "status": "started", "archive": str(archive)})
        safe_rename(album_dir, archive)
        journal({**entry, "new_path": str(archive)})
        return

    new_artist = entry.get("new_artist_folder", entry["artist"])
    for value in (new_artist, entry["new_folder"]):
        if not value or Path(value).name != value or value.startswith((".", "$")):
            raise ValueError("invalid destination folder in plan")
    artist_dir = find_dir_case_insensitive(config.MUSIC_PATH, new_artist) or config.MUSIC_PATH / new_artist
    target = artist_dir / entry["new_folder"]
    if artist_dir.is_symlink() or target.is_symlink():
        raise ValueError("symbolic link in maintenance destination")
    if target != album_dir and target.exists():
        raise FileExistsError(f"destination already exists: {target}")

    scratch = _maintenance_dir() / "work"
    if not scratch.resolve().is_relative_to(config.state_dir().resolve()):
        raise ValueError("maintenance workspace escapes the state directory")
    if scratch.exists():
        shutil.rmtree(scratch)
    work = scratch / album_dir.name
    snapshot = jobs.manifest(album_dir)
    shutil.copytree(album_dir, work, symlinks=True)
    if any(path.is_symlink() for path in work.rglob("*")) or jobs.manifest(work) != snapshot:
        raise RuntimeError("album changed while making the working copy")
    _apply_to_copy(work, entry, pairs)
    if jobs.manifest(album_dir) != snapshot:
        raise RuntimeError("album changed during maintenance; publication cancelled")

    backups = [[str(album_dir), str(archive_path(album_dir))]]
    step = {"entry": {key: value for key, value in entry.items() if key not in {"manifest", "duplicate_manifest"}},
            "work": str(work), "target": str(target), "backups": backups,
            "prepared_manifest": jobs.manifest(work)}
    pending = _maintenance_dir() / "pending.json"
    write_json(pending, step)
    journal({**entry, "status": "started", "archive": backups[0][1]})
    publish_album(work, target, backups)
    recover_maintenance()

    # This single directory rename also updates paths for later entries in the plan.
    if artist_dir == album_dir.parent and artist_dir.name != new_artist:
        artist_target = config.MUSIC_PATH / new_artist
        safe_rename(artist_dir, artist_target)
        journal({"path": str(artist_dir), "new_path": str(artist_target), "kind": "artist-folder"})
    if artist_dir != album_dir.parent:
        try:
            album_dir.parent.rmdir()
        except OSError:
            pass  # Other albums may still use the folder.


def apply_library_fixes(plan: list[dict]) -> int:
    """Apply each album independently, preserving the original in the archive."""
    recover_maintenance()
    failed = 0
    for entry in plan:
        album_dir = Path(entry["path"])
        if not album_dir.is_dir():
            artist_dir = find_dir_case_insensitive(config.MUSIC_PATH, entry["old_artist_folder"])
            if artist_dir:
                album_dir = artist_dir / entry["old_folder"]
        log.info("Applying library plan: %s", album_dir)
        try:
            _apply_entry(album_dir, entry)
        except Exception as exc:
            recover_maintenance()
            log.error("  Failed: %s", exc)
            journal({**entry, "status": "failed", "error": str(exc)})
            failed += 1
    return failed
