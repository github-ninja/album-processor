# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Mastering passport: DR (TT DR Meter method), hard-clipping flat tops, loudness and
  true peak for every album, with a 🟢/🟡/🔴 grade in the import report.
- `/quality` (CLI `--quality`): the most crushed albums, or one album track by track.
- The bot sets its `/` command menu on start, visible in the owner's chat only.

### Added — Anton
- The bot has a name: Anton. It sets it in Telegram itself.
- `/opinion <name>` and a button under every import report: Anton's verdict on an
  album's mastering and editions, written by a Gemini model (`GEMINI_API_KEY`,
  `GEMINI_MODEL`, `GEMINI_PROXY`) from a dossier of facts — passport, Discogs
  editions, credits and notes, the listener profile in `.album_processor/listener.md`.
  An overloaded model (HTTP 503) hands over to `GEMINI_FALLBACK_MODEL`; the prompt
  carries reading scales for DR, flat tops and true peak.

### Added — not found in Discogs
- The import report ends with the albums Discogs did not know (filed without year and
  genre), so a misspelt or transliterated artist is noticed at once.

### Added — Deezer
- `/deezer <query>`: search Deezer, download an album through deezer-downloader and
  file it; `/newreleases` gets a download button under every new album.
- deezer-downloader's albums folder is picked up by `/newalbum` (not while it is
  downloading); partly available albums are moved to `deezer/incomplete/`.

### Fixed
- An "Artist - Album" folder is recognised when file names dropped the artist's punctuation:
  deezer-downloader's "BremerMcCoy - Ordet" for "Bremer/McCoy".
- Track artists come from Discogs when it names them: a store's "HollowGraphik + Code Rising"
  becomes "Code Rising; HollowGraphiK" ("+" is not split blindly: Florence + the Machine).
- Folder names like "2000 M3" (year and title without a dash) are understood.
- Titles match across compatibility forms, accents and bracketed subtitles:
  "M3" finds Discogs' "M³ (Daily Assortment Of Sound)".
- `/newreleases` (CLI `--releases`): new albums by the library's artists from Deezer —
  albums only, no reissues or remix/live albums; automatic checks only when scheduled
  (`daily` / `weekly` / `monthly`, `stop`), silent without news; possible namesakes
  reported separately with the reason.
- An album already in the library is no longer replaced silently: the bot compares
  both passports and asks (`REPLACE_ACTION`, `ask` in the bot, `replace` on the CLI).

### Changed
- Multi-disc albums: a copy of `Cover.jpg` in every disc folder; one placed there by hand is kept.
- `requirements.txt` pins the full dependency tree; direct dependencies moved to `requirements.in`.

## [1.0.0] — 2026-10-24

First public release.

### Import
- New albums from the downloads folder: nested or flat layouts, multi-disc albums.
- Whole-album images split by CUE sheet, sample-exact (UTF-8, UTF-16, cp1251 sheets).
- ALAC, APE, WAV and hi-res FLAC converted to FLAC 16/44.1 (soxr VHQ, TPDF dither).
- Spectral detection of lossy→lossless upconverts with a spectrogram and a decision in Telegram.

### Metadata
- Discogs master title, original year, genres, tracklist, credits and release type,
  with a tracklist check against the files.
- Artist spelling from Discogs, aliases, joint and featured credits, remixers kept in titles.
- Tags: `RELEASETYPE`, `COMPILATION`, `REMIXER` and ReplayGain 2.0 with true peaks.
- Covers from Deezer or the download, 1000×1000, embedded in every track.

### Library maintenance
- `/fixlibrary`: plan → review → apply for names, tags, covers, format, ReplayGain,
  release types and duplicates; journal and moved-aside copies for every change.

[1.0.0]: https://github.com/github-ninja/album-processor/releases/tag/v1.0.0
