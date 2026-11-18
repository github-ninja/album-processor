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

A label catalogue confirmed a case-only album rename was generated from Discogs title-case conventions. Albums and tracks now use the same title styling rule. Previously accepted corrections with substantive spelling or year differences still take effect. The next replay will verify both on the same library snapshot.

The same styling pass also found catalogue entries with an acute accent used as an apostrophe, capitalization added to only one word, and alternate apostrophe typography. Preserve already valid typography while allowing a real correction such as an acute accent replaced with an apostrophe. The track and album code use the same rule. Official artist or label material confirms both positive and negative cases.

The rehearsal caught eight incorrect spellings on an album whose cassette and master responses shared the same catalogue transcription. A contemporary track list supports the library's original spellings. Treat a repeated word replacement across distinct tracks as evidence of a parallel transcription and suppress that edition's spelling edits. Isolated spelling errors still use programme, timing and master evidence. Add a regression covering both the repeated pattern and the unrelated misspelling from the same candidate.

The original CD back cover independently confirms the transcription mismatch. The cached CLI replay preserved the eight original titles and kept the positive spelling benchmarks. The full suite exposed a vinyl-position fixture that intentionally reused the same misspelling on every track. The fixture now uses three distinct typos to keep measuring out-of-order vinyl positions, and the new transcription test covers repeated differences separately.

The full suite now passes all 214 tests in the rebuilt public Docker image with networking disabled. Image dependencies, CLI help and the Compose structure passed validation. An isolated CLI replay against the read-only library reached the normal lock and dispatch path by mounting a separate temporary state directory. A live artwork upgrade was verified against the current local image: thumbnail correlation 0.990119, 500×500 to 1000×1000. The full live pass is past 430 albums and the final CLI replay is queued.

The final replay also retains the Discogs client status in maintenance: a service failure is reported as an incomplete check and a rejected candidate is distinct from an empty result. This reuses the existing lookup contract without adding a request. Local planning still completes.

The 468-album live scan completed without HTTP 429. The first complete replay identified a credit provenance issue in the captured release: the artist profile name and the actual printed credit are distinct fields. The Discogs adapter now uses the edition's artist name variation in track credits, guests and remixers, while retaining the primary name for album identity. This needs no additional query. Replay the same catalogue data once more after validating the change.

The rehearsal returned two different JPEG digests for the same visually matching cover on separate Deezer lookups. A digest check alone would prevent that unsafe replacement but force a new plan. Store the approved Deezer JPEG in the plan, verify its digest and apply it offline. Keep image bytes out of the journal. Local artwork is read from the already validated source snapshot. Retain the verification path for legacy plans.

The last cached replay exposed duplicate guest credits when a release-specific variant was applied to a supplementary role. Keep the artist variation for the edition's track performers, and the existing canonical names for guest and remix roles. One journal representation now also removes raw JPEG data from both success and failure records. The focused suite passed 29 tests. Replay the final planner with cached Discogs replies and the captured Deezer JPEG, then compare every proposal against its read-only source.

## Live maintenance verification complete

- The authenticated catalogue pass processed 468 albums with no HTTP 429. The final exact CLI replay used 2,118 exact Discogs responses and 15 compatible cached responses, with zero external requests. The originally selected Deezer JPEG was replayed with the network disabled.
- The first full pass proposed changes for 149 albums. The final plan proposes 63. It contains six album identity/path changes, 204 individual tag-field updates, 95 track renames and one artwork upgrade. No duplicates, conversion work or ReplayGain recalculation were proposed.
- A read-only verifier compared every affected album with its source snapshot and every recorded old tag with its file. It checked the generated filenames, case-insensitive destination collisions, per-disc counts and the saved JPEG digest. The upgraded cover matches the existing artwork and increases resolution from 500×500 to 1000×1000.
- The final public image built with the pinned dependency layer. All 217 tests passed with networking unavailable. Compose structure, CLI help, dependency integrity and the Git whitespace check passed.
- The reviewed files and captured responses stayed outside the public repository. The active deployment and media were left as observed. The plan is a preview; all application tests used synthetic media. Published release dates may disagree with Discogs where its catalogue lacks an original release.
