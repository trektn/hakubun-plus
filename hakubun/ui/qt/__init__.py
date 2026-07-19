# This file is part of Hakubun.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#


import glob
import os
import sys

from hakubun import utils
from hakubun.ui.qt.mainwindow import MainWindow


def _add_system_qt_plugin_path():
    """
    PyQt6 installed from PyPI (as opposed to a distro package) bundles
    its own private copy of Qt6, whose plugin directory only ships the
    plugins Qt itself provides -- not third-party platform themes like
    qt6ct, which are only installed system-wide by the distro's own
    'qt6ct' package. Without this, QT_QPA_PLATFORMTHEME=qt6ct (or
    similar) silently falls back to the default style instead of
    picking up the user's configured system theme, since the plugin
    can never be found. Making the system Qt6 plugin directory
    additionally searchable fixes that, without needing the user to
    set anything in their own environment.

    Only appends, and only if the system directory exists and isn't
    already present -- an explicit QT_PLUGIN_PATH the user has already
    set themselves is respected as-is.
    """
    if os.name == 'nt':
        return

    candidates = [
        '/usr/lib/qt6/plugins',
        '/usr/lib/qt/plugins',
        '/usr/lib64/qt6/plugins',
        '/usr/lib/x86_64-linux-gnu/qt6/plugins',
    ]
    # Distro-specific multiarch paths (Debian/Ubuntu on non-x86_64, etc.)
    candidates += glob.glob('/usr/lib/*/qt6/plugins')

    existing = os.environ.get('QT_PLUGIN_PATH', '')
    existing_dirs = existing.split(os.pathsep) if existing else []

    for candidate in candidates:
        if os.path.isdir(candidate) and candidate not in existing_dirs:
            existing_dirs.append(candidate)

    if existing_dirs:
        os.environ['QT_PLUGIN_PATH'] = os.pathsep.join(existing_dirs)


def main():
    _add_system_qt_plugin_path()
    print("Hakubun+-qt v{}".format(utils.VERSION))

    debug = False

    if '-h' in sys.argv or '--help' in sys.argv:
        print("Usage: hakubun-plus-qt [options]")
        print()
        print('Options:')
        print(' -d, --debug  Shows debugging information')
        print(' --taiga      Starts in Taiga mode, regardless of the saved setting')
        print(' -h, --help   Shows this help')
        sys.exit(0)
    if '-d' in sys.argv or '--debug' in sys.argv:
        print('Showing debug information.')
        debug = True
    force_taiga = '--taiga' in sys.argv

    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("Couldn't import Qt6 dependencies. "
              "Make sure you installed the PyQt6 package.")

    try:
        from PIL import Image
        os.environ['imaging_available'] = "1"
    except ImportError:
        print("Warning: PIL or Pillow isn't available. "
              "Preview images will be disabled.")

    app = QApplication(sys.argv)
    app.setApplicationName("hakubun-plus")
    app.setDesktopFileName("hakubun-plus-qt")
    if os.name == "nt":
        import ctypes
        myappid = 'hakubun-plus' + utils.VERSION
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    try:
        # keep the variable around to prevent it from being gc'ed
        main_window = MainWindow(debug, force_taiga=force_taiga)
        sys.exit(app.exec())
    except utils.HakubunFatal as e:
        QMessageBox.critical(None, 'Fatal Error', "{0}".format(e), QMessageBox.StandardButton.Ok)
