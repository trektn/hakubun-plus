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

import copy
import datetime
import decimal
import difflib
import html
import json
import locale
import os
import pickle
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from enum import Enum, auto

from hakubun.i18n import _

try:
    from rapidfuzz import fuzz as rapidfuzz_fuzz
    from rapidfuzz import process as rapidfuzz_process
except ImportError:
    rapidfuzz_fuzz = rapidfuzz_process = None

VERSION = '0.13'

DATADIR = os.path.dirname(__file__) + '/data'


class Login(Enum):
    PASSWD = auto()
    OAUTH = auto()
    OAUTH_PKCE = auto()


class BaseEnum(Enum):
    @classmethod
    def find(cls, name):
        try:
            return cls(name)
        except ValueError:
            pass
        try:
            return cls[name.upper().replace(' ', '_')]
        except KeyError:
            pass
        return cls.UNKNOWN

    @classmethod
    def from_int(cls, index):
        try:
            return list(cls.__members__.values())[index]
        except IndexError:
            return cls.UNKNOWN

    def __int__(self):
        try:
            return list(self.__class__.__members__.values()).index(self)
        except IndexError:
            return list(self.__class__.__members__.values()).index(self.__class__.UNKNOWN)

    def __lt__(self, other):
        return int(self) < int(other)

    def __le__(self, other):
        return int(self) <= int(other)

    def __gt__(self, other):
        return int(self) > int(other)

    def __ge__(self, other):
        return int(self) >= int(other)

    def __add__(self, other):
        if isinstance(other, str):
            return str(self) + other
        else:
            return super().__add__(other)

    def __str__(self):
        if isinstance(self.value, (str,)):
            return self.value
        else:
            return self.name.replace('_', ' ')


class Status(BaseEnum):
    UNKNOWN = 'Unknown'
    ONGOING = 'Ongoing'
    FINISHED = 'Finished'
    NOTYET = 'Not yet started'
    CANCELLED = 'Cancelled'
    OTHER = 'Other'

    # aliases
    AIRING = ONGOING
    RELEASING = ONGOING
    PUBLISHING = ONGOING
    CURRENTLY_AIRING = ONGOING
    FINISHED_AIRING = FINISHED
    NOT_YET_AIRED = NOTYET
    NOT_YET_RELEASED = NOTYET
    NOT_YET_PUBLISHED = NOTYET


class Type(BaseEnum):
    UNKNOWN = "Unknown"
    OTHER = "Other"

    # anime
    TV = "TV"
    TV_SHORT = "TV Short"
    MOVIE = "Movie"
    OVA = "OVA"
    ONA = "ONA"
    SPECIAL = "Special"

    # manga
    MANGA = "Manga"
    NOVEL = "Novel"
    ONE_SHOT = "One Shot"
    # Kept after the existing concrete members so their persisted integer
    # positions do not move. Music videos used to alias OTHER, which made
    # it impossible for the UI to display the more useful "MV" type.
    MUSIC = "Music Video"

    # aliases
    SP = SPECIAL


class Tracker(Enum):
    NOVIDEO = auto()
    PLAYING = auto()
    UNRECOGNIZED = auto()
    NOT_FOUND = auto()
    IGNORED = auto()


class Season(BaseEnum):
    WINTER = 'Winter'
    SPRING = 'Spring'
    SUMMER = 'Summer'
    FALL = 'Fall'


class SearchMethod(Enum):
    KEYWORD = auto()
    SEASON = auto()

    # aliases
    KW = KEYWORD


HOME = os.path.expanduser("~")
EXTENSIONS = ('.mkv', '.mp4', '.avi', '.ts')

# Put the available APIs here
available_libs = {
    'anilist':   ('Anilist',      DATADIR + '/anilist.jpg',     Login.OAUTH,
                  "https://anilist.co/api/v2/oauth/authorize?client_id=537&response_type=token"),
    'kitsu':     ('Kitsu',        DATADIR + '/kitsu.png',       Login.PASSWD),
    'mal':       ('MyAnimeList',  DATADIR + '/mal.jpg',     Login.OAUTH_PKCE,
                  "https://myanimelist.net/v1/oauth2/authorize?response_type=code&client_id=54ac679bcb073d20fb81ca9e5c78837b&code_challenge=%s"),
    'shikimori': ('Shikimori',    DATADIR + '/shikimori.jpg',   Login.OAUTH,
                  "https://shikimori.io/oauth/authorize?client_id=Jfu9MKkUKPG4fOC95A6uwUVLHy3pwMo3jJB7YLSp7Ro&redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob&response_type=code&scope=user_rates"),
    'vndb':      ('VNDB',         DATADIR + '/vndb.jpg',        Login.PASSWD),
}

available_trackers = [
    ('auto', 'Auto-detect'),
    ('inotify_auto', 'inotify'),
    ('polling', 'Polling (lsof)'),
    ('mpris', 'MPRIS'),
    ('plex', 'Plex Media Server'),
    ('jellyfin', 'Jellyfin'),
    ('kodi', 'Kodi'),
    ('win32', 'Win32'),
]

available_parsers = [
    ('aie', 'AnimeInfoExtractor (Default)'),
    ('anitopy', 'Anitopy'),
    ('anitomy_ng', 'Anitomy-NG (Experimental)'),
]

def oauth_generate_pkce() -> str:
    import secrets

    token = secrets.token_urlsafe(100)
    return token[:128]


def parse_config(filename, default):
    config = copy.copy(default)

    try:
        with open(filename) as configfile:
            loaded_config = json.load(configfile)
            if 'colors' in config and 'colors' in loaded_config:
                # Need to prevent nested dict from being overwritten with an incomplete dict
                config['colors'].update(loaded_config['colors'])
                loaded_config['colors'] = config['colors']
            config.update(loaded_config)
    except IOError:
        # Will just use the default config
        # and create the file for manual editing
        save_config(config, filename)
    except ValueError:
        # There's a syntax error in the config file
        errorString = "Erroneous config %s requires manual fixing or deletion to proceed." % filename
        log_error(errorString)
        raise HakubunFatal(errorString)

    return config


def _atomic_write(filename, write_func):
    """Write to a temp file in the same directory, fsync, then rename onto
    filename. Guarantees filename is either the old contents or the new
    contents in full, never a truncated/corrupted partial write."""
    path = os.path.dirname(filename)
    if not os.path.isdir(path):
        os.mkdir(path)

    fd, tmp_path = tempfile.mkstemp(dir=path or '.', prefix='.tmp-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as tmpfile:
            write_func(tmpfile)
            tmpfile.flush()
            os.fsync(tmpfile.fileno())
        os.replace(tmp_path, filename)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_config(config_dict, filename):
    _atomic_write(filename, lambda f: f.write(
        json.dumps(config_dict, sort_keys=True,
                   indent=4, separators=(',', ': ')).encode('utf-8')))


def load_data(filename):
    with open(filename, 'rb') as datafile:
        return pickle.load(datafile, encoding='bytes')


def save_data(data, filename):
    _atomic_write(filename, lambda f: pickle.dump(data, f, protocol=2))


def log_error(msg):
    with open(to_data_path('error.log'), 'a') as logfile:
        logfile.write(msg)


def expand_path(path):
    return os.path.expanduser(path)


def expand_paths(paths):
    return (expand_path(path) for path in paths)


def is_media(filename):
    return os.path.splitext(filename)[1] in EXTENSIONS


def get_any(dict_, *keys, default=None):
    """Get the first key present on a dict."""
    for key in keys:
        value = dict_.get(key, Ellipsis)
        if value is not Ellipsis:
            return value
    return default


def regex_find_videos(subdirectory=''):
    if subdirectory:
        path = os.path.expanduser(subdirectory)
    else:
        path = os.getcwd()

    for root, dirs, names in os.walk(path, followlinks=True):
        for filename in names:
            if is_media(filename):
                yield (os.path.join(root, filename), filename)


def regex_rename_files(pattern, source_dir, dest_dir):
    in_path = to_data_path(source_dir)
    out_path = to_data_path(dest_dir)
    for filename in os.listdir(in_path):
        if re.match(pattern, filename):
            in_file = os.path.join(in_path, filename)
            out_file = os.path.join(out_path, filename)
            os.rename(in_file, out_file)


def make_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def dir_exists(dirname):
    return os.path.isdir(dirname)


def file_exists(filename):
    return os.path.isfile(filename)


def file_older_than(filename, mtime):
    return os.path.getmtime(filename) < time.time() - mtime


def sync_file(fname, sync_url):
    if not sync_url:
        return False

    import urllib.request
    import socket

    try:
        with urllib.request.urlopen(sync_url) as r, open(fname, 'wb') as f:
            shutil.copyfileobj(r, f)
    except (socket.timeout, urllib.error.URLError):
        return False

    return True


def to_config_path(*paths):
    local_home = os.environ.get("HAKUBUN_HOME")
    if local_home:
        return os.path.join(local_home, *paths)
    if dir_exists(os.path.join(HOME, ".hakubun")):
        return os.path.join(HOME, ".hakubun", *paths)

    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "hakubun", *paths)


def to_data_path(*paths):
    local_home = os.environ.get("HAKUBUN_HOME")
    if local_home:
        return os.path.join(local_home, *paths)
    if dir_exists(os.path.join(HOME, ".hakubun")):
        return os.path.join(HOME, ".hakubun", *paths)

    return os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")), "hakubun", *paths)


def to_cache_path(*paths):
    local_home = os.environ.get("HAKUBUN_HOME")
    if local_home:
        return os.path.join(local_home, "cache", *paths)
    if dir_exists(os.path.join(HOME, ".hakubun")):
        return os.path.join(HOME, ".hakubun", "cache", *paths)

    return os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "hakubun", *paths)


def change_permissions(filename, mode):
    os.chmod(filename, mode)


def estimate_aired_episodes(show):
    """ Estimate how many episodes have passed since airing """

    if show['status'] == Status.FINISHED:
        return show['total']
    elif show['status'] == Status.NOTYET:
        return 0
    # It's airing, so we make an estimate based on available information
    elif show['status'] == Status.AIRING:
        if 'next_ep_number' in show:  # Do we have the upcoming episode number?
            return show['next_ep_number'] - 1
        elif show['start_date']:  # Do we know when it started? Let's just assume 1 episode = 1 week
            days = (datetime.datetime.now() - show['start_date']).days
            if days <= 0:
                return 0

            eps = days // 7 + 1
            if show['total'] and eps > show['total']:
                return show['total']
            return eps
    return 0


GUESS_SHOW_CUTOFF = 0.7

# How much better the suffix-stripped title must score before it displaces the
# decorated one -- see _PARSER_SUFFIX_RE. Swept over 12k real filenames (local
# library in both library_full_path modes + nyaa's 2000 most-seeded releases):
# 0.00 and 0.05 are net regressions, 0.10-0.25 are a broad plateau all clearly
# better than fallback-only, and >=0.30 decays back to it. Mid-plateau chosen,
# so the exact value is not load-bearing.
GUESS_STRIP_MARGIN = 0.15

# Every parser wrapper (AnimeInfoExtractor, AnitopyWrapper, AnitomyNgWrapper)
# unconditionally appends these decorations to a parsed title -- " Season N"
# for season > 1, a bare special/OVA/etc. type keyword, and "(YYYY)" for a
# detected year -- in that order, so they read right-to-left off a title
# built as base + season + type + year. None of that text exists in a
# tracker's own title for franchises with a single continuous entry (e.g.
# "Naruto", "Bleach", "One Punch Man"), so the decoration alone routinely
# drags an otherwise-exact title below GUESS_SHOW_CUTOFF.
#
# Both the decorated and the stripped title are scored, and the stripped one
# wins only if it beats the decorated one by GUESS_STRIP_MARGIN (see
# guess_show). Trying the stripped form only as a fallback -- i.e. only once
# the decorated one had already missed the cutoff -- left the decoration free
# to carry a WRONG show over the cutoff, at which point the fallback never
# ran: "Naruto Season 02" scored 0.71 against "Fate/Zero 2nd Season" purely
# on the shared season words, and 43 files were filed under Fate/Zero.
#
# The margin is what makes this safe. Preferring the stripped title whenever
# it merely scores higher regresses badly, because stripping the season also
# makes a title match the WRONG entry of a franchise the user tracks per
# season: "Initial D Season 2" scores 0.97 against "Initial D SECOND STAGE"
# (right) and 1.00 against "Initial D" (wrong). Requiring a clear margin
# keeps those, while still rescuing the cases where the decoration alone
# carried a completely unrelated show over the line.
_PARSER_SUFFIX_RE = re.compile(
    r'(\s+\((?:19|20)\d{2}\))?'
    r'(\s+(?:OAD|OAV|ONA|OVA|SPECIALS?))?'
    r'(\s+Season\s+\d+)?$',
    re.IGNORECASE)

# Flat (choices, owners) view of the most recent tracker list, so a library scan
# doesn't rebuild it once per filename. _get_tracker_list builds a fresh dict on
# every call, so keying the cache by object identity makes it a guaranteed miss
# for any caller that doesn't hold one dict across a whole scan (e.g. the
# inotify-driven single-file add path) -- keyed by a cheap (count, id-set hash)
# content signature instead, so a rebuilt-but-unchanged list still hits. This
# won't notice a show's titles/aliases changing with the id set held constant;
# that's an accepted, harmless staleness window (a slightly stale match list,
# not a crash) rather than a full invalidation contract.
_guess_index = (None, None, None)


def _title_index(showlist):
    global _guess_index

    signature = (len(showlist), hash(frozenset(showlist.keys())))
    (cached_signature, choices, owners) = _guess_index
    if cached_signature == signature:
        return (choices, owners)

    choices, owners = [], []
    for item in showlist.values():
        for title in item['titles']:
            choices.append(title.lower())
            owners.append(item)

    _guess_index = (signature, choices, owners)
    return (choices, owners)


def _guess_show_difflib(show_title, showlist):
    """ Fallback matcher for when rapidfuzz isn't installed.

    Same metric and same result as a plain ratio() sweep, but skips the real
    comparison whenever difflib's cheap upper bounds already rule a title out.
    Roughly 4x faster than the naive loop on a large list.

    Note the argument order matters: difflib's ratio() is not symmetric, so the
    query has to stay as seq1 the way the original sweep had it.
    """
    highest_ratio = (None, 0)
    matcher = difflib.SequenceMatcher()
    matcher.set_seq1(show_title.lower())

    for item in showlist.values():
        # Make sure to search through all the aliases
        for title in item['titles']:
            matcher.set_seq2(title.lower())
            if matcher.real_quick_ratio() <= highest_ratio[1]:
                continue
            if matcher.quick_ratio() <= highest_ratio[1]:
                continue
            ratio = matcher.ratio()
            if ratio > highest_ratio[1]:
                highest_ratio = (item, ratio)

    return highest_ratio


def _guess_show_rapidfuzz(show_title, showlist):
    (choices, owners) = _title_index(showlist)

    # fuzz.ratio is difflib's ratio computed with an optimal alignment rather
    # than difflib's greedy one, so it only ever differs on pairs far below the
    # cutoff -- where difflib undercounts and both answers mean "no match".
    match = rapidfuzz_process.extractOne(
        show_title.lower(), choices, scorer=rapidfuzz_fuzz.ratio)

    if not match:
        return (None, 0)
    return (owners[match[2]], match[1] / 100)


def guess_show(show_title, tracker_list):
    """ Take a title and search for it fuzzily in the tracker list """
    (showlist, altnames_map) = tracker_list

    # Return the show immediately if we find an altname for it
    if altnames_map and show_title.lower() in altnames_map:
        showid = altnames_map[show_title.lower()]

        if showid in showlist:
            return showlist[showid]

    # Compare to every show in our list to see which one has the most
    # similar name. This runs once per unique title in the library during a
    # scan, against every title and alias, so it dominates scan time.
    search = _guess_show_rapidfuzz if rapidfuzz_fuzz else _guess_show_difflib

    highest_ratio = search(show_title, showlist)

    # Also score the title with a parser-added "Season N"/type/year decoration
    # stripped off the end, and let it win only by a clear margin -- see
    # _PARSER_SUFFIX_RE for why this is not a fallback, and why the margin
    # matters. Skipped when the decorated title already matched perfectly,
    # since nothing can beat that.
    stripped_title = _PARSER_SUFFIX_RE.sub('', show_title).strip()
    if (highest_ratio[1] < 1.0 and stripped_title
            and stripped_title.lower() != show_title.lower()):
        stripped_ratio = search(stripped_title, showlist)
        if stripped_ratio[1] >= highest_ratio[1] + GUESS_STRIP_MARGIN:
            highest_ratio = stripped_ratio

    if highest_ratio[1] > GUESS_SHOW_CUTOFF:
        return highest_ratio[0]


def redirect_show(show_tuple, redirections, tracker_list):
    """ Use a redirection dictionary and return the new show ID and episode accordingly """
    if not redirections:
        return show_tuple

    (show, ep) = show_tuple
    if show.get('total') and ep <= show['total']:
        # redirect only for invalid ep
        return show_tuple

    showlist = tracker_list[0]
    if show['id'] in redirections:
        for redirection in redirections[show['id']]:
            (src_eps, dst_id, dst_eps) = redirection

            if (src_eps[1] == -1 and ep >= src_eps[0]) or (ep in range(src_eps[0], src_eps[1] + 1)):
                new_show_id = dst_id
                new_ep = ep + (dst_eps[0] - src_eps[0])

                if new_show_id in showlist:
                    return (showlist[new_show_id], new_ep)

    return show_tuple


def subminer_available():
    """Whether the SubMiner CLI (external sentence-mining MPV wrapper) is on PATH."""
    return shutil.which('subminer') is not None


def open_folder(path):
    if sys.platform == 'darwin':
        spawn_process(["open", path])
    elif sys.platform == 'win32':
        spawn_process(["explorer", path])
    else:
        spawn_process(["xdg-open", path])


_spawned_processes = []


def _poll_spawned_processes():
    """Periodically poll and prune process handles to prevent zombies."""
    while True:
        time.sleep(5)
        _spawned_processes[:] = [child for child in _spawned_processes if child.poll() is None]


_process_collector_thread = None


def spawn_process(arg_list):
    """
    Helper generic function to spawn a subprocess.

    The handles are collected and periodically polled
    to prevent zombie processes on *NIX.
    """
    global _process_collector_thread

    if not arg_list:
        return

    proc = subprocess.Popen(arg_list, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _spawned_processes.append(proc)

    if not _process_collector_thread:
        _process_collector_thread = threading.Thread(target=_poll_spawned_processes, daemon=True)
        _process_collector_thread.start()


def is_mpv_player(player_path):
    """True if a resolved player executable path looks like mpv (as
    opposed to e.g. mplayer, which the same 'player' config historically
    also allowed) -- used to gate the mpv-only instance-reuse feature."""
    return bool(player_path) and os.path.basename(player_path).lower().startswith('mpv')


def mpv_ipc_socket_path():
    """Fixed path for mpv's JSON IPC socket, shared by every playback
    started with instance reuse on (see mpv_ipc_loadfile / the
    'player_reuse_mpv_instance' config setting) so later calls can find
    the same running instance."""
    return to_cache_path('mpv-socket')


def subminer_mpv_socket_path(subminer_bin):
    """SubMiner's own mpv IPC socket path (it manages a single mpv
    instance under a fixed socket, normally /tmp/subminer-socket, rather
    than our own mpv_ipc_socket_path()). Asked from the binary itself,
    since it's not exposed in config.jsonc and could move with TMPDIR.
    Returns None if subminer can't be asked (e.g. it hung or errored)."""
    try:
        result = subprocess.run(
            [subminer_bin, 'mpv', 'socket'],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = result.stdout.strip()
    return path or None


def mpv_ipc_loadfile(filename, socket_path=None):
    """
    Tries to tell an already-running mpv instance (found via the fixed
    IPC socket above, or `socket_path` if given -- e.g. SubMiner's own
    socket) to play `filename` in place of whatever it's currently
    playing. Returns True if the command was sent successfully -- the
    caller shouldn't spawn a new process in that case -- or False if
    nothing is listening on the socket (no reusable instance up yet, or
    the previous one wasn't started with --input-ipc-server), meaning
    the caller should fall back to spawning a new mpv process normally.
    """
    if not hasattr(socket, 'AF_UNIX'):
        # mpv's IPC socket is a Windows named pipe there, not a Unix
        # domain socket -- instance reuse is POSIX-only for now.
        return False
    if socket_path is None:
        socket_path = mpv_ipc_socket_path()
    if not os.path.exists(socket_path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(socket_path)
            command = json.dumps(
                {'command': ['loadfile', filename, 'replace']}) + '\n'
            sock.sendall(command.encode('utf-8'))
    except OSError:
        return False
    return True


def get_terminal_size(fd=1):
    """
    Returns height and width of current terminal. First tries to get
    size via termios.TIOCGWINSZ, then from environment. Defaults to 25
    lines x 80 columns if both methods fail.

    :param fd: file descriptor (default: 1=stdout)
    """
    try:
        import fcntl
        import termios
        import struct
        hw = struct.unpack('hh', fcntl.ioctl(fd, termios.TIOCGWINSZ, '1234'))
    except Exception:
        try:
            width = int(os.environ.get('COLUMNS', 80))
            height = int(os.environ.get('LINES', 25))
            hw = (height, width)
        except Exception:
            hw = (25, 80)

    return hw


def show():
    return {
        'id':           0,
        'title':        '',
        'url':          '',
        'aliases':      [],
        'my_id':        None,
        'my_progress':  0,
        'my_status':    1,
        'my_score':     0,
        'my_start_date':  None,
        'my_finish_date': None,
        'type':         0,
        'status':       0,
        'platform_score': None,
        # Cross-referenced MyAnimeList id/community score, for accounts on
        # a different backend (e.g. AniList) -- fetched separately since
        # it isn't part of that backend's own list response. See
        # Engine._fetch_mal_scores_task.
        'mal_id':       None,
        'mal_score':    None,
        'total':        0,
        'start_date':   None,
        'end_date':     None,
        'image':        '',
        'image_thumb':  '',
        'queued':       False,
        # Minutes per episode, where the API provides it (MAL/AniList/
        # Kitsu all do). Used only for the Statistics page's "Time
        # spent watching"/"Time to complete".
        'duration':     None,
        # Raw numeric score (backend's own scale -- 0-10 for MAL, 0-100
        # for AniList/Kitsu) and popularity rank (lower = more popular,
        # uniformly across backends -- see each lib's _parse_info for how
        # AniList's raw favorites count gets negated into this
        # convention). Used only for the Seasons page's Sort by.
        'score_raw':    None,
        'popularity':   None,
        # Display-ready popularity string ("#42" for rank-based backends,
        # "15,234 users" for AniList's count) -- 'popularity' itself may
        # be negated for sorting (see each lib's _parse_info) and isn't
        # meant to be shown as-is.
        'popularity_label': None,
        # Was 'my_last-update' (dash typo) for a long time, so shows
        # where the API didn't set this explicitly were missing the
        # underscore key entirely, causing KeyErrors wherever it's read.
        'my_last_update': None
    }


class HakubunError(Exception):
    pass


class EngineError(HakubunError):
    pass


class DataError(HakubunError):
    pass


class APIError(HakubunError):
    pass


class AccountError(HakubunError):
    pass


class HakubunFatal(Exception):
    pass


class EngineFatal(HakubunFatal):
    pass


class DataFatal(HakubunFatal):
    pass


class APIFatal(HakubunFatal):
    pass


# Configuration defaults
config_defaults = {
    'player': '/usr/bin/mpv',
    # When True and 'player' resolves to mpv, playing an episode is
    # handed off to an already-running mpv instance (via its JSON IPC
    # socket, see mpv_ipc_loadfile) instead of spawning a new mpv
    # process/window each time. Falls back to spawning normally if no
    # instance is listening yet.
    'player_reuse_mpv_instance': True,
    'use_subminer': False,
    'searchdir': ['~/Videos'],
    'tracker_enabled': True,
    'tracker_update_wait_s': 300,
    # When False, the MPRIS tracker updates at 80% of the episode's
    # actual duration (from the player's reported position/length)
    # instead of the fixed wait above -- matches the Plex/Kodi trackers'
    # own obey_update_wait_s toggles, which do the same thing for their
    # own duration sources.
    'mpris_obey_update_wait_s': False,
    # How far into an episode (MPRIS-reported duration) counts as
    # "watched", when the toggle above is off.
    'tracker_update_percentage': 80,
    'tracker_update_close': False,
    'tracker_update_prompt': False,
    'tracker_not_found_prompt': True,
    'tracker_interval': 10,
    'tracker_process': 'mplayer|mplayer2|mpv',
    'autoretrieve': 'always',
    'autoretrieve_days': 3,
    'autosend': 'always',
    'autosend_minutes': 60,
    'autosend_size': 5,
    'autosend_at_exit': True,
    'library_autoscan': True,
    'library_full_path': False,
    'scan_whole_list': False,
    'add_dialog_default_status': 'start',
    'sync_on_settings_apply': False,
    'debug_disable_lock': True,
    'auto_status_change': True,
    'auto_status_change_if_scored': True,
    'auto_date_change': True,
    'tracker_type': "auto",
    'plex_host': "localhost",
    'plex_port': "32400",
    'plex_obey_update_wait_s': False,
    'plex_user': '',
    'plex_passwd': '',
    'plex_uuid': str(uuid.uuid1()),
    'plex_ssl': False,
    'jellyfin_host': "localhost",
    'jellyfin_port': "8096",
    'jellyfin_api_key': '',
    'jellyfin_user': '',
    'kodi_host': "localhost",
    'kodi_port': "8080",
    'kodi_obey_update_wait_s': False,
    'kodi_user': '',
    'kodi_passwd': '',
    'redirections_url': 'https://raw.githubusercontent.com/erengy/anime-relations/master/anime-relations.txt',
    'redirections_time': 1,
    'use_hooks': True,
    'title_parser': 'aie',
    # Automatically cross-reference MyAnimeList's community score after
    # every sync, using a MAL account added in Hakubun+ (see
    # Engine.fetch_mal_scores). Off by default since it needs a MAL
    # account configured to do anything.
    'auto_add_mal_scores': False,
    # Which Kitsu backend to use: 'legacy' (REST/JSON:API, libkitsu) or
    # 'graphql' (libkitsu_graphql). Defaults to legacy so existing Kitsu
    # accounts keep working exactly as before unless explicitly switched.
    'kitsu_api': 'legacy',
    # Language-aware primary title shown in list views. ``auto`` selects the
    # first mode for the active UI language. Tracker data stays untouched;
    # hakubun.titles supplies a display overlay from local AOD + AniDB data.
    'title_language': 'auto',
    # For Simplified/Traditional modes, prefer the work's native title when
    # no matching Chinese title exists, before falling back to romaji or the
    # tracker-provided title.
    'chinese_native_title_fallback': True,
    # TMDB v3 API key used for localized synopsis lookups. Kept in the
    # user's config (or HAKUBUN_TMDB_API_KEY), never bundled in source.
    'tmdb_api_key': '',
    # AniDB's compact title dump is cached separately from user-supplied
    # files. Zero disables automatic fetching; positive values are the
    # maximum cache age in days.
    'anidb_titles_url': 'https://anidb.net/api/anime-titles.xml.gz',
    'anidb_titles_time': 7,
}

userconfig_defaults = {
    'mediatype': '',
    'userid': 0,
    'username': '',
}

curses_defaults = {
    'show_help': True,
    'keymap': {
        'help': '?',
        'prev_filter': 'left',
        'next_filter': 'right',
        'sort': 'f3',
        'sort_order': 'r',
        'update': 'u',
        'play': 'p',
        'openfolder': 'o',
        'play_random': '&',
        'status': 'f6',
        'score': 'z',
        'send': 's',
        'retrieve': 'R',
        'addsearch': 'a',
        'reload': 'c',
        'switch_account': 'f9',
        'delete': 'd',
        'quit': 'q',
        'altname': 'A',
        'search': '/',
        'neweps': 'N',
        'details': 'enter',
        'details_exit': 'esc',
        'open_web': 'O',
        'left': 'h',
        'up': 'k',
        'down': 'j',
        'right': 'l',
        'page_up': 'K',
        'page_down': 'J',
    },
    'palette': {
        'body':             ('', ''),
        'focus':            ('standout', ''),
        'head':             ('light red', 'black'),
        'header':           ('bold', ''),
        'status':           ('white', 'dark blue'),
        'error':            ('light red', 'dark blue'),
        'window':           ('white', 'dark blue'),
        'button':           ('black', 'light gray'),
        'button hilight':   ('white', 'dark red'),
        'item_airing':      ('dark blue', ''),
        'item_notaired':    ('yellow', ''),
        'item_neweps':      ('white', 'brown'),
        'item_updated':     ('white', 'dark green'),
        'item_playing':     ('white', 'dark blue'),
        'info_title':       ('light red', ''),
        'info_section':     ('dark blue', ''),
    }
}

gtk_defaults = {
    'show_tray': True,
    'close_to_tray': True,
    'start_in_tray': False,
    'tray_api_icon': False,
    'remember_geometry': False,
    'last_width': 740,
    'last_height': 480,
    'visible_columns': ['Title', 'Progress', 'Score', 'Percent', 'Season', 'Platform Score', 'Synced Score'],
    'episodebar_style': 1,
    'filter_global': False,
    # Whether the SubMiner on/off toggle is shown at all -- purely
    # cosmetic, doesn't touch config_defaults' 'use_subminer' switch
    # itself. Same key and meaning as qt_defaults below; the GTK
    # Preferences window reads it directly, so its absence here was a
    # KeyError on opening Preferences at all.
    'show_subminer_toggle': True,
    # UI language override. 'auto' follows the system locale; otherwise
    # one of hakubun.i18n.SUPPORTED_LANGUAGES. Read once at startup, so
    # changing it needs a restart to take effect.
    'language': 'auto',
    # Multi-sync (same keys/semantics as qt_defaults below): the list
    # overlay, owner-system score editing and the Sync button's
    # headless multi-sync all gate on these.
    'multisync_enabled': True,
    # Never auto-apply: always fetch, plan, and surface the sync window
    # for review. On by default while multisync is beta -- the safe
    # posture for anyone who hasn't audited what their field policies
    # do to their real accounts yet.
    'multisync_plan_only': True,
    # When on (and multisync is enabled), the sidebar score editor can
    # edit an owned entry's score in its OWNER's rating system, toggled
    # live via the Synced/Platform switch by the slider. Off keeps the
    # editor on the active account's own system for every entry.
    'multisync_edit_owned_score': True,
    'colors': {
        'is_airing': '#0099CC',
        'is_playing': '#6C2DC7',
        'is_queued': '#54C571',
        'new_episode': '#FBB917',
        'not_aired': '#999900',
        'progress_bg': '#E5E5E5',
        'progress_fg': '#99B3CC',
        'progress_sub_bg': '#B3B3B3',
        'progress_sub_fg': '#668099',
        'progress_complete': '#99CCB3',
    },
}

qt_defaults = {
    'show_tray': True,
    'close_to_tray': True,
    'notifications': True,
    'start_in_tray': False,
    'tray_api_icon': False,
    'remember_geometry': False,
    'remember_columns': False,
    'last_x': 0,
    'last_y': 0,
    'last_width': 740,
    'last_height': 480,
    'visible_columns': ['Title', 'Progress', 'Score', 'Percent', 'Season', 'Platform Score', 'Synced Score'],
    'inline_edit': True,
    'columns_state': None,
    'columns_per_api': False,
    'episodebar_style': 1,
    'episodebar_text': False,
    'filter_bar_position': 2,
    'filter_global': False,
    # Whether the SubMiner on/off toggle (toolbar in normal mode, Tools
    # menu in Taiga mode) is shown at all -- purely cosmetic, doesn't
    # touch config_defaults' 'use_subminer' switch itself.
    'show_subminer_toggle': True,
    # Same key/semantics as gtk_defaults above.
    'language': 'auto',
    'multisync_enabled': True,
    # Same keys/semantics as config_defaults above.
    'multisync_plan_only': True,
    'multisync_edit_owned_score': True,
    'colors': {
        'is_airing': '#D2FAFA',
        'is_playing': '#9696FA',
        'is_queued': '#D2FAD2',
        'new_episode': '#FAFA82',
        'not_aired': '#FAFAD2',
        'progress_bg': '#F5F5F5',
        'progress_fg': '#74C0FA',
        'progress_sub_bg': '#D2D2D2',
        'progress_sub_fg': '#5187B1',
        'progress_complete': '#00D200',
    },
    'sort_index': 1,
    'sort_order': 0,
    'taiga_mode': False,
}

qt_per_api_defaults = {
    'visible_columns': ['Title', 'Progress', 'Score', 'Percent', 'Season', 'Platform Score', 'Synced Score'],
    'columns_state': None,
}

_SYNOPSIS_PARA_RE = re.compile(r'</p>\s*<p[^>]*>', re.IGNORECASE)
_SYNOPSIS_LINE_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
# Only matches things that actually look like HTML tags (start with a
# letter, optionally preceded by a slash) -- a plain "<" or ">" used as a
# comparison operator in a synopsis (e.g. "power < 9000") isn't a tag and
# shouldn't be eaten along with whatever follows it on the line.
_SYNOPSIS_TAG_RE = re.compile(r'</?[a-zA-Z][^>]*>')
_SYNOPSIS_BLANK_LINES_RE = re.compile(r'\n{3,}')


def clean_synopsis(text):
    """Normalizes an API-supplied synopsis/description into plain text
    with real newlines for paragraph breaks.

    Some backends (Kitsu, AniList) embed literal HTML (``<br>``, ``<p>``)
    in this field instead of using real line breaks. Left as-is, that
    markup either gets escaped and shown as literal text, or silently
    stripped/collapsed by the UI toolkit's markup renderer -- neither of
    which is what the API meant. Callers are expected to still escape the
    returned text for whatever markup dialect they render it in.
    """
    if not text:
        return text
    text = _SYNOPSIS_PARA_RE.sub('\n\n', text)
    text = _SYNOPSIS_LINE_RE.sub('\n', text)
    text = _SYNOPSIS_TAG_RE.sub('', text)
    text = html.unescape(text)
    text = _SYNOPSIS_BLANK_LINES_RE.sub('\n\n', text)
    return text.strip()


def as_utc(dt):
    """Attach UTC to a naive datetime. The two sources of airing times
    disagree on this: engine.get_airing_schedule builds aware UTC out of
    AniList's airingAt, while libanilist's own 'next_ep_time' comes from
    utcfromtimestamp() and is naive. Both mean UTC, so normalize rather
    than letting a naive/aware subtraction raise."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def format_airtime_delta(dt, now=None) -> str:
    """How far off an airing time is, as a bare quantity: "3 days",
    "5 hours", "12 minutes". Returns None once it's no longer in the
    future, since there is no sensible quantity to name then -- callers
    that need a label for that case should use format_relative_airtime.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    seconds = (as_utc(dt) - as_utc(now)).total_seconds()

    if seconds <= 0:
        return None

    minutes = seconds / 60
    if minutes < 60:
        n = max(1, round(minutes))
        return _('%d minute') % n if n == 1 else _('%d minutes') % n

    hours = minutes / 60
    if hours < 24:
        n = max(1, round(hours))
        return _('%d hour') % n if n == 1 else _('%d hours') % n

    days = hours / 24
    n = max(1, round(days))
    return _('%d day') % n if n == 1 else _('%d days') % n


def format_relative_airtime(dt, now=None) -> str:
    """Human-relative description of an airing time -- days out by
    default, switching to hours and then minutes as it gets closer, e.g.
    "In 3 days" -> "In 5 hours" -> "In 12 minutes". Callers should show
    the absolute time somewhere too (e.g. a tooltip), since the relative
    form alone loses precision.
    """
    delta = format_airtime_delta(dt, now)
    if delta is not None:
        return _('In %s') % delta

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    seconds = (as_utc(dt) - as_utc(now)).total_seconds()
    return _('Airing now') if seconds > -1800 else _('Aired')


def _localized_date(dt, include_year=False) -> str:
    """Format a short date using the active language's native grammar."""
    from hakubun import i18n

    local = as_utc(dt).astimezone()
    short_weekdays = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug',
              'Sep', 'Oct', 'Nov', 'Dec')
    language = i18n.active_language()
    if language == 'ja':
        weekday = ('月', '火', '水', '木', '金', '土', '日')[local.weekday()]
        year = '%d年' % local.year if include_year else ''
        return '%s%d月%d日（%s）' % (year, local.month, local.day, weekday)
    if language in ('zh_CN', 'zh_TW'):
        prefix = '周' if language == 'zh_CN' else '週'
        weekday = ('一', '二', '三', '四', '五', '六', '日')[local.weekday()]
        year = '%d年' % local.year if include_year else ''
        return '%s%d月%d日（%s%s）' % (
            year, local.month, local.day, prefix, weekday)
    weekday = _(short_weekdays[local.weekday()])
    month = _(months[local.month - 1])
    if language == 'es':
        suffix = ' de %d' % local.year if include_year else ''
        return '%s, %d de %s%s' % (weekday, local.day, month, suffix)
    suffix = ', %d' % local.year if include_year else ''
    return '%s %s %02d%s' % (weekday, month, local.day, suffix)


def format_airing_day(day) -> str:
    """Formats an airing-schedule day using the active UI language."""
    from hakubun import i18n

    language = i18n.active_language()
    if language == 'ja':
        weekday = ('月', '火', '水', '木', '金', '土', '日')[day.weekday()]
        return '%d月%d日（%s）' % (day.month, day.day, weekday)
    if language in ('zh_CN', 'zh_TW'):
        prefix = '周' if language == 'zh_CN' else '週'
        weekday = ('一', '二', '三', '四', '五', '六', '日')[day.weekday()]
        return '%d月%d日（%s%s）' % (day.month, day.day, prefix, weekday)
    weekdays = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
                'Saturday', 'Sunday')
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug',
              'Sep', 'Oct', 'Nov', 'Dec')
    if language == 'es':
        return '%s\n%d de %s' % (
            _(weekdays[day.weekday()]), day.day,
            _(months[day.month - 1]))
    return '%s\n%s %02d' % (
        _(weekdays[day.weekday()]), _(months[day.month - 1]), day.day)


def format_next_airing(dt, now=None) -> str:
    """The details view's phrasing for a next-episode time: the same
    reduced quantity the airing scheduler shows, followed by the
    absolute local date it resolves to -- "3 days (Sat Aug 02, 2026)".
    The scheduler can rely on its own day-grouped columns for the date;
    a lone detail row can't, and "3 days" with no anchor is unreadable
    a day later. Returns None when the time isn't in the future."""
    delta = format_airtime_delta(dt, now)
    if delta is None:
        return None
    from hakubun import i18n

    local = as_utc(dt).astimezone()
    language = i18n.active_language()
    reference = as_utc(now).astimezone() if now is not None \
        else datetime.datetime.now().astimezone()
    include_year = local.year != reference.year
    date = _localized_date(local, include_year=include_year)
    if language == 'ja':
        return '%s・あと%s' % (date, delta)
    if language == 'zh_CN':
        return '%s · %s后' % (date, delta)
    if language == 'zh_TW':
        return '%s · %s後' % (date, delta)
    if language == 'es':
        return '%s · dentro de %s' % (date, delta)
    return '%s (%s)' % (delta, _localized_date(local, include_year=True))


def format_episode_number(episode) -> str:
    """Display an episode number with native counters where appropriate."""
    from hakubun import i18n

    language = i18n.active_language()
    if language == 'ja':
        return '第%s話' % episode
    if language in ('zh_CN', 'zh_TW'):
        return '第%s集' % episode
    return str(episode)


def format_clock(milliseconds) -> str:
    """12345678ms -> '3:25:45'; drops the hours field when there are none."""
    total_seconds = int(milliseconds / 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return '%d:%02d:%02d' % (hours, minutes, seconds)
    return '%d:%02d' % (minutes, seconds)


def format_playback_position(view_offset_ms, length_ms=None, include_percent=True) -> str:
    """'12:34 / 24:00 (52%)' when the tracker knows the episode's
    duration (currently MPRIS only), '12:34' when it only knows elapsed
    time, '' when it knows neither (nothing playing).

    `include_percent` should be False when the caller already shows the
    percentage next to this (e.g. Qt's PlaybackBar has its own percent
    label beside the bar) -- otherwise it ends up printed twice right
    next to each other, e.g. "52% 12:34 / 24:00 (52%)".
    """
    if view_offset_ms is None:
        return ''
    position = format_clock(view_offset_ms)
    if not length_ms:
        return position
    if not include_percent:
        return '%s / %s' % (position, format_clock(length_ms))
    percent = min(100, round(view_offset_ms / length_ms * 100))
    return '%s / %s (%d%%)' % (position, format_clock(length_ms), percent)


def format_local_time(
    dt,
    fallback_message='No data',
    error_message='?'
) -> str:
    if dt is None or dt.tzinfo is None:
        return fallback_message
    try:
        return dt.astimezone().strftime(locale.nl_langinfo(locale.D_T_FMT))
    except ValueError:
        return error_message


def decimal_places(step) -> int:
    """Number of decimal places represented in a float step value (e.g. 0.25 -> 2)."""
    if isinstance(step, float):
        fraction = str(step).split('.')[1]
        if fraction != '0':
            return len(fraction)
    return 0


def score_display_factor(mediainfo) -> float:
    """
    Multiplier used to present a friendlier score to the user than an
    API's raw scale.

    Only fine-grained small-scale systems (Kitsu: 0-5 in 0.25 increments)
    are doubled to a less awkward 0-10 in 0.5 increments. Coarser
    small-scale systems that step by whole units (e.g. AniList's 5-star
    or 3-point smiley scales) are left as-is, since showing "2, 4, 6, 8,
    10" for a 5-star rating would misrepresent it.
    """
    if mediainfo['score_max'] <= 5 and mediainfo['score_step'] < 1:
        return 2
    return 1


def score_display_range(mediainfo):
    """Returns (display_max, display_step, decimals) for the score UI."""
    factor = score_display_factor(mediainfo)
    display_max = mediainfo['score_max'] * factor
    display_step = mediainfo['score_step'] * factor

    return display_max, display_step, decimal_places(display_step)


def score_to_display(raw_score, mediainfo):
    """Converts a raw API score into its friendlier display value."""
    return raw_score * score_display_factor(mediainfo)


def score_to_raw(display_score, mediainfo):
    """Converts a display score back into the API's raw scale."""
    return display_score / score_display_factor(mediainfo)


def snap_score_to_step(raw_score, mediainfo):
    """Quantize a raw score to the provider's own grid (score_step),
    half up, clamped to [0, score_max].

    The score widgets set their increment to the account's display step,
    but a value can still land off-grid -- the widget was left on another
    provider's finer scale (owner-mode editing), or the user typed one --
    and every backend forwards the number verbatim, so an off-grid score
    (e.g. Kitsu 4.35 when its grid is quarter-stars) reaches the API as
    an invalid rating. Snapping here guarantees a value the provider can
    actually store. Uses Decimal(str(...)) so binary float noise on fine
    steps can't tip a genuine half the wrong way (mirrors
    sync.normalize.provider_score)."""
    step = mediainfo.get('score_step') or 1
    smax = mediainfo.get('score_max', 10)
    if not raw_score:
        return 0 if float(step).is_integer() else 0.0
    raw = decimal.Decimal(str(raw_score))
    step_d = decimal.Decimal(str(step))
    ticks = (raw / step_d).quantize(decimal.Decimal(1),
                                    rounding=decimal.ROUND_HALF_UP)
    snapped = min(max(ticks * step_d, decimal.Decimal(0)),
                  decimal.Decimal(str(smax)))
    if float(step).is_integer():
        return int(snapped)
    return round(float(snapped), 2)


def kitsu_rating_twenty(my_score):
    """Convert a my_score (0-5, half-star/0.5 grid) to Kitsu's
    ratingTwenty (2-20, EVEN values only) -- my_score*4 always lands on
    an even value on that grid. Used by both the REST and GraphQL
    Kitsu adapters. 0 means 'clear the rating' (None), not a real
    zero score."""
    return int(my_score * 4) or None


def date_to_season(dt) -> str:
    """Formats a date as an anime-style 'Season Year' label, e.g. 'Winter 2009'."""
    if dt is None:
        return '?'
    seasons = (Season.WINTER, Season.SPRING, Season.SUMMER, Season.FALL)
    season = seasons[(dt.month - 1) // 3]
    from hakubun.metadata import localize_season
    return localize_season(f'{season!s} {dt.year}')


_RELEASE_STATUS_LABELS = {
    Status.ONGOING: 'Airing',
    Status.FINISHED: 'Finished',
    Status.NOTYET: 'Upcoming',
    Status.CANCELLED: 'Cancelled',
}


def release_status_label(status) -> str:
    """Human label for a show's airing status ('Airing', 'Finished',
    'Upcoming', ...). Shared by the list views' Release Status column;
    same paint-path rules as get_season_label below: cheap, and never
    mutates anything."""
    from hakubun.metadata import localize_status
    label = _RELEASE_STATUS_LABELS.get(status)
    if label:
        return localize_status(status)
    if status in (None, Status.UNKNOWN):
        return '?'
    return str(status)


def get_season_label(show) -> str:
    """
    Returns a 'Season Year' label for a show (e.g. 'Winter 2009').

    Prefers an API-native season/year when the backend supplies one (e.g.
    AniList's season/seasonYear, surfaced as a 'Season' entry in
    show['extra']) since that's authoritative, falling back to deriving
    one from start_date for backends that don't (MAL, Kitsu).

    Called from the Qt models/delegates data()/paint() path, so it must
    stay cheap -- and it must NOT write to the show dict: these dicts
    are shared with engine threads that pickle them (save_cache), and
    inserting a key mid-pickle raises "dictionary changed size during
    iteration". (An earlier version memoized the label onto the show;
    that intermittently aborted cache saves during background MAL score
    fetches.) The computation below is a short tuple scan plus an
    f-string -- cheap enough to redo per call.
    """
    canonical = show.get('season_canonical')
    if canonical:
        from hakubun.metadata import localize_season
        return localize_season(canonical)
    for key, value in show.get('extra') or []:
        if key == 'Season' and value:
            from hakubun.metadata import localize_season
            return localize_season(value)
    return date_to_season(show.get('start_date'))


# Chronological (not alphabetical) position of each season name within a
# year, matching the WINTER/SPRING/SUMMER/FALL order used by date_to_season.
_SEASON_CHRONO_ORDER = {str(s): i for i, s in enumerate(
    (Season.WINTER, Season.SPRING, Season.SUMMER, Season.FALL))}


def season_sort_key(label):
    """Sort key for a get_season_label()-style 'Season Year' label (e.g.
    'Summer 2026'), ordering by year first and season second.

    A plain text sort on the label compares the season name before the
    year, since the name comes first in the string -- 'Fall 2011' sorts
    before 'Spring 2006' alphabetically even though 2006 is earlier.
    Swapping the priority so year dominates fixes that. The season
    tiebreaker for same-year rows uses _SEASON_CHRONO_ORDER rather than
    the raw name, so it lands in calendar order (Winter, Spring, Summer,
    Fall) instead of alphabetical order (Fall, Spring, Summer, Winter).
    Unparseable/missing labels ('?') sort last.
    """
    text = str(label)
    if text.isdigit():
        return (int(text), -1)
    # Localized forms may put the year first (2026年夏) or between words
    # (Verano de 2026), so identify the year and translated season token
    # independently instead of assuming English word order.
    year_match = re.search(r'(?<!\d)(\d{4})(?!\d)', text)
    if year_match:
        from hakubun.metadata import localize_season
        found_year = year_match.group(1)
        for season, position in _SEASON_CHRONO_ORDER.items():
            localized = localize_season(
                '%s %s' % (season, found_year))
            marker = localized.replace(found_year, '')
            marker = marker.replace('年', '').replace(' de ', '').strip()
            if season in text or (marker and marker in text):
                return (int(year_match.group(1)), position)
        return (int(year_match.group(1)), -1)
    season, sep, year = text.rpartition(' ')
    if sep and year.isdigit():
        return (int(year), _SEASON_CHRONO_ORDER.get(season, len(_SEASON_CHRONO_ORDER)))
    return (float('inf'), 0)
