"""Locate external engines. GUI apps on macOS get a minimal PATH, so we
search the usual Homebrew and app-bundle locations explicitly."""
from __future__ import annotations

import os
import shutil
from functools import lru_cache

_SEARCH_DIRS = [
    "/opt/homebrew/bin",   # Homebrew on Apple Silicon
    "/usr/local/bin",      # Homebrew on Intel
    "/usr/bin",
]

_CANDIDATES = {
    "ffmpeg": ["ffmpeg"],
    "ffprobe": ["ffprobe"],
    "libreoffice": ["soffice", "libreoffice"],
}

_EXTRA_PATHS = {
    "libreoffice": ["/Applications/LibreOffice.app/Contents/MacOS/soffice"],
}

# Shown in the app next to the Install button.
INSTALL_HINT = {
    "ffmpeg": "brew install ffmpeg",
    "ffprobe": "brew install ffmpeg",
    "libreoffice": "brew install --cask libreoffice",
}


@lru_cache(maxsize=None)
def find(engine: str) -> str | None:
    # Binary overrides are for development only: a hostile launch environment
    # must not be able to redirect us to another program.
    if os.environ.get("RECAST_DEV") == "1":
        override = os.environ.get(f"RECAST_{engine.upper()}")
        if override and os.path.exists(override):
            return override
    for p in _EXTRA_PATHS.get(engine, []):
        if os.path.exists(p):
            return p
    for name in _CANDIDATES.get(engine, [engine]):
        for d in _SEARCH_DIRS:
            p = os.path.join(d, name)
            if os.path.exists(p) and os.access(p, os.X_OK):
                return p
        p = shutil.which(name)
        if p:
            return p
    return None


def status() -> dict[str, bool]:
    out = {"ffmpeg": find("ffmpeg") is not None, "libreoffice": find("libreoffice") is not None}
    # HEIC: Apple's sips on macOS, else the optional pillow-heif fallback.
    heic = os.path.exists("/usr/bin/sips") and os.access("/usr/bin/sips", os.X_OK) \
        and os.environ.get("RECAST_FORCE_PILLOW_HEIF") != "1"
    if not heic:
        try:
            import pillow_heif  # noqa: F401
            heic = True
        except Exception:
            heic = False
    out["heic"] = heic
    try:
        import pypdfium2  # noqa: F401
        out["pdf"] = True
    except Exception:
        out["pdf"] = False
    return out
