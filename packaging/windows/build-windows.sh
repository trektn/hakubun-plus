#!/usr/bin/env bash
#
# Cross-build the Hakubun+ Windows bundle from Linux, using Wine to run a
# real Windows CPython + PyInstaller. No Windows machine required.
#
# Requirements on the host: wine (64-bit), curl, zip, and xvfb-run for the
# silent Python install (the installer wants a display).
#
# The Wine prefix is kept outside the source tree and reused across builds,
# so only the first run pays for downloading and installing Python.
#
# Usage:
#   packaging/windows/build-windows.sh              # build
#   packaging/windows/build-windows.sh --clean      # rebuild from scratch
#   BUILD_PREFIX=~/somewhere packaging/windows/build-windows.sh
#   HAKUBUN_BUILD_LABEL=0.14-beta packaging/windows/build-windows.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

PYTHON_VERSION="${PYTHON_VERSION:-3.12.10}"
BUILD_PREFIX="${BUILD_PREFIX:-$HOME/.cache/hakubun-win-build}"
export WINEPREFIX="${WINEPREFIX:-$BUILD_PREFIX/wineprefix}"
export WINEARCH=win64
export WINEDEBUG="${WINEDEBUG:--all}"

# Read by the spec to name the output folder/zip (see hakubun-plus.spec).
export HAKUBUN_BUILD_LABEL="${HAKUBUN_BUILD_LABEL:-}"

WINPY='C:\Python312\python.exe'
DOWNLOADS="$BUILD_PREFIX/downloads"
DISTDIR="$HERE/dist"
WORKDIR="$HERE/build"

CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '\n==> %s\n' "$*"; }

# Wine chatters on stderr about graphics drivers no matter what we do; keep
# the build log readable without hiding real errors.
run_wine() {
    wine "$@" 2>&1 | grep -viE 'libEGL|pci id for fd|dri2 screen|DRI3|X connection to|^$' || true
}

for tool in wine curl zip; do
    command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 1; }
done

if [ "$CLEAN" = 1 ]; then
    log "Removing Wine prefix and previous build output"
    rm -rf "$WINEPREFIX" "$DISTDIR" "$WORKDIR"
fi

if [ ! -f "$ROOT/hakubun/data/anime-relations/anime-relations.txt" ]; then
    log "Fetching the anime-relations submodule"
    git -C "$ROOT" submodule update --init hakubun/data/anime-relations
fi

if [ ! -f "$WINEPREFIX/drive_c/Python312/python.exe" ]; then
    mkdir -p "$DOWNLOADS"
    installer="$DOWNLOADS/python-$PYTHON_VERSION-amd64.exe"
    if [ ! -f "$installer" ]; then
        log "Downloading Windows CPython $PYTHON_VERSION"
        curl -fsSL -o "$installer" \
            "https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-amd64.exe"
    fi

    log "Creating the Wine prefix"
    command -v xvfb-run >/dev/null || {
        echo "missing required tool: xvfb-run (needed for the Python installer)" >&2
        exit 1
    }
    xvfb-run -a wineboot --init >/dev/null 2>&1

    log "Installing Windows CPython into the prefix"
    xvfb-run -a wine "$installer" /quiet InstallAllUsers=1 PrependPath=0 \
        Include_test=0 Include_launcher=0 SimpleInstall=1 \
        'TargetDir=C:\Python312' >/dev/null 2>&1

    [ -f "$WINEPREFIX/drive_c/Python312/python.exe" ] || {
        echo "Python install failed -- no python.exe in the prefix" >&2
        exit 1
    }

    log "Installing build and runtime dependencies"
    run_wine "$WINPY" -m pip install --upgrade pip
    run_wine "$WINPY" -m pip install \
        "pyqt6>=6.2.0,<7" "pillow>=11.0.0,<12" "anitopy>=2.0.0,<3" \
        "pypresence>=4.2.1,<5" pyinstaller
fi

log "Running PyInstaller"
rm -rf "$DISTDIR" "$WORKDIR"
spec_win="$(winepath -w "$HERE/hakubun-plus.spec")"
dist_win="$(winepath -w "$DISTDIR")"
work_win="$(winepath -w "$WORKDIR")"
mkdir -p "$DISTDIR" "$WORKDIR"

run_wine "$WINPY" -m PyInstaller --noconfirm --clean \
    --distpath "$dist_win" --workpath "$work_win" "$spec_win"

bundle="$(find "$DISTDIR" -maxdepth 1 -mindepth 1 -type d -print -quit)"
[ -n "$bundle" ] || { echo "PyInstaller produced no bundle" >&2; exit 1; }
[ -f "$bundle/Hakubun+.exe" ] || { echo "bundle is missing Hakubun+.exe" >&2; exit 1; }

log "Zipping the bundle"
(cd "$DISTDIR" && zip -qr "$(basename "$bundle").zip" "$(basename "$bundle")")

log "Done"
du -sh "$bundle"
ls -la "$DISTDIR"/*.zip
