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
  - cover: Cover.jpg at COVER_SIZE², the same image embedded in every track,
    no stray cover files; small covers are upgraded from Deezer when the
    same artwork exists there in a higher resolution
  - audio format: FLAC that isn't 16/44.1 is converted
  - ReplayGain tags (track and album) where missing or after a conversion
  - RELEASETYPE, COMPILATION and REMIXER where missing (Discogs, else by length)
  - duplicates: one album in two folders (another year or case) — the
    older copy is moved aside
  - track file names, album folder and artist folder derived from the tags
"""

import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC

from . import audioformat, config, covers, discogs, replaygain
from .files import (
    album_audio_files, find_dir_case_insensitive, journal, library_album_dirs, move_aside,
    safe_rename,
)
from .names import (
    album_folder_name, album_identity, artist_folder_name, credit_track, has_credit_markers, artists_match, clean_folder_name, clean_text,
    is_various_artists, sanitize_path_component, split_genres, split_multi_value, strip_artist_prefix,
    track_file_name, track_remixers,
)
from .tags import album_tag_values, embed_picture, get_embedded_picture, read_tags, update_album_tags

log = logging.getLogger(__name__)

_LIBRARY_DIR_RE = re.compile(r"^(\d{4}) - (.+)$")
_LOOSE_YEAR_DIR_RE = re.compile(r"^(\d{4})\s*[-–—]\s*(.+)$")
_DISC_DIR_RE = re.compile(r"^CD(\d+)$")

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


def _disc_of(file_path: Path, album_dir: Path) -> int:
    """Disc of a track: its CD<n> folder, else (flat album) its disc number tag, else 1."""
    if file_path.parent != album_dir:
        m = _DISC_DIR_RE.match(file_path.parent.name)
        return int(m.group(1)) if m else 1
    tagged = read_tags(file_path)["disc_number"]
    return int(tagged) if tagged.isdigit() and int(tagged) > 0 else 1


def _disc_folder(file_path: Path, album_dir: Path, discs: list[int]) -> Path:
    """Relative folder a track belongs in: "CD<n>" on a multi-disc album, else the album itself."""
    return Path(f"CD{_disc_of(file_path, album_dir)}") if len(discs) > 1 else Path(".")


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
    for key, spellings in counts.items():
        if len(spellings) > 1:
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
    found = None
    # Tags usually carry the exact title (with ":" "/" "?"), so try them first
    for query in dict.fromkeys(q for q in (old_album.strip(), folder_title) if q):
        found = discogs.fetch_album(artist, query)
        if found:
            break
    if found and found.get("artist") and not artists_match(artist, found["artist"]):
        result["note"] = f"Discogs нашёл другого артиста ({found['artist']}) — проигнорирован"
        found = None

    if found and found.get("title"):
        result.update(new_album=found["title"], new_year=found.get("year") or folder_year or old_year,
                      source="discogs")
    else:
        local_title = clean_text(clean_folder_name(old_album.strip() or folder_title))
        result.update(new_album=strip_artist_prefix(local_title, artist) or local_title,
                      new_year=folder_year or old_year)
        result["note"] = result["note"] or "нет совпадения в Discogs — только локальная чистка"
    result["new_folder"] = album_folder_name(result["new_year"], result["new_album"])
    return result


def _plan_tag_fixes(album_dir: Path, files: list[Path], genres: dict[str, str],
                    artists: dict[str, str], fill_genre: Optional[str],
                    tracklist: Optional[list[dict]] = None,
                    release_types: Optional[list[str]] = None) -> list[list]:
    """
    Per-field tag corrections: [[relative file, field, old values, new values], …]
    (an empty new value list removes the field).  With *release_types* the
    tracks also get RELEASETYPE, COMPILATION and REMIXER.
    """
    fixes = []
    discs = sorted({_disc_of(f, album_dir) for f in files})
    tracks_per_disc = Counter(_disc_of(f, album_dir) for f in files)
    album_artists = {tuple(FLAC(f).get("artist") or []) for f in files if f.suffix.lower() == ".flac"}

    for f in files:
        if f.suffix.lower() != ".flac":
            continue
        t = FLAC(f)
        rel = str(f.relative_to(album_dir))
        current = {k.lower(): list(v) for k, v in t.items()}
        wanted = {k: list(v) for k, v in current.items()}

        for field in _TEXT_FIELDS:
            if field in wanted:
                wanted[field] = [clean_text(v) for v in wanted[field] if clean_text(v)]
        if "genre" in wanted or fill_genre:
            values = wanted.get("genre") or split_multi_value(fill_genre or "")
            split = [g for v in values for g in split_genres(v)]
            wanted["genre"] = list(dict.fromkeys(genres.get(_genre_key(g), g) for g in split))
        if wanted.get("title"):
            title, credited = credit_track(wanted["title"][0], wanted.get("artist", []),
                                           wanted.get("albumartist") or [album_dir.parent.name], tracklist)
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
            remixers = track_remixers((wanted.get("title") or [""])[0], tracklist)
            if remixers:
                wanted["remixer"] = remixers

        # Totals: one convention, values from the legacy fields when needed
        disc = _disc_of(f, album_dir)
        legacy_tracks = wanted.pop("totaltracks", None)
        legacy_discs = wanted.pop("totaldiscs", None)
        number = (wanted.get("tracknumber") or [""])[0]
        if "/" in number:  # "3/12"
            number, _, total = number.partition("/")
            wanted["tracknumber"] = [number]
            wanted.setdefault("tracktotal", [total])
        wanted["tracktotal"] = wanted.get("tracktotal") or legacy_tracks or [str(tracks_per_disc[disc])]
        wanted["discnumber"] = [str(disc)]
        wanted["disctotal"] = [str(len(discs))] if len(discs) > 1 else (
            wanted.get("disctotal") or legacy_discs or ["1"])

        for field in sorted(set(current) | set(wanted)):
            old, new = current.get(field, []), wanted.get(field, [])
            if old != new:
                fixes.append([rel, field, old, new])
    return fixes


def _credit_markers(file_path: Path) -> bool:
    tags = FLAC(file_path) if file_path.suffix.lower() == ".flac" else None
    return bool(tags) and has_credit_markers((tags.get("title") or [""])[0], tags.get("artist") or [])


def _choose_cover(album_dir: Path, files: list[Path], artist: str, album: str) -> tuple[Optional[bytes], str, Optional[tuple]]:
    """
    (final JPEG, origin, original size) for the album: its own artwork,
    upgraded from Deezer when the current one is missing, small or not
    square and Deezer has the same image in a higher resolution.
    """
    source = covers.current_cover(album_dir, files)
    size = covers.image_size(source) if source else None
    chosen, origin = source, "local"

    if size is None or not covers.is_square(size) or min(size) < config.COVER_SIZE:
        better = covers.fetch_deezer_cover(artist, album)
        better_size = covers.image_size(better) if better else None
        if better_size and covers.is_square(better_size) and (size is None or min(better_size) > min(size)):
            similarity = covers.image_similarity(source, better) if source else None
            if similarity is None or similarity >= covers.SAME_IMAGE_MIN_SIMILARITY:
                chosen, origin = better, "deezer"
            else:
                log.info("  ℹ️  Deezer cover differs from the current one (%.2f) — kept.", similarity)
    final = covers.normalize_cover(chosen) if chosen else None
    return final, origin, size


def _cover_changes(album_dir: Path, files: list[Path], final: bytes) -> tuple[bool, list[str], list[Path]]:
    """(Cover.jpg needs rewriting, stray cover files, tracks whose embedded art differs)."""
    cover_file = album_dir / config.COVER_NAME
    rewrite = not (cover_file.exists() and cover_file.read_bytes() == final)
    stray = [str(f.relative_to(album_dir)) for f in covers.cover_like_files(album_dir) if f != cover_file]
    stale = [f for f in files if get_embedded_picture(f) != final]
    return rewrite, stray, stale


def _plan_cover(album_dir: Path, files: list[Path], artist: str, album: str) -> Optional[dict]:
    """What to do about the album's artwork, or None when it is already right."""
    final, origin, size = _choose_cover(album_dir, files, artist, album)
    if final is None:
        return None
    rewrite, stray, stale = _cover_changes(album_dir, files, final)
    if not (rewrite or stray or stale):
        return None
    final_size = covers.image_size(final)
    return {
        "origin": origin,
        "from_size": list(size) if size else None,
        "to_size": list(final_size) if final_size else None,
        "rewrite_cover": rewrite,
        "embed": len(stale),
        "remove": stray,
    }


def _plan_duplicates(album_dirs: list[Path]) -> list[dict]:
    """
    Albums present twice under one artist ("2020 - Hybrid Theory" and
    "2000 - Hybrid Theory"): the most recently added copy is kept, the
    others are planned for moving aside.
    """
    groups: dict[tuple, list[Path]] = defaultdict(list)
    for album_dir in album_dirs:
        groups[(album_dir.parent, album_identity(album_dir.name))].append(album_dir)
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
    for album_dir in album_dirs:
        files = album_audio_files(album_dir)
        artist = album_dir.parent.name
        title = _plan_title(album_dir, files, check_all)

        missing_genre = not any(read_tags(f)["genre"] for f in files)
        # Release type, compilation flag and remixers are filled in once, from Discogs
        missing_type = not all("releasetype" in FLAC(f) for f in files if f.suffix.lower() == ".flac")
        found = None
        if missing_genre or missing_type or any(_credit_markers(f) for f in files):
            found = discogs.fetch_album(artist, title["new_album"])
            if found and not artists_match(artist, found.get("artist", "")):
                found = None
        fill_genre = found.get("genre") if found and missing_genre else None
        album_artist = "; ".join(FLAC(files[0]).get("albumartist") or [artist]) if files else artist
        release_types = discogs.album_release_types(found.get("release_types", []) if found else [],
                                                    len(files), album_artist) if missing_type else None

        tag_fixes = _plan_tag_fixes(album_dir, files, genres, artists, fill_genre,
                                    found.get("tracklist") if found else None, release_types)
        cover = _plan_cover(album_dir, files, artist, title["new_album"])
        convert = [[str(f.relative_to(album_dir)), audioformat.describe_format(f)]
                   for f in files if audioformat.needs_conversion(f)]

        planned: dict[str, dict] = defaultdict(dict)  # relative file → {field: new values}
        for rel, field, _, new in tag_fixes:
            planned[rel][field] = new

        file_renames = []
        discs = sorted({_disc_of(f, album_dir) for f in files})
        for f in files:
            rel = f.relative_to(album_dir)
            expected = expected_track_filename(f, planned.get(str(rel))) or f.name
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


def _apply_cover(album_dir: Path, files: list[Path], artist: str, album: str) -> Optional[str]:
    """Re-decide the cover (images are not stored in plans) and apply it."""
    final, origin, _ = _choose_cover(album_dir, files, artist, album)
    if final is None:
        return None
    rewrite, stray, stale = _cover_changes(album_dir, files, final)
    cover_file = album_dir / config.COVER_NAME
    if rewrite:
        if cover_file.exists():
            move_aside(cover_file)
        cover_file.write_bytes(final)
    for rel in stray:
        move_aside(album_dir / rel)
    for f in stale:
        embed_picture(f, final)
    size = "×".join(map(str, covers.image_size(final) or ()))
    return f"{origin}, {size}, встроена в {len(stale)} файл(ов)"


def apply_library_fixes(plan: list[dict]) -> int:
    """
    Apply a plan from :func:`plan_library_fixes`: album tags, tag hygiene,
    cover, track file names, album folder and artist folder.  Every change
    is journaled with the old values.  Returns the number of failed albums.
    """
    failed = 0
    for entry in plan:
        album_dir = Path(entry["path"])
        if not album_dir.is_dir():
            # An earlier entry may have renamed the artist folder (case change)
            artist_dir = find_dir_case_insensitive(config.MUSIC_PATH, entry["old_artist_folder"])
            album_dir = artist_dir / entry["old_folder"] if artist_dir else album_dir
        new_artist = entry.get("new_artist_folder", entry["artist"])
        log.info("\n📀 %s / %s → %s / %s", entry["artist"], entry["old_folder"], new_artist, entry["new_folder"])
        if not album_dir.is_dir():
            log.warning("  ⚠️  Папка не найдена — пропуск.")
            failed += 1
            continue

        try:
            if entry.get("duplicate_of"):
                target = move_aside(album_dir)
                log.info("  ♊ Дубль убран → %s", target)
                journal({**entry, "new_path": str(target)})
                continue

            if entry.get("convert"):
                for rel, fmt in entry["convert"]:
                    audioformat.convert_to_library_format(album_dir / rel)
                log.info("  🎚️  Сконвертировано в 16/44.1: %d файл(ов)", len(entry["convert"]))

            files = album_audio_files(album_dir)
            if (entry["new_album"], entry["new_year"]) != (entry["old_album"], entry["old_year"]):
                for f in files:
                    update_album_tags(f, entry["new_album"], entry["new_year"])
                log.info("  🏷️  Теги: album=%r, date=%r", entry["new_album"], entry["new_year"])

            if entry.get("tag_fixes"):
                changed = _apply_tag_fixes(album_dir, entry["tag_fixes"])
                log.info("  🏷️  Исправлено полей: %d в %d файл(ах)", len(entry["tag_fixes"]), changed)

            if entry.get("replaygain"):
                replaygain.tag_album(album_audio_files(album_dir))

            if entry.get("cover"):
                result = _apply_cover(album_dir, files, entry["artist"], entry["new_album"])
                if result:
                    log.info("  🖼️  Обложка: %s", result)

            renamed = 0
            for old_rel, new_rel in entry.get("file_renames", []):
                src, dst = album_dir / old_rel, album_dir / new_rel
                if not src.exists() or dst.exists():
                    log.warning("  ⚠️  Файл пропущен: %s", old_rel)
                    continue
                dst.parent.mkdir(exist_ok=True)
                src.rename(dst)
                renamed += 1
            if renamed:
                log.info("  📝 Файлов переименовано: %d", renamed)

            # Artist folder: reuse an existing one; a case-only difference renames it
            artist_dir = find_dir_case_insensitive(config.MUSIC_PATH, new_artist)
            if artist_dir and artist_dir.name != new_artist and artist_dir == album_dir.parent:
                artist_dir = artist_dir.rename(config.MUSIC_PATH / new_artist)
                album_dir = artist_dir / album_dir.name
                log.info("  📂 Папка артиста переименована → %s", new_artist)
            artist_dir = artist_dir or config.MUSIC_PATH / new_artist

            target = artist_dir / entry["new_folder"]
            if target != album_dir:
                if target.exists():
                    raise FileExistsError(f"папка {target} уже существует")
                old_parent = album_dir.parent
                artist_dir.mkdir(parents=True, exist_ok=True)
                safe_rename(album_dir, target)
                log.info("  📁 Папка альбома → %s", target.relative_to(config.MUSIC_PATH))
                if old_parent != config.MUSIC_PATH and not any(old_parent.iterdir()):
                    old_parent.rmdir()
        except Exception as exc:  # noqa: BLE001
            log.error("  ❌ Ошибка: %s", exc)
            failed += 1
            continue

        journal({**entry, "new_path": str(target)})
    return failed
