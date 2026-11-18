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
