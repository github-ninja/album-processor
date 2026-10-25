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
#   "ask"  — leave it untouched and list it in DEFERRED_REPORT_FILE so the
#            Telegram bot can ask the user what to do
UPCONVERT_ACTION = os.environ.get("UPCONVERT_ACTION", "warn").strip().lower()

# What to do with an album that is already in the library (maybe under another year):
#   "replace" — move the old copy aside and put the new one in place (default)
#   "ask"     — leave the processed album in downloads and list it, with both
#               mastering passports, in DEFERRED_REPORT_FILE for the bot
REPLACE_ACTION = os.environ.get("REPLACE_ACTION", "replace").strip().lower()

# JSON list of the albums left in downloads for the user to decide on (set by the bot)
DEFERRED_REPORT_FILE = os.environ.get("DEFERRED_REPORT_FILE", "")

# Optional Discogs personal access token: raises the rate limit from 25 to 60 req/min
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")

# Optional: Anton's opinions on albums (opinion.py) — a Gemini API key from
# aistudio.google.com; the free tier is plenty.  GEMINI_PROXY routes only these
# requests (e.g. "socks5h://host:1080" or "http://host:3128") for servers in
# countries where the Gemini API is not available.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# The newest Flash is often overloaded (HTTP 503): the previous one answers reliably,
# and an overloaded or silent model hands over to the fallback
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
GEMINI_PROXY = os.environ.get("GEMINI_PROXY", "")

FILE_MODE = 0o644
DIR_MODE = 0o755


def state_dir() -> Path:
    """Hidden folder in the library for the journal and replaced files (Navidrome skips it)."""
    return MUSIC_PATH / ".album_processor"
