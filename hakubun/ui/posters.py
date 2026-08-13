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

"""A small on-disk cache of cover art, shared by both toolkits.

The Mirror preview draws a grid of posters, which is a few hundred
pictures for a list-sized plan. Neither toolkit's existing image helper
is right for that on its own: Qt's ThumbManager is keyed on a show id
from a single account, and GTK's ImageBox spawns one thread per image.

So this holds the parts that are neither Qt's nor GTK's: where a URL
lands on disk, how many downloads may be in flight, and how a widget
that goes away cancels the one it was waiting for. Each toolkit keeps
what only it can do -- decoding the file and getting back onto its own
main loop.

Nothing here is user data. A poster that fails to download is a tile
with no picture, never an error the user has to deal with.
"""

import hashlib
import os
import queue
import threading
import urllib.request
from io import BytesIO

from hakubun import utils

try:
    from PIL import Image
    imaging_available = True
except ImportError:
    imaging_available = False

# Wide enough for a crisp tile on a HiDPI screen, small enough that a
# few hundred of them are not a download worth thinking about.
POSTER_WIDTH = 240
POSTER_HEIGHT = 340

# Cover art is served by the trackers' own CDNs, which are perfectly
# happy to be slow. Three at a time keeps a big grid moving without
# turning one preview into a burst of connections.
_WORKERS = 3


def cache_dir():
    return utils.to_cache_path('posters')


def cache_path(url):
    """Where `url` lives on disk.

    Keyed by a hash of the URL rather than by provider and id: the same
    work can be drawn from whichever provider happened to supply the
    art first, and the URL is the only thing that identifies the actual
    picture.
    """
    digest = hashlib.sha1(url.encode('utf-8')).hexdigest()
    return os.path.join(cache_dir(), '%s.jpg' % digest)


class PosterCache:
    """Downloads cover art in the background, at most a few at a time.

    `get(url)` returns a path immediately when the file is already
    cached -- the common case after the first preview -- and otherwise
    queues a download and returns None. `ready(url, path)` is called
    from a worker thread; callers marshal it onto their own main loop.
    """

    def __init__(self, ready, width=POSTER_WIDTH, height=POSTER_HEIGHT):
        self._ready = ready
        self._width = width
        self._height = height
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = set()
        self._failed = set()
        self._stopped = False
        self._threads = []

    # -- public --------------------------------------------------------

    def get(self, url):
        """The cached file for `url`, or None (a download is started)."""
        if not url or not imaging_available:
            return None
        path = cache_path(url)
        if os.path.isfile(path):
            return path
        with self._lock:
            if self._stopped or url in self._pending \
                    or url in self._failed:
                return None
            self._pending.add(url)
            self._start_worker()
        self._queue.put(url)
        return None

    def cancel(self, url):
        """Stop caring about `url` -- the widget waiting for it is
        gone. The download may already be in flight; this only makes
        sure nothing is called back about it."""
        with self._lock:
            self._pending.discard(url)

    def stop(self):
        """Shut down for good. Workers exit at their next task."""
        with self._lock:
            self._stopped = True
            self._pending.clear()
        for _ in self._threads:
            self._queue.put(None)

    # -- internals -----------------------------------------------------

    def _start_worker(self):
        """Grow the pool on demand, never past _WORKERS.

        Called with the lock held. A preview whose art is entirely
        cached never starts a thread at all.
        """
        if len(self._threads) >= _WORKERS:
            return
        thread = threading.Thread(target=self._run, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _run(self):
        while True:
            url = self._queue.get()
            if url is None:
                return
            with self._lock:
                if self._stopped:
                    return
                wanted = url in self._pending
            if not wanted:
                continue
            path = self._download(url)
            with self._lock:
                if self._stopped or url not in self._pending:
                    continue
                self._pending.discard(url)
                if path is None:
                    # Remember the failure: without this a grid that
                    # rebuilds on every preview would retry a dead URL
                    # for as long as the window is open.
                    self._failed.add(url)
            if path is not None:
                self._ready(url, path)

    def _download(self, url):
        path = cache_path(url)
        tmp = '%s.%d.part' % (path, os.getpid())
        try:
            utils.make_dir(cache_dir())
            request = urllib.request.Request(url)
            request.add_header(
                'User-Agent', 'HakubunPlusImage/%s' % utils.VERSION)
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read()
            image = Image.open(BytesIO(data))
            image.thumbnail((self._width, self._height), Image.BICUBIC)
            # Write via a temporary name: two Hakubun windows previewing
            # at once would otherwise race, and a half-written JPEG is
            # a decode error in whichever one reads it first.
            image.convert('RGB').save(tmp, 'JPEG')
            os.replace(tmp, path)
            return path
        except Exception:
            # Cover art is decoration. A dead URL, a redirect to HTML,
            # a provider having a bad afternoon -- none of it is worth
            # interrupting a preview over.
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
