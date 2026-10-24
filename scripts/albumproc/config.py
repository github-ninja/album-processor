"""Configuration: paths and switches, overridable via environment variables."""

import os
from pathlib import Path

DOWNLOADS_PATH = Path(os.environ.get("DOWNLOADS_PATH", "/mnt/data/downloads"))
MUSIC_PATH = Path(os.environ.get("MUSIC_PATH", "/mnt/data/music"))

AUDIO_EXTENSIONS: frozenset[str] = frozenset({".flac", ".mp3", ".m4a", ".wav", ".aac", ".ape"})
LOSSLESS_EXTENSIONS: frozenset[str] = frozenset({".flac", ".wav"})

COVER_NAME = "Cover.jpg"
COVER_SIZE = 1000             # covers are stored as COVER_SIZE × COVER_SIZE JPEG
COVER_JPEG_QUALITY = 92

# Library audio format: lossless files (FLAC, ALAC, APE, WAV) are converted to FLAC of this resolution
TARGET_BITS = 16
TARGET_RATE = 44100

# What to do with an album that looks like a lossy→lossless upconvert:
#   "warn" — log a warning and process as usual (default)
#   "skip" — leave the album in downloads untouched
#   "ask"  — leave it untouched and list it in UPCONVERT_REPORT_FILE so the
#            Telegram bot can ask the user what to do
UPCONVERT_ACTION = os.environ.get("UPCONVERT_ACTION", "warn").strip().lower()
UPCONVERT_REPORT_FILE = os.environ.get("UPCONVERT_REPORT_FILE", "")

# Optional Discogs personal access token: raises the rate limit from 25 to 60 req/min
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")

FILE_MODE = 0o644
DIR_MODE = 0o755


def state_dir() -> Path:
    """Hidden folder in the library for the journal and replaced files (Navidrome skips it)."""
    return MUSIC_PATH / ".album_processor"
