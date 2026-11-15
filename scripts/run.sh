#!/usr/bin/env bash
# The one entry point for routines and for the owner's requests.
#
#   bash /workspace/pickletime-grokbot/scripts/run.sh book --slot 11:15
#   bash /workspace/pickletime-grokbot/scripts/run.sh report
#   bash /workspace/pickletime-grokbot/scripts/run.sh status | timing | doctor [--full]
#   bash /workspace/pickletime-grokbot/scripts/run.sh capture [--open-slot]
#   bash /workspace/pickletime-grokbot/scripts/run.sh schedule show|propose --answers JSON|apply --owner-tapped
#   bash /workspace/pickletime-grokbot/scripts/run.sh go-live --owner-tapped
#
# Before 'book', 'doctor' and 'capture' it re-checks what a computer sleep can take away
# (Chrome's system libraries), quietly unless something had to be fixed.
set -euo pipefail

# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

cmd="${1:-}"
case "$cmd" in
  book|report|status|timing|doctor|capture|schedule|go-live|rehearse) ;;
  *) echo "usage: run.sh {book|report|status|timing|doctor|capture|schedule|go-live|rehearse} [options]" >&2; exit 2 ;;
esac

if [ "$cmd" = "book" ] || [ "$cmd" = "doctor" ] || [ "$cmd" = "capture" ]; then
  if ! out="$(bash "$PT_CHECKOUT/scripts/install.sh" --quick 2>&1)"; then
    echo "FAILED: the booking computer is not ready:"
    echo "$out" | grep -E '^(FAIL|info):' || echo "$out"
    exit 3
  fi
  # Only mention the repair when one happened.
  echo "$out" | grep -q 'installed' && echo "$out" | grep '^ok: chrome system libraries installed' || true
  # install.sh --quick may have unpacked libraries; pick up LD_LIBRARY_PATH.
  # shellcheck source=env.sh
  . "$PT_CHECKOUT/scripts/env.sh"
fi

exec "$PT_VENV/bin/python" "$PT_CHECKOUT/scripts/ptbot.py" "$@"
