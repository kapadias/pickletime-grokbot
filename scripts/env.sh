# shellcheck shell=bash
# Sourced by every script. Paths only; no side effects beyond exports.
#
# Grok Bot keeps /workspace between sessions but not installed packages, so
# everything the bot needs - Python, the venv, Chrome, state - lives under
# PT_DESK, outside the release checkout (which must stay clean).

PT_CHECKOUT="${PT_CHECKOUT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PT_DESK="${PT_DESK:-/workspace/pickletime-desk}"

# shellcheck source=../pins.env
. "$PT_CHECKOUT/pins.env"

PT_OPT="$PT_DESK/opt"
PT_VENV="$PT_DESK/venv"
PT_ENGINE="$PT_DESK/engine/PickleTime"
PT_STATE="$PT_DESK/state"
PT_LOGS="$PT_DESK/logs"
PT_SECRETS_FILE="${PT_SECRETS_FILE:-/workspace/.pickletime-secrets/courtreserve.env}"

export PT_CHECKOUT PT_DESK PT_OPT PT_VENV PT_ENGINE PT_STATE PT_LOGS PT_SECRETS_FILE
export BOT_RELEASE PICKLETIME_REPO PICKLETIME_COMMIT CHROME_VERSION PYTHON_VERSION

# uv and its Python downloads stay in /workspace so they survive a sleep.
export UV_INSTALL_DIR="$PT_OPT/bin"
export UV_PYTHON_INSTALL_DIR="$PT_OPT/python"
export UV_CACHE_DIR="$PT_OPT/cache/uv"
export PATH="$PT_OPT/bin:$PATH"

# PickleTime reads these.
export CHROME_BIN="$PT_OPT/chrome/chrome"
export CHROME_DRIVER_PATH="$PT_OPT/chromedriver/chromedriver"
export PICKLETIME_CONFIG="$PT_DESK/config.yaml"
export PICKLETIME_LEDGER_PATH="$PT_STATE/ledger.json"
# Container Chrome flags (--no-sandbox, no /dev/shm reliance) - the Grok Bot
# computer is a container, not a desktop.
export PICKLETIME_CONTAINER=1
# Never pick up a stray .env from whatever directory a routine starts in.
export PICKLETIME_SKIP_DOTENV=1
export SE_CACHE_PATH="$PT_OPT/cache/selenium"
export PYTHONUNBUFFERED=1
# Libraries extracted without root (see install.sh), if any were needed.
if [ -d "$PT_OPT/syslibs" ]; then
  export LD_LIBRARY_PATH="$PT_OPT/syslibs/usr/lib/x86_64-linux-gnu:$PT_OPT/syslibs/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
