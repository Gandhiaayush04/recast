#!/bin/bash
# Xcode build phase: copies Recast's private Python and the conversion engine
# into Recast.app/Contents/Resources before the app is code-signed.
set -euo pipefail

# Unit-test and CI builds don't need the bundled Python; skip the embed there.
if [ "${SKIP_ENGINE_EMBED:-0}" = "1" ]; then
  echo "SKIP_ENGINE_EMBED=1: not embedding the engine"
  exit 0
fi

ROOT="${PROJECT_DIR}/.."                       # .../Recast
RES="${TARGET_BUILD_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}"
PY_SRC="${ROOT}/build/python"
ENGINE_SRC="${ROOT}/engine/recast_engine"

if [ ! -x "${PY_SRC}/bin/python3" ]; then
  echo "error: ${PY_SRC} is missing. Run 2-setup-xcode-and-test.command first."
  exit 1
fi

mkdir -p "${RES}/engine"
rsync -a --delete --exclude '__pycache__' "${PY_SRC}/" "${RES}/python/"
rsync -a --delete --exclude '__pycache__' "${ENGINE_SRC}/" "${RES}/engine/recast_engine/"

# Don't ship test tooling or caches inside the app.
rm -rf "${RES}/python/lib/python3."*/site-packages/{pytest,_pytest,pluggy,iniconfig,py.py}* 2>/dev/null || true
# pillow-heif is GPLv2 (bundles x265); HEIC uses macOS sips instead, so never ship it.
rm -rf "${RES}/python/lib/python3."*/site-packages/{pillow_heif,pillow_heif.libs,pillow_heif-*.dist-info} 2>/dev/null || true
find "${RES}/python" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Ship only what the engine needs: no pip, test tools, IDLE or helper scripts
# (their launchers would carry this Mac's build paths), and nothing from pillow-heif
# (GPL; HEIC goes through macOS's own sips instead).
PYBIN="${RES}/python/bin"
find "${PYBIN}" -mindepth 1 ! -name 'python3' ! -name 'python3.13' ! -name 'python' -exec rm -rf {} + 2>/dev/null || true
SP=$(echo "${RES}"/python/lib/python3.*/site-packages)
rm -rf "${SP}"/pip "${SP}"/pip-* "${SP}"/pygments "${SP}"/pygments-* \
       "${SP}"/pillow_heif* "${SP}"/_pillow_heif* "${SP}"/pytest* "${SP}"/_pytest* \
       "${SP}"/pluggy* "${SP}"/iniconfig* "${SP}"/py.py 2>/dev/null || true
rm -rf "${RES}"/python/lib/python3.*/idlelib "${RES}"/python/lib/python3.*/ensurepip \
       "${RES}"/python/lib/python3.*/test "${RES}"/python/lib/python3.*/tkinter "${RES}"/python/lib/tcl* "${RES}"/python/lib/tk* 2>/dev/null || true
if ls "${SP}" | grep -qi heif; then echo "error: pillow-heif still bundled"; exit 1; fi

# Ship the license texts of everything bundled (Python, Pillow and its codecs, pypdfium2/PDFium, pypdf)
LIC="${RES}/Licenses"; rm -rf "${LIC}"; mkdir -p "${LIC}"
cp "${RES}/python/lib/python3."*/LICENSE.txt "${LIC}/Python-LICENSE.txt" 2>/dev/null || true
for d in "${SP}"/*.dist-info; do
  name=$(basename "$d" .dist-info)
  if [ -d "$d/licenses" ]; then mkdir -p "${LIC}/${name}"; cp -R "$d/licenses/." "${LIC}/${name}/"; fi
  for f in "$d"/LICENSE* "$d"/COPYING* "$d"/NOTICE*; do [ -f "$f" ] && mkdir -p "${LIC}/${name}" && cp "$f" "${LIC}/${name}/"; done
done
cp "${PROJECT_DIR}/../LICENSE" "${LIC}/Recast-LICENSE.txt" 2>/dev/null || true
cp "${PROJECT_DIR}/../THIRD_PARTY.md" "${LIC}/THIRD_PARTY.md" 2>/dev/null || true

# Sign every embedded binary ad-hoc so Apple Silicon will run them.
find "${RES}/python" \( -name '*.so' -o -name '*.dylib' \) -exec codesign --force --sign - {} \; 2>/dev/null || true
codesign --force --sign - "${RES}/python/bin/python3."* 2>/dev/null || true

echo "Embedded Python + engine into ${RES}"
