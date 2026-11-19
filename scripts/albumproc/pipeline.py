"""
Processing of new downloads (any source): album folder → tagged, named album in the library.

Supported download layouts:
  <DOWNLOADS_PATH>/<Artist>/<Album>   (nested)
  <DOWNLOADS_PATH>/<Album>            (flat)
"""

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import audioformat, config, covers, cue, deezer_dl, discogs, jobs, quality, replaygain, upconvert
from .files import (
    album_audio_files, archive_path, find_or_create_artist_dir, is_album_dir,
    normalize_permissions, publish_album, remove_non_music_files, rename_files, restore_backups,
)
from .names import (
    TITLE_MIN_SIMILARITY, album_folder_name, album_identity, credit_name, credit_track, clean_folder_name,
    extract_year_from_folder, is_contaminated_artist, is_generic_title, is_various_artists, name_key,
    split_genres, split_multi_value, strip_artist_prefix, strip_featuring, title_similarity, track_file_name,
    track_remixers,
)
from .tags import TrackMetadata, album_tag_values, embed_picture_in_all, read_tags, track_disc, write_tags

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
    from_discogs: bool = False  # False: title and artist are the download's own, no year or genre
    release_id: Optional[int] = None
    master_id: Optional[int] = None
    lookup_status: str = 'unmatched'


@dataclass
class RunState:
    """What a processing run reports at the end."""
    flagged: list[str] = field(default_factory=list)   # suspected upconverts, for the summary
    deferred: list[dict] = field(default_factory=list)  # left in downloads for the bot to ask about
    processed: list[dict] = field(default_factory=list)  # moved to the library, for the bot's opinion buttons
    not_found: list[str] = field(default_factory=list)  # filed without Discogs data, for the summary
    lookups: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_new_albums() -> list[AlbumCandidate]:
    """
    Album folders in DOWNLOADS_PATH (nested or flat layout; hidden folders
    ignored), plus the albums of deezer-downloader — unless it is still
    downloading: a half-written album must not be filed.
    """
    if not config.DOWNLOADS_PATH.exists():
        log.warning("⚠️  Downloads path does not exist: %s", config.DOWNLOADS_PATH)
        return []

    candidates = []
    deezer_albums = config.DEEZER_DOWNLOADER_DIR / "albums"
    if deezer_albums.is_dir():
        if deezer_dl.busy():
            log.info("⏳ deezer-downloader is still downloading — its albums are left for the next run.")
        else:
            candidates += [AlbumCandidate(path=d, artist=None) for d in sorted(deezer_albums.iterdir())
                           if d.is_dir() and not d.name.startswith(".") and is_album_dir(d)]
    for entry in sorted(config.DOWNLOADS_PATH.iterdir()):
        # deezer-downloader's folder: its albums are taken above, songs and playlists are not albums
        if (not entry.is_dir() or entry.is_symlink() or entry.name.startswith((".", "$"))
                or entry.resolve() == config.DEEZER_DOWNLOADER_DIR.resolve()):
            continue
        if is_album_dir(entry):
            candidates.append(AlbumCandidate(path=entry, artist=None))
        else:
            for sub in sorted(entry.iterdir()):
                if sub.is_dir() and not sub.is_symlink() and not sub.name.startswith(".") and is_album_dir(sub):
                    candidates.append(AlbumCandidate(path=sub, artist=entry.name))
    return candidates


def candidate_from_path(path: Path) -> AlbumCandidate:
    """
    Candidate for an album folder given on the command line: "downloads/Artist/Album"
    names its artist, other places (deezer-downloader's albums/) do not.
    """
    parent = path.resolve().parent
    named = (parent.parent == config.DOWNLOADS_PATH.resolve()
             and parent != config.DEEZER_DOWNLOADER_DIR.resolve())
    return AlbumCandidate(path=path, artist=parent.name if named else None)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _existing_genre(album_path: Path) -> str:
    files = album_audio_files(album_path)
    return "; ".join(split_genres(read_tags(files[0])["genre"])) if files else ""


def _is_part_of(artist: str, discogs_artist: str) -> bool:
    """True when every artist in *artist* is also credited in *discogs_artist* (case aside)."""
    if not artist.strip() or not discogs_artist.strip():
        return False
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
    """Reject an edition that shares too few titles with the tagged tracks."""
    local = [t for t in (read_tags(f)["title"] for f in album_audio_files(album_path)) if not is_generic_title(t)]
    if not local or not any(t.get("title") for t in tracklist):
        return True
    overlap, _ = discogs.tracklist_agreement(local, tracklist)
    return overlap > 0.5


def resolve_album_info(candidate: AlbumCandidate) -> Optional[AlbumInfo]:
    """Artist, title, year, genre and tracklist — from Discogs when possible."""
    album_path = candidate.path
    artist = candidate.artist or _tagged_album_artist(album_path)

    title = _album_title(album_path, artist)
    local_titles = [read_tags(f)["title"] for f in album_audio_files(album_path)]
    tagged_artist = _tagged_album_artist(album_path, track_artist=True)

    def accepts(found: dict) -> bool:
        remote_artist = found.get("artist")
        local_artist = discogs.canonical_credited_artist(artist, found)
        local_tagged = discogs.canonical_credited_artist(tagged_artist, found)
        if artist and remote_artist and not (_is_part_of(local_artist, remote_artist)
                                             or _is_part_of(local_tagged, remote_artist)):
            return False
        return _tracklist_matches(album_path, found.get("tracklist", []))

    found, lookup_status = discogs.lookup_album(artist or None, title, track_titles=local_titles, accept=accepts)
    if not found and artist and tagged_artist and not _is_part_of(artist, tagged_artist):
        other, other_status = discogs.lookup_album(tagged_artist, title, track_titles=local_titles, accept=accepts)
        if other or lookup_status == "unmatched":
            found, lookup_status = other, other_status
    if found and not accepts(found):
        log.info("Discogs candidate rejected by track or artist evidence: %s", found.get("title"))
        found, lookup_status = None, "rejected"
    if found:
        discogs_artist = found.get("artist", "")
        download_artist = ""
        if discogs_artist and accepts(found):
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
            from_discogs=True,
            release_id=found.get('release_id'), master_id=found.get('master_id'),
            lookup_status=lookup_status,
        )
        log.info("✅ Discogs match: %s – %s (%s) | genre: %s",
                 info.artist, info.album, info.year or "?", info.genre or "—")
    else:
        title = clean_folder_name(title)
        info = AlbumInfo(
            artist=artist,
            year=extract_year_from_folder(album_path.name) or album_tag_values(album_audio_files(album_path))[1],
            album=strip_artist_prefix(title, artist) if artist else title,
            genre=_existing_genre(album_path),
            lookup_status=lookup_status,
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
    has this exact album); preserve existing art unless the same image can be upgraded.
    """
    log.info("🖼️  Fetching cover art…")
    audio_files = album_audio_files(album_path)
    jpeg, _, _ = covers.choose_cover(album_path, audio_files, info.artist, info.album)
    if not jpeg:
        log.warning("No cover art found.")
        return

    (album_path / config.COVER_NAME).write_bytes(jpeg)
    for f in covers.cover_like_files(album_path):
        if f != album_path / config.COVER_NAME and f.parent == album_path:
            f.unlink()
    embedded = embed_picture_in_all(audio_files, jpeg)
    if embedded != len(audio_files):
        raise OSError("could not embed the cover in every track")
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


def plan_tracks(album_path: Path, info: AlbumInfo) -> list[tuple[Path, Path, TrackMetadata]]:
    """
    Build the track metadata and naming plan without changing files.

    Tag priority per field:
    - title:  existing tag unless missing/generic ("Track 1"), then Discogs
    - artist: existing tag unless empty or contaminated ("Artist - Title"),
              then the album artist; featured guests are added as artists
              and leave the title, remixers are removed (see credit_track)
    - disc:   the disc sub-folder (CD1, Disc 2…), else the disc number tag;
              a multi-disc album gets one "CD<n>" folder per disc
    - remixer: Discogs Remix credits, else the "(X Remix)" of the title
    - release type: Discogs format, else by length; compilation for Various Artists
    - all other fields: album-level values from AlbumInfo
    """
    audio_files = album_audio_files(album_path)
    discs = {f: track_disc(f, album_path) for f in audio_files}
    disc_numbers = sorted(set(discs.values()))
    tracks_on_disc = Counter(discs.values())
    log.info("📝 Processing %d track(s)%s…", len(audio_files),
             f" on {len(disc_numbers)} discs" if len(disc_numbers) > 1 else "")

    release_types = discogs.album_release_types(info.release_types, len(audio_files), info.artist)

    allow_unpositioned = len(disc_numbers) == 1 and len(info.tracklist) == len(audio_files)
    position_on_disc: Counter = Counter()
    prepared = []
    for audio_file in audio_files:
        existing = read_tags(audio_file)
        disc = discs[audio_file]
        position_on_disc[disc] += 1
        idx = position_on_disc[disc]

        raw_number = existing["track_number"].split("/")[0]
        track_num = int(raw_number) if raw_number.isdigit() else idx

        existing_title = existing["title"].strip()
        discogs_title = (
            discogs.resolve_track_title(info.tracklist, track_num, idx - 1, disc, allow_unpositioned=allow_unpositioned)
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
        target = target_dir / track_file_name(track_num, artist, title, audio_file.suffix)
        prepared.append((audio_file, target, TrackMetadata(
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
        )))

    return prepared


def process_tracks(album_path: Path, info: AlbumInfo) -> None:
    prepared = plan_tracks(album_path, info)
    targets = [target for _, target, _ in prepared]
    if len(set(targets)) != len(targets):
        raise ValueError("several tracks would have the same file name")
    rename_files([(src, target) for src, target, _ in prepared])
    for _, audio_file, metadata in prepared:
        write_tags(audio_file, metadata)
        log.info("  ✅ %s", audio_file.relative_to(album_path))

    # Disc folders of the download that were renamed to CD<n> are empty now
    for sub in album_path.iterdir():
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()


def _destination(info: AlbumInfo) -> Path:
    folder = album_folder_name(info.year, info.album)
    if not folder or folder.startswith("."):
        raise ValueError("album does not have a usable library folder name")
    return find_or_create_artist_dir(info.artist) / folder


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


def move_to_library(album_path: Path, info: AlbumInfo, job: Optional[dict] = None,
                    expected_copies: list | None = None) -> Path:
    """
    Move the album to <MUSIC_PATH>/<artist>/<YYYY - Album>, moving an older
    copy of it aside; returns the new path.
    """
    log.info("📦 Moving to music library…")
    dest = _destination(info)
    if not dest.resolve().is_relative_to(config.MUSIC_PATH.resolve()):
        raise ValueError("destination escapes the library")
    if album_path.resolve() == dest.resolve():
        log.info("✅ Already in destination: %s", dest)
        return dest

    copies = library_copies(album_path, info)
    if dest.parent.is_symlink() or any(path.is_symlink() for path in copies):
        raise ValueError("symbolic link in library publication paths")
    if expected_copies is not None and [[str(path), jobs.manifest(path)] for path in copies] != expected_copies:
        raise RuntimeError("library copies changed during preparation")
    normalize_permissions(album_path)
    backups = [[str(path), str(archive_path(path))] for path in copies]
    if job is not None:
        job.update(status='committing', destination=str(dest), backups=backups,
                   prepared_manifest=jobs.manifest(album_path))
        jobs.save(job)
    publish_album(album_path, dest, backups)
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

def _process_album(candidate: AlbumCandidate, state: RunState, job: dict,
                   approved_copies: list | None = None) -> Optional[bool]:
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
    if album_path.resolve() in {config.DOWNLOADS_PATH.resolve(), config.MUSIC_PATH.resolve()}:
        raise ValueError("select an album folder, not the downloads or library root")
    if album_path.is_symlink() or any(p.is_symlink() for p in album_path.rglob("*")):
        raise ValueError("symbolic links inside an album are not supported")
    unsupported = [f.name for f in album_audio_files(album_path)
                   if f.suffix.lower() not in {".flac", ".mp3", ".m4a"} and not audioformat.is_lossless(f)]
    if unsupported:
        raise ValueError(f"unsupported audio container; use MP3 or M4A for lossy audio: {unsupported}")

    cue.split_album(album_path)
    files = album_audio_files(album_path)
    unexpected = set(p for p in album_path.rglob('*')
                     if p.is_file() and p.suffix.lower() in config.AUDIO_EXTENSIONS) - set(files)
    if not files or unexpected:
        raise ValueError(f"missing audio or audio in unsupported subfolders: {sorted(unexpected)}")
    low_rate_sources = {f.with_suffix(".flac") for f in files
                        if audioformat.is_lossless(f) and audioformat.sample_rate(f) < config.TARGET_RATE}
    if any(audioformat.needs_conversion(f) for f in files):
        log.info("🎚️  Converting to FLAC 16/44.1…")
        audioformat.convert_album(files)

    report = upconvert.check_upconvert(album_path, low_rate_sources=low_rate_sources)
    if report:
        upconvert.log_upconvert_report(report)
        if report.verdict == "lossy":
            state.flagged.append(f"{album_path.name} — {upconvert.describe(report)}")
            if config.UPCONVERT_ACTION == "skip":
                log.error("⛔ Апконверт — альбом оставлен в загрузках: %s", album_path)
                return False
            if config.UPCONVERT_ACTION == "ask" and not job.get('approve_upconvert'):
                log.warning("⏸️  Апконверт — альбом ждёт решения: %s", album_path)
                state.deferred.append(upconvert.deferred_entry(album_path, report))
                return None

    info = AlbumInfo(**job['metadata']) if job.get('metadata') else resolve_album_info(candidate)
    if info is None:
        return False
    state.lookups.append({'artist': info.artist, 'album': info.album, 'status': info.lookup_status})
    if info.from_discogs:
        job['metadata'] = asdict(info)
        jobs.save(job)

    apply_cover(album_path, info)
    log.info("🧹 Cleaning non-music files…")
    remove_non_music_files(album_path)
    process_tracks(album_path, info)
    covers.copy_to_discs(album_path)  # the disc folders exist only now
    files = album_audio_files(album_path)
    for path in files:
        if path.suffix.lower() != ".flac":
            audioformat.validate_audio(path)
    replaygain.tag_album(files)
    measured = quality.passport(album_path, persist=False)

    copies = library_copies(album_path, info)
    copies_snapshot = [[str(path), jobs.manifest(path)] for path in copies]
    if copies and config.REPLACE_ACTION == "ask" and approved_copies != copies_snapshot:
        old = quality.passport(copies[0])
        log.warning("⏸️  Альбом уже есть в медиатеке — ждёт решения: %s", copies[0].relative_to(config.MUSIC_PATH))
        log.info("   было:  %s\n   новое: %s", quality.summary(old), quality.summary(measured))
        state.deferred.append({
            "kind": "replace",
            "path": str(album_path),
            "existing": str(copies[0].relative_to(config.MUSIC_PATH)),
            "old": quality.summary(old),
            "new": quality.summary(measured),
            "copies": copies_snapshot,
        })
        return None

    if not Path(job['source']).is_dir() or jobs.manifest(Path(job['source'])) != job['source_manifest']:
        raise RuntimeError('source changed during preparation; publication cancelled')
    job.update(measured=measured, name=f"{info.artist.split(';')[0]} — {info.album}")
    dest = move_to_library(album_path, info, job, copies_snapshot)
    quality.remember(dest, measured)
    for line in quality.describe(measured):
        log.info(line)
    if not info.from_discogs:
        state.not_found.append(f"{info.artist.split(';')[0]} — {info.album}")
    log.info("✅ Album processed successfully!")
    return True


def process_album(candidate: AlbumCandidate, state: RunState, decision: dict | None = None) -> Optional[bool]:
    source = candidate.path.resolve()
    music = config.MUSIC_PATH.resolve()
    downloads = config.DOWNLOADS_PATH.resolve()
    if source.is_relative_to(music) or music.is_relative_to(source):
        raise ValueError('use library maintenance for albums already inside the library')
    if downloads.is_relative_to(source) or candidate.path.is_symlink() or any(
            p.is_symlink() for p in source.rglob('*')):
        raise ValueError('invalid album root or symbolic link in source')
    job = jobs.open_job(source)
    if job.get('status') == 'deleting':
        jobs.finish(job, 'deleted')
        return True
    # A rename into the library is atomic; a restart after that point only needs cleanup.
    if job.get('status') == 'committing':
        destination = Path(job['destination'])
        if destination.is_dir() and not Path(job['work']).exists():
            if jobs.manifest(destination) != job['prepared_manifest']:
                raise RuntimeError('published album changed; source retained for manual review')
            quality.remember(destination, job['measured'])
            jobs.finish(job)
            state.processed.append({'kind': 'processed', 'path': str(destination), 'name': job['name']})
            return True
        restore_backups(job.get('backups', []))
        if not source.is_dir():
            raise FileNotFoundError(f'import source is missing: {source}')
    if source.is_relative_to((config.DEEZER_DOWNLOADER_DIR / 'albums').resolve()):
        if deezer_dl.busy():
            raise deezer_dl.DeezerError('deezer-downloader занят или недоступен; импорт остановлен')
        deezer_dl.ensure_complete(source)
        # The completeness gate may have saved the source's Deezer identity.
        job = jobs.open_job(source)
    if decision is not None:
        jobs.check_decision(job, decision)
    if job.get('source_manifest') is not None and jobs.manifest(source) != job['source_manifest']:
        job.update(approve_upconvert=False, approve_replace=False, deferred=None, metadata=None)
    if decision is not None and decision['kind'] == 'upconvert':
        job['approve_upconvert'] = True
    if decision is not None and decision['kind'] == 'replace':
        approved_copies = decision.get('copies')
    else:
        approved_copies = None
    try:
        work = jobs.prepare(job)
        if (source.is_relative_to((config.DEEZER_DOWNLOADER_DIR / 'albums').resolve())
                and job['source_manifest'] != job.get('deezer_manifest')):
            raise deezer_dl.DeezerError('файлы Deezer изменились после проверки полноты')
        if decision is not None and job['source_manifest'] != decision['source_manifest']:
            job.update(approve_upconvert=False, metadata=None)
            raise ValueError('Загрузка изменилась — запустите /newalbum ещё раз.')
        before = len(state.deferred)
        result = _process_album(AlbumCandidate(work, candidate.artist), state, job, approved_copies)
        if result is True:
            jobs.finish(job)
            state.processed.append({"kind": "processed", "path": job["destination"], "name": job["name"]})
            _cleanup_empty_download_dir(candidate.artist)
        elif result is None:
            entry = state.deferred[before]
            entry['path'] = str(source)
            entry['source_manifest'] = job['source_manifest']
            job.update(status='waiting', deferred=entry)
            jobs.save(job)
        else:
            job.update(status='failed')
            jobs.save(job)
        return result
    except Exception as exc:
        if job.get('status') != 'committing':
            job.update(status='failed', error=str(exc))
            jobs.save(job)
        raise
