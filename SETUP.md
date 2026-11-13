# Set up PickleTime Bot

You are a Grok Bot setting up the owner's court-booking bot. It runs
[PickleTime](https://github.com/kapadias/PickleTime), pinned to an exact commit, on
this Grok Bot computer, and replaces the owner's Cloud Run service and Cloud
Scheduler job. Sections 1 to 6 are the first run (your checklist:
`pickletime-bot-bootstrap`). Section 7 holds the later steps, each started by a
phrase the owner says. Steps marked **You** are the owner's alone.

| Path | What it is |
| --- | --- |
| `/workspace/pickletime-grokbot` | this repository, cloned at the tag `v0.1.0`; never edited |
| `/workspace/pickletime-desk` | everything the bot writes: `config.yaml`, `state/` (mode, ledger, run records), `logs/`, and `opt/`, `venv/`, `engine/` (Chrome, Python, PickleTime) |
| `/workspace/.pickletime-secrets/courtreserve.env` | only if the owner chooses a file instead of the secret card (mode 0600) |

Grok Bot keeps `/workspace` between sessions but not installed packages, and
the computer sleeps soon after the owner disconnects. So everything lives in
`/workspace`, and every booking run first re-checks the one thing a sleep can
take away (Chrome's system libraries) and repairs it.

Every command below is `bash /workspace/pickletime-grokbot/scripts/run.sh ...`
unless it says otherwise; it is written `run.sh` for short.

## 1. Get the pinned release

```text
git clone --depth 1 --branch v0.1.0 https://github.com/kapadias/pickletime-grokbot.git /workspace/pickletime-grokbot
```

If the tag does not exist, stop and tell the owner. An existing checkout stays
as it is.

## 2. Install

```text
bash /workspace/pickletime-grokbot/scripts/install.sh
```

About a minute the first time; a re-run only prints `skip:` lines. It installs,
all under `/workspace/pickletime-desk`:

- uv, then Python 3.11 (PickleTime needs 3.11 or later), whatever `python3` the computer has;
- PickleTime at the commit in `pins.env`, into its own venv;
- Chrome and ChromeDriver from one Chrome for Testing release (the version PickleTime's Dockerfile pins);
- Chrome's system libraries, if any are missing: with `apt-get` as root or passwordless sudo, otherwise downloaded and unpacked under `/workspace` without root;
- `config.yaml`, copied once from `config/config.template.yaml`, and the mode `rehearsal`.

A `FAIL:` line stops setup. Post it verbatim with the matching entry from
section 11. One failure is expected when PickleTime is a private repository:
`FAIL: could not clone PickleTime; if ... is private, add a read-only GitHub
token as PICKLETIME_GITHUB_TOKEN ...`. Then do section 4 now (all three
secrets in one visit to the card) and run `install.sh` again.

## 3. Prove Chrome works here

```text
run.sh doctor --full
```

`--full` books a court on PickleTime's own fake CourtReserve, served on
`127.0.0.1`, with the installed Chrome: the same code path as a real booking,
with no live site involved. Expect:

```text
[PASS] fake_site_booking: booked 11:30 AM for 60 min on the fake site in 1.5s (Chrome works here)
```

`credentials` fails until section 4. Post the output, labelled "local fake site, not CourtReserve".

## 4. The CourtReserve login

**You: add two secrets with the secure secret card**, never in chat:

| Secret | Value |
| --- | --- |
| `USERID` | the email you log in to CourtReserve with |
| `PASSWORD` | its password |
| `PICKLETIME_GITHUB_TOKEN` | only if `kapadias/PickleTime` is private: a fine-grained GitHub token with access to that one repository and **Contents: Read-only**, nothing else (GitHub, Settings, Developer settings, Fine-grained tokens) |

A secret on the card is visible to every Bot on the Grok computer, so use a
CourtReserve password you use nowhere else, and a token that can only read
PickleTime. The token is used only by `install.sh` to clone and fetch the
engine; it is never written to disk. Fallback if the card is
unavailable: create `/workspace/.pickletime-secrets/courtreserve.env` yourself,
mode 0600, with the two lines `USERID=...` and `PASSWORD=...`. The card wins
when both exist.

Then `run.sh doctor`. Expect `[PASS] credentials: USERID s***k@...` and `verdict: PASS`.

## 5. Show the plan

`run.sh status` prints the mode, `release_time`, and the next six play dates
with the day each fires. **You:** check them against your calendar.

## 6. Return the receipt

As the bootstrap skill describes. Its last line is exactly:

```text
Setup booked nothing: mode is rehearsal; your CourtReserve login was added by you through the secret card.
```

## 7. When the owner asks for more

### 7.1 "change my booking window" (or days, times, durations)

Show the change as a diff to `/workspace/pickletime-desk/config.yaml`; write it
only after the owner taps approve on the Ask-first card; then `run.sh status`
and post the new play dates and windows. Keys and their meaning:
PickleTime's [README](https://github.com/kapadias/PickleTime#configuration) and
`config.sample.yaml` in `/workspace/pickletime-desk/engine/PickleTime`. If
`release_time` changes, the `book` routine moves with it (release_time minus
the offset from 7.2).

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
   Ask first: any command carrying --owner-tapped or --ignore-ledger, including run.sh go-live and run.sh rehearse; writing, replacing or deleting /workspace/pickletime-desk/config.yaml or anything under /workspace/pickletime-desk/state; editing anything under /workspace/pickletime-grokbot or /workspace/pickletime-desk/engine; cancelling or changing a court reservation; any payment; typing the CourtReserve password into any page or command yourself.
   ```

   Auto-review is model-based and an "Ask first" rule wins over an "Allow"
   rule. A routine that stops to ask for approval at 11:15 cannot book, which is
   why the Allow rule matters.
4. **Create the `book` routine, paused**, from `routines/manifest.toml`: daily,
   at its `slot` (11:15 for a 12:00 PM release), prompt exactly:

   ```text
   Run bash /workspace/pickletime-grokbot/scripts/run.sh book --slot 11:15 and post its output verbatim.
   ```

   Read it back. Daily, because the 14-day rotation cannot be written as a
   weekly schedule; an off-day ends in seconds with one `quiet` line.
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

Prerequisites, each checked and shown to the owner:

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

**You**, on a machine with `gcloud` signed in to the project PickleTime was deployed to:

```bash
gcloud scheduler jobs pause pickletime-scheduler --location=us-west1
```

Pausing keeps the service and job, so going back is one `resume`. After a few
weeks of live bookings from Grok, delete them if you want: `gcloud scheduler
jobs delete pickletime-scheduler --location=us-west1` and `gcloud run services
delete pickletime --region=us-west1`. The Bot never runs these: it has no
Google Cloud access, and should not.

## 10. Updating

A new release changes `pins.env` (the PickleTime commit, Chrome) and the
skills. Ask the Bot: "update PickleTime Bot to vX.Y.Z". It clones the new tag
next to the old one, runs `install.sh` and `doctor --full` from the new
checkout, and after the owner's tap swaps the directories. `config.yaml`,
`state/` and the secrets are never touched.

## 11. Troubleshooting

| Line | Meaning and fix |
| --- | --- |
| `FAIL: could not download uv` / `Chrome` | the computer cannot reach astral.sh or storage.googleapis.com; ask Grok Bot support about outbound access |
| `FAIL: Chrome needs: libnss3.so ...` | no root, no sudo and no `apt-get`; ask Grok Bot support whether packages can be installed |
| `FAIL: ... has local edits` | someone changed the engine checkout; `git -C /workspace/pickletime-desk/engine/PickleTime checkout -- .` after the owner's tap |
| `[FAIL] fake_site_booking` | Chrome starts but cannot drive the page; post the line and `logs/*-doctor.log` |
| `[WARN] release_pin` | the checkout is not the tag; re-clone section 1 |
| `[FAIL] credentials` | a secret is missing or misnamed; the names are exactly `USERID` and `PASSWORD` |
| `FAILED for <date>: LoginError` | wrong password or a locked account; check it in CourtReserve, then update the card |
| `NOT BOOKED: ... not offered` | the club's duration wording changed; set `duration_labels` (7.1), see PickleTime's README |

The engine's full log for every run is in `/workspace/pickletime-desk/logs/`,
and each run's record (timings, verdict, lateness) in `state/runs/`.

## 12. If you are not Grok Bot

The same scripts run on any Linux machine with cron. Set `PT_DESK` if you want
the desk somewhere other than `/workspace/pickletime-desk`, run `install.sh`,
export `USERID` and `PASSWORD` (or use the secrets file), and add a cron line:

```text
15 11 * * * bash /path/to/pickletime-grokbot/scripts/run.sh book --slot 11:15 >> /path/to/book.out 2>&1
```

(with the machine's timezone set to the facility's, or `CRON_TZ=America/Los_Angeles`).
