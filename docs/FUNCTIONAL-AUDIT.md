# Functional audit

Baseline: `fef8fd8`. The initial suite passed 169 tests using Python 3.13, the pinned libraries and an isolated container with networking disabled. The public README and local README were byte-identical.

| Contract | Baseline implementation | Baseline tests | Findings and changes to verify |
| --- | --- | --- | --- |
| Import queue and discovery | CLI scans flat, nested and disc layouts. A job copies each album into the library's state directory. Per-album exceptions leave the rest of the queue running. | Workflows, import jobs, safety, Deezer | An empty disc folder containing nested audio can fall through cleanup. Explicit paths need an ancestor guard. |
| Discogs identity, credits and track matching | Master determines identity, audio edition supplies tracks, independent artist/title/ordering checks guard candidates. | Names, workflows, transport, jobs | Preserve recent master/edition and vinyl position rules. Check malformed API replies and maintenance's supplementary lookup. |
| Names, tags and layout | Shared name/credit rules, format-specific Mutagen writers, metadata plans and batch renames. | Names, workflows, media, safety | Cover rename/format collisions, preserve multi-value tags and lossy payloads. Import and maintenance use the same disc-number rule. |
| CUE and conversion | CUE parser and sample-aligned slicing, then PyAV/soxr/libsndfile conversion. MP3 and AAC in M4A skip conversion. | CUE, media, workflows, safety | Check advertised input encodings, output frame counts and truncated audio. Preserve files that collide with conversion outputs. |
| Upconvert | Channel power spectra and cutoff heuristic; per-track spectrogram in Telegram. | Media, safety, workflows | Preserve numerical and antiphase coverage. Pending decisions must refer to the same source snapshot. |
| ReplayGain, DR and passport | EBU R128 true peaks and gated gains; FLAC sample identity and versioned DR/flat-top cache. | Media, safety, jobs, workflows | Validate recoverable audio before publication; confirm measured true peaks and handle missing or invalid source tags. |
| Covers | Deezer same-album lookup, local fallback, image similarity, size normalization and disc copies. | Media, workflows, safety | Preserve manually supplied disc covers. Handle invalid responses and image decoding failures. |
| Deezer downloads | Downloader queue, retries, album ID and actual track count gate, quarantine. | Deezer, safety | Validate response shape and bind durable album identity to the current folder contents. |
| Releases and schedule | Deezer artist search and albums, ownership and edition filters, same-name warning, persistent schedule, cursor and history. | Releases, safety | Partial outages preserve the cursor; inspect state shape and reporting on failures. |
| Gemini opinion | Library lookup, passport, listener profile and Discogs edition dossier; fallback on overload or empty output. | Opinion, jobs, safety | Verify malformed responses and retained identity; factual quality remains subject to the model. |
| Telegram and CLI | Authorized command/callback handlers; cancellable CLI subprocess with JSON reports; polling for automatic releases. | Bot lifecycle, safety, workflow calls | Callback validation and action must share the same process lock. Exercise CLI entry points and shutdown. |
| Library maintenance | Plan/apply, stale tag and collision checks, duplicate and cover archives, in-place track edits. | Workflows, safety | Stage changes per album, preserve originals and reject stale file sets. Maintain saved plan compatibility. |
| Persistent state and recovery | Atomic JSON writes, per-library flock, JSONL journal, private staging, replacement archives. | Jobs, safety, releases, workflows | An interruption can land between archive rename and job update; source cleanup can stop midway. Record intent first and exercise recovery. |
| Docker and CI | Python 3.13 slim, pinned installs, mounted media, exec-form bot and unittest workflow. | Existing GitHub workflow | Build and verify with disposable resources and synthetic media. |

## Independent decision benchmarks

The benchmark expected outcomes come from catalogue identity, position semantics, or synthetic signals with known properties. The cases below were run against both the original commit and the revised worker. All nine failed on the baseline and passed on the revised code.

| Case | Expected outcome | Baseline result |
| --- | --- | --- |
| First Discogs hit for Plaid's *Spokes* contains an unrelated track, second lists “Even Spring” | Use the hit supported by the local track. Warp lists “Even Spring” as the opener. | Accepted the first hit without usable track evidence. |
| Three local tracks titled “Intro” and one catalogue track with that title | One possible pair, overlap `2 × 1 / (3 + 1) = 0.5`. | Overlap `1.0`. |
| Complete track positions from an unrelated release | Preserve the library album's identity. | Proposed a replacement identity. |
| Malformed Discogs search payload | Report a service failure and preserve local metadata. | Raised outside the normal lookup result. |
| Two same-title albums by one artist with different programmes | Keep both albums in the library. | Proposed archiving one as a duplicate. |
| Ordinary title containing “Live” and a new numbered sequel | Include both, excluding the live recording and remaster controls. | Filtered ordinary studio titles. |
| Catalogue adds an album after its listed release date | Report the late addition once without repeating alternate IDs from the previous run. | Missed the late addition and reported the alternate ID. |
| Native 32 kHz lossless audio resampled to 44.1 kHz | Allow import without interpreting the introduced bandwidth limit as a codec signature. | Deferred the source for a possible upconvert. |
| Four equal samples straddling two 3-second DR blocks | Count one flat top. | Counted zero. |

Additional controlled cases cover two discs sharing track numbers, sequential vinyl sides, a UTF-16 CUE with embedded art, invalid or changing Deezer data, distinct versus matching cover images, corrupt Gemini output and decoder truncation. The AAC import test compares every compressed packet before and after tagging. The true-peak benchmark uses a quarter-rate tone with sample peak `0.8 / √2` and continuous amplitude `0.8` and verifies the oversampled result. Fault-injection tests cover both sides of the archive/publication rename, partially completed source deletion, a stale replacement preview and a stale maintenance plan. A real subprocess verifies the bot's worker shutdown path.

References: [Warp's Plaid catalogue](https://plaid.warp.net/release/72583-plaid-spokes), [Deezer recording catalogue](https://www.deezer.com/album/302127), [FFmpeg EBU R128 filter](https://ffmpeg.org/ffmpeg-filters.html#ebur128), [PyAV frame API](https://pyav.basswood.io/docs/stable/api/frame.html), [Python filesystem semantics](https://docs.python.org/3.13/library/os.html#os.rename).

## Verification limits

- The public Deezer API returned 14 tracks for Daft Punk's *Discovery*. Catalog fixtures and application tests require no credentials.
- Live Discogs requests timed out in this environment. Telegram, Gemini and deezer-downloader interactions were verified with controlled responses. The installed service and the owner's media were outside the test setup.
- Catalogue search is deliberately bounded by queries and candidate counts. Ambiguous metadata, incomplete catalogue entries and different masters with the same programme may still require manual review. The benchmark validates specific error cases, so a catalogue-wide precision or recall rate would require a larger labelled corpus.
- Deezer completeness checks the album ID, file snapshot and expected distinct file count. Exact recording identity would require per-track evidence from the download provider.
- Release notification IDs are written when the worker produces the report. A Telegram delivery failure after that point can require a manual catalogue check.
- Filesystem recovery covers interruption of the process. Power loss, storage failure and independent writes during commit remain outside the guarantee. CUE splitting keeps the source sample positions and uses 16-bit or 24-bit FLAC intermediates. The quality cache and loudness tag writer cover FLAC.
