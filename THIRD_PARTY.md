# Third-party software

## Bundled in the downloadable Recast.app

| Component | License |
|---|---|
| Python (python-build-standalone) | PSF License |
| Pillow | MIT-CMU (HPND) |
| pypdfium2 (includes PDFium) | BSD-3-Clause or Apache-2.0 |
| pypdf | BSD-3-Clause |

pillow-heif is not bundled. It is GPLv2 (its wheel includes x265), so the app does not ship it.
HEIC is handled by macOS's own `sips`. pillow-heif is used only as an optional fallback when `sips`
is absent (for example on a Linux development machine).

## Used but not bundled

| Component | License | How it is obtained |
|---|---|---|
| FFmpeg | LGPL/GPL (depends on build) | Installed by the user via Homebrew |
| LibreOffice | MPL-2.0 | Installed by the user via Homebrew |
| macOS `sips` | Part of macOS | Built into the operating system |

Recast runs FFmpeg and LibreOffice as separate programs found on the user's Mac; it does not
include or link them.
