# Album Processor architecture

Album Processor runs as one Telegram process and a short-lived Python worker per command. The same worker is available through the CLI. Domain rules and filesystem operations live in `albumproc`. The package needs no database, daemon, or message broker.

## Entry points and components

| Component | Responsibility |
| --- | --- |
| `bot.py`, `spectrogram.py` | Authenticate the owner, handle Telegram commands and buttons, render spectra and show progress. Start workers in separate process groups and terminate them during shutdown. |
| `album_processor.py`, `cli.py` | Dispatch commands, acquire the per-library lock, recover pending maintenance and translate results into exit codes, log output and JSON reports. |
| `pipeline.py`, `jobs.py`, `files.py` | Discover imports, copy and prepare one album at a time, persist decisions, verify snapshots, publish by rename, archive previous versions and recover interrupted operations. |
| `library.py` | Build reviewable plans and apply each accepted album independently. Keep album maintenance policy separate from import policy. |
| `names.py`, `tags.py` | Share metadata, credit and filename rules. Read and write container-specific tags. |
| `cue.py`, `audioformat.py`, `upconvert.py`, `replaygain.py`, `quality.py` | Split by sample position, convert lossless audio, look for spectral cutoffs, apply FLAC loudness tags and build FLAC mastering passports. |
| `discogs.py`, `covers.py`, `deezer_dl.py`, `releases.py`, `opinion.py` | Own their external calls, response handling and domain policies. The worker can run all decision tests with these calls mocked. |

## Import and maintenance

1. Import records the download snapshot and works on a private copy under `.album_processor/work/`. Deezer adds an album ID and verifies the track count and current file snapshot. CUE splitting comes before conversion. The pipeline prepares the tags, artwork and measurements before publication.
2. Album or upconvert approval refers to a specific job and source snapshot. Replacement also captures the library copies shown to the user. The worker rechecks consent under the same lock it uses for publication.
3. The worker records publication intent, archive paths and the prepared snapshot before moving any current album. It renames prior versions into `replaced/` and the prepared directory into the library. Recovery either restores the prior versions or verifies the published directory and finishes the import. Original downloads are detached under a private sibling name before recursive cleanup, which can resume after interruption.
4. Maintenance plans include the album's file snapshot, previous tag values and the digest of any approved artwork. When Deezer supplies the cover, the plan carries the chosen JPEG for offline application. The journal stores the digest. Application validates the plan, stages changes under `maintenance/work/` and uses the same archive and rename operations. A pending record allows the next worker to recover an interrupted swap. The archive holds the entire prior version, including artwork. Duplicate plans require a matching ordered programme and archive the duplicate as one directory.
5. Jobs, cache and schedule use atomic JSON replacement. `fixes.jsonl` records planned old values and application results. Existing import jobs and legacy plans remain readable. Legacy plans lack a file snapshot and retain their field-level checks. Version 1 mastering passports are recomputed lazily because version 2 fixes flat-top counting at block boundaries.

## Decision boundaries

- A Discogs master supplies album identity when the release links to it. An audio edition supplies the tracklist. Search tries bounded ranked release candidates and uses master search as a fallback. The planner reuses the checked catalogue result across metadata fields. Track matching pairs each title at most once and considers ordering when scoring editions. Full maintenance requires evidence to change album identity. Track fields also require a complete position map whose titles agree with the existing programme, including version qualifiers and available durations. Spelling changes need timing and master agreement. A repeated change across separate tracks signals a different transcription. One rule preserves existing track and album styling when Discogs only standardizes capitalization or supplies a punctuation variant. Explicit alternate recordings such as instrumental editions keep their own identity.
- Deezer release discovery uses edition qualifiers, exact normalized ownership checks, the last successful check and a 90-day overlap. An artist with a conflicting genre remains a possible namesake. Cover selection uses exact normalized artist and album identity. Existing art is upgraded when the images agree.
- Audio conversion validates decoded frames and output FLAC parameters. Spectral detection is a heuristic. Known low-rate sources do not alert on the cutoff introduced by resampling. DR and flat tops are signal measurements. They do not establish audible distortion or identify a master. Gemini receives the measured dossier and identified catalogue editions. Its prose is still a model output.

## Operational limits

Publication uses same-filesystem renames and a cooperative Linux file lock. External editors do not share the lock, and file snapshots use names, sizes and modification times. Recovery is tested for process interruption. Disk and power failures still require an independent backup. Archive retention is manual. Each successful maintenance album consumes archive space equal to its previous size. Refresh old Telegram replacement previews that predate the library snapshot by rerunning `/newalbum`.

The Docker image runs as a non-root UID and expects writable download and library volumes. Compose runs an init process for cleanup. The repository's public Compose configuration is independent of any existing self-hosted stack.

See [functional audit](FUNCTIONAL-AUDIT.md) for the benchmark cases and verification limits.
