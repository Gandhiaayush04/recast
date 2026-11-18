#!/bin/bash
# Recast setup step 1: installs Homebrew (if missing), FFmpeg, LibreOffice, git and GitHub CLI.
# Everything it prints is also saved to setup-log.txt in this folder so Claude can read it.

cd "$(dirname "$0")"
LOG="$(pwd)/setup-log.txt"
exec > >(tee "$LOG") 2>&1

echo "=== Recast setup started $(date) ==="
echo "macOS: $(sw_vers -productVersion)  chip: $(uname -m)"

# --- iCloud check (Desktop & Documents sync) ---
if [ -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Desktop" ]; then
  echo "ICLOUD_DESKTOP_SYNC=on"
else
  echo "ICLOUD_DESKTOP_SYNC=off"
fi

# --- Homebrew ---
if [ -x /opt/homebrew/bin/brew ]; then BREW=/opt/homebrew/bin/brew
elif [ -x /usr/local/bin/brew ]; then BREW=/usr/local/bin/brew
else
  echo ""
  echo ">>> Installing Homebrew. When it asks for your PASSWORD, type your Mac login password"
  echo ">>> and press Return. The characters will NOT appear while you type - that's normal."
  echo ""
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  if [ -x /opt/homebrew/bin/brew ]; then BREW=/opt/homebrew/bin/brew; else BREW=/usr/local/bin/brew; fi
fi

if [ ! -x "$BREW" ]; then
  echo "HOMEBREW=FAILED"
  echo "Something went wrong installing Homebrew. Tell Claude."
  read -p "Press Return to close this window."
  exit 1
fi
echo "HOMEBREW=ok ($BREW)"

# Make brew available in future Terminal windows too
if ! grep -q 'brew shellenv' "$HOME/.zprofile" 2>/dev/null; then
  echo "eval \"\$($BREW shellenv)\"" >> "$HOME/.zprofile"
fi
eval "$($BREW shellenv)"
export HOMEBREW_NO_AUTO_UPDATE=1

echo ""
echo ">>> Installing FFmpeg, git and GitHub CLI (a few minutes)..."
$BREW install ffmpeg git gh && echo "FFMPEG=ok" || echo "FFMPEG=FAILED"

echo ""
echo ">>> Installing LibreOffice (about 1 GB, can take 5-15 minutes)..."
if [ -d /Applications/LibreOffice.app ]; then
  echo "LIBREOFFICE=ok (already installed)"
else
  $BREW install --cask libreoffice && echo "LIBREOFFICE=ok" || echo "LIBREOFFICE=FAILED"
fi

echo ""
echo "--- versions ---"
ffmpeg -version 2>/dev/null | head -1
/Applications/LibreOffice.app/Contents/MacOS/soffice --version 2>/dev/null | head -1
git --version; gh --version 2>/dev/null | head -1
ffmpeg -hide_banner -filters 2>/dev/null | grep -E " (zscale|tonemap) " || echo "no zscale/tonemap filters"
xcode-select -p 2>/dev/null && echo "XCODE_TOOLS=ok" || echo "XCODE_TOOLS=missing"

echo ""
echo "=== SETUP_DONE $(date) ==="
echo "All finished. You can close this window and tell Claude 'done'."
read -p "Press Return to close this window."
