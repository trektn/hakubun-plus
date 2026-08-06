# Windows build

Packages Hakubun+ as a self-contained Windows folder — no Python or Qt
install needed on the target machine. Two executables share one bundle:

| File | What it is |
| --- | --- |
| `Hakubun+.exe` | Qt interface, no console window |
| `hakubun-plus.exe` | CLI interface, console |

The GTK interface is not packaged: PyGObject's Windows story would balloon the
bundle for an interface no Windows user is likely to prefer over Qt.

## Building from Linux (cross-build under Wine)

```sh
HAKUBUN_BUILD_LABEL=0.13-beta packaging/windows/build-windows.sh
```

Needs `wine` (64-bit), `curl`, `zip` and `xvfb-run` on the host. The first run
downloads Windows CPython, installs it into a dedicated Wine prefix under
`~/.cache/hakubun-win-build` (override with `BUILD_PREFIX`) and pip-installs
PyQt6, Pillow, anitopy, pypresence and PyInstaller into it. Later runs reuse
that prefix and take about a minute. `--clean` throws the prefix away and
starts over.

Output lands in `packaging/windows/dist/`: the bundle folder plus a zip of it.

`HAKUBUN_BUILD_LABEL` only names the output folder and zip; the version
compiled into the executables always comes from `VERSION` in
`hakubun/utils.py`.

## Building on Windows

With Python 3.9+ and Git for Windows installed:

```bat
git submodule update --init hakubun/data/anime-relations
py -m pip install pyqt6 pillow anitopy pypresence pyinstaller
py -m PyInstaller --noconfirm packaging\windows\hakubun-plus.spec
```

## Notes on the packaged build

- The tracker defaults to the win32 backend, which reads media player window
  titles. Plex, Jellyfin and Kodi work too; the inotify and MPRIS backends are
  Unix-only and are excluded from the bundle.
- Config, data and cache go to `%USERPROFILE%\.config\hakubun`,
  `%USERPROFILE%\.local\share\hakubun` and `%USERPROFILE%\.cache\hakubun`,
  following the same XDG-style layout as on Linux. Setting `HAKUBUN_HOME` puts
  all three in one directory of your choosing.
- The windowed executable has no console, so anything the interface prints
  goes to `hakubun-plus-qt.log` in the cache directory.
- The bundle is unsigned, so SmartScreen will warn on first launch.
