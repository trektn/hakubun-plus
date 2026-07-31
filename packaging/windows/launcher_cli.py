"""PyInstaller entry point for the console (CLI) build.

Mirrors the ``hakubun-plus`` console script from pyproject.toml.
"""

if __name__ == '__main__':
    from hakubun.ui.cli import main
    main()
