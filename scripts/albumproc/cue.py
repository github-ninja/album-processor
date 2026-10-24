"""
Splitting of a whole-album image (one FLAC/APE/WAV/ALAC file plus a CUE
sheet) into tracks.

Tracks are cut at their INDEX 01 marks, so a pregap (INDEX 00) stays at the
end of the previous track, as on the CD; the first track starts at the
beginning of the image.  The audio is copied sample for sample at the
image's own resolution — conversion to 16/44.1, when needed, comes later.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import av
import soundfile as sf
from mutagen.flac import FLAC

from . import config
from .audioformat import bits, decoded_blocks, is_lossless
from .files import disc_dir_number, get_audio_files
from .tags import read_tags

log = logging.getLogger(__name__)

_CD_FRAMES_PER_SECOND = 75
_COMMAND_RE = re.compile(r'^\s*(?:REM\s+)?([A-Z]+)\s+(?:"([^"]*)"|(\S+))', re.IGNORECASE)
_INDEX_RE = re.compile(r"^\s*INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", re.IGNORECASE)


@dataclass
class CueTrack:
    number: int
    title: str = ""
    performer: str = ""
    start: Optional[int] = None  # INDEX 01 in CD frames (1/75 s)


@dataclass
class CueSheet:
    files: list[str] = field(default_factory=list)
    title: str = ""
    performer: str = ""
    date: str = ""
    genre: str = ""
    disc: str = ""
    tracks: list[CueTrack] = field(default_factory=list)


def _decode(data: bytes) -> str:
    """CUE sheets come in UTF-8 (with or without BOM), UTF-16 or a legacy code page (cp1251)."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def parse_cue(path: Path) -> CueSheet:
    sheet = CueSheet()
    track: Optional[CueTrack] = None
    for line in _decode(path.read_bytes()).splitlines():
        index = _INDEX_RE.match(line)
        if index:
            number, minutes, seconds, frames = map(int, index.groups())
            if track and number == 1:
                track.start = (minutes * 60 + seconds) * _CD_FRAMES_PER_SECOND + frames
            continue
        command = _COMMAND_RE.match(line)
        if not command:
            continue
        key, value = command.group(1).upper(), command.group(2) if command.group(2) is not None else command.group(3)
        if key == "FILE":
            sheet.files.append(value)
        elif key == "TRACK":
            track = CueTrack(int(value))
            sheet.tracks.append(track)
        elif key in ("TITLE", "PERFORMER"):
            setattr(track or sheet, key.lower(), value.strip())
        elif key in ("DATE", "GENRE") and not track:
            setattr(sheet, key.lower(), value.strip())
        elif key == "DISCNUMBER" and not track:
            sheet.disc = value.strip()
    return sheet


def _image_of(audio: list[Path], sheet: CueSheet) -> Optional[Path]:
    """
    The lossless image a CUE sheet describes among a directory's *audio*
    files.  The name in the sheet often keeps the extension the image had
    before compression ("Album.wav" for "Album.ape"), or is stale altogether
    — then a lone audio file is taken.
    """
    if len(sheet.files) != 1 or len(sheet.tracks) < 2:
        return None
    name = Path(sheet.files[0].replace("\\", "/"))
    found = ([f for f in audio if f.name.lower() == name.name.lower()]
             or [f for f in audio if f.stem.lower() == name.stem.lower()]
             or (audio if len(audio) == 1 else []))
    return found[0] if found and is_lossless(found[0]) else None


def _write_tags(path: Path, sheet: CueSheet, track: CueTrack, fallback: dict[str, str]) -> None:
    audio = FLAC(path)
    album_artist = sheet.performer or fallback["album_artist"] or fallback["artist"]
    for key, value in (("artist", track.performer or album_artist), ("title", track.title),
                       ("album", sheet.title or fallback["album"]), ("albumartist", album_artist),
                       ("date", sheet.date or fallback["date"]), ("genre", sheet.genre or fallback["genre"]),
                       ("tracknumber", str(track.number)), ("discnumber", sheet.disc or fallback["disc_number"])):
        if value:
            audio[key] = value
    audio.save()


def split_image(image: Path, sheet: CueSheet, prefix: str = "") -> list[Path]:
    """
    Cut *image* into "<prefix>NN.flac" tracks next to it, tagged from *sheet*,
    and delete it; returns the tracks.  Nothing is changed when a cut fails.
    """
    tracks = sheet.tracks
    if any(t.start is None for t in tracks) or [t.start for t in tracks] != sorted(t.start for t in tracks):
        raise ValueError("CUE sheet has a track without INDEX 01 or out of order")
    targets = [image.with_name(f"{prefix}{t.number:02d}.flac") for t in tracks]
    temps = [image.with_name(f".{prefix}{t.number:02d}.splitting.flac") for t in tracks]
    if any(t.exists() for t in targets):
        raise FileExistsError("track files already exist next to the image")
    try:
        with av.open(str(image)) as container:
            stream = container.streams.audio[0]
            rate, channels = stream.codec_context.sample_rate, stream.codec_context.channels
            subtype = "PCM_16" if bits(stream.codec_context) <= 16 else "PCM_24"
            # Track boundaries in samples; the first track takes the audio before its INDEX 01 too
            ends = [t.start * rate // _CD_FRAMES_PER_SECOND for t in tracks[1:]] + [None]
            written = [0] * len(tracks)
            current, position = 0, 0
            out = sf.SoundFile(temps[0], "w", rate, channels, subtype, format="FLAC")
            try:
                for block in decoded_blocks(container, stream):
                    while len(block):
                        end = ends[current]
                        take = len(block) if end is None else min(len(block), end - position)
                        out.write(block[:take])
                        written[current] += take
                        position += take
                        block = block[take:]
                        if end is not None and position == end:
                            out.close()
                            current += 1
                            out = sf.SoundFile(temps[current], "w", rate, channels, subtype, format="FLAC")
            finally:
                out.close()
        if current != len(tracks) - 1 or not all(written):
            raise RuntimeError(f"image is shorter than the CUE sheet ({position} samples)")

        fallback = read_tags(image)
        for track, temp, target in zip(tracks, temps, targets):
            _write_tags(temp, sheet, track, fallback)
            temp.rename(target)
            target.chmod(config.FILE_MODE)
        image.unlink()
        return targets
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def split_album(album_dir: Path) -> int:
    """
    Split the whole-album images in *album_dir* and its disc folders; returns
    how many were split.  A folder is left alone when it also holds audio no
    CUE sheet describes: those are tracks, already split.
    """
    directories = [album_dir] + [d for d in sorted(album_dir.iterdir()) if d.is_dir() and disc_dir_number(d) is not None]
    split = 0
    for directory in directories:
        audio = get_audio_files(directory)
        images: dict[Path, tuple[Path, CueSheet]] = {}  # one sheet per image: copies in other encodings are skipped
        for cue in sorted(directory.glob("*.[cC][uU][eE]")):
            sheet = parse_cue(cue)
            image = _image_of(audio, sheet)
            if image:
                images.setdefault(image, (cue, sheet))
        if not images or len(images) != len(audio):
            continue
        for disc, image in enumerate(sorted(images), 1):
            cue, sheet = images[image]
            # Several discs in one folder: numbered by the sheet or by name order
            if len(images) > 1:
                sheet.disc = sheet.disc or str(disc)
            tracks = split_image(image, sheet, f"{sheet.disc}-" if len(images) > 1 else "")
            log.info("✂️  %s → %d tracks by %s", image.name, len(tracks), cue.name)
            split += 1
    return split
