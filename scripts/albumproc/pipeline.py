"""
Processing of new downloads (any source): album folder → tagged, named album in the library.

Supported download layouts:
  <DOWNLOADS_PATH>/<Artist>/<Album>   (nested)
  <DOWNLOADS_PATH>/<Album>            (flat)
"""

import logging
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import audioformat, config, covers, cue, discogs, quality, replaygain, upconvert
from .files import (
    album_audio_files, disc_dir_number, find_or_create_artist_dir, is_album_dir, move_aside,
    normalize_permissions, remove_non_music_files, safe_rename,
)
from .names import (
    TITLE_MIN_SIMILARITY, album_folder_name, album_identity, credit_name, credit_track, clean_folder_name,
    extract_year_from_folder, is_contaminated_artist, is_generic_title, is_various_artists, name_key,
    split_genres, split_multi_value, strip_artist_prefix, strip_featuring, title_similarity, track_file_name,
    track_remixers,
)
from .tags import TrackMetadata, album_tag_values, embed_picture_in_all, read_tags, write_tags

log = logging.getLogger(__name__)


@dataclass
class AlbumCandidate:
    """A raw album folder discovered in the downloads directory."""
    path: Path
    artist: Optional[str]  # None when the album sits directly under DOWNLOADS_PATH


@dataclass
class AlbumInfo:
    artist: str
    year: str
    album: str
    genre: str = ""
    tracklist: list[dict] = field(default_factory=list)  # [{position, title}] from Discogs
    download_artist: str = ""  # the download's shorter credit when Discogs lists more artists
    release_types: list[str] = field(default_factory=list)  # RELEASETYPE from Discogs


@dataclass
class RunState:
    """What a processing run reports at the end."""
    flagged: list[str] = field(default_factory=list)   # suspected upconverts, for the summary
    deferred: list[dict] = field(default_factory=list)  # left in downloads for the bot to ask about


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_new_albums() -> list[AlbumCandidate]:
    """Album folders in DOWNLOADS_PATH (nested or flat layout; hidden folders ignored)."""
    if not config.DOWNLOADS_PATH.exists():
        log.warning("⚠️  Downloads path does not exist: %s", config.DOWNLOADS_PATH)
        return []

    candidates = []
    for entry in sorted(config.DOWNLOADS_PATH.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "$")):
            continue
        if is_album_dir(entry):
            candidates.append(AlbumCandidate(path=entry, artist=None))
        else:
            for sub in sorted(entry.iterdir()):
                if sub.is_dir() and not sub.name.startswith(".") and is_album_dir(sub):
                    candidates.append(AlbumCandidate(path=sub, artist=entry.name))
    return candidates


def candidate_from_path(path: Path) -> AlbumCandidate:
    """Candidate for an album folder given on the command line."""
    artist = None if path.parent.resolve() == config.DOWNLOADS_PATH.resolve() else path.parent.name
    return AlbumCandidate(path=path, artist=artist)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _existing_genre(album_path: Path) -> str:
    files = album_audio_files(album_path)
    return "; ".join(split_genres(read_tags(files[0])["genre"])) if files else ""


def _is_part_of(artist: str, discogs_artist: str) -> bool:
    """True when every artist in *artist* is also credited in *discogs_artist* (case aside)."""
    if credit_name(discogs_artist).lower() == credit_name(artist).lower():
        return True
    credited = {name.lower() for name in split_multi_value(discogs_artist)}
    if {name.lower() for name in split_multi_value(artist)} <= credited:
        return True
    # The same people listed differently: "Noisia, Calyx, TeeBee & Evol Intent"
    # as one value for Discogs' "Noisia; Calyx & Teebee; Evol Intent"
    return _name_parts(artist) <= _name_parts(discogs_artist)


def _name_parts(credit: str) -> set[str]:
    """Every name in a credit however it is joined: "A, B & C" / "A; B & C" → {a, b, c}."""
    return {name_key(n) for n in re.split(r"\s*(?:[,;&]|\band\b)\s*", credit) if n.strip()}


def _tagged_album_artist(album_path: Path, track_artist: bool = False) -> str:
    """
    Album artist (or with *track_artist* the track artist) from the
    download's tags: the value most tracks agree on, without a "feat." tail —
    some sources copy the first track's credit ("Blu Mar Ten feat. Airwalker")
    into the album artist.
    """
    values = []
    for audio_file in album_audio_files(album_path):
        tags = read_tags(audio_file)
        value = strip_featuring(tags["artist"] if track_artist else tags["album_artist"] or tags["artist"])
        if value:
            values.append(value)
    return Counter(values).most_common(1)[0][0] if values else ""


def _album_title(album_path: Path, artist: str) -> str:
    """
    The download's album title: its folder name — or its album tag when the
    folder is named after the artist alone ("Nirvana/" holding In Utero).
    """
    name = album_path.name
    if artist and name_key(strip_artist_prefix(clean_folder_name(name), artist)) == name_key(artist):
        return album_tag_values(album_audio_files(album_path))[0] or name
    return name


def _tracklist_matches(album_path: Path, tracklist: list[dict]) -> bool:
    """
    Whether a Discogs tracklist belongs to these files: at least half of the
    tracks on one side have a namesake on the other (either side may be an
    expanded edition).  Too few titled files to judge counts as a match.
    """
    local = [t for t in (read_tags(f)["title"] for f in album_audio_files(album_path)) if not is_generic_title(t)]
    remote = [t["title"] for t in tracklist if t.get("title")]
    if len(local) < 3 or not remote:
        return True

    def share(titles: list[str], pool: list[str]) -> float:
        found = sum(any(title_similarity(a, b) >= TITLE_MIN_SIMILARITY for b in pool) for a in titles)
        return found / len(titles)

    return max(share(local, remote), share(remote, local)) >= 0.5


def resolve_album_info(candidate: AlbumCandidate) -> Optional[AlbumInfo]:
    """Artist, title, year, genre and tracklist — from Discogs when possible."""
    album_path = candidate.path
    artist = candidate.artist or _tagged_album_artist(album_path)

    title = _album_title(album_path, artist)
    found = discogs.fetch_album(artist or None, title)
    if found and not _tracklist_matches(album_path, found.get("tracklist", [])):
        # Same-named album by a namesake artist: "Nirvana" (1990) by the 60s band
        log.info("  ℹ️  Discogs release %r has other tracks — ignored.", found.get("title"))
        found = None
    if found:
        discogs_artist = found.get("artist", "")
        download_artist = ""
        if discogs_artist and (not artist or _is_part_of(artist, discogs_artist)
                               or _is_part_of(_tagged_album_artist(album_path, track_artist=True), discogs_artist)):
            # Same artist(s): take Discogs' spelling and its full list of
            # collaborators, which download tags often cut down to the first one.
            # Or an alias: the store credits the real name ("Carl Finlow"),
            # the tracks and Discogs the alias the album came out under ("Silicon Scally")
            if artist and credit_name(artist).lower() != credit_name(discogs_artist).lower():
                download_artist = artist
            artist = discogs_artist
        genre = found.get("genre") or _existing_genre(album_path)
        info = AlbumInfo(
            artist=artist,
            year=found.get("year", ""),
            album=found.get("title") or clean_folder_name(title),
            genre=genre,
            tracklist=found.get("tracklist", []),
            download_artist=download_artist,
            release_types=found.get("release_types", []),
        )
        log.info("✅ Discogs match: %s – %s (%s) | genre: %s",
                 info.artist, info.album, info.year or "?", info.genre or "—")
    else:
        title = clean_folder_name(title)
        info = AlbumInfo(
            artist=artist,
            year=extract_year_from_folder(album_path.name),
            album=strip_artist_prefix(title, artist) if artist else title,
            genre=_existing_genre(album_path),
        )
        log.info("  ℹ️  No Discogs data%s.", f" — using existing genre tag: {info.genre}" if info.genre else "")

    if not info.artist:
        log.error("❌ Could not determine artist for: %s — skipping.", album_path)
        return None
    log.info("  Artist : %s", info.artist)
    log.info("  Album  : %s", info.album)
    log.info("  Year   : %s", info.year or "unknown")
    return info


def apply_cover(album_path: Path, info: AlbumInfo) -> None:
    """
    Cover.jpg + embedded art for every track.  Deezer is preferred (when it
    has this exact album); otherwise the art that came with the download.
    """
    log.info("🖼️  Fetching cover art…")
    audio_files = album_audio_files(album_path)
    existing = covers.current_cover(album_path, audio_files)
    for f in covers.cover_like_files(album_path):
        f.unlink()

    image = covers.fetch_deezer_cover(info.artist, info.album)
    if not image:
        log.info("  ℹ️  Deezer cover unavailable — using the art from the download.")
        image = existing
    if not image:
        log.warning("  ⚠️  No cover art found — Cover.jpg not created.")
        return

    jpeg = covers.normalize_cover(image)
    (album_path / config.COVER_NAME).write_bytes(jpeg)
    embedded = embed_picture_in_all(audio_files, jpeg)
    log.info("  🖼️  Cover.jpg written (%s), embedded into %d file(s).",
             "×".join(map(str, covers.image_size(jpeg) or ())), embedded)


def _expand_joint_credit(artists: list[str], album_artist: str) -> list[str]:
    """
    A track artist written as one value "Noisia & Mayhem" or "Noisia, Calyx &
    TeeBee" when the album artists are "Noisia; Mayhem" or "Noisia; Calyx &
    Teebee; …" becomes those artists; an album artist spelt otherwise in the
    track tags ("VIMES") gets the album's spelling.  A display credit next to
    the guests it lists — ["Noisia & Camo & Krooked", "Camo & Krooked"] —
    stands for the album artist.
    """
    album_artists = split_multi_value(album_artist)
    spelling = {name_key(a): a for a in album_artists}
    expanded: list[str] = []
    for value in artists:
        parts = _name_parts(value)
        joint = [a for a in album_artists if _name_parts(a) <= parts]
        others = [a for a in artists if a != value]
        if joint and others and len(parts) > 1 and all(_name_parts(a) <= parts for a in others):
            expanded += joint
        elif len(joint) > 1 and set().union(*map(_name_parts, joint)) == parts:
            expanded += joint
        else:
            expanded.append(spelling.get(name_key(value), value))
    return list(dict.fromkeys(expanded))


def _disc_of(audio_file: Path, album_path: Path) -> int:
    """Disc of a track: its disc number tag, else its disc sub-folder, else 1."""
    tagged = read_tags(audio_file)["disc_number"]
    if tagged.isdigit() and int(tagged) > 0:
        return int(tagged)
    folder = disc_dir_number(audio_file.parent) if audio_file.parent != album_path else None
    return folder or 1


def process_tracks(album_path: Path, info: AlbumInfo) -> None:
    """
    Rename and tag every audio file in *album_path*.

    Tag priority per field:
    - title:  existing tag unless missing/generic ("Track 1"), then Discogs
    - artist: existing tag unless empty or contaminated ("Artist - Title"),
              then the album artist; featured guests are added as artists
              and leave the title, remixers are removed (see credit_track)
    - disc:   the disc number tag, else the disc sub-folder (CD1, Disc 2…);
              a multi-disc album gets one "CD<n>" folder per disc
    - remixer: Discogs Remix credits, else the "(X Remix)" of the title
    - release type: Discogs format, else by length; compilation for Various Artists
    - all other fields: album-level values from AlbumInfo
    """
    audio_files = album_audio_files(album_path)
    discs = {f: _disc_of(f, album_path) for f in audio_files}
    disc_numbers = sorted(set(discs.values()))
    tracks_on_disc = Counter(discs.values())
    log.info("📝 Processing %d track(s)%s…", len(audio_files),
             f" on {len(disc_numbers)} discs" if len(disc_numbers) > 1 else "")

    release_types = discogs.album_release_types(info.release_types, len(audio_files), info.artist)

    position_on_disc: Counter = Counter()
    for audio_file in audio_files:
        existing = read_tags(audio_file)
        disc = discs[audio_file]
        position_on_disc[disc] += 1
        idx = position_on_disc[disc]

        raw_number = existing["track_number"].split("/")[0]
        track_num = int(raw_number) if raw_number.isdigit() else idx

        existing_title = existing["title"].strip()
        discogs_title = (
            discogs.resolve_track_title(info.tracklist, track_num, idx - 1, disc) if info.tracklist else None
        )
        if existing_title and not is_generic_title(existing_title):
            title = existing_title
        elif discogs_title:
            title = discogs_title
        else:
            title = f"Track {idx}"
            log.warning("  ⚠️  No title found for track %d — using placeholder.", idx)

        raw_artist = existing["artist"].strip()
        artist = info.artist if not raw_artist or is_contaminated_artist(raw_artist) else raw_artist
        title, credited = credit_track(title, split_multi_value(artist), split_multi_value(info.artist), info.tracklist)
        artist = "; ".join(_expand_joint_credit(credited, info.artist))
        if info.download_artist and artist.lower() == info.download_artist.lower():
            # A track carrying the download's cut-down credit of a joint album
            artist = info.artist

        target_dir = album_path / f"CD{disc}" if len(disc_numbers) > 1 else album_path
        target_dir.mkdir(exist_ok=True)
        audio_file = safe_rename(audio_file, target_dir / track_file_name(track_num, artist, title, audio_file.suffix))
        write_tags(audio_file, TrackMetadata(
            artist=artist,
            title=title,
            album=info.album,
            date=info.year,
            genre=info.genre,
            album_artist=info.artist,
            track_number=track_num,
            total_tracks=tracks_on_disc[disc],
            disc_number=disc,
            total_discs=len(disc_numbers),
            release_types=tuple(release_types),
            compilation=is_various_artists(info.artist),
            remixers=tuple(track_remixers(title, info.tracklist)),
        ))
        log.info("  ✅ %s", audio_file.relative_to(album_path))

    # Disc folders of the download that were renamed to CD<n> are empty now
    for sub in album_path.iterdir():
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()


def _destination(info: AlbumInfo) -> Path:
    return find_or_create_artist_dir(info.artist) / album_folder_name(info.year, info.album)


def library_copies(album_path: Path, info: AlbumInfo) -> list[Path]:
    """
    The album already in the library, maybe under another year or case
    ("2020 - Hybrid Theory" vs "2000 - Hybrid Theory").
    """
    dest = _destination(info)
    if not dest.parent.is_dir():
        return []
    return [existing for existing in sorted(dest.parent.iterdir())
            if existing.is_dir() and existing.resolve() != album_path.resolve()
            and album_identity(existing.name) == album_identity(dest.name)]


def move_to_library(album_path: Path, info: AlbumInfo) -> Path:
    """
    Move the album to <MUSIC_PATH>/<artist>/<YYYY - Album>, moving an older
    copy of it aside; returns the new path.
    """
    log.info("📦 Moving to music library…")
    dest = _destination(info)
    if album_path.resolve() == dest.resolve():
        log.info("✅ Already in destination: %s", dest)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    for existing in library_copies(album_path, info):
        aside = move_aside(existing)
        log.warning("  ⚠️  Album already in the library — old version moved to %s", aside)
    shutil.move(str(album_path), str(dest))
    normalize_permissions(dest.parent)
    log.info("✅ Moved to: %s", dest)
    return dest


def _cleanup_empty_download_dir(artist_folder: Optional[str]) -> None:
    """Remove the artist folder from downloads once it is empty."""
    if not artist_folder:
        return
    folder = config.DOWNLOADS_PATH / artist_folder
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            log.info("🗑️  Removed empty downloads dir: %s", folder)
    except Exception as exc:  # noqa: BLE001
        log.warning("⚠️  Could not remove %s: %s", folder, exc)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_album(candidate: AlbumCandidate, state: RunState) -> Optional[bool]:
    """
    Full pipeline for one album:

    0. Split a whole-album image by its CUE sheet; convert ALAC, APE, WAV
       and hi-res FLAC to FLAC 16/44.1
    1. Spectral check for lossy→lossless upconverts (UPCONVERT_ACTION)
    2. Artist, title, year, genre, tracklist (Discogs, else folder/tags)
    3. Cover art: Deezer or the download's own, normalised and embedded
    4. Remove non-music files
    5. Rename & tag all tracks, ReplayGain included; mastering passport
    6. Move to the library as <artist>/<YYYY - Album> — or, when the album is
       already there and REPLACE_ACTION is "ask", leave it in downloads with
       both passports for the user to compare
    7. Clean up the empty download folder

    Returns True on success, False on failure, None when the album was left
    in downloads for the user to decide on.
    """
    album_path = candidate.path
    log.info("\n🎵 Processing: %s", album_path)

    cue.split_album(album_path)
    files = album_audio_files(album_path)
    if any(audioformat.needs_conversion(f) for f in files):
        log.info("🎚️  Converting to FLAC 16/44.1…")
        audioformat.convert_album(files)

    report = upconvert.check_upconvert(album_path)
    if report:
        upconvert.log_upconvert_report(report)
        if report.verdict == "lossy":
            state.flagged.append(f"{album_path.name} — {upconvert.describe(report)}")
            if config.UPCONVERT_ACTION == "skip":
                log.error("⛔ Апконверт — альбом оставлен в загрузках: %s", album_path)
                return False
            if config.UPCONVERT_ACTION == "ask":
                log.warning("⏸️  Апконверт — альбом ждёт решения: %s", album_path)
                state.deferred.append(upconvert.deferred_entry(album_path, report))
                return None

    info = resolve_album_info(candidate)
    if info is None:
        return False

    apply_cover(album_path, info)
    log.info("🧹 Cleaning non-music files…")
    remove_non_music_files(album_path)
    process_tracks(album_path, info)
    covers.copy_to_discs(album_path)  # the disc folders exist only now
    replaygain.tag_album(album_audio_files(album_path))
    measured = quality.passport(album_path)

    copies = library_copies(album_path, info)
    if copies and config.REPLACE_ACTION == "ask":
        old = quality.passport(copies[0])
        log.warning("⏸️  Альбом уже есть в медиатеке — ждёт решения: %s", copies[0].relative_to(config.MUSIC_PATH))
        log.info("   было:  %s\n   новое: %s", quality.summary(old), quality.summary(measured))
        state.deferred.append({
            "kind": "replace",
            "path": str(album_path),
            "existing": str(copies[0].relative_to(config.MUSIC_PATH)),
            "old": quality.summary(old),
            "new": quality.summary(measured),
        })
        return None

    dest = move_to_library(album_path, info)
    quality.remember(dest, measured)
    for line in quality.describe(measured):
        log.info(line)
    _cleanup_empty_download_dir(candidate.artist)

    log.info("✅ Album processed successfully!")
    return True
