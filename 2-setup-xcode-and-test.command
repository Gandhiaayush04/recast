#!/bin/bash
# Recast setup step 2: finishes Xcode setup, downloads Recast's own private copy of Python,
# and runs the full engine test suite on this Mac. Output is saved to setup-log-2.txt.

cd "$(dirname "$0")"
ROOT="$(pwd)"
LOG="$ROOT/setup-log-2.txt"
exec > >(tee "$LOG") 2>&1
echo "=== Recast setup step 2 started $(date) ==="

# --- Xcode ---
if [ ! -d /Applications/Xcode.app ]; then
  echo "XCODE=missing (Xcode.app not found in Applications)"
else
  echo ""
  echo ">>> Finishing Xcode setup. If asked for your PASSWORD, type your Mac login password"
  echo ">>> and press Return (the characters won't show)."
  echo ""
  sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
  sudo xcodebuild -license accept
  xcodebuild -runFirstLaunch
  xcodebuild -version && echo "XCODE=ok" || echo "XCODE=FAILED"
fi

# --- Private Python for Recast (python-build-standalone, Apple Silicon) ---
PYDIR="$ROOT/build/python"
if [ -x "$PYDIR/bin/python3" ]; then
  echo "PYTHON=ok (already downloaded)"
else
  echo ""
  echo ">>> Downloading Recast's own copy of Python..."
  mkdir -p "$ROOT/build"
  URL=$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest | \
    /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(next(a['browser_download_url'] for a in d['assets'] if a['name'].startswith('cpython-3.13') and a['name'].endswith('aarch64-apple-darwin-install_only.tar.gz')))")
  echo "from: $URL"
  curl -fL "$URL" -o "$ROOT/build/python.tar.gz" && tar -xzf "$ROOT/build/python.tar.gz" -C "$ROOT/build" \
    && echo "PYTHON=ok" || echo "PYTHON=FAILED"
fi
PY="$PYDIR/bin/python3"
"$PY" --version

echo ""
echo ">>> Installing Python libraries for Recast..."
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$ROOT/engine/requirements.txt" -r "$ROOT/engine/requirements-dev.txt" && echo "PYDEPS=ok" || echo "PYDEPS=FAILED"

echo ""
echo ">>> Running the engine test suite on this Mac (about 1-2 minutes)..."
cd "$ROOT/engine"
"$PY" -m pytest -q -p no:cacheprovider 2>&1 | tail -40
echo "TESTS_EXIT=${PIPESTATUS[0]}"

echo ""
echo "=== STEP2_DONE $(date) ==="
echo "All finished. You can close this window and tell Claude 'done'."
read -p "Press Return to close this window."
