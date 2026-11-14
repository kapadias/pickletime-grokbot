---
name: pickletime-bot-operations
description: Standing instructions for PickleTime Bot after setup - what each owner phrase runs (turn on the routine, show routine timing, go live, status, change my booking window, book now, retire Cloud Run), how routine output is posted, which commands need the owner's tap, and what to do when a run fails or a routine is late. Use for every request after the first run.
license: MIT
metadata:
  version: "0.2.0"
  category: booking
---

# PickleTime Bot: operations

Every command is `bash /workspace/pickletime-grokbot/scripts/run.sh ...`,
written `run.sh` below. Post its output verbatim; add at most one plain
sentence of your own, and never a number the tool did not print.

## Phrases

| The owner says | You do |
| --- | --- |
| "status", "what's next?" | `run.sh status` |
| "run the doctor" | `run.sh doctor` (add `--full` to re-prove Chrome on the fake site) |
| "turn on the routine" | SETUP.md 7.2: create the `book` routine from `routines/manifest.toml`, paused; one Test run; unpause; read it back |
| "show routine timing" | `run.sh timing`; if it recommends a different offset, offer to move the routine (owner confirms) |
| "go live" | SETUP.md 7.3: check the prerequisites, then `run.sh go-live --owner-tapped` after the owner taps approve |
| "back to rehearsal", "pause bookings" | `run.sh rehearse --owner-tapped` after the tap; to stop runs entirely, pause the routine |
| "change my booking window / days / times" | SETUP.md 7.1: show the diff to `/workspace/pickletime-desk/config.yaml`, write it after the tap, then `run.sh status` |
| "book now", "try again today" | only after the owner's tap: `run.sh book` (a live run if mode is live). Never `--ignore-ledger` unless the owner asks for it by name |
| "did it work?" | `run.sh report` |
| "retire Cloud Run" | SETUP.md 9 |
| "capture the booking page" | SETUP.md 11.1: after the owner's tap, `run.sh capture --open-slot`; post its output verbatim and tell the owner the screenshots' paths |

## Routine output

- The `book` routine always posts. Off-days post one `quiet - ...` line;
  that is a correct outcome, not a failure.
- A play day posts the verdict line (`BOOKED ...`, `NOT BOOKED ...`,
  `REHEARSAL (dry run, nothing saved): ...` or `FAILED ...`) and a timing line.
- `WARNING: this routine started after the release` means the routine was too
  late to win the race. Tell the owner, run `run.sh timing`, and offer to move
  the routine earlier.

## When something fails

| Output | Next step |
| --- | --- |
| `FAILED: configuration or credentials` | `run.sh doctor`; fix the failing check (a missing secret: ask the owner to re-add it through the card) |
| `FAILED: the booking computer is not ready` | `bash /workspace/pickletime-grokbot/scripts/install.sh`, then `run.sh doctor --full` |
| `FAILED for <date>: LoginError ...` | the site rejected the login: the owner checks the password on the card, or whether CourtReserve locked the account |
| `FAILED for <date>: NavigationError: The bookings page loaded but has no Kendo scheduler grid ...` | CourtReserve changed the page layout. Nothing was booked. Offer to pause the `book` routine, then SETUP.md 11.1 (capture) so the engine can be updated |
| `UNVERIFIED <date> at <time> ...` | Not a failure and not a booking. Tell the owner to check CourtReserve now; never run `book` again for that date until they have. If the court is there, ask them to share the `Site's answer after Confirm` line |
| `NOT BOOKED <date>: ...` | a business outcome (courts were gone). Post it; suggest widening the window only if the owner asks |
| `UNAVAILABLE - today's run ... was cut off` | Grok stopped the long-running command; follow SETUP.md 8 (detached mode) |
| `NOT RUN - started ... min before` | the routine is scheduled too early for the 120-minute cap; move it later |

Missed runs are never replayed later: a court released at noon is gone by
afternoon. If the owner wants a court anyway, they say "book now".

## Never

- Never drive CourtReserve in the computer's browser, and never type the password.
- Never edit `/workspace/pickletime-grokbot`.
- Never cancel, move or pay for a reservation.
- Never run `go-live`, `rehearse`, `--owner-tapped` or `--ignore-ledger` without the owner's tap on that exact command.
