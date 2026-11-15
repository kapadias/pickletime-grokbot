<p align="center">
  <picture>
    <source media="(max-width: 600px)" srcset="assets/hero-mobile.svg">
    <img src="assets/hero.svg" width="800" alt="PickleTime Bot, an automatic CourtReserve pickleball court booking bot for Grok Bot. A pickleball court, a release clock at 12:00:00, and a Grok chat message reading: BOOKED Saturday 21 November at 6:30 PM for 60 minutes.">
  </picture>
</p>

<h1 align="center">PickleTime Bot: automatic CourtReserve court booking for Grok Bot</h1>

<p align="center">
  <a href="https://github.com/kapadias/pickletime-grokbot/releases"><img src="https://img.shields.io/github/v/release/kapadias/pickletime-grokbot?include_prereleases&style=flat-square&label=release" alt="Latest release"></a>
  <a href="https://github.com/kapadias/pickletime-grokbot/actions/workflows/ci.yml"><img src="https://github.com/kapadias/pickletime-grokbot/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776ab?style=flat-square" alt="Python 3.11 or later">
  <img src="https://img.shields.io/badge/runs%20on-Grok%20Bot-111?style=flat-square" alt="Runs on Grok Bot">
</p>

<p align="center">
  <a href="#start">Start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#what-protects-you">Safety</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="SETUP.md">Full runbook</a>
</p>

**PickleTime Bot books your pickleball court on CourtReserve the second it is
released**, from a [Grok Bot](#start), and tells you in your Grok chat. Good
courts at busy clubs go in seconds at the daily release (a fixed time, a set
number of days ahead). The bot is already logged in, on the right date and armed
before the release, then fires on the clock and works through your preferred
times and durations until it wins. You set your club, days and times in a short
chat with the Bot; no files to edit.

No server to run, no cloud bill: a single Grok Bot does it all, and the result
arrives as a chat message on your phone. Everything it needs is in this repository: the booking engine
(`src/pickletime`), the Grok Bot setup, and the scripts that glue them.

| | |
| --- | --- |
| ⏱️ **Fires on the second** | Warms up three minutes early, then sprints at the exact release time |
| 🗣️ **Your own schedule** | Set up in a short chat: your club, days, times and durations |
| 🗓️ **Flexible patterns** | Weekly days, longer rotations, weekday-specific windows, fallback durations |
| 🧪 **Rehearses first** | Starts in dry-run mode; real bookings only after you approve "go live" |
| 🛡️ **Books once** | A ledger that survives restarts stops a second court on the same date |
| 💬 **Reports in chat** | `BOOKED`, `NOT BOOKED` or `quiet` on off days, with timings |
| 🔁 **Both CourtReserve layouts** | The older calendar and the newer member portal |

## Start

Open Grok Bot, create a new Bot, and paste:

> Set up PickleTime Bot from https://github.com/kapadias/pickletime-grokbot/blob/v0.3.0/skills/pickletime-bot-bootstrap/SKILL.md. Follow that bootstrap skill and the runbook at https://github.com/kapadias/pickletime-grokbot/blob/v0.3.0/SETUP.md step by step. Install everything under /workspace, prove Chrome works on the local fake site, ask me for my CourtReserve login only through the secure secret card (never in chat), keep the bot in rehearsal mode, and finish with the setup receipt.

The Bot installs Python, the booking engine and Chrome under `/workspace`, then
books a court on a local fake CourtReserve to prove Chrome works on its
computer. It asks for your CourtReserve email and password, added as `USERID`
and `PASSWORD` through the secure secret card, never in chat. Then it
**interviews you for your schedule**: your club's booking link, how far ahead
and when courts open, which days, what times, how long. It shows you the
result with your next play dates, and saves it only when you tap approve.
Nothing is booked yet: the bot starts in **rehearsal** mode, where every run
stops before confirming.

Then say these, in order:

| Say | What happens | What you do |
| --- | --- | --- |
| **"turn on the routine"** | creates the daily `book` routine (11:15), runs one test, unpauses it | set the Bot's Timezone and Notifications; paste two Auto-review rules |
| **"show routine timing"** (after a few play days) | how late the routines really started, and the offset it recommends | move the routine if asked |
| **"go live"** | shows the safety checks (rehearsals, timing; you may waive those), then switches to real bookings | tap approve |

Any time: **"show my schedule"**, **"change my schedule"**, **"status"**,
**"did it work?"**, **"run the doctor"**, **"back to rehearsal"**,
**"book now"**, **"capture the booking page"**. Moving from an older Cloud Run
setup? Say **"retire Cloud Run"** for the command that pauses it.

### Already running PickleTime Bot?

Paste this in your Bot's chat. Your schedule, login and booking history are
kept; it shows your current schedule and asks whether to keep it or change it:

> Update PickleTime Bot to v0.3.0 following section 10 of https://github.com/kapadias/pickletime-grokbot/blob/v0.3.0/SETUP.md, keeping my config and state. Then show my schedule and ask me whether to keep it or change it.

## How it works

<p align="center">
  <picture>
    <source media="(max-width: 600px)" srcset="assets/how-it-works-mobile.svg">
    <img src="assets/how-it-works.svg" width="800" alt="How PickleTime Bot books a court: a Grok routine starts at 11:15 every day; off days post quiet within seconds; on a play day the bot warms up at 11:57 with Chrome, login and the booking date, sprints at 12:00:00 through your preferred times and durations, books one court, and posts the result in your Grok chat.">
  </picture>
</p>

The routine fires about 45 minutes early on purpose. Grok Bot routines have been
reported to start 10 to 37 minutes late, and a court released at noon is gone in
seconds, so the bot starts early and holds on its own clock. Once the engine
takes over it fires on the second, exactly as the Cloud Run version did.

On the newer CourtReserve member portal, the bot steps the scheduler to your
date, finds the open slot on your preferred time's row, and opens the
reservation form. **Before confirming, it checks that the form shows exactly that
day and start time**, then picks the duration and ticks the disclosure.

### What you see in your chat

```text
quiet - Sat 21 Nov is cycle day 5, not one of your play days [3, 7, 11, 12]

BOOKED Saturday, November 21, 2026 at 6:30 PM for 60 min (explicit confirmation)
chrome=0.4s login=0.3s navigate=0.1s select_date=2.8s armed_wait=171.2s sprint=0.9s

REHEARSAL (dry run, nothing saved): Saturday, November 21, 2026: Dry run: 6:30 PM / 60min was available and would have been booked
```

### Set your own schedule

Say **"set up my schedule"** (a new bot does this during setup) or **"change my
schedule"**. The Bot asks a few questions, then shows you exactly what it will
use before anything changes:

```text
PROPOSED schedule (not in use yet):
  Club: CourtReserve organisation 12345, Pickleball (sport 678) at https://app.courtreserve.com
  Courts open: 7:00 AM America/New_York, 7 days ahead -> the book routine runs daily at 06:15
  Play days: every Tuesday, Thursday
  Times: 6:00 PM-8:00 PM, tries 7:00 PM, 6:30 PM; 60 then 30 min
  Next play dates:
    Tue 24 Nov 2026  6:00 PM-8:00 PM   (booked on Tue 17 Nov)
    Thu 26 Nov 2026  6:00 PM-8:00 PM   (booked on Thu 19 Nov)
Say 'apply my schedule' to use it, or 'discard the proposal'.
```

Your tap applies it, and the previous version is kept as a backup. Weekly days
are the common case; longer rotations (a 14-day cycle) and different times on
particular weekdays (Saturday mornings) work too. A fresh bot names no club and
refuses to book until you set one, so it can never book at somebody else's
club. Every setting, with its default: [config/config.sample.yaml](config/config.sample.yaml).

## What protects you

- **Rehearsal first.** A new bot only does dry runs. "go live" needs your tap on
  an Ask-first card, and refuses on the command line without it.
- **One command.** Routines and the Bot run only
  `bash /workspace/pickletime-grokbot/scripts/run.sh ...`. The Bot never drives
  CourtReserve in its own browser, never types your password, never cancels or
  pays. The Auto-review rules in [routines/autoreview.txt](routines/autoreview.txt)
  enforce this.
- **The right slot or nothing.** On the member portal the reservation form must
  show the exact day and time the bot meant, or it closes the form unconfirmed.
- **Pinned code.** The Bot clones this repository at a release tag and installs
  the engine from that checkout, with Chrome pinned in [pins.env](pins.env), so a
  later push cannot change what books your court.
- **No double bookings.** The ledger in `/workspace/pickletime-desk/state/`
  survives the computer's sleeps. If the site ever leaves the outcome unclear,
  the bot reports `UNVERIFIED`, stops, and asks you to check CourtReserve.
- **Your login stays on the card.** A secret on Grok's card is visible to every
  Bot on that computer, so use a CourtReserve password you use nowhere else.

## Limits

- **Routine timing is the risk.** A routine that starts after the release still
  tries, and says so. "show routine timing" measures lateness from real runs,
  and "go live" refuses while any rehearsal started late.
- **A long hold may be cut off.** If Grok stops the 45-minute hold, the runbook
  switches to a detached run plus a 12:30 report routine ([SETUP.md](SETUP.md) §8).
- **Missed runs are not replayed.** Say "book now" to try later the same day.
- **When CourtReserve changes its pages,** the bot fails safely and names the
  fix: "capture the booking page" records the new layout, scrubbed of names and
  emails, so the engine can be updated.

## FAQ

### How do I automatically book pickleball courts on CourtReserve?

Create a Grok Bot, paste the [Start](#start) prompt, add your CourtReserve login
through the secret card, answer a few questions about your club, days and
times, and say "turn on the routine". The bot rehearses first, then books for
real once you say "go live".

### How do I change the days or times it books?

Tell the Bot "change my schedule" and say what is different ("play Wednesdays
instead of Thursdays", "try 7 PM first"). It shows the new schedule and next
play dates, and changes nothing until you tap approve.

### Does it work with the new CourtReserve member portal?

Yes. It books on both the older calendar and the newer single-page member
portal, and picks the right one automatically.

### Can it book on a rotating or alternating-week schedule?

Yes. Tell the Bot the pattern: weekly days, or a longer cycle such as 14 days
where the free weekdays alternate between weeks. Particular weekdays can have
their own times, too.

### What happens if the court I want is already taken?

It works down your list: priority times first, then the rest of your window,
then shorter durations, retrying for `sprint_seconds` after the release. If
nothing is left it posts `NOT BOOKED` with the reason.

### Is my CourtReserve password safe?

It is stored only in Grok Bot's secure secret card, never in chat, files or this
repository, and the engine's logs redact it. Every Bot on the same Grok computer
can read card secrets, so use a password unique to CourtReserve.

### Do I need a server or Google Cloud?

No. Everything runs on the Grok Bot's own computer under `/workspace`. The same
scripts also run on any Linux machine with cron ([SETUP.md](SETUP.md) §12).

### Which clubs does it support?

Clubs that take bookings through CourtReserve (app.courtreserve.com). You give
the Bot your club's booking link and it reads the club and sport from it. It
was built for pickleball and has been used live on a large club's courts; other
CourtReserve sports are a setting, untested so far.

<details>
<summary><b>Repository layout</b></summary>

```
src/pickletime/       the booking engine: settings, Chrome, login, both layouts, the slot race
scripts/run.sh        the one entry point (routines, owner requests)
scripts/install.sh    installs uv, Python 3.11, the engine and Chrome under /workspace
scripts/ptbot.py      book / report / status / timing / doctor / capture / go-live
scripts/env.sh        paths; sourced by the others
pins.env              release tag, Chrome version, Python version
config/               the config template copied to the desk, and the full key reference
skills/               the Bot's skills: first run, and operations
agents/               the Bot's profile and system prompt
routines/             routine manifest and Auto-review rule texts
template/             the Grok Bot template contract (skill sha256s)
tests/                runner tests; tests/engine: engine tests, fake CourtReserve and fake portal
assets/               the pictures in this README
```

</details>

<details>
<summary><b>Development</b></summary>

```bash
pip install -e '.[dev]'
```

```bash
pytest
```

`pytest` runs the runner tests, the engine tests and the end-to-end tests,
which drive a real Chrome against a fake CourtReserve and a fake member portal
(and skip when no Chrome is found). Also run `python3 scripts/check_template.py`,
`ruff check src tests scripts` and `mypy`.

To try the whole thing on a Linux machine outside Grok, point the desk somewhere
disposable:

```bash
PT_DESK=/tmp/pt-desk bash scripts/install.sh
```

```bash
PT_DESK=/tmp/pt-desk bash scripts/run.sh doctor --full
```

</details>

## License and credits

MIT. See [LICENSE](LICENSE). The setup pattern (pinned release, bootstrap skill,
secret card, Auto-review rules, receipt) follows
[aegis-grok-desk](https://github.com/fairgradient-ai/aegis-grok-desk).

PickleTime Bot is an independent project, not affiliated with or endorsed by
CourtReserve, Life Time or xAI. Use it within your club's booking rules.
