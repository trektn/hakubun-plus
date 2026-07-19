![Hakubun+](assets/hakubunplusheader.png)

Hakubun+ (博聞十)
=================

Hakubun+ is an open source client for media tracking websites, an
independent fork of [Trackma](https://github.com/z411/trackma) that adds
Taiga mode and an airing schedule window on top of the base
[Hakubun](https://github.com/trektn/hakubun) fork. Not affiliated with
Trackma or Taiga.moe.

It aims to be a lightweight and simple but feature-rich program for Unix
based systems for fetching, updating and using data from personal lists
hosted in several media tracking websites.

Features
--------

- Manage local list and synchronize when necessary, useful when offline
- Manage multiple accounts on different media tracking sites
- Support for several media types (as supported by the site)
- Multiple user interfaces (Qt, GTK, command-line)
- Detection of running media player, updates list if necessary
- Ability to launch media player for a requested media in the list and update list if necessary
- Highly scalable, easy to code new interfaces and support for other sites
- Secure, uses HTTPS wherever possible.

Which tracker should I use?
--------
There isn't a single "best" tracker. Each has different goals and strengths.

| Project | Choose it if... |
|---------|-----------------|
| [**Taiga**](https://github.com/erengy/taiga) | You primarily use **Windows**, and/or **torrent anime**, or prefer its built-in search. |
| [**Hakubun**](https://github.com/trektn/hakubun) | You use ***nix** and wanted **Trackma** with a more opinionated UI/UX, or you use **Kitsu** and want GraphQL support. |
| [**Trackma**](https://github.com/z411/trackma) | You prefer a **CLI-first** workflow, are building wrappers or automation around it, or simply want to support the original project and its maintainers. |
| [**Hakubun+**](https://github.com/trektn/hakubun-plus) | The same as Hakubun but you also want features not available elsewhere, such as the **airing schedule** or **MAL score** additions. It should work across platforms, including Windows, but Windows is not tested. Expect it to be less stable than all of these other options on *nix or Windows. |


Currently supported websites
----------------------------

- [Anilist](https://anilist.co/) (Anime, Manga)
- [Kitsu](https://kitsu.app/) (Anime, Manga, Drama)
- [MyAnimeList](https://myanimelist.net/) (Anime, Manga)
- [Shikimori](https://shikimori.io/) (Anime, Manga)
- [VNDB](https://vndb.org/) (VNs)

Dependencies
------------

The only required dependencies to run Hakubun+ are:

- Python 3.9+
- For installation: `python-pip` (to install through `pip`) *or* `python-uv` (to install through `uv`)

But only basic features will work (only CLI interface and no tracker). Everything else is optional.

The following user interfaces are available and their requirements are as follows:

| UI | Dependencies |
| --- | --- |
| Qt | PyQt6 (`python-pyqt6`) |
| GTK 3 | PyGI (`python-gi` and `python-cairo`) |
| CLI | None |

The following media recognition trackers are available and their requirements are as follows:

| Tracker | Description | Dependencies |
| --- | --- | --- |
| inotify | Instant, but only supported in Linux. Uses it whenever possible. | `inotify` *or* `pyinotify` |
| Polling | Slow, but supported in every POSIX platform. Fallback. | `lsof` |
| Plex | Connects to Plex server. Enabled manually. | None |
| Kodi | Connects to Kodi server. Enabled manually. | None |
| Jellyfin | Connects to Jellyfin server. Enabled manually. | None |
| MPRIS | Connects to running MPRIS capable media players. | `python-jeepney` |
| Win32 | Recognition for Windows platforms. | None |

Additional optional Python dependencies:

- PIL (`python-pil`) - for showing preview images in the Qt/GTK interfaces.
- pypresence (???) - for announcing activity on Discord.
- anitopy (-) - for the anitopy title parser

Installation
------------

### Arch Linux (AUR)

An [AUR package](https://aur.archlinux.org/packages/hakubun-plus-git) tracking the latest `master` is available:

```sh
$ yay -S hakubun-plus-git
# or
$ paru -S hakubun-plus-git
```

### Manual installation

Make sure you've installed the proper dependencies (listed above)
according to the user interface you plan to use, and then run the
following command:

```sh
$ pip3 install hakubun-plus
```

You can also install the git (probably unstable, but newer) version like this:

```sh
$ pip3 install -U git+https://github.com/trektn/hakubun-plus.git
```

Or download the source code and install:

```sh
$ git clone --recursive https://github.com/trektn/hakubun-plus.git
$ cd hakubun-plus
$ uv build
$ pip3 install dist/hakubun_plus-0.12-py3-none-any.whl
```

### Extras (User Interfaces)

All user interfaces except for the default CLI mode require additional dependencies to function.
You may specify these as "extras" to be installed by the Python package manager.

The following extras are available:

| Extra | Description |
| --- | --- |
| `gtk` | The GTK interface. |
| `qt` | The Qt interface. |
| `ui` | All user interfaces. |
| `trackers` | All tracker libraries. |
| `discord_rpc` | Set your watching activity in Discord. |

If you want to install any of the extras be sure to specify them during installation:

#### pip

```sh
# With pip
$ pip3 install hakubun-plus[gtk,trackers]
$ pip3 install hakubun-plus[ui,discord_rpc]
```

Note that pip does not have a way to install all available extras,
so you'll have to provide them all manually if desired.

Then you can run the program with the interface you like.

```sh
$ hakubun-plus
$ hakubun-plus-gtk
$ hakubun-plus-qt
```

#### uv

When using uv on the cloned repository (see above),
you can install your desired extras as follows:

```sh
$ uv sync --extra gtk --extra trackers
$ uv sync --extra ui --extra discord_rpc
$ uv sync --all-extras
```

Then you can run the interface you like in your virtual environment managed by uv:

```sh
$ uv run hakubun-plus
$ uv run hakubun-plus-gtk
$ uv run hakubun-plus-qt
```

Configuration
-------------

A configuration file will be created in `~/.config/hakubun/config.json`, make sure to fill in the directory
where you store your video files and other settings. Set the `HAKUBUN_HOME` environment variable to run an
isolated profile without sharing accounts, configuration, cache, or list data with an installed copy.

Alternatively, the GTK and Qt interfaces provide a visual Settings panel.

If you are migrating from Trackma, you can transfer all your settings in `~/.config/trackma/` to `~/.config/hakubun/` and this usually shouldn't be a problem. However I recommend only transferring your account logins in `~/.config/trackma/accounts.dict`

Development
-----------

The code is hosted as a git repository on [GitHub](https://github.com/trektn/hakubun-plus).

Clone the repo and create the virtual environment using `uv`:

```sh
$ git clone --recursive https://github.com/trektn/hakubun-plus.git
$ cd hakubun-plus
$ uv sync --all-extras
```

Use the above commands from the [uv](#uv) section
for how to run your desired interface.

If you encounter any problems or have anything to suggest, please don't
hesitate to submit an issue in the GitHub [issue tracker](https://github.com/trektn/hakubun-plus/issues).

License
-------

Hakubun+ is licensed under the GPLv3 license, please see [LICENSE](../COPYING) for details.

Authors
-------

Hakubun+ is maintained by trektn as an independent fork of
[Trackma](https://github.com/z411/trackma), which was originally written
by z411 <z411@omaera.org>. For other contributors see AUTHORS file.

Acknowledgments
---------------

The optional `anitopy` title parser uses the
[anitopy](https://github.com/igorcafe/anitopy) library, a Python port of
[Anitomy](https://github.com/erengy/anitomy).
