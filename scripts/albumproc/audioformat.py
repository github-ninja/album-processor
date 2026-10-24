"""
Conversion of lossless audio to the library format, FLAC 16 bit / 44.1 kHz.

Hi-res FLAC as well as ALAC, APE and WAV are converted; PyAV (FFmpeg's
decoders bundled into a wheel) reads them all.  A 16/44.1 source is copied
sample for sample.

16/44.1 is audibly transparent; higher resolutions only take space.  The
conversion is done properly so that it stays transparent: the soxr
resampler at its very-high-quality setting, and TPDF dither when reducing
to 16 bit.  The result differs from the source only by dither noise at
about -96 dBFS.
"""

import logging
import os
from pathlib import Path

import av
import numpy as np
import soundfile as sf
import soxr
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4

from . import config
from .tags import get_embedded_picture, read_tags

log = logging.getLogger(__name__)

_FULL_SCALE = 32768  # 16-bit sample value of 1.0
_LOSSLESS_CODECS = {".m4a": "ALAC", ".ape": "APE", ".wav": "WAV"}  # converted to FLAC whatever their format


def is_lossless(path: Path) -> bool:
    """FLAC, ALAC, APE or WAV (AAC shares the .m4a extension with ALAC)."""
    ext = path.suffix.lower()
    if ext == ".m4a":
        return MP4(path).info.codec == "alac"
    return ext == ".flac" or ext in _LOSSLESS_CODECS


def needs_conversion(path: Path) -> bool:
    """True for a FLAC file that isn't 16 bit / 44.1 kHz and for any ALAC, APE or WAV file."""
    if path.suffix.lower() == ".flac":
        info = FLAC(path).info
        return (info.bits_per_sample, info.sample_rate) != (config.TARGET_BITS, config.TARGET_RATE)
    return is_lossless(path)


def describe_format(path: Path) -> str:
    """"24/96" for a FLAC file, "ALAC 16/44.1" for the other formats."""
    with av.open(str(path)) as container:
        ctx = container.streams.audio[0].codec_context
        resolution = f"{bits(ctx)}/{ctx.sample_rate / 1000:g}"
    codec = _LOSSLESS_CODECS.get(path.suffix.lower())
    return f"{codec} {resolution}" if codec else resolution


def bits(ctx) -> int:
    """Bit depth of a decoder: what the stream declares, else what its sample format holds."""
    return ctx.bits_per_raw_sample or (16 if ctx.format.bytes == 2 else 24)


def decoded_blocks(container, stream):
    """The stream's audio as decoded: blocks of frames × channels (int16, int32 or float)."""
    for frame in container.decode(stream):
        samples = frame.to_ndarray()
        yield samples.T if frame.format.is_planar else samples.reshape(-1, len(frame.layout.channels))


def _as_float(samples: np.ndarray) -> np.ndarray:
    """Decoded samples → float64 in [-1, 1)."""
    if samples.dtype.kind == "i":
        return samples / float(2 ** (8 * samples.dtype.itemsize - 1))
    return samples.astype(np.float64)


def _to_16bit(block: np.ndarray, rng: np.random.Generator | None) -> np.ndarray:
    """Float samples → int16, with TPDF dither (±1 LSB triangular noise) unless *rng* is None."""
    dither = rng.random(block.shape) - rng.random(block.shape) if rng else 0
    return np.clip(np.round(block * _FULL_SCALE + dither), -_FULL_SCALE, _FULL_SCALE - 1).astype(np.int16)


def _copy_metadata(src: Path, dst: Path) -> None:
    """Tags and pictures of *src* onto the FLAC file *dst*."""
    new = FLAC(dst)
    new.delete()
    if src.suffix.lower() == ".flac":
        old = FLAC(src)
        for key, values in (old.tags.as_dict() if old.tags else {}).items():
            new[key] = values
        for picture in old.pictures:
            new.add_picture(picture)
    else:
        # Only the fields the pipeline reads; they are all rewritten from Discogs later
        tags = read_tags(src)
        for key, field in (("artist", "artist"), ("title", "title"), ("album", "album"), ("date", "date"),
                           ("albumartist", "album_artist"), ("tracknumber", "track_number"),
                           ("discnumber", "disc_number"), ("genre", "genre")):
            if tags[field]:
                new[key] = tags[field].split("; ")
        image = get_embedded_picture(src)
        if image:
            picture = Picture()
            picture.type, picture.mime, picture.data = 3, "image/png" if image[:4] == b"\x89PNG" else "image/jpeg", image
            new.add_picture(picture)
    new.save()


def convert_to_library_format(path: Path) -> Path:
    """
    Replace *path* (hi-res FLAC, ALAC, APE or WAV) with a 16/44.1 FLAC file,
    keeping its tags, pictures and permissions; returns the new file's path.
    The original goes only after the new file has been written and checked.
    """
    target = path.with_suffix(".flac")
    if target != path and target.exists():
        raise FileExistsError(f"{target.name} already exists")
    tmp = path.with_name(f".{path.stem}.converting.flac")
    try:
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            rate, channels = stream.codec_context.sample_rate, stream.codec_context.channels
            resampler = (soxr.ResampleStream(rate, config.TARGET_RATE, channels, dtype="float64", quality="VHQ")
                         if rate != config.TARGET_RATE else None)
            # CD resolution is copied as is: no dither on top of the source's own
            exact = not resampler and bits(stream.codec_context) <= config.TARGET_BITS
            rng = None if exact else np.random.default_rng()
            read = written = 0
            with sf.SoundFile(tmp, "w", config.TARGET_RATE, channels, "PCM_16", format="FLAC",
                              compression_level=1.0) as dst:
                for block in map(_as_float, decoded_blocks(container, stream)):
                    read += len(block)
                    if resampler:
                        block = resampler.resample_chunk(block)
                    if len(block):
                        dst.write(_to_16bit(block, rng))
                        written += len(block)
                if resampler:
                    tail = resampler.resample_chunk(np.empty((0, channels)), last=True)
                    dst.write(_to_16bit(tail, rng))
                    written += len(tail)

        expected = round(read * config.TARGET_RATE / rate)
        if not read or abs(written - expected) > 1:
            raise RuntimeError(f"length mismatch: {written} frames, expected {expected}")
        _copy_metadata(path, tmp)
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, target)
        if target != path:
            path.unlink()
        return target
    finally:
        tmp.unlink(missing_ok=True)


def convert_album(paths: list[Path]) -> int:
    """Convert every file of *paths* that needs it; returns how many were converted."""
    converted = 0
    for path in paths:
        if needs_conversion(path):
            fmt = describe_format(path)
            convert_to_library_format(path)
            log.info("  🎚️  %s: %s → FLAC 16/44.1", path.name, fmt)
            converted += 1
    return converted
