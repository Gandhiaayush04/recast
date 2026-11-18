#!/bin/bash
# Recast step 3: builds Recast.app from source, installs it to /Applications,
# sets up version control, and opens the app. Output saved to build-log.txt.

cd "$(dirname "$0")"
ROOT="$(pwd)"
LOG="$ROOT/build-log.txt"
exec > >(tee "$LOG") 2>&1
echo "=== Recast build started $(date) ==="

BREW=/opt/homebrew/bin/brew
export HOMEBREW_NO_AUTO_UPDATE=1
eval "$($BREW shellenv)"

# XcodeGen turns app/project.yml into an Xcode project.
if ! command -v xcodegen >/dev/null; then
  echo ">>> Installing XcodeGen..."
  $BREW install xcodegen && echo "XCODEGEN=ok" || { echo "XCODEGEN=FAILED"; read -p "Press Return to close."; exit 1; }
fi

echo ""
echo ">>> Generating the Xcode project..."
cd "$ROOT/app" && xcodegen generate --quiet && echo "PROJECT=ok" || { echo "PROJECT=FAILED"; read -p "Press Return to close."; exit 1; }

echo ""
echo ">>> Building Recast (1-3 minutes)..."
xcodebuild -project Recast.xcodeproj -scheme Recast -configuration Release \
  -derivedDataPath "$ROOT/build/DerivedData" build > "$ROOT/build/xcodebuild-full.log" 2>&1
STATUS=$?
grep -E "error:|warning: .*(deprecated|isolated)|BUILD (SUCCEEDED|FAILED)" "$ROOT/build/xcodebuild-full.log" | head -60
if [ $STATUS -ne 0 ]; then
  echo "BUILD=FAILED"
  echo "The build failed. Tell Claude 'done' — it will read the log and fix it."
  read -p "Press Return to close this window."
  exit 1
fi
echo "BUILD=ok"

APP="$ROOT/build/DerivedData/Build/Products/Release/Recast.app"
echo ""
echo ">>> Installing Recast into Applications..."
osascript -e 'quit app "Recast"' 2>/dev/null
rm -rf /Applications/Recast.app
ditto "$APP" /Applications/Recast.app && echo "INSTALL=ok" || echo "INSTALL=FAILED"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f /Applications/Recast.app

echo ""
echo ">>> Checking the built-in engine..."
RES=/Applications/Recast.app/Contents/Resources
env -i HOME="$HOME" PATH="/usr/bin:/bin" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RES/engine" "$RES/python/bin/python3" -m recast_engine status \
  && echo "ENGINE_IN_APP=ok" || echo "ENGINE_IN_APP=FAILED"
codesign --verify --deep --strict /Applications/Recast.app && echo "SIGNATURE=ok" || echo "SIGNATURE=FAILED"

# --- engine tests on this Mac ---
echo ""
echo ">>> Running the engine tests (about 1-2 minutes)..."
( cd "$ROOT/engine" && "$ROOT/build/python/bin/python3" -m pytest -q -p no:cacheprovider 2>&1 | tail -5 )

# --- Finder Quick Action: right-click a file > Quick Actions > Convert with Recast ---
echo ""
echo ">>> Installing the Finder Quick Action..."
mkdir -p "$HOME/Library/Services"
rm -rf "$HOME/Library/Services/Convert with Recast.workflow"
ditto "$ROOT/quick-action/Convert with Recast.workflow" "$HOME/Library/Services/Convert with Recast.workflow" \
  && echo "QUICK_ACTION=ok" || echo "QUICK_ACTION=FAILED"
/System/Library/CoreServices/pbs -update 2>/dev/null

# --- version control (local only for now; GitHub comes later) ---
cd "$ROOT"
if [ ! -d .git ]; then
  git init -q -b main
  git config user.name "Aayush Gandhi"
  git config user.email "Gandhiaayush04@users.noreply.github.com"
fi
git add -A && git commit -q -m "Recast: engine, Mac app, setup scripts" && echo "GIT=committed" || echo "GIT=nothing new"

echo ""
echo "=== BUILD_DONE $(date) ==="
open /Applications/Recast.app
echo "Recast should now be open. You can close this window and tell Claude 'done'."
read -p "Press Return to close this window."
