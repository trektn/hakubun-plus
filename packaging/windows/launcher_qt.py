"""PyInstaller entry point for the windowed (Qt) build.

Mirrors the ``hakubun-plus-qt`` console script from pyproject.toml, but as a
real module so PyInstaller has a script to analyse. Because the frozen GUI
executable has no console, stdout/stderr may be invalid handles on Windows;
they are redirected to a log file next to the user's config so the prints in
hakubun.ui.qt don't raise.
"""

import os
import sys


def _redirect_output():
    if sys.stdout is not None and sys.stderr is not None:
        return

    try:
        from hakubun import utils
        log_dir = utils.to_cache_path()
        os.makedirs(log_dir, exist_ok=True)
        log = open(os.path.join(log_dir, 'hakubun-plus-qt.log'), 'a',
                   encoding='utf-8', errors='replace')
    except Exception:
        log = open(os.devnull, 'w')

    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def main():
    _redirect_output()
    from hakubun.ui.qt import main as qt_main
    qt_main()


if __name__ == '__main__':
    main()
