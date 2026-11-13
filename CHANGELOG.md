# Changelog

## 0.1.0

First release.

- The booking engine (`src/pickletime`): validated config, rotation and per-weekday windows, Chrome lifecycle, login, grid navigation, the candidate plan, the release-time arm and sprint, and the double-booking ledger. Standalone: nothing is cloned from another repository at install time. The Cloud Run HTTP service and Google Cloud alerting are not included; the Grok chat is the alert.
- Grok Bot setup: Start prompt, bootstrap and operations skills, agent profile, runbook (SETUP.md).
- `scripts/install.sh`: uv, Python 3.11, the engine from this checkout and Chrome for Testing, all under `/workspace`; repairs Chrome's system libraries after a computer sleep, with or without root.
- `scripts/run.sh` / `ptbot.py`: `book` skips off-days in seconds, holds until 3 minutes before `release_time`, then runs the engine; `report`, `status`, `timing`, `doctor [--full]`, `go-live` / `rehearse` (owner-tapped).
- Rehearsal mode by default; every run recorded with its routine lateness so the schedule can be tuned from real data.
- `doctor --full` books on the test suite's local fake CourtReserve to prove Chrome works on the Grok computer without touching the live site.
- Tests: the runner's unit tests, the engine's unit tests and end-to-end tests that drive a real Chrome against the fake site.
