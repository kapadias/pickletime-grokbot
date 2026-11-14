# Changelog

## 0.1.2

The first live capture showed the new layout: a single-page portal (Ant Design) whose slots come from `ReadConsolidated` JSON, open slots as `[data-testid=consolidated-reserve-slot]`, and a booking form with a duration combobox, a disclosure checkbox and a Confirm button. It left three gaps, which this capture fills:

- Slot map: the grid is scrolled top to bottom and every slot element is paired with the time label on its row, so evening slots below the fold are included.
- Date stepping looks for "next" only inside the date toolbar (the one with Today), re-finds it after every click, and records the toolbar's controls and the label after each step. The live run stopped after one day.
- The booking form's duration list is opened and its options recorded, then closed without choosing; checkbox and expanded states are recorded.
- Every `data-testid` on the page, with counts and sample text.
- `/Date(ms)/` timestamps and JSON inside query strings (such as `jsonData`) are kept in scrubbed form.
- The summary leads with what the engine rewrite needs, leaves routine requests out (they stay in capture.json) and allows 48,000 characters.
- Tests: a fake of the new portal (`tests/engine/fake_portal.py`) built from the capture, including a decoy "next" control outside the toolbar.

## 0.1.1

- The first live run showed Lifetime Sunnyvale's bookings page on a new CourtReserve layout without the Kendo scheduler the engine reads. Booking cannot work on it until the engine is updated for that layout; this release gathers what that needs.
- New `run.sh capture [--open-slot]`: signs in and records the bookings page's structure, the site's data requests and (with `--open-slot`) one booking form, scrubbed of names and free text, plus screenshots. Never books. Ask-first.
- The navigation error now says when the bookings page loaded without the grid this release reads, instead of blaming a missing menu entry, and names the capture command.

## 0.1.0

First release.

- The booking engine (`src/pickletime`): validated config, rotation and per-weekday windows, Chrome lifecycle, login, grid navigation, the candidate plan, the release-time arm and sprint, and the double-booking ledger. Standalone: nothing is cloned from another repository at install time. The Cloud Run HTTP service and Google Cloud alerting are not included; the Grok chat is the alert.
- Grok Bot setup: Start prompt, bootstrap and operations skills, agent profile, runbook (SETUP.md).
- `scripts/install.sh`: uv, Python 3.11, the engine from this checkout and Chrome for Testing, all under `/workspace`; repairs Chrome's system libraries after a computer sleep, with or without root.
- `scripts/run.sh` / `ptbot.py`: `book` skips off-days in seconds, holds until 3 minutes before `release_time`, then runs the engine; `report`, `status`, `timing`, `doctor [--full]`, `go-live` / `rehearse` (owner-tapped).
- Rehearsal mode by default; every run recorded with its routine lateness so the schedule can be tuned from real data.
- `doctor --full` books on the test suite's local fake CourtReserve to prove Chrome works on the Grok computer without touching the live site.
- Tests: the runner's unit tests, the engine's unit tests and end-to-end tests that drive a real Chrome against the fake site.
