---
name: pickletime-bot-bootstrap
description: First run of PickleTime Bot on Grok Bot - clone the pinned release, install the booking engine, Chrome and Python under /workspace, prove Chrome works on a local fake CourtReserve, take the CourtReserve login through the secure secret card (never chat), interview the owner for their club, days and times and set up their schedule after their tap, and return the setup receipt. Booking stays in rehearsal (dry runs) until the owner says "go live". Use for a first setup, a repair or a readiness check.
license: MIT
metadata:
  version: "0.3.1"
  category: booking
---

# PickleTime Bot: first run

`SETUP.md` in the checkout is the full runbook; this skill is its checklist for
sections 1 to 6. Each step ends with evidence: a tool line, a readback or the
owner's own confirmation. A step that cannot be done is reported with its exact
manual step, and everything that depends on it stays pending. Nothing is
reported as done on a guess.

The first run asks the owner for one thing: their CourtReserve login, added as
two secrets through the secure secret card. Everything else is the Bot's work.

## Safety boundary for setup

- Setup books nothing. The mode starts as `rehearsal`: every booking run is a
  dry run that walks the whole flow but never clicks Save. Only "go live"
  (SETUP.md 7.3), after the owner's tap, makes bookings real.
- The only credentials are `USERID` and `PASSWORD` for CourtReserve, added by
  the owner through the secret card. Never ask for them in chat.
- Local writes stay under `/workspace/pickletime-grokbot` (the release
  checkout, written once by `git clone`) and `/workspace/pickletime-desk`.
- Never sign in to CourtReserve in the computer's browser.

## 1. Get the pinned release

```text
git clone --depth 1 --branch release/v0.3.1 https://github.com/kapadias/pickletime-grokbot.git /workspace/pickletime-grokbot
```

If the release does not exist, stop and tell the owner. An existing checkout is
kept as it is (no reset, no overwrite); report what the doctor says about it.

## 2. Install

```text
bash /workspace/pickletime-grokbot/scripts/install.sh
```

One line per step, `ok:` or `skip:`. It installs uv, Python 3.11, the booking
engine from this checkout, and Chrome for Testing into `/workspace/pickletime-desk`,
and copies the config template to `/workspace/pickletime-desk/config.yaml`.
A `FAIL:` line stops setup; post it verbatim with SETUP.md's troubleshooting
step for it.

## 3. Prove Chrome works here, without the live site

```text
bash /workspace/pickletime-grokbot/scripts/run.sh doctor --full
```

Expect `[PASS] fake_site_booking: booked 11:30 AM for 60 min on the fake site`
and `[PASS] fake_portal_booking: stepped 8 days and booked 6:30 PM for 60 min on
the fake portal` (CourtReserve's newer layout). At this point `credentials`
fails (no secret yet), `config` warns `no club set yet` (step 5 sets it) and
`release_pin` passes on the tagged clone. Post the output verbatim, labelled "local fake site, not
CourtReserve".

## 4. The one owner action: the CourtReserve login

Ask the owner to open the secure secret card and add:

- `USERID`: the email they log in to CourtReserve with;
- `PASSWORD`: its password.

Tell them, before they add it: a secret on the card is visible to every Bot on
this Grok computer, so use a CourtReserve password that is not used anywhere
else. Fallback, only if the card is unavailable: the file
`/workspace/.pickletime-secrets/courtreserve.env` (lines `USERID=...` and
`PASSWORD=...`, mode 0600), created by the owner, never by you.

Then run `bash /workspace/pickletime-grokbot/scripts/run.sh doctor` and expect
`[PASS] credentials: USERID s***k@...`.

## 5. Their schedule

Follow SETUP.md section 5: a short interview (club booking link, sport,
timezone, how far ahead and when courts open, days, times, durations), then
`run.sh schedule propose --answers '<json>'`. Post its output verbatim and ask
the owner to check the play dates and the day each is booked against their real
calendar: a schedule off by one day is invisible until a court is booked on the
wrong evening. Fix and propose again until they are happy; after their tap,
`run.sh schedule apply --owner-tapped`. Then `run.sh doctor`: expect
`[PASS] config: ... club <number>` and `verdict: PASS`.

## 6. The receipt

One message. Three plain lines first: the bot is installed (release v0.3.1
and its commit), Chrome works on this computer, and it is in rehearsal mode
so nothing will be booked yet. Then the evidence: install lines, the doctor
verdict, `run.sh schedule show`, the credential readback (masked). Name the
time the daily routine will run (from `schedule show`).

Then the next phrases, in order:

1. **"turn on the routine"**: the daily booking routine in rehearsal (SETUP.md 7.2);
   "change my schedule" any time (SETUP.md 7.1);
2. **"show routine timing"** after a few days: how late Grok routines really start;
3. **"go live"**: real bookings, after the owner's tap (SETUP.md 7.3);
4. **"retire Cloud Run"**: pause the old Cloud Scheduler job (SETUP.md 9).

Last line, exactly, when the doctor passed:

```text
Setup booked nothing: mode is rehearsal; your CourtReserve login was added by you through the secret card.
```
