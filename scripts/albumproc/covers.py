"""
Cover art: finding the album's image, fetching a better one from Deezer,
comparing images and normalising them to the library standard.

Standard: Cover.jpg, RGB baseline JPEG, COVER_SIZE × COVER_SIZE.  Larger
square images are downscaled; smaller ones are kept as they are (upscaling
adds no detail — the same reason upconverted audio is rejected).
"""

import io
import logging
import re
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageOps

from . import config
from .names import artists_match, title_similarity, TITLE_MIN_SIMILARITY
from .tags import get_embedded_picture

log = logging.getLogger(__name__)

_DEEZER_SEARCH_URL = "https://api.deezer.com/search/album"
_MIN_COVER_BYTES = 10_000          # Deezer placeholders are tiny
_SQUARE_TOLERANCE = 0.02           # 1486×1485 is square, 1200×1085 is not
SAME_IMAGE_MIN_SIMILARITY = 0.90   # correlation of 32×32 thumbnails


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def image_size(data: bytes) -> Optional[tuple[int, int]]:
    try:
        return Image.open(io.BytesIO(data)).size
    except Exception:  # noqa: BLE001
        return None


def is_square(size: tuple[int, int]) -> bool:
    return abs(size[0] - size[1]) <= max(size) * _SQUARE_TOLERANCE


def is_standard(data: bytes) -> bool:
    """Already a COVER_SIZE² RGB JPEG — nothing to do."""
    try:
        img = Image.open(io.BytesIO(data))
        return img.format == "JPEG" and img.mode == "RGB" and img.size == (config.COVER_SIZE,) * 2
    except Exception:  # noqa: BLE001
        return False


def _to_rgb(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, "white")
        background.paste(img, mask=img.split()[-1])
        return background
    return img.convert("RGB")


def normalize_cover(data: bytes) -> bytes:
    """
    Bring an image to the library standard.  Square images larger than
    COVER_SIZE are downscaled to exactly COVER_SIZE²; others keep their size.
    Images already in a usable form (RGB JPEG, not too large) are returned
    unchanged to avoid needless re-encoding.
    """
    img = Image.open(io.BytesIO(data))
    size = img.size
    needs_resize = is_square(size) and size != (config.COVER_SIZE,) * 2 and min(size) >= config.COVER_SIZE
    if img.format == "JPEG" and img.mode == "RGB" and not needs_resize:
        return data

    img = _to_rgb(img)
    if needs_resize:
        img = img.resize((config.COVER_SIZE, config.COVER_SIZE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=config.COVER_JPEG_QUALITY, subsampling=0, optimize=True)
    return out.getvalue()


def image_similarity(a: bytes, b: bytes) -> float:
    """
    How alike two images look (correlation of 32×32 grayscale thumbnails,
    -1…1).  Different resolutions or JPEG re-encodes of one cover score
    above SAME_IMAGE_MIN_SIMILARITY; different covers score well below.
    """
    def thumb(data: bytes) -> list[float]:
        img = _to_rgb(Image.open(io.BytesIO(data))).convert("L").resize((32, 32), Image.LANCZOS)
        pixels = list(img.tobytes())  # mode "L": one byte per pixel
        mean = sum(pixels) / len(pixels)
        return [p - mean for p in pixels]

    ta, tb = thumb(a), thumb(b)
    num = sum(x * y for x, y in zip(ta, tb))
    den = (sum(x * x for x in ta) * sum(y * y for y in tb)) ** 0.5
    return num / den if den else 0.0


# ---------------------------------------------------------------------------
# Finding covers
# ---------------------------------------------------------------------------

_COVER_NAME_RE = re.compile(r"^[cс]over\.(jpe?g|png)$", re.IGNORECASE)  # Latin or Cyrillic "с"


def cover_like_files(album_dir: Path) -> list[Path]:
    """Cover image files of an album: "Cover.jpg" and look-alikes ("cover.png",
    "Сover.jpg" with a Cyrillic С, covers inside CD1/CD2…), the proper one first."""
    found = []
    for directory in [album_dir, *sorted(d for d in album_dir.iterdir() if d.is_dir())]:
        for f in sorted(directory.iterdir()):
            if f.is_file() and _COVER_NAME_RE.match(f.name):
                found.append(f)
    return sorted(found, key=lambda f: (f != album_dir / config.COVER_NAME, len(f.parts), f.name))


def current_cover(album_dir: Path, audio_files: list[Path]) -> Optional[bytes]:
    """The album's existing artwork: a cover file, else the first embedded picture."""
    for f in cover_like_files(album_dir):
        data = f.read_bytes()
        if image_size(data):
            return data
    for f in audio_files[:1]:
        return get_embedded_picture(f)
    return None


def fetch_deezer_cover(artist: str, album: str) -> Optional[bytes]:
    """
    Cover from Deezer (``cover_xl``, normally 1000×1000) for the search
    result that matches both *artist* and *album*; None when there is no
    confident match.  No API key required.
    """
    log.info("🎨 Querying Deezer for cover art: %s – %s", artist, album)
    try:
        resp = requests.get(_DEEZER_SEARCH_URL, params={"q": f"{artist} {album}", "limit": 10}, timeout=10)
        resp.raise_for_status()
        best, best_score = None, 0.0
        for result in resp.json().get("data", []):
            if not artists_match(artist, result.get("artist", {}).get("name", "")):
                continue
            score = title_similarity(album, result.get("title", ""))
            if score > best_score:
                best, best_score = result, score
        if not best or best_score < TITLE_MIN_SIMILARITY:
            log.info("  ℹ️  No matching Deezer album found.")
            return None

        cover_url = best.get("cover_xl") or best.get("cover_big")
        if not cover_url:
            log.info("  ℹ️  Deezer result has no cover URL.")
            return None
        img = requests.get(cover_url, timeout=15)
        img.raise_for_status()
        if len(img.content) < _MIN_COVER_BYTES or not image_size(img.content):
            log.warning("  ⚠️  Deezer cover looks like a placeholder — skipping.")
            return None
        log.info("  ✅ Deezer cover fetched (%d KB): %s – %s",
                 len(img.content) // 1024, best["artist"]["name"], best["title"])
        return img.content
    except requests.Timeout:
        log.warning("  ⚠️  Deezer request timed out.")
    except Exception as exc:  # noqa: BLE001
        log.warning("  ⚠️  Deezer error: %s", exc)
    return None
