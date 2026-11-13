# PickleTime Bot

A Grok Bot that books your pickleball courts on CourtReserve the moment they
open. Everything it needs is in this repository: the booking engine
(`src/pickletime`), the Grok Bot setup, and the scripts that glue them. It
replaces the Cloud Run service and Cloud Scheduler job: the Bot's daily routine starts the
booking, and the result arrives as a chat message on your phone.

```
Grok routine 11:15 ──▶ off-day? ──yes──▶ "quiet - Sat 21 Nov is cycle day 5 ..."   (seconds)
   (daily)                │
                          no
                          ▼
                 hold until 11:57 ──▶ engine: Chrome, login, grid, date ──▶ 12:00:00 sprint ──▶ "BOOKED Thu 19 Nov at 6:30 PM for 60 min"
```

The routine fires 45 minutes early on purpose. Grok Bot routines have been
reported to start 10 to 37 minutes late, and a court released at noon is gone
in seconds. So the Bot starts early and holds on its own clock. Once the engine
takes over, it fires on the second, exactly as it did on Cloud Run.

## Start

Open Grok Bot, create a new Bot, and paste:

> Set up PickleTime Bot from https://github.com/kapadias/pickletime-grokbot/blob/v0.1.0/skills/pickletime-bot-bootstrap/SKILL.md. Follow that bootstrap skill and the runbook at https://github.com/kapadias/pickletime-grokbot/blob/v0.1.0/SETUP.md step by step. Install everything under /workspace, prove Chrome works on the local fake site, ask me for my CourtReserve login only through the secure secret card (never in chat), keep the bot in rehearsal mode, and finish with the setup receipt.

The Bot installs Python, the booking engine and Chrome under
`/workspace`, then books a court on a local fake CourtReserve to prove Chrome
works on its computer. It asks you for one thing: your CourtReserve email and
password, added as `USERID` and `PASSWORD` through the secure secret card. It
ends with your next play dates and a receipt. Nothing is booked yet: the bot
starts in **rehearsal** mode, where every run stops before Save.

Then say these, in order:

| Say | What happens | What you do |
| --- | --- | --- |
| **"turn on the routine"** | creates the daily `book` routine (11:15), runs one test, unpauses it | set the Bot's Timezone and Notifications; paste two Auto-review rules |
| **"show routine timing"** (after a few play days) | how late the routines really started, and the offset it recommends | move the routine if asked |
| **"go live"** | checks three rehearsals passed and timing is safe, then switches to real bookings | tap approve; pause the Cloud Scheduler job the same day |
| **"retire Cloud Run"** | gives you the one `gcloud` command to pause the old job | run it on your own machine |

Any time: **"status"**, **"did it work?"**, **"run the doctor"**, **"change my
booking window to ..."**, **"back to rehearsal"**, **"book now"**.

[SETUP.md](SETUP.md) is the full runbook the Bot follows.

## What protects you

- **Rehearsal first.** A new bot only does dry runs. "go live" needs your tap
  on an Ask-first card, and refuses on the command line without it
  (`refused: changing the mode needs the owner's approval`).
- **One command.** Routines and the Bot run only
  `bash /workspace/pickletime-grokbot/scripts/run.sh ...`. The Bot never drives
  CourtReserve in its browser, never types your password, never cancels or
  pays. The Auto-review rules in [routines/autoreview.txt](routines/autoreview.txt)
  enforce this.
- **Pinned code.** The Bot clones this repository at a tag and installs the
  engine from that checkout, with Chrome at the version in [pins.env](pins.env),
  so a later push cannot change what books your court. The doctor checks the
  tag and that the installed engine came from it.
- **No double bookings.** The engine's ledger lives in
  `/workspace/pickletime-desk/state/ledger.json`, which survives the computer's
  sleeps, so a date booked once is not booked again.
- **Your login stays on the card.** A secret on Grok's card is visible to every
  Bot on the computer, so use a CourtReserve password you use nowhere else.

## Limits

- **Routine timing is the risk.** If a routine starts after the release, the
  run still tries, and posts `WARNING: this routine started after the release`.
  `show routine timing` measures lateness from real runs, and "go live" refuses
  while any rehearsal started late.
- **A long hold may be cut off.** The routine's command holds about 45 minutes.
  If Grok stops it, `show routine timing` says so, and SETUP.md section 8
  switches to a detached run plus a 12:30 report routine.
- **Missed runs are not replayed.** Say "book now" to try later the same day.
- Notifications come from Grok, not Cloud Monitoring SMS; the config template
  turns `gcp_notifications_enabled` off.

## Repository

```
scripts/run.sh        the one entry point (routines, owner requests)
src/pickletime/      the booking engine: settings, Chrome, login, grid, the slot race
scripts/install.sh    installs uv, Python 3.11, the engine, Chrome under /workspace
scripts/ptbot.py      book / report / status / timing / doctor / go-live
scripts/env.sh        paths; sourced by the others
pins.env              release tag, Chrome version, Python version
config/               the config template copied to the desk, and the full key reference
skills/               the Bot's skills: first run, and operations
agents/               the Bot's profile and system prompt
routines/             routine manifest and Auto-review rule texts
template/             the Grok Bot template contract (skill sha256s)
tests/                runner tests; tests/engine: engine tests and a fake CourtReserve
pyproject.toml        the engine's package and dependencies
```

## Development

```bash
pip install -e '.[dev]'
```

```bash
pytest
```

`pytest` runs the runner tests, the engine tests and the end-to-end tests,
which drive a real Chrome against `tests/engine/fake_site.py` (a stand-in
CourtReserve) and skip when no Chrome is found. Also run
`python3 scripts/check_template.py`, `ruff check src tests scripts` and `mypy`.

To try the whole thing on a Linux machine, outside Grok, point the desk somewhere disposable:

```bash
PT_DESK=/tmp/pt-desk bash scripts/install.sh
```

```bash
PT_DESK=/tmp/pt-desk bash scripts/run.sh doctor --full
```

## License

MIT. See [LICENSE](LICENSE). The setup pattern (pinned release, bootstrap
skill, secret card, Auto-review rules, receipt) follows
[aegis-grok-desk](https://github.com/fairgradient-ai/aegis-grok-desk).
