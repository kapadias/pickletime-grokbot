# Changelog

## 0.1.0

First release.

- Grok Bot setup: Start prompt, bootstrap and operations skills, agent profile, runbook (SETUP.md).
- `scripts/install.sh`: uv, Python 3.11, PickleTime at a pinned commit and Chrome for Testing, all under `/workspace`; repairs Chrome's system libraries after a computer sleep, with or without root.
- `scripts/run.sh` / `ptbot.py`: `book` skips off-days in seconds, holds until 3 minutes before `release_time`, then runs PickleTime; `report`, `status`, `timing`, `doctor [--full]`, `go-live` / `rehearse` (owner-tapped).
- Rehearsal mode by default; every run recorded with its routine lateness so the schedule can be tuned from real data.
- `doctor --full` books on PickleTime's local fake CourtReserve to prove Chrome works on the Grok computer without touching the live site.
