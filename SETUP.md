# Set up PickleTime Bot

You are a Grok Bot setting up the owner's court-booking bot. It runs the
booking engine in this repository (`src/pickletime`) on this Grok Bot computer,
and replaces the owner's Cloud Run service and Cloud
Scheduler job. Sections 1 to 6 are the first run (your checklist:
`pickletime-bot-bootstrap`). Section 7 holds the later steps, each started by a
phrase the owner says. Steps marked **You** are the owner's alone.

| Path | What it is |
| --- | --- |
| `/workspace/pickletime-grokbot` | this repository, cloned at the release `release/v0.3.2` (a branch that never moves once published); never edited |
| `/workspace/pickletime-desk` | everything the bot writes: `config.yaml`, `state/` (mode, ledger, run records), `logs/`, and `opt/`, `venv/` (Chrome, Python, the installed engine) |
| `/workspace/.pickletime-secrets/courtreserve.env` | only if the owner chooses a file instead of the secret card (mode 0600) |

Grok Bot keeps `/workspace` between sessions but not installed packages, and
the computer sleeps soon after the owner disconnects. So everything lives in
`/workspace`, and every booking run first re-checks the one thing a sleep can
take away (Chrome's system libraries) and repairs it.

Every command below is `bash /workspace/pickletime-grokbot/scripts/run.sh ...`
unless it says otherwise; it is written `run.sh` for short.

## 1. Get the pinned release

```text
git clone --depth 1 --branch release/v0.3.2 https://github.com/kapadias/pickletime-grokbot.git /workspace/pickletime-grokbot
```

If the release does not exist, stop and tell the owner. An existing checkout
stays as it is.

## 2. Install

```text
bash /workspace/pickletime-grokbot/scripts/install.sh
```

About a minute the first time; a re-run only prints `skip:` lines. It installs,
all under `/workspace/pickletime-desk`:

- uv, then Python 3.11 (the engine needs 3.11 or later), whatever `python3` the computer has;
- the booking engine (`src/pickletime` in this checkout) and its dependencies, into their own venv;
- Chrome and ChromeDriver from one Chrome for Testing release (the version in `pins.env`);
- Chrome's system libraries, if any are missing: with `apt-get` as root or passwordless sudo, otherwise downloaded and unpacked under `/workspace` without root;
- `config.yaml`, copied once from `config/config.template.yaml`, and the mode `rehearsal`.

A `FAIL:` line stops setup. Post it verbatim with the matching entry from
section 11.

## 3. Prove Chrome works here

```text
run.sh doctor --full
```

`--full` books a court on the test suite's fake CourtReserve, served on
`127.0.0.1`, with the installed Chrome: the same code path as a real booking,
with no live site involved. Expect:

```text
[PASS] fake_site_booking: booked 11:30 AM for 60 min on the fake site in 1.5s (Chrome works here)
[PASS] fake_portal_booking: stepped 8 days and booked 6:30 PM for 60 min on the fake portal in 6.0s
```

`credentials` fails until section 4. Post the output, labelled "local fake site, not CourtReserve".

## 4. The CourtReserve login

**You: add two secrets with the secure secret card**, never in chat:

| Secret | Value |
| --- | --- |
| `USERID` | the email you log in to CourtReserve with |
| `PASSWORD` | its password |

A secret on the card is visible to every Bot on the Grok computer, so use a
CourtReserve password you use nowhere else. Fallback if the card is
unavailable: create `/workspace/.pickletime-secrets/courtreserve.env` yourself,
mode 0600, with the two lines `USERID=...` and `PASSWORD=...`. The card wins
when both exist.

Then `run.sh doctor`. Expect `[PASS] credentials: USERID s***k@...` and `verdict: PASS`.

## 5. Set up the schedule

A fresh bot names no club, and refuses to book until it does. Interview the
owner in plain words, one short question at a time, offering the default in
brackets:

1. **Club:** "Open your club's court booking page in CourtReserve (the one for
   your sport) and paste its address." It looks like
   `https://app.courtreserve.com/Online/Reservations/Bookings/12345?sId=678`.
2. **Sport name** as the club writes it [Pickleball], and the club's
   **timezone** [America/Los_Angeles].
3. **When courts open:** how many days ahead members may book [7], and the time
   the furthest day opens [12:00 PM] (or "rolling" if there is no fixed time).
4. **Days:** which weekdays to play ("Monday and Thursday", or "every day"). For
   a pattern that alternates between weeks, ask for a start Monday, the cycle
   length and which days of the cycle.
5. **Times:** the window to search ("6 to 8 PM"), the preferred start times in
   order, and the durations in order [60 then 30 minutes]. Ask whether any
   weekday needs different times (Saturday mornings, say).

Turn the answers into JSON (keys in `src/pickletime/schedule.py`'s docstring)
and propose it. Nothing live changes:

```text
run.sh schedule propose --answers '{"facility_url": "https://app.courtreserve.com/Online/Reservations/Bookings/12345?sId=678", "timezone": "America/New_York", "release_time": "7:00 AM", "days_in_advance": 7, "play_days": ["Tuesday", "Thursday"], "window": {"start": "6:00 PM", "end": "8:00 PM"}, "priority_times": ["7:00 PM", "6:30 PM"], "durations": [60, 30]}'
```

It prints the schedule in plain words, the next six play dates with the day
each is booked, any notes (a preferred time outside the window, say) and the
changes. Post it verbatim and ask the owner to check the dates against their
calendar. A `FAILED:` line says which answer to fix; ask again. When the owner
taps approve:

```text
run.sh schedule apply --owner-tapped
```

The previous config is kept in `/workspace/pickletime-desk/backups/`.
`run.sh schedule show` describes the schedule at any time, including the time
the daily routine must run (the release time minus 45 minutes).

## 6. Return the receipt

As the bootstrap skill describes. Its last line is exactly:

```text
Setup booked nothing: mode is rehearsal; your CourtReserve login was added by you through the secret card.
```

## 7. When the owner asks for more

### 7.1 "show my schedule", "change my schedule"

`run.sh schedule show` describes the current schedule. To change it, ask only
about what the owner wants different and propose just those answers; every
other key, including advanced ones the interview never asks about, is kept:

```text
run.sh schedule propose --answers '{"priority_times": ["7:00 PM", "6:30 PM"]}'
```

Post the result; after the owner's tap, `run.sh schedule apply --owner-tapped`.
"keep my schedule" or "discard the proposal": `run.sh schedule discard`. If
`apply` prints `ACTION: move the daily book routine to HH:MM`, edit the `book`
routine's time and its `--slot` to match, and read it back. Every key, its
default and meaning: `config/config.sample.yaml`.

### 7.2 "turn on the routine"

1. **Routines work.** Create one paused routine whose prompt is only "Reply
   with the single word quiet.", read it back, delete it, read back that it is
   gone. Without routines the bot cannot book on its own: say so and stop.
2. **You: Timezone and Notifications.** In this Bot's settings, set Timezone to
   the `timezone` in `config.yaml` (America/Los_Angeles) and turn
   Notifications on, so a booking reaches your phone. Then say "done".
3. **You: the Auto-review rules.** In **Settings, General, Auto-review**, add
   the two rules from `routines/autoreview.txt`, exactly as written: the first
   as an "Allow automatically" rule, the second as an "Ask first" rule.

   ```text
   Allow automatically: the command bash /workspace/pickletime-grokbot/scripts/run.sh followed by exactly one of: book (optionally --slot and a time HH:MM, optionally --detach), report, status, timing, doctor (optionally --full), with nothing else on the command line (no pipe, redirection, semicolon or second command). These runs sign in to CourtReserve with the owner's stored credentials, book at most one court per play date as /workspace/pickletime-desk/config.yaml says, and write files only under /workspace/pickletime-desk.
   ```

   ```text
   Ask first: any command carrying --owner-tapped or --ignore-ledger, including run.sh go-live and run.sh rehearse; run.sh capture, which signs in and may open a booking form; writing, replacing or deleting /workspace/pickletime-desk/config.yaml or anything under /workspace/pickletime-desk/state; editing anything under /workspace/pickletime-grokbot; cancelling or changing a court reservation; any payment; typing the CourtReserve password into any page or command yourself.
   ```

   Auto-review is model-based and an "Ask first" rule wins over an "Allow"
   rule. A routine that stops to ask for approval at 11:15 cannot book, which is
   why the Allow rule matters.
4. **Create the `book` routine, paused**, from `routines/manifest.toml`: daily,
   at the time `run.sh schedule show` names ("the book routine runs daily at
   HH:MM": the release time minus 45 minutes; 11:15 for a 12:00 PM release),
   prompt exactly, with that time:

   ```text
   Run bash /workspace/pickletime-grokbot/scripts/run.sh book --slot 11:15 and post its output verbatim.
   ```

   Read it back. Daily even for a weekly pattern: off-days end in seconds with
   one `quiet` line, and rotations longer than a week need it.
5. **One Test run.** Run the routine once now. What it posts depends on the
   day and the hour, and each one is a pass:
   - an off-day: `quiet - ...`, in seconds;
   - a play day after today's release: it runs at once and posts a REHEARSAL
     verdict from the live site (nothing is saved);
   - a play day within 2 hours before release: it waits until 3 minutes before
     release, then posts a REHEARSAL verdict;
   - a play day more than 2 hours before release: `NOT RUN - started ... min before`.

   Any of these proves the routine sees the secrets and the desk files. Report
   whether it raised an Ask-first card; if it did, the Allow rule needs fixing.
6. **Unpause** and read back the routine's next run time.
7. **Optional:** the weekly `health` routine from the manifest (Sunday 18:05,
   `run.sh doctor --full`) catches a broken Chrome or a deleted secret before the
   week's bookings.

The mode stays `rehearsal`. Each play day the routine walks the whole booking
flow against the live CourtReserve and stops before Save.

### 7.3 "go live"

Prerequisites, each checked and shown to the owner. 1 and 2 are advice: the
owner may waive them by saying so plainly ("go live now, no more
rehearsals"); record that in the reply. 3 is not optional while the old service
can still book:

1. at least three play-day rehearsals with a REHEARSAL verdict (`run.sh status`
   lists recent runs), so the flow is proven against the live site;
2. `run.sh timing` shows no run that started after the release, and its
   recommended offset is not larger than the routine's current offset (move the
   routine first if it is);
3. **the Cloud Run booking is retired at the same time** (section 9). Two bots
   booking the same date keep separate ledgers and can book two courts.

Then, after the owner taps approve on its card:

```text
run.sh go-live --owner-tapped
```

It prints `mode: rehearsal -> live`. From the next play day, a found slot is
booked for real. `run.sh rehearse --owner-tapped` goes back.

**Confirmation on the member portal.** A message saying the reservation was
made is an explicit confirmation. A form that closes after Confirm without an
error, on the same page, also counts as booked ("the site closed the form
without an error"): the first live booking showed the portal closes the form
quietly on success. A form that stays open or shows an error is a refusal. A
form that closes while the page moves elsewhere is `UNVERIFIED`, and the run
stops so it cannot book a second court. Every run that presses Confirm prints a
line starting `Site's answer after Confirm`; keep it in the reply.

## 8. If routines are cut off (detached mode)

If `run.sh timing` reports runs "cut off or still running", Grok stopped the
`book` command during its wait. Switch the `book` routine to the manifest's
`prompt_detached`:

```text
Run bash /workspace/pickletime-grokbot/scripts/run.sh book --slot 11:15 --detach and post its output verbatim.
```

It starts the booking as a background process and returns at once. Add the
`report` routine (daily 12:30: `run.sh report`), which posts the outcome. If
detached runs are also cut off (the computer slept), the Grok computer cannot
hold a run until release: keep the Cloud Run booking (section 9 undone) and
tell the owner.

## 9. "retire Cloud Run"

**You**, on a machine with `gcloud` signed in to the project the Cloud Run service was deployed to:

```bash
gcloud scheduler jobs pause pickletime-scheduler --location=us-west1
```

Pausing keeps the service and job, so going back is one `resume`. After a few
weeks of live bookings from Grok, delete them if you want: `gcloud scheduler
jobs delete pickletime-scheduler --location=us-west1` and `gcloud run services
delete pickletime --region=us-west1`. The Bot never runs these: it has no
Google Cloud access, and should not.

## 10. Updating

A new release changes the engine, `pins.env` (Chrome) and the skills. Ask the
Bot: "update PickleTime Bot to vX.Y.Z". It clones the new release
(`git clone --depth 1 --branch release/vX.Y.Z ...`) next to the old
one, runs `install.sh` and `doctor --full` from the new checkout, and after the
owner's tap swaps the directories. `config.yaml`, `state/` and the secrets are
never touched.

After an update, run `run.sh schedule show`, post it, and ask the owner: keep
this schedule, or change it? "Keep" changes nothing. "Change" follows 7.1. If
the routine's time no longer matches what `schedule show` names, offer to move
it.

## 11. Troubleshooting

| Line | Meaning and fix |
| --- | --- |
| `FAIL: could not download uv` / `Chrome` | the computer cannot reach astral.sh or storage.googleapis.com; ask Grok Bot support about outbound access |
| `FAIL: Chrome needs: libnss3.so ...` | no root, no sudo and no `apt-get`; ask Grok Bot support whether packages can be installed |
| `[FAIL] engine: ... run scripts/install.sh` | the venv holds code from another checkout; run `install.sh` again |
| `[FAIL] fake_site_booking` | Chrome starts but cannot drive the page; post the line and `logs/*-doctor.log` |
| `[FAIL] fake_portal_booking` | the engine cannot drive the member-portal layout on this computer; post the line and `logs/*-doctor.log` |
| `UNVERIFIED <date> at <time>` | the form closed after Confirm with no message. The owner checks CourtReserve; if the court is there, share the `Site's answer` line so the engine learns the confirmation. Never re-run for that date before checking |
| `[WARN] release_pin` | the checkout is not the tag; re-clone section 1 |
| `[FAIL] credentials` | a secret is missing or misnamed; the names are exactly `USERID` and `PASSWORD` |
| `FAILED for <date>: LoginError` | wrong password or a locked account; check it in CourtReserve, then update the card |
| `NavigationError: The bookings page loaded but has no Kendo scheduler grid` | CourtReserve moved the facility to a new page layout; the engine needs updating. Pause the `book` routine and follow 11.1 |
| `Site's answer ... (no verdict, form still open)` | the form neither closed nor showed an error after Confirm, even after the bot waited while it looked busy and pressed again when nothing happened. The owner checks My Bookings; share the line and its screenshot |
| `Confirm opened another dialog the bot does not know` | a second step after Confirm (for example a charge to accept). The bot never clicks unknown dialogs; share the line and screenshot |
| `NOT BOOKED: ... not offered` | the club's duration wording changed; set `duration_labels` (7.1); `config/config.sample.yaml` explains it |

The engine's full log for every run is in `/workspace/pickletime-desk/logs/`,
and each run's record (timings, verdict, lateness) in `state/runs/`.

### 11.1 "capture the booking page"

When CourtReserve changes its pages, the engine needs their new structure.
After the owner taps approve on the Ask-first card:

```text
run.sh capture --open-slot
```

It signs in, opens the bookings page, steps to the date the bot would book,
and opens one open slot's booking form, then closes it. It never clicks Save,
Confirm, Submit, Pay, Book or Reserve inside a form, and it writes:

- `summary.txt`: steps, the site's data requests (JSON reduced to its shape),
  the booking form's controls, and an outline of each page. Text is kept only
  when it is times, dates, numbers or interface words; names, emails and notes
  become placeholders such as `«text:14»`;
- `capture.json`: the same, in full;
- screenshots (`*.png`), which are **not** scrubbed.

All under `/workspace/pickletime-desk/captures/<time>/`. Post the output
verbatim; the owner shares `summary.txt` (and screenshots, if they choose) with
whoever updates the engine. Without `--open-slot` it stops before opening any
form.

## 12. If you are not Grok Bot

The same scripts run on any Linux machine with cron. Set `PT_DESK` if you want
the desk somewhere other than `/workspace/pickletime-desk`, run `install.sh`,
export `USERID` and `PASSWORD` (or use the secrets file), and add a cron line:

```text
15 11 * * * bash /path/to/pickletime-grokbot/scripts/run.sh book --slot 11:15 >> /path/to/book.out 2>&1
```

(with the machine's timezone set to the facility's, or `CRON_TZ=America/Los_Angeles`).
