# Changelog

## 0.3.1

Releases are now published as branches named `release/vX.Y.Z`, which never move once published (a tag `vX.Y.Z` still works if one exists). The Start and update prompts, the clone commands and the doctor's release check use them, so a release no longer needs a tag pushed by hand.

The first live run on the member portal reached Confirm once (7:30 PM, 30 min) and the form stayed open with no message; the other nine attempts never reached Confirm, and the single pass took 59.5 s. This release makes the next run both more likely to win and decisive about what happened.

- Before pressing Confirm, the bot waits (up to 5 s) for the form's own background loading to finish: opening the form and changing the duration each make it recalculate cost and courts, and a press during that can be silently ignored, one likely cause of the live run's form staying open.
- After Confirm, the bot keeps waiting while the form is busy (loading button, spinner, or a request still on its way), up to 45 s, instead of closing it after 15 s. The page's own requests are watched as they start, not only when they finish.
- If Confirm caused nothing at all (no request started since the press, no busy state, no new text) for 5 s, it is pressed once more with a real mouse click. Since nothing reached the site, this cannot double-book; a slow request with no spinner is never pressed twice.
- An unknown second dialog after Confirm (a charge to accept, say) is never clicked: the run stops as `UNVERIFIED`, recording the dialog's text and buttons.
- A longer booking is skipped without opening the form when a later half hour it needs reads "None Available" (60 min at 6:30 needs 7:00 open). It stays in the plan, so a court that frees up is still tried on the next pass.
- Every attempt logs one line with its outcome and reason, and the chat shows them under the verdict as `Tried: ...`. Every Confirm (up to five) logs what the page showed, including new text in the form, the Confirm button's state and request counts, and saves a screenshot when the form stays open or a second dialog appears.

## 0.3.0

Anyone can set the bot up for their own club and schedule, and owners can change theirs by chat.

- `run.sh schedule show|propose|apply|discard`: the Bot interviews the owner (club booking link, sport, timezone, how far ahead and when courts open, days, times, durations), proposes a validated schedule with the next play dates and a diff, and installs it only after the owner's tap, keeping a backup. Changing one thing keeps every other key, including advanced ones the interview never asks about. A club link gives the organisation and sport; weekday names become a 7-day rotation; longer rotations and per-weekday times are supported.
- A fresh bot names no club: the config template is generic, the runner refuses to book until a club is set, and the doctor warns `no club set yet` instead of passing.
- The daily routine time follows the schedule (`schedule show` names it: release time minus 45 minutes), and `schedule apply` says when the routine must move.
- After an update the Bot shows the current schedule and asks whether to keep it or change it.
- On the member portal, a form that closes after Confirm without an error, on the same page, now counts as booked (`inferred`) and is recorded in the ledger: the first live booking showed the portal closes the form quietly on success. A form that stays open or errors is a refusal; one that closes while the page navigates away stays `UNVERIFIED`.
- "go live": the rehearsal and timing prerequisites are advice the owner may waive.
- The README, template and sample config no longer carry one owner's club or rotation.

## 0.2.0

Booking works on CourtReserve's member portal, the layout Lifetime Sunnyvale moved to in November 2026. Built from two scrubbed live captures.

- Navigation recognises either layout and the bot picks the matching booking service; the older calendar keeps working as before.
- New `PortalBookingService`: steps the scheduler with its own Next control, waiting for it to re-enable and for the day's slot data to load (the live toolbar is disabled while a day loads, which is what stalled the first capture after one day); finds a time's open slot by pairing it with the row's time label; opens the form and confirms it shows exactly that weekday, day and start time before doing anything else; picks the duration and checks the form took it; ticks the disclosure and checks it reads ticked; presses Confirm.
- After Confirm a booking counts only when a message confirms it. Until the first live booking shows what the portal says, a silent close is reported `UNVERIFIED` (the run stops rather than risk a second court), and every run that presses Confirm prints the site's answer so it can be shared.
- At the release, the day that is not open yet is retried via the page's own Next control, its refresh control, then a full reload, for `release_grace_seconds`.
- `doctor --full` also books on a fake of the portal.
- Tests: end to end against the fake portal (priority slot and duration, duration fallback, dry run, a form showing the wrong time is never confirmed, a refused slot moves on, slow-loading days, the release); each safety check was confirmed to fail its test when removed.

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
