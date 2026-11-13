#!/usr/bin/env bash
# Install (or repair) everything the bot needs, under /workspace/pickletime-desk.
#
#   bash scripts/install.sh          full install; idempotent, safe to re-run
#   bash scripts/install.sh --quick  only what a sleep can take away (Chrome's
#                                    system libraries); run before every booking
#
# Prints one line per step: "ok:", "skip:" or "FAIL:". Exits non-zero on the
# first failure, naming what to do. Never touches the release checkout.
set -euo pipefail

# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

say()  { printf '%s\n' "$*"; }
fail() { say "FAIL: $*"; exit 1; }

# Chrome's shared-library dependencies (the list PickleTime's Dockerfile uses).
CHROME_DEB_PACKAGES="ca-certificates fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
libatspi2.0-0 libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 \
libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
libxrandr2 tzdata"

mkdir -p "$PT_OPT/bin" "$PT_STATE/runs" "$PT_LOGS" "$(dirname "$PT_ENGINE")"

# ---------------------------------------------------------------------------
# Chrome system libraries (both modes)
# ---------------------------------------------------------------------------
missing_libs() {
  [ -x "$CHROME_BIN" ] || return 0
  ldd "$CHROME_BIN" 2>/dev/null | awk '/not found/ {print $1}' | sort -u
}

ensure_chrome_libs() {
  local missing
  missing="$(missing_libs)"
  if [ -z "$missing" ]; then
    say "ok: chrome system libraries present"
    return
  fi
  say "info: chrome is missing: $(echo "$missing" | tr '\n' ' ')"
  local apt=""
  if [ "$(id -u)" = "0" ]; then apt="apt-get"
  elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then apt="sudo apt-get"
  fi
  if [ -n "$apt" ] && command -v apt-get >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive $apt update -qq >/dev/null \
      && DEBIAN_FRONTEND=noninteractive $apt install -y -qq --no-install-recommends $CHROME_DEB_PACKAGES >/dev/null \
      || fail "apt-get could not install Chrome's libraries; see SETUP.md 'Chrome will not start'"
  elif command -v apt-get >/dev/null 2>&1 && command -v dpkg-deb >/dev/null 2>&1; then
    # No root: download the .debs and unpack them under /workspace, which
    # survives a sleep. env.sh adds them to LD_LIBRARY_PATH.
    local debs="$PT_OPT/cache/debs" lists="$PT_OPT/cache/apt-lists"
    mkdir -p "$debs" "$lists/partial" "$PT_OPT/syslibs"
    apt-get -qq -o Dir::State::Lists="$lists" -o Debug::NoLocking=1 update >/dev/null 2>&1 \
      || fail "no root and apt-get update failed; ask Grok Bot support whether the computer can install packages"
    # shellcheck disable=SC2086
    (cd "$debs" && apt-get -qq -o Dir::State::Lists="$lists" -o Debug::NoLocking=1 download $CHROME_DEB_PACKAGES >/dev/null 2>&1) \
      || fail "could not download Chrome's libraries without root"
    for deb in "$debs"/*.deb; do dpkg-deb -x "$deb" "$PT_OPT/syslibs"; done
    # shellcheck source=env.sh
    . "$(dirname "${BASH_SOURCE[0]}")/env.sh"
  else
    fail "Chrome needs: $(echo "$missing" | tr '\n' ' ')- and this computer has no apt-get"
  fi
  missing="$(missing_libs)"
  [ -z "$missing" ] || fail "still missing after install: $(echo "$missing" | tr '\n' ' ')"
  say "ok: chrome system libraries installed"
}

if [ "$QUICK" = "1" ]; then
  [ -x "$PT_VENV/bin/python" ] || fail "not installed yet: run bash $PT_CHECKOUT/scripts/install.sh"
  [ -x "$CHROME_BIN" ]         || fail "Chrome is gone from $CHROME_BIN: run bash $PT_CHECKOUT/scripts/install.sh"
  ensure_chrome_libs
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. uv (fetches Python and installs the engine)
# ---------------------------------------------------------------------------
UV_VERSION=0.11.32
if [ -x "$PT_OPT/bin/uv" ] && "$PT_OPT/bin/uv" --version | grep -q "$UV_VERSION"; then
  say "skip: uv $UV_VERSION already in $PT_OPT/bin"
else
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
    | env UV_INSTALL_DIR="$PT_OPT/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 \
    || fail "could not download uv $UV_VERSION from astral.sh"
  say "ok: uv $UV_VERSION installed in $PT_OPT/bin"
fi

# ---------------------------------------------------------------------------
# 2. The booking engine, at the pinned commit
# ---------------------------------------------------------------------------
if [ ! -d "$PT_ENGINE/.git" ]; then
  git clone -q "$PICKLETIME_REPO" "$PT_ENGINE" || fail "could not clone $PICKLETIME_REPO"
fi
if [ "$(git -C "$PT_ENGINE" rev-parse HEAD 2>/dev/null)" != "$PICKLETIME_COMMIT" ]; then
  git -C "$PT_ENGINE" fetch -q origin "$PICKLETIME_COMMIT" 2>/dev/null || git -C "$PT_ENGINE" fetch -q origin
  git -C "$PT_ENGINE" -c advice.detachedHead=false checkout -q "$PICKLETIME_COMMIT" \
    || fail "pinned commit $PICKLETIME_COMMIT is not in $PICKLETIME_REPO"
fi
[ -z "$(git -C "$PT_ENGINE" status --porcelain)" ] || fail "$PT_ENGINE has local edits; it must be the pinned commit, unmodified"
say "ok: PickleTime at ${PICKLETIME_COMMIT:0:12}, clean"

# ---------------------------------------------------------------------------
# 3. Python + venv with the engine installed
# ---------------------------------------------------------------------------
STAMP="$PT_VENV/.pickletime-commit"
if [ -x "$PT_VENV/bin/python" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$PICKLETIME_COMMIT" ] \
   && "$PT_VENV/bin/python" -c "import pickletime" 2>/dev/null; then
  say "skip: venv already has PickleTime ${PICKLETIME_COMMIT:0:12}"
else
  uv venv -q --allow-existing --python "$PYTHON_VERSION" "$PT_VENV" \
    || fail "uv could not create a Python $PYTHON_VERSION venv"
  uv pip install -q --python "$PT_VENV/bin/python" "$PT_ENGINE" \
    || fail "could not install PickleTime into $PT_VENV"
  echo "$PICKLETIME_COMMIT" > "$STAMP"
  say "ok: venv $("$PT_VENV/bin/python" --version 2>&1) with PickleTime installed"
fi

# ---------------------------------------------------------------------------
# 4. Chrome + ChromeDriver, same Chrome for Testing release
# ---------------------------------------------------------------------------
chrome_ok() {
  [ -x "$CHROME_BIN" ] && [ -x "$CHROME_DRIVER_PATH" ] \
    && [ "$(cat "$PT_OPT/chrome/.version" 2>/dev/null)" = "$CHROME_VERSION" ]
}
if chrome_ok; then
  say "skip: Chrome for Testing $CHROME_VERSION already in $PT_OPT"
else
  base="https://storage.googleapis.com/chrome-for-testing-public/$CHROME_VERSION/linux64"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/chrome.zip" "$base/chrome-linux64.zip" || fail "could not download Chrome $CHROME_VERSION"
  curl -fsSL -o "$tmp/driver.zip" "$base/chromedriver-linux64.zip" || fail "could not download ChromeDriver $CHROME_VERSION"
  rm -rf "$PT_OPT/chrome" "$PT_OPT/chromedriver"
  unzip_to() {
    if command -v unzip >/dev/null 2>&1; then unzip -q "$1" -d "$2"
    else "$PT_VENV/bin/python" -m zipfile -e "$1" "$2"; fi
  }
  unzip_to "$tmp/chrome.zip" "$tmp"
  unzip_to "$tmp/driver.zip" "$tmp"
  mv "$tmp/chrome-linux64" "$PT_OPT/chrome"
  mv "$tmp/chromedriver-linux64" "$PT_OPT/chromedriver"
  # python's zipfile drops the executable bit.
  chmod +x "$PT_OPT/chrome/chrome" "$PT_OPT/chromedriver/chromedriver"
  for helper in chrome_crashpad_handler chrome-wrapper chrome_sandbox; do
    [ -f "$PT_OPT/chrome/$helper" ] && chmod +x "$PT_OPT/chrome/$helper"
  done
  echo "$CHROME_VERSION" > "$PT_OPT/chrome/.version"
  rm -rf "$tmp"
  say "ok: Chrome for Testing $CHROME_VERSION installed in $PT_OPT"
fi

ensure_chrome_libs

# ---------------------------------------------------------------------------
# 5. Desk files (never overwritten)
# ---------------------------------------------------------------------------
if [ -f "$PT_DESK/config.yaml" ]; then
  say "skip: $PT_DESK/config.yaml exists (kept as is)"
else
  cp "$PT_CHECKOUT/config/config.template.yaml" "$PT_DESK/config.yaml"
  say "ok: wrote $PT_DESK/config.yaml from the template"
fi
if [ -f "$PT_STATE/mode" ]; then
  say "skip: mode is $(cat "$PT_STATE/mode")"
else
  echo rehearsal > "$PT_STATE/mode"
  say "ok: mode set to rehearsal (dry runs only until you say 'go live')"
fi

say "ok: install complete in $PT_DESK"
