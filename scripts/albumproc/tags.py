"""
Reading and writing tags and embedded pictures (FLAC, MP3, M4A; APE is
only read, before its conversion to FLAC).

Conventions:
  - artist, albumartist and genre are real multi-value fields (one FLAC
    field per value), so foobar2000 and Navidrome both split them; in the
    plain-string API below several values are joined with "; ";
  - track/disc totals use TRACKTOTAL / DISCTOTAL (the legacy TOTALTRACKS /
    TOTALDISCS spelling is removed when a file is rewritten).
"""

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mutagen.apev2 import APEv2, error as APEError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
from mutagen.mp4 import MP4, MP4Cover

from .names import split_multi_value

log = logging.getLogger(__name__)

MULTI_VALUE_FIELDS = frozenset({"artist", "albumartist", "genre"})
LEGACY_TOTAL_FIELDS = ("totaltracks", "totaldiscs")


@dataclass
class TrackMetadata:
    artist: str
    title: str
    album: str
    date: str
    genre: str
    album_artist: str
    track_number: int
    total_tracks: int
    disc_number: int = 1
    total_discs: int = 1
    release_types: tuple[str, ...] = ()  # RELEASETYPE: "album", "compilation"…
    compilation: bool = False            # Various Artists
    remixers: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_tags(file_path: Path) -> dict[str, str]:
    """
    Basic tags of an audio file as strings ("" when absent): artist, title,
    album, date, album_artist, track_number, disc_number, genre.
    """
    result = {k: "" for k in ("artist", "title", "album", "date", "album_artist", "track_number",
                              "disc_number", "genre")}
    ext = file_path.suffix.lower()
    try:
        if ext == ".flac":
            audio = FLAC(file_path)
            first = lambda key: (audio.get(key) or [""])[0]  # noqa: E731
            result.update(
                artist="; ".join(audio.get("artist") or []),
                title=first("title"),
                album=first("album"),
                date=first("date"),
                album_artist="; ".join(audio.get("albumartist") or []),
                track_number=first("tracknumber"),
                disc_number=first("discnumber").split("/")[0],
                genre="; ".join(audio.get("genre") or []),
            )
        elif ext == ".mp3":
            try:
                audio = ID3(file_path)
            except ID3NoHeaderError:
                return result
            joined = lambda key: "; ".join(audio[key].text) if key in audio else ""  # noqa: E731
            result.update(
                artist=joined("TPE1"),
                title=str(audio.get("TIT2", "")),
                album=str(audio.get("TALB", "")),
                date=str(audio.get("TDRC", "")),
                album_artist=joined("TPE2"),
                track_number=str(audio.get("TRCK", "")).split("/")[0],
                disc_number=str(audio.get("TPOS", "")).split("/")[0],
                genre=joined("TCON"),
            )
        elif ext == ".m4a":
            audio = MP4(file_path)
            first = lambda key: (audio.get(key) or [""])[0]  # noqa: E731
            trkn, disk = audio.get("trkn"), audio.get("disk")
            result.update(
                artist="; ".join(audio.get("\xa9ART") or []),
                title=first("\xa9nam"),
                album=first("\xa9alb"),
                date=first("\xa9day"),
                album_artist="; ".join(audio.get("aART") or []),
                track_number=str(trkn[0][0]) if trkn else "",
                disc_number=str(disk[0][0]) if disk else "",
                genre=first("\xa9gen"),
            )
        elif ext == ".ape":
            try:
                audio = APEv2(file_path)
            except APEError:
                return result
            text = lambda key: str(audio[key]) if key in audio else ""  # noqa: E731
            result.update(
                artist=text("Artist").replace("\x00", "; "),
                title=text("Title"),
                album=text("Album"),
                date=text("Year"),
                album_artist=text("Album Artist").replace("\x00", "; "),
                track_number=text("Track").split("/")[0],
                disc_number=text("Disc").split("/")[0],
                genre=text("Genre").replace("\x00", "; "),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("  Could not read tags from %s: %s", file_path.name, exc)
    return result


def album_tag_values(files: list[Path]) -> tuple[str, str]:
    """(album, year) tags of the first of *files*."""
    if not files:
        return "", ""
    tags = read_tags(files[0])
    return tags["album"], tags["date"][:4]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _write_flac_tags(file_path: Path, meta: TrackMetadata) -> None:
    audio = FLAC(file_path)
    audio.clear()
    tag_map = {
        "artist":      meta.artist,
        "title":       meta.title,
        "album":       meta.album,
        "date":        meta.date,
        "genre":       meta.genre,
        "albumartist": meta.album_artist,
        "tracknumber": str(meta.track_number) if meta.track_number else "",
        "tracktotal":  str(meta.total_tracks) if meta.total_tracks else "",
        "discnumber":  str(meta.disc_number),
        "disctotal":   str(meta.total_discs),
    }
    for key, value in tag_map.items():
        if value:
            # One field per artist/genre, so foobar2000 & co. see them as multi-value
            audio[key] = split_multi_value(value) if key in MULTI_VALUE_FIELDS else value
    for key, values in (("releasetype", meta.release_types), ("remixer", meta.remixers),
                        ("compilation", ("1",) if meta.compilation else ())):
        if values:
            audio[key] = list(values)
    audio.save()


def _write_mp3_tags(file_path: Path, meta: TrackMetadata) -> None:
    try:
        audio = ID3(file_path)
        audio.clear()
    except ID3NoHeaderError:
        audio = ID3()

    if meta.title:
        audio["TIT2"] = TIT2(text=[meta.title])
    if meta.artist:
        audio["TPE1"] = TPE1(text=split_multi_value(meta.artist))
    if meta.album:
        audio["TALB"] = TALB(text=[meta.album])
    if meta.date:
        audio["TDRC"] = TDRC(text=[meta.date])
    if meta.genre:
        audio["TCON"] = TCON(text=split_multi_value(meta.genre))
    if meta.album_artist:
        audio["TPE2"] = TPE2(text=split_multi_value(meta.album_artist))
    if meta.track_number:
        total = f"/{meta.total_tracks}" if meta.total_tracks else ""
        audio["TRCK"] = TRCK(text=[f"{meta.track_number}{total}"])
    if meta.disc_number:
        audio["TPOS"] = TPOS(text=[f"{meta.disc_number}/{meta.total_discs}"])
    audio.save(file_path, v2_version=4)


def _write_m4a_tags(file_path: Path, meta: TrackMetadata) -> None:
    audio = MP4(file_path)
    audio.clear()
    for key, value in (("\xa9ART", meta.artist), ("\xa9nam", meta.title), ("\xa9alb", meta.album),
                       ("\xa9day", meta.date), ("\xa9gen", meta.genre), ("aART", meta.album_artist)):
        if value:
            audio[key] = [value]
    audio["trkn"] = [(meta.track_number, meta.total_tracks)]
    audio["disk"] = [(meta.disc_number, meta.total_discs)]
    audio.save()


_TAG_WRITERS = {".flac": _write_flac_tags, ".mp3": _write_mp3_tags, ".m4a": _write_m4a_tags}


def write_tags(file_path: Path, meta: TrackMetadata) -> None:
    """Replace all tags of *file_path* with *meta* (embedded pictures are kept)."""
    writer = _TAG_WRITERS.get(file_path.suffix.lower())
    if writer is None:
        log.debug("  ⏭️  Unsupported format for tagging: %s", file_path.name)
        return
    try:
        writer(file_path, meta)
    except Exception as exc:  # noqa: BLE001
        log.error("  ❌ Failed to write tags for %s: %s", file_path.name, exc)


def update_album_tags(file_path: Path, album: str, year: str) -> None:
    """Rewrite only the album title and year tags of *file_path*."""
    ext = file_path.suffix.lower()
    if ext == ".flac":
        audio = FLAC(file_path)
        audio["album"] = album
        if year:
            audio["date"] = year
        audio.save()
    elif ext == ".mp3":
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            audio = ID3()
        audio["TALB"] = TALB(text=[album])
        if year:
            audio["TDRC"] = TDRC(text=[year])
        audio.save(file_path, v2_version=4)
    elif ext == ".m4a":
        audio = MP4(file_path)
        audio["\xa9alb"] = [album]
        if year:
            audio["\xa9day"] = [year]
        audio.save()


# ---------------------------------------------------------------------------
# Embedded pictures
# ---------------------------------------------------------------------------

def get_embedded_picture(file_path: Path) -> Optional[bytes]:
    """Front cover (or the first picture) embedded in *file_path*."""
    ext = file_path.suffix.lower()
    try:
        if ext == ".flac":
            pictures = FLAC(file_path).pictures
            front = [p for p in pictures if p.type == 3] or pictures
            return front[0].data if front else None
        if ext == ".mp3":
            frames = [f for f in ID3(file_path).values() if isinstance(f, APIC)]
            front = [f for f in frames if f.type == 3] or frames
            return front[0].data if front else None
        if ext == ".m4a":
            covers = MP4(file_path).get("covr")
            return bytes(covers[0]) if covers else None
        if ext == ".ape":
            # Binary item: "<file name>\0<image data>"
            cover = APEv2(file_path).get("Cover Art (Front)")
            return cover.value.split(b"\x00", 1)[1] if cover else None
    except Exception as exc:  # noqa: BLE001
        log.debug("  Could not read picture from %s: %s", file_path.name, exc)
    return None


def embed_picture(file_path: Path, jpeg: bytes) -> None:
    """Replace all embedded pictures of *file_path* with *jpeg* as the front cover."""
    ext = file_path.suffix.lower()
    if ext == ".flac":
        from PIL import Image  # lazy: only needed when embedding

        width, height = Image.open(io.BytesIO(jpeg)).size
        audio = FLAC(file_path)
        audio.clear_pictures()
        pic = Picture()
        pic.type, pic.mime, pic.desc = 3, "image/jpeg", ""
        pic.width, pic.height, pic.depth = width, height, 24
        pic.data = jpeg
        audio.add_picture(pic)
        audio.save()
    elif ext == ".mp3":
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            audio = ID3()
        audio.delall("APIC")
        audio["APIC:"] = APIC(type=3, mime="image/jpeg", desc="Cover", data=jpeg)
        audio.save(file_path, v2_version=4)
    elif ext == ".m4a":
        audio = MP4(file_path)
        audio["covr"] = [MP4Cover(jpeg, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()


def embed_picture_in_all(files: list[Path], jpeg: bytes) -> int:
    """Embed *jpeg* into every file; returns how many succeeded."""
    embedded = 0
    for file_path in files:
        try:
            embed_picture(file_path, jpeg)
            embedded += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("  ⚠️  Could not embed cover in %s: %s", file_path.name, exc)
    return embedded
