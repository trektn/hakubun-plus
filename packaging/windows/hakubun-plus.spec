# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Hakubun+ Windows build (PyInstaller >= 6).

Produces one folder containing two executables:

  Hakubun+.exe      -- windowed Qt interface (no console window)
  hakubun-plus.exe  -- console CLI interface

Build it with build-windows.sh (cross-build under Wine), or directly on
Windows with:

    pyinstaller packaging\\windows\\hakubun-plus.spec --noconfirm
"""

import os
import re
import sys

from PyInstaller.utils.hooks import collect_submodules

HERE = os.path.abspath(SPECPATH)
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))

# The package isn't installed in the build environment; make it importable so
# collect_submodules() can walk it.
sys.path.insert(0, ROOT)

with open(os.path.join(ROOT, 'hakubun', 'utils.py'), encoding='utf-8') as fp:
    VERSION = re.search(r"^VERSION = '([^']+)'", fp.read(), re.M).group(1)

# Names the output folder/zip. Defaults to the source version ('0.13.dev0');
# set HAKUBUN_BUILD_LABEL=0.13-beta for a friendlier release name. The version
# baked into the executables always comes from utils.py.
LABEL = os.environ.get('HAKUBUN_BUILD_LABEL') or VERSION

DATA_DIR = os.path.join(ROOT, 'hakubun', 'data')
if not os.path.exists(os.path.join(DATA_DIR, 'anime-relations',
                                   'anime-relations.txt')):
    raise SystemExit(
        "hakubun/data/anime-relations/anime-relations.txt is missing -- run "
        "'git submodule update --init' before building.")


def _version_resource():
    """Write a Win32 VERSIONINFO resource for both executables.

    The dotted VERSION ('0.13.dev0') can't go in the numeric field, so only
    its leading numbers are used there; the readable string keeps it whole.
    """
    numbers = [int(part) for part in re.findall(r'\d+', VERSION)[:4]]
    numbers += [0] * (4 - len(numbers))
    filevers = tuple(numbers)

    resource = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vers!r},
    prodvers={vers!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'trektn'),
        StringStruct('FileDescription', 'Hakubun+ -- multi-site list manager'),
        StringStruct('FileVersion', {version!r}),
        StringStruct('InternalName', 'hakubun-plus'),
        StringStruct('LegalCopyright', 'GPL-3.0-or-later'),
        StringStruct('OriginalFilename', 'Hakubun+.exe'),
        StringStruct('ProductName', 'Hakubun+'),
        StringStruct('ProductVersion', {version!r}),
      ]),
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""".format(vers=filevers, version=VERSION)

    out_dir = globals().get('workpath') or os.path.join(HERE, 'build')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, 'file_version_info.txt')
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(resource)
    return path


VERSION_FILE = _version_resource()

datas = [
    (DATA_DIR, os.path.join('hakubun', 'data')),
    (os.path.join(ROOT, 'COPYING'), '.'),
    (os.path.join(ROOT, 'CHANGELOG'), '.'),
    (os.path.join(ROOT, 'README.md'), '.'),
]

# API backends, sync engine pieces and trackers are imported by name at
# runtime, so the analyser can't see them.
hiddenimports = (
    collect_submodules('hakubun.lib')
    + collect_submodules('hakubun.sync')
    + collect_submodules('hakubun.parser')
    + collect_submodules('hakubun.extras')
    + [
        'hakubun.tracker.win32',
        'hakubun.tracker.polling',
        'anitopy',
        'pypresence',
    ]
)

# Unix-only trackers and the GTK interface have no place in a Windows bundle;
# pulling them in would only add missing-module noise (or, for gi, fail).
excludes = [
    'gi',
    'cairo',
    'gtk',
    'hakubun.ui.gtk',
    'hakubun.tracker.inotify',
    'hakubun.tracker.inotifyBase',
    'hakubun.tracker.pyinotify',
    'hakubun.tracker.mpris',
    'inotify',
    'pyinotify',
    'jeepney',
    'tkinter',
    'test',
    'unittest',
    'pydoc_data',
]

a_qt = Analysis(
    [os.path.join(HERE, 'launcher_qt.py')],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

a_cli = Analysis(
    [os.path.join(HERE, 'launcher_cli.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes + ['PyQt6'],
    noarchive=False,
    optimize=0,
)

pyz_qt = PYZ(a_qt.pure)
pyz_cli = PYZ(a_cli.pure)

exe_qt = EXE(
    pyz_qt,
    a_qt.scripts,
    [],
    exclude_binaries=True,
    name='Hakubun+',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(DATA_DIR, 'icon.ico'),
    version=VERSION_FILE,
)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name='hakubun-plus',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=os.path.join(DATA_DIR, 'icon.ico'),
    version=VERSION_FILE,
)

coll = COLLECT(
    exe_qt,
    exe_cli,
    a_qt.binaries,
    a_qt.datas,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Hakubun+-{}-win64'.format(LABEL),
)
