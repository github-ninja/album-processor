# Album Processor

[![Tests](https://github.com/github-ninja/album-processor/actions/workflows/tests.yml/badge.svg)](https://github.com/github-ninja/album-processor/actions/workflows/tests.yml)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

*[Русская версия](README.ru.md)*

A self-hosted Telegram bot that turns whatever lands in your downloads folder
into a clean, consistent FLAC library — named, tagged and covered after
[Discogs](https://www.discogs.com), loudness-tagged with ReplayGain, checked
for fake lossless — and keeps that library tidy afterwards.

You drop an album into `downloads/`, send `/newalbum` to the bot, and a minute
later it sits in `music/Artist/YYYY - Album/` the way you would have filed it
by hand. Built for [Navidrome](https://www.navidrome.org) and foobar2000, works
with any player that reads tags.

> The bot talks to you in Russian; the code, logs and this documentation are in English.

## Features

**Import**
- Finds new albums in the downloads folder: `Album/`, `Artist/Album/`, multi-disc
  albums in `CD1/`, `CD2/`… sub-folders or flat with disc tags.
- Splits a whole-album image (one FLAC/APE/WAV/ALAC file + CUE sheet) into tracks,
  sample-exact at the `INDEX 01` marks; CUE sheets in UTF-8, UTF-16 or cp1251;
  several images in one folder become separate discs.
- Converts ALAC, APE, WAV and hi-res FLAC to FLAC 16 bit / 44.1 kHz (soxr VHQ
  resampling, TPDF dither); a 16/44.1 source is copied bit for bit.
- Detects lossy→lossless upconverts by spectral analysis (MP3/AAC low-pass
  cutoff) and asks what to do — with a spectrogram and *Process / Keep / Delete*
  buttons.
- Removes non-music files (logs, cues, scans, playlists) and empty folders.

**Metadata from Discogs**
- Album title from the Discogs *master* release, year of the *original* release,
  genres from Discogs styles.
- Artist names spelled as the Discogs artist entity ("VIMES" → "Vimes"),
  aliases followed when the tracks agree (a store's "Carl Finlow" → "Silicon Scally").
- Guards against wrong matches: a release whose tracklist does not overlap
  the files (a namesake band's album) is rejected.
- Another version of an album — *Instrumentals*, *Remixes* — stays a separate
  album even when Discogs files it under the original's master.
- Cleans messy folder names: `2013-09-02 - I Am Legion [GB - DVSNCD003] (FLAC 24-96)` → `I Am Legion`.

**Credits**
- Several artists are separate tag values (multi-value tags), joined in file
  names as "A", "A & B", "A, B & C".
- Featured guests are track artists, not part of the title:
  `The Universe (feat. Jennifer Neal)` → artists *A Guy Called Gerald; Jennifer Neal*.
- Remixers are not artists — "(X Remix)" stays in the title and X goes to `REMIXER`.
- Untangles store credits: a display string next to its guests
  (`["Noisia & Camo & Krooked", "Camo & Krooked"]`), one-value joint credits
  (`"Noisia, Calyx & TeeBee"`), duplicates spelled differently ("J P Cooper" / "JP Cooper").

**Tags written**
- Artist, title, album, album artist, date, genre, track/disc numbers and totals.
- `RELEASETYPE` (album / ep / single + compilation, soundtrack, dj-mix, remix)
  — Navidrome groups an artist's page by it; `COMPILATION=1` for Various Artists.
- ReplayGain 2.0: track and album gain (EBU R128, −18 LUFS) with **true** peaks,
  so a player's clipping prevention also covers inter-sample overs.
- Nothing else: no comments, no lyrics, no leftovers from the source.

**Covers**
- `Cover.jpg`, 1000×1000 JPEG, the same image embedded in every track.
- Fetched from Deezer when it has this exact album, else the download's own art;
  never upscaled; a small cover is upgraded only when Deezer has the same image larger.

**Library maintenance — `/fixlibrary`**
- Checks every album against the conventions and shows a plan before touching
  anything: apply all, go through it one by one, or cancel.
- Fixes names, tag hygiene (invisible characters, genre spelling, totals),
  artist spelling across the library, covers, audio format, missing ReplayGain
  and release types, duplicates (the same album under another year or case).
- `/fixlibrary all` also re-checks every album title against Discogs.

**Safety**
- One user only: the bot ignores everyone but `ALLOWED_ID`.
- Nothing in the library is deleted: replaced albums and covers are moved to
  `music/.album_processor/replaced/`, every change is journaled with its old values.
- Files are replaced only after the new version has been written and checked
  (conversion, CUE split, tags).

## How it works

For every album found in the downloads folder:

1. **Split** a whole-album image by its CUE sheet; **convert** ALAC, APE, WAV and hi-res FLAC to FLAC 16/44.1.
2. **Upconvert check** — spectral cutoff of every track; a suspected MP3→FLAC
   is held back for your decision (`UPCONVERT_ACTION`).
3. **Identify** — artist and title from folder and tags, looked up on Discogs
   (title, original year, genre, tracklist, credits, release type), the match verified against the files.
4. **Cover** — Deezer or the download's own art, normalised and embedded.
5. **Clean** — non-music files removed.
6. **Tag and rename** every track: `NN. Artist - Title.flac`, credits resolved.
7. **ReplayGain** — loudness of every track and of the album (tracks measured in parallel).
8. **Move** to `music/<Album artist>/<YYYY - Album>/`; an older copy of the same album is moved aside.

The bot only starts `album_processor.py` and talks to you; all the work is
done by the `albumproc` package, which can also be used from the command line.

## Library layout

```
music/
├── Noisia/
│   ├── 2010 - Split The Atom/
│   │   ├── 01. Noisia - Split The Atom.flac
│   │   ├── …
│   │   └── Cover.jpg
│   └── 2013 - Hyenas, The Liquid/          ← joint release: first credited artist's folder
├── Some Artist/
│   └── 2001 - Double Album/
│       ├── CD1/ …                          ← multi-disc albums
│       ├── CD2/ …
│       └── Cover.jpg
└── .album_processor/                       ← journal and replaced files (hidden from players)
```

- Album folder: `YYYY - Album` with the original year; characters illegal in file names replaced.
- Artist folder: the first credited album artist ("Noisia; Mayhem" → `Noisia/`);
  a duo with its own Discogs entry keeps its own folder (`Fred V & Grafix/`).
- Tags are the source of truth: file and folder names are derived from them.

## Quick start

You need Docker, a Telegram bot token and your Telegram user ID.

1. **Create a bot** with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy the token.
2. **Find your user ID**, e.g. with [@userinfobot](https://t.me/userinfobot).
3. *(Optional)* **Create a Discogs token** at
   [discogs.com/settings/developers](https://www.discogs.com/settings/developers):
   it raises the rate limit from 25 to 60 requests per minute.
4. **Get the code and configure it:**
   ```sh
   git clone https://github.com/github-ninja/album-processor.git
   cd album-processor
   cp .env.example .env
   nano .env        # BOT_TOKEN, ALLOWED_ID, DOWNLOADS_DIR, MUSIC_DIR
   ```
   In `docker-compose.yml`, set `user:` to the uid:gid that owns your music
   folders (`id -u`, `id -g`).
5. **Start it:**
   ```sh
   docker compose up -d --build
   docker compose logs -f        # "Bot started. Waiting for commands…"
   ```
6. Send `/help` to your bot.

To update: `git pull && docker compose up -d --build`.

### Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | Telegram bot token |
| `ALLOWED_ID` | yes | — | The only Telegram user ID the bot answers |
| `DOWNLOADS_PATH` | | `/mnt/data/downloads` | Downloads folder inside the container |
| `MUSIC_PATH` | | `/mnt/data/music` | Library folder inside the container |
| `DISCOGS_TOKEN` | | — | Discogs personal token (higher rate limit) |
| `UPCONVERT_ACTION` | | `ask` | Suspected upconverts: `ask` (bot asks), `warn` (process anyway), `skip` (leave in downloads) |
| `TZ` | | UTC | Time zone for logs |

`docker-compose.yml` maps `DOWNLOADS_DIR` and `MUSIC_DIR` from `.env` to
`/downloads` and `/music` and sets the two paths accordingly.

## Usage

### Bot commands

| Command | What it does |
|---|---|
| `/newalbum` | Process every new album in the downloads folder and report what was done. |
| `/fixlibrary` | Check the library, show the plan; apply all, one by one, or cancel. |
| `/fixlibrary all` | The same plus a Discogs title check of every album (slow: Discogs rate limit). |
| `/help` | List the commands. |

### Command line

The same engine without Telegram, e.g. `docker compose exec album-processor …`:

```sh
python album_processor.py                       # process all new albums in downloads
python album_processor.py "<album folder>"…     # process the given folders
python album_processor.py --fix-library [--all] --plan-out plan.json   # check, save the plan
python album_processor.py --fix-library --apply plan.json              # apply a (reviewed) plan
```

### Recovering from a mistake

- `music/.album_processor/fixes.jsonl` — every library change with its old values.
- `music/.album_processor/replaced/<run>/` — albums and covers that were replaced.

## Development

```
scripts/
├── bot.py                Telegram front-end: commands, buttons, spectrograms
├── album_processor.py    entry point the bot runs
├── spectrogram.py        spectrogram images for upconvert decisions
├── albumproc/
│   ├── pipeline.py       processing of new downloads
│   ├── library.py        /fixlibrary: plan and apply
│   ├── discogs.py        Discogs lookup: title, year, genre, tracklist, credits, release type
│   ├── names.py          title cleanup, naming rules, credits, fuzzy matching
│   ├── tags.py           reading and writing tags and pictures
│   ├── covers.py         Deezer covers, comparison, normalisation
│   ├── cue.py            splitting album images by CUE sheet
│   ├── audioformat.py    decoding (PyAV) and conversion to FLAC 16/44.1
│   ├── replaygain.py     EBU R128 loudness and true peaks
│   ├── upconvert.py      spectral detection of lossy sources
│   ├── files.py          filesystem helpers and the journal
│   ├── config.py         settings from environment variables
│   └── cli.py            command line
└── tests/                unit and end-to-end tests on synthetic audio, network mocked
```

Run the tests:

```sh
cd scripts
pip install -r requirements.txt
python -B -m unittest discover -s tests -t .
```

Dependencies: [PyAV](https://github.com/PyAV-Org/PyAV) (FFmpeg decoders and the
EBU R128 meter, bundled — no system FFmpeg needed), python-soxr, soundfile,
mutagen, NumPy, Pillow, matplotlib, requests, python-telegram-bot.

## Limitations

- One user per bot; one processing run at a time.
- Opinionated: the library is FLAC 16/44.1 with the conventions above; lossy
  files are accepted as they are, but not converted or upgraded.
- Metadata comes from Discogs: an album that Discogs does not know keeps its
  folder name, tags and year from the download.
- Discogs allows 25 (60 with a token) requests per minute; a full
  `/fixlibrary all` on a large library takes a while.

## License

[MIT](LICENSE)
