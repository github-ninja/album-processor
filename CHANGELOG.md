# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
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
