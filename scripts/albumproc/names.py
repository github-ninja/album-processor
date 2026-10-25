"""
Naming rules and text helpers: title cleanup, file/folder names, fuzzy matching.

Everything here is pure (no I/O), so it is easy to test.
"""

import difflib
import re
import unicodedata
from typing import Optional

# Words that mark a bracketed group in a folder name / release title as edition
# noise rather than part of the album name: "[FLAC]", "(Hi-Res 24-96)",
# "(2021 Remaster)", "(15th Anniversary Edition)", "[2019, Warp Records]" …
_JUNK_WORD_PAT = (
    r"(?:FLAC|MP3|AAC|ALAC|OGG|OPUS|WAV|AIFF|DSD\d*|SACD|MQA"            # formats
    r"|Hi-?Res|HD|HQ|Lossless|Studio\s+Masters?"                          # quality
    r"|\d{1,2}\s*-?\s*bits?|\d{2,3}(?:[.,]\d)?\s*-?\s*kHz"               # 24bit, 96kHz
    r"|(?:16|24|32)\s*[-/]\s*(?:44|48|88|96|176|192)(?:[.,]\d)?"         # 24-96
    r"|\d{2,4}\s*kbps|CBR|VBR|V[0-9]"                                    # bitrates
    r"|WEB(?:-?DL|-?FLAC|-?RIP)?|CD(?:-?RIP)?\d*|Vinyl(?:-?Rip)?|LP-?Rip|Rip"  # sources
    r"|Tidal|Qobuz|Deezer|Bandcamp|iTunes|Beatport|HDTracks"             # shops (tags like "[Bandcamp]")
    r"|Re-?master(?:ed)?|Re-?mastering|Re-?issue|Repack|Proper"          # reissues
    r"|Deluxe|Expanded|Anniversary|Edition|Bonus\s+Tracks?"              # editions
    r"|Explicit|Clean\s+Version)"
)
_JUNK_WORD_RE = re.compile(r"(?<![^\W_])" + _JUNK_WORD_PAT + r"(?![^\W_])", re.IGNORECASE)
_BRACKET_GROUP_RE = re.compile(r"\s*[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]")
# Group content that is just a year, a year range, or starts with one: "(1998)", "[2019, Label]"
_YEAR_GROUP_RE = re.compile(r"^\s*\d{4}(?:\s*[-–]\s*\d{4})?\s*(?:[,;].*)?$")
# Catalogue numbers, optionally after a country code: "[WARP123]", "(ABC-001)", "[GB - DVSNCD003]"
_CATNO_GROUP_RE = re.compile(r"^\s*(?:[A-Z]{2}\s*[-–,]\s*)?[A-Z]{2,}\s*-?\s*\d{2,}\s*$")
# Junk words left outside brackets at the end: "Album - FLAC", "Album WEB 24bit"
_TRAILING_JUNK_RE = re.compile(
    r"(?:\s*[-–_.]?\s*(?<![^\W_])" + _JUNK_WORD_PAT + r")+\s*$", re.IGNORECASE
)
# Optional "YYYY - Title" pattern in folder names
_YEAR_TITLE_RE = re.compile(r"^(\d{4})\s*[-–]\s*(.+)$")
# "2000 M3": a release year and the title with only a space between (not "1999" alone)
_YEAR_SPACE_TITLE_RE = re.compile(r"^((?:19|20)\d{2})\s+(\S.*)$")
# A bare 4-digit year anywhere in a folder name (any bracket type)
_YEAR_ANYWHERE_RE = re.compile(r"[\(\[\s]+(\d{4})[\)\]\s]*")
_INVISIBLE_RE = re.compile(r"[​-‏⁠﻿]")

TITLE_MIN_SIMILARITY = 0.6
ARTIST_MIN_SIMILARITY = 0.5


# ---------------------------------------------------------------------------
# Album titles
# ---------------------------------------------------------------------------

def _is_junk_group(content: str) -> bool:
    return bool(
        _JUNK_WORD_RE.search(content)
        or _YEAR_GROUP_RE.match(content)
        or _CATNO_GROUP_RE.match(content)
        or not content.strip()
    )


def clean_folder_name(name: str) -> str:
    """
    Reduce a folder name (or a downloaded/Discogs release title) to the bare album
    title: drop bracketed groups with format/quality/edition/year noise,
    trailing junk words, and a leading "YYYY - " prefix.  Bracketed groups
    that look like part of the title, e.g. "Artifakts (BC)", are kept.
    """
    cleaned = name
    # Scene-style names without spaces: "Album.Name.2019.FLAC"
    if " " not in cleaned and len(re.findall(r"[._]+", cleaned)) >= 2:
        cleaned = re.sub(r"[._]+", " ", cleaned)
        cleaned = _TRAILING_JUNK_RE.sub("", cleaned)
        cleaned = re.sub(r"(?<=\S)\s+\d{4}$", "", cleaned)
    cleaned = _BRACKET_GROUP_RE.sub(
        lambda m: "" if _is_junk_group(m.group(1)) else m.group(0), cleaned
    )
    cleaned = _TRAILING_JUNK_RE.sub("", cleaned)
    # Store suffixes and numeric prefixes: "Exodus - Single", "05-07 - Exodus"
    cleaned = re.sub(r"\s+[-–]\s+(?:Single|EP)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\d{1,2}[-.]\d{2}\s+[-–]\s+", "", cleaned)
    # Release dates: "2013-09-02 - I Am Legion"
    cleaned = re.sub(r"^\d{4}[-.]\d{2}[-.]\d{2}\s+[-–]\s+", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–_.")
    # Also drop a leading year prefix ("2001 - Album Name" → "Album Name")
    m = _YEAR_TITLE_RE.match(cleaned) or _YEAR_SPACE_TITLE_RE.match(cleaned)
    if m:
        cleaned = m.group(2).strip()
    return cleaned or name.strip()


def extract_year_from_folder(name: str) -> str:
    """
    Extract a 4-digit release year from a folder name: "2001 - Title",
    "2001 Title", "Title [2001]" or "Title (2001)".  Returns "" if not found.
    """
    m = _YEAR_TITLE_RE.match(name) or _YEAR_SPACE_TITLE_RE.match(name) or _YEAR_ANYWHERE_RE.search(name)
    return m.group(1) if m else ""


def strip_artist_prefix(title: str, artist: str) -> str:
    """
    Remove a leading "Artist - " prefix from *title* if it duplicates *artist*:
    strip_artist_prefix("Omni Trio - Even Angels", "Omni Trio") → "Even Angels".
    """
    prefix = re.escape(artist.strip()) + r"\s*[-–—]\s*"
    return re.sub(f"^{prefix}", "", title, flags=re.IGNORECASE).strip()


def clean_text(value: str) -> str:
    """NFC-normalise, drop invisible characters, collapse and trim whitespace."""
    value = _INVISIBLE_RE.sub("", unicodedata.normalize("NFC", value))
    return " ".join(value.split())


# ---------------------------------------------------------------------------
# File and folder names
# ---------------------------------------------------------------------------

def sanitize_path_component(name: str, strip_dots: bool = True) -> str:
    """
    Remove characters that are illegal or problematic in file/directory names.

    Removes:
    - Null bytes and ASCII control characters (0x00–0x1F, 0x7F)
    - Windows-unsafe chars kept for cross-platform safety: ``\\ : * ? " < > |``
    Forward slashes become ", " between spaced titles ("Nova / Moth" →
    "Nova, Moth") and "-" otherwise ("AC/DC" → "AC-DC").
    Repeated whitespace is collapsed; trailing dots and spaces are stripped
    (they cause issues on some filesystems).
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name)
    cleaned = re.sub(r"\s+/\s+", ", ", cleaned).replace("/", "-")
    cleaned = re.sub(r'[<>:"\\|?*]', "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    # Only spaces are stripped for parts of a file name, where a dot is
    # harmless: "01. Alfa Mist - .44.flac"
    return cleaned.strip(". " if strip_dots else " ")


def split_multi_value(value: str) -> list[str]:
    """Split a "; "-joined tag value ("Dissident; Cyberworm") into its values."""
    return [part.strip() for part in value.split(";") if part.strip()]


def split_genres(value: str) -> list[str]:
    """
    Split a genre string into values.  Besides ";" downloaded tags often use
    ", " ("Breaks, Big Beat"); "/" is kept because Discogs genres contain it
    ("Funk / Soul").
    """
    return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]


def join_artist_names(names: list[str]) -> str:
    """Join artists the way releases credit them: "A", "A & B", "A, B & C"."""
    if len(names) <= 2:
        return " & ".join(names)
    return ", ".join(names[:-1]) + " & " + names[-1]


def credit_name(artist: str) -> str:
    """
    File-name form of an artist tag value: "Dissident; Cyberworm" →
    "Dissident & Cyberworm".  The tags keep the individual artists as
    separate values.
    """
    return sanitize_path_component(join_artist_names(split_multi_value(artist)) or artist)


def artist_folder_name(artist: str) -> str:
    """
    Library folder for an album artist tag value: the first credited artist,
    so joint releases sit with that artist's other albums
    ("Noisia; Mayhem" → "Noisia").  Duos with their own Discogs entry are one
    value and keep their name ("Fred V & Grafix").
    """
    values = split_multi_value(artist)
    return sanitize_path_component(values[0] if values else artist)


def album_folder_name(year: str, album: str) -> str:
    """"YYYY - Album" (or just "Album" when the year is unknown)."""
    return sanitize_path_component(f"{year} - {album}" if year else album)


def album_identity(folder_name: str) -> str:
    """
    An album folder name without its year, case-insensitive: "2020 - Hybrid
    Theory" and "2000 - Hybrid theory" are the same album in two versions.
    """
    return re.sub(r"^\d{4}\s*[-–—]\s*", "", clean_text(folder_name)).casefold()


def track_file_name(number: int, artist: str, title: str, suffix: str) -> str:
    """"NN. Artist - Title.ext" — the library's track naming rule."""
    safe_title = sanitize_path_component(title, strip_dots=False)
    return f"{number:02d}. {credit_name(artist)} - {safe_title}{suffix}"


# ---------------------------------------------------------------------------
# Tag value heuristics
# ---------------------------------------------------------------------------

def is_generic_title(title: str) -> bool:
    """Return True if *title* is a meaningless placeholder like 'Track 1' or 'track01'."""
    return not title.strip() or bool(re.match(r"^track\s*\d+$", title.strip(), re.IGNORECASE))


def is_contaminated_artist(value: str) -> bool:
    """True if *value* looks like a mixed-up "Artist - Title" string in the artist field."""
    return " - " in value


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    """
    Lower-case, junk-free, punctuation-free form of a title for comparison;
    compatibility forms and accents folded ("M³" = "M3", "Réunion" = "Reunion").
    """
    cleaned = unicodedata.normalize("NFKD", clean_folder_name(title).casefold().replace("&", " and "))
    cleaned = "".join(c for c in cleaned if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", cleaned).split())


_SUBTITLE_RE = re.compile(r"\s*[\(\[][^\(\)\[\]]*[\)\]]\s*$")


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    # The same title without a bracketed subtitle: "M3" vs "M³ (Daily Assortment Of Sound)"
    sa, sb = normalize_title(_SUBTITLE_RE.sub("", a)), normalize_title(_SUBTITLE_RE.sub("", b))
    if sa and sa == sb:
        ratio = max(ratio, 0.9)
    # One title is the other plus a subtitle: "Illinoize" vs "Illinoize: The Remix Tape"
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 4 and long_.startswith(short + " "):
        ratio = max(ratio, 0.8)
    return ratio


def artists_match(a: str, b: str) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= ARTIST_MIN_SIMILARITY


# ---------------------------------------------------------------------------
# Track credits: guests are artists, remixers are part of the title
# ---------------------------------------------------------------------------

# "(feat. X)", "[ft. X & Y]", "(featuring X)"
_FEAT_RE = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+?)\s*[\)\]]", re.IGNORECASE)
_FEAT_SEP_RE = re.compile(r"\s+(?:feat\.?|ft\.|featuring)\s+", re.IGNORECASE)
# "(X Remix)", "[X Rework]", "(X VIP Mix)" — names a version; X may be the remixer
_REMIX_RE = re.compile(
    r"[\(\[]\s*([^\(\)\[\]]+?)\s+(?:vip\s+)?(?:re-?mix|rmx|mix|rework|refix|edit|re-?edit|version|dub|vip)\s*[\)\]]",
    re.IGNORECASE,
)


# Discogs placeholders, not names
_PLACEHOLDER_ARTISTS = {"unknown artist", "no artist", "various", "anonymous"}


def name_key(name: str) -> str:
    """
    Case-, accent- and punctuation-insensitive form of a name: "Fejká" and
    "fejka", "T.C." and "TC" are the same person.
    """
    decomposed = unicodedata.normalize("NFKD", clean_text(name).casefold())
    return "".join(c for c in decomposed if c.isalnum() or c == " ").strip()


def _split_names(text: str) -> list[str]:
    """'Cody B. Ware & Ryu' → ['Cody B. Ware', 'Ryu']."""
    return [n.strip() for n in re.split(r",\s*|\s+&\s+", text) if n.strip()]


def split_featuring(title: str) -> tuple[str, list[str]]:
    """'The Universe (feat. Jennifer Neal)' → ('The Universe', ['Jennifer Neal'])."""
    guests: list[str] = []
    for match in _FEAT_RE.finditer(title):
        guests += _split_names(match.group(1))
    return clean_text(_FEAT_RE.sub("", title)), guests


def strip_featuring(artist: str) -> str:
    """'Blu Mar Ten feat. Airwalker' / 'A (feat. B)' → 'Blu Mar Ten' / 'A': the main credit only."""
    return _FEAT_SEP_RE.split(split_featuring(artist)[0], maxsplit=1)[0].strip()


def has_credit_markers(title: str, artists: list[str]) -> bool:
    """Whether :func:`credit_track` has anything to decide (worth a Discogs lookup)."""
    return bool(_FEAT_RE.search(title) or any(_FEAT_SEP_RE.search(a) for a in artists)
                or (len(artists) > 1 and _REMIX_RE.search(title)))


def _find_track(tracklist: list[dict], title: str) -> Optional[dict]:
    """The Discogs track titled like *title* (positions differ between editions)."""
    best = max(tracklist, key=lambda t: title_similarity(title, t.get("title", "")), default=None)
    return best if best and title_similarity(title, best.get("title", "")) >= 0.9 else None


# "(X Remix)", "[X Rework]": X is a person, unless it names a version ("2010 Remix",
# "VIP Remix", "Noisia's 'Outer Edges' Remix")
_REMIXER_RE = re.compile(r"[\(\[]\s*([^\(\)\[\]]+?)\s+(?:re-?mix|rework|refix|re-?edit|bootleg|flip)\s*[\)\]]",
                         re.IGNORECASE)
_NOT_A_REMIXER_RE = re.compile(r"\d|'s\b|['\"‘’“”]|^(?:vip|original|extended|radio|club|album|dub|instrumental|"
                               r"official|bonus|alternative|alternate)\b"
                               # A lone description, not a name: "(Slow Remix)", "[Distorted Remix]"
                               r"|^(?:slow|fast|funky|acoustic|distorted|dark|deep|dirty|clean|stripped|piano|orchestral|"
                               r"symphonic|ambient|chill|short|long|live|vocal|main|demo|heavy|hard|soft|unplugged|special|"
                               r"full|dance|new|final|alt|rough|minimal|electric|electronic|string|strings)$", re.IGNORECASE)


def track_remixers(title: str, tracklist: Optional[list[dict]] = None) -> list[str]:
    """REMIXER values of a track: its Discogs Remix credits, else the "(X Remix)" in its title."""
    discogs_track = _find_track(tracklist, title) if tracklist else None
    if discogs_track and discogs_track.get("remixers"):
        return list(dict.fromkeys(discogs_track["remixers"]))
    names = [re.sub(r"\s+VIP$", "", m.group(1).strip(), flags=re.IGNORECASE) for m in _REMIXER_RE.finditer(title)]
    return list(dict.fromkeys(n for n in names if not _NOT_A_REMIXER_RE.search(n)))


def is_various_artists(artist: str) -> bool:
    return name_key(artist) in {"various artists", "various"}


def credit_track(title: str, artists: list[str], album_artists: list[str],
                 tracklist: Optional[list[dict]] = None) -> tuple[str, list[str]]:
    """
    Title and artist values of a track under the library's credit rule:
    featured guests are artists (moved out of the title), remixers are not
    (their "(X Remix)" stays in the title).  The matching track of a Discogs
    *tracklist* supplies guests and remixers by role when available.
    """
    title, guests = split_featuring(title)
    discogs_track = _find_track(tracklist, title) if tracklist else None
    # "A feat. B & C" written into one artist value by some sources
    split_artists = []
    for value in artists:
        main, *rest = _FEAT_SEP_RE.split(value, maxsplit=1)
        split_artists.append(main.strip())
        guests = _split_names(rest[0]) + guests if rest else guests
    artists = [a for a in split_artists if a]
    remixers = {name_key(m.group(1)) for m in _REMIX_RE.finditer(title)}
    if discogs_track:
        guests = [g for g in discogs_track.get("featuring", []) if name_key(g) not in _PLACEHOLDER_ARTISTS] or guests
        remixers |= {name_key(n) for n in discogs_track.get("remixers", [])}
    performers = {name_key(a) for a in album_artists}
    credited = [a for a in artists if name_key(a) not in remixers or name_key(a) in performers] or list(artists)
    for guest in guests:
        if not any(_same_name(guest, a) for a in credited):
            credited.append(guest)
    return title, credited


def _same_name(a: str, b: str) -> bool:
    """
    One person spelt two ways: "J P Cooper" / "JP Cooper", "Rachael" / "Racheal
    Ofori", or credited by part of the name: "Shapla" / "Shapla Salique".
    """
    words_a, words_b = set(name_key(a).split()), set(name_key(b).split())
    ka, kb = name_key(a).replace(" ", ""), name_key(b).replace(" ", "")
    return (ka == kb or words_a <= words_b or words_b <= words_a
            or difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.85)
