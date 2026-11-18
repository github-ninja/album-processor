# Engineering review

Baseline: `fef8fd8e46e0ec8b2ab722aa4f460ebdc19f031b`, matching GitHub main and its README on 2026-11-18. Branch: `audit/engineering-review`. This checkout is separate from the production bind mount. The original checkout has an untracked local AGENTS.md.

- Read the GitHub README, local instructions, deployment files, dependency locks, earlier audit and relevant Git history.
- Baseline: 169 tests passed in 29.9 s on Python 3.13 with pinned project libraries and the container network disabled.
- Decision: retain one CLI worker, the Telegram adapter, existing domain modules and file-based state. Strengthen the two album publication paths first. Preserve old jobs and plans.
- In progress: make publication recoverable by recording archive destinations before moving any album. Then tighten source cleanup and decisions, followed by maintenance and remaining subsystems.
- Production services, credentials and media are outside the test setup.

Import changes are implemented. Archive paths are written before publication, cleanup resumes from a private sibling directory, and preview decisions are validated under the writer lock. Added fault injection after an archive rename and during source cleanup. Focused tests are running. Next operation: stage maintenance changes per album and recover pending publication under the same lock.

The 17 import job tests passed, and the initial maintenance build passed the full 175-test suite. Maintenance now writes a pending record before the album swap and keeps the original in replaced/. Added recovery tests for both sides of the swap. Next operation: verify media length and numeric measurements, then validate integration response shapes and Docker packaging.

The owner extended the review to independently verify domain decisions, with explicit false-positive and false-negative cases. Keep the current module layout, revise the Discogs selection boundary so callers can reject a candidate without terminating search, and assess audio and release heuristics against independently expected outcomes. The focused media, safety and import suite passed 80 tests before the integration changes.

The domain pass found one-to-many Discogs track matches, early termination after a rejected result, release keywords used out of context, and false spectral positives caused by resampling. Changes now cover these cases, distinct same-title albums in maintenance, and invalid integration responses. The source fixtures are being extended with correct catalog/programme data and negative controls. The public Deezer API returned Daft Punk's Discovery as a 14-track album. Unauthenticated Discogs timed out with both transport options. A Docker build reached PyPI and is downloading pinned wheels.

Architecture and the functional audit are documented in docs/. The baseline failed all nine independently selected comparison tests. The revised suite passed 200 tests before the final silence/ReplayGain check. Compose configuration and dependency integrity passed. Next operation: run the full suite on the final built image, smoke-test the CLI and runtime user with isolated volumes, then commit the branch. The first build is downloading Pillow after successful acquisition of PyAV, Matplotlib and NumPy.

The complete 202-test suite passed from commit 0b5c0f1. A final callback trace found that Telegram's short key still omitted the compared library snapshot. The fix includes that snapshot in the key so a refreshed preview cannot revive the previous button. A dedicated unit test covers both keys. The initial image build is finishing the remaining wheels.

## Final verification

- Built the public Dockerfile with the pinned dependencies and the final source tree (image ID starts with `314f3bf84c7e`).
- Ran the complete suite in the built image and with networking disabled. All 203 tests passed in 39.1 s. `pip check` passed.
- The disposable container ran with init, UID/GID 1000, writable synthetic media mounts and the correct Europe/Moscow offset. CLI smoke checks passed for help, maintenance planning and application, release status, quality and empty import discovery.
- Public Compose configuration validation and `git diff --check` passed. The code is in commits `0b5c0f1` and `d2b2e1e`.
- Production still uses the original checkout. Live Telegram, Gemini, downloader and Discogs checks remain outside this verification. The public Deezer metadata probe succeeded. The functional audit records the decision benchmarks and remaining limits.

## Follow-up: live library plan review

- Production baseline `fef8fd8`, audit branch baseline `1d9a7af`. The full production planner is running against a read-only bind mount in a disposable container. Discogs responded through the bot's existing network route. Responses and private review artifacts are kept outside the repository.
- The first proposals include case-only and competing catalogue spellings. Compare an independent label source when the editions disagree. Check album identity, local audio programme, path collisions, tag coherence, covers, conversion and gain separately.
- Next operation: classify the baseline proposals, patch confirmed policy errors in the responsible module, replay the same responses and verify the fixes.

Live audit replies showed that fully populated Discogs track positions could still import names from a different edition. Existing album tags also lost meaningful punctuation through the download-folder parser. Maintenance now preserves local album punctuation on an unverified match and checks the complete positional programme before accepting catalogue track titles and credits. Discogs follows the verified release's master link, uses master search as fallback and avoids a second lookup for supplemental metadata during the full check. The targeted pre-change and post-change samples share the captured API responses. The optimized full run is active with a single live caller.

Independent source checks confirmed the need to distinguish catalogue editing from release metadata. Discogs applies standard title casing (the Warp release keeps the lower-case styling). A CD and a current digital edition of the same album carry different text, and their timings separate the editions in the library. Spelling changes now require timing agreement and corroboration from the master response already fetched. Position-validated credits stay attached to the relevant track. Sample replay preserves the affected existing titles while retaining independently confirmed corrections.

The live run reached 170 of 468 albums. A recorded release response contained null format descriptions. The client now treats absent optional collections as empty, so this valid audio edition stays in consideration without repeated candidate requests. The focused regressions and earlier full suite passed. The final replay is queued after the live run and only requests data missing from the captured response set.

The full audit has passed 290 albums without an HTTP 429. A confirmed maintenance gap remains: applying a reviewed cover plan fetches the artwork again, so the selected image can change between review and application. Record the image digest in the plan and verify the selected bytes on the staged copy before publication. Test both changed and unavailable artwork on synthetic albums. Also check cross-album destination collisions in the final audit output.

The staged cover verification now binds application to the planned JPEG digest. A synthetic test rejected both a different Deezer image and a missing response without writing the source album. The same plan applied successfully when the approved image was available. The complete 212-test suite passed on Python 3.13 with pinned dependencies and networking disabled. The full catalogue pass continues before the cached replay.
