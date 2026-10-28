"""Test fixtures: synthetic audio files and images, isolated library paths."""

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC
from PIL import Image

from albumproc import config


def make_flac(path: Path, seconds: float = 2.0, sr: int = 44100, lowpass_hz: float | None = None,
              subtype: str = "PCM_16", **tags) -> Path:
    """White-noise FLAC (optionally brick-wall low-passed, like an MP3 source) with *tags*."""
    rng = np.random.default_rng(len(path.name))
    noise = rng.standard_normal(int(seconds * sr)).astype(np.float32) * 0.1
    if lowpass_hz:
        spectrum = np.fft.rfft(noise)
        spectrum[np.fft.rfftfreq(len(noise), 1 / sr) > lowpass_hz] = 0
        noise = np.fft.irfft(spectrum, len(noise)).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.stack([noise, noise], axis=1), sr, subtype=subtype)
    audio = FLAC(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


def make_m4a(path: Path, seconds: float = 2.0, sr: int = 44100, codec: str = "alac", sample_format: str = "s16p",
             **tags) -> Path:
    """White-noise M4A (ALAC, or a lossy codec) with MP4 *tags* ("\xa9nam", "trkn", "covr"…)."""
    import av
    from mutagen.mp4 import MP4

    rng = np.random.default_rng(len(path.name))
    dtype = np.int16 if sample_format == "s16p" else np.int32
    noise = (rng.standard_normal((2, int(seconds * sr))) * 0.1 * np.iinfo(dtype).max).astype(dtype)
    if codec != "alac":
        sample_format, noise = "fltp", (noise / np.iinfo(dtype).max).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="ipod") as container:
        stream = container.add_stream(codec, rate=sr, layout="stereo", format=sample_format)
        for start in range(0, noise.shape[1], 4096):
            frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(noise[:, start:start + 4096]),
                                               format=sample_format, layout="stereo")
            frame.sample_rate = sr
            container.mux(stream.encode(frame))
        container.mux(stream.encode(None))
    audio = MP4(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


def make_image(size: tuple[int, int], fmt: str = "JPEG", mode: str = "RGB", seed: int = 1) -> bytes:
    """A blocky random picture (distinct per *seed*) in the given format."""
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 255, (8, 8, 3), dtype=np.uint8)
    img = Image.fromarray(blocks, "RGB").resize(size, Image.NEAREST).convert(mode)
    out = io.BytesIO()
    img.save(out, fmt)
    return out.getvalue()


class TempLibraryCase(unittest.TestCase):
    """Runs each test against empty temporary downloads/ and music/ folders."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.downloads = self.tmp / "downloads"
        self.music = self.tmp / "music"
        self.downloads.mkdir()
        self.music.mkdir()
        for name, value in (("DOWNLOADS_PATH", self.downloads), ("MUSIC_PATH", self.music),
                            ("DEEZER_DOWNLOADER_DIR", self.downloads / "deezer"), ("DEEZER_DOWNLOADER_URL", "")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
