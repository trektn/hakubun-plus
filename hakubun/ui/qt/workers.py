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

import os
import traceback
import urllib.error
import urllib.request
from io import BytesIO

try:
    from PIL import Image
except ImportError:
    # Pillow is optional (see ui/qt/__init__): without it images are
    # saved unscaled instead of thumbnailed.
    Image = None
from PyQt6 import QtCore

from hakubun import utils
from hakubun.engine import Engine


class ImageWorker(QtCore.QThread):
    """
    Image thread

    Downloads an image and shrinks it if necessary.

    """
    cancelled = False
    finished = QtCore.pyqtSignal(str)

    def __init__(self, remote, local, size=None):
        self.remote = remote
        self.local = local
        self.size = size
        super(ImageWorker, self).__init__()

    def __del__(self):
        self.wait()

    def run(self):
        self.cancelled = False

        req = urllib.request.Request(self.remote)
        req.add_header("User-agent", "HakubunPlusImage/{}".format(utils.VERSION))
        try:
            img_file = BytesIO(urllib.request.urlopen(req).read())
            if self.size and Image and "imaging_available" in os.environ:
                im = Image.open(img_file)
                im.thumbnail((self.size[0], self.size[1]), Image.BICUBIC)
                im.convert("RGB").save(self.local)
            else:
                # No Pillow: save unscaled rather than emitting finished
                # for a file that was never written (blank posters).
                with open(self.local, 'wb') as f:
                    f.write(img_file.read())
        except urllib.error.URLError as e:
            print("Warning: Error getting image ({})".format(e))
            return

        if self.cancelled:
            return

        self.finished.emit(self.local)

    def cancel(self):
        self.cancelled = True


class EngineWorker(QtCore.QThread):
    """
    Worker thread

    Contains the engine and manages every process in a separate thread.

    """
    engine = None
    function = None
    # Internal: carries (ret_function, result) from the worker thread back
    # to the main thread, where _dispatch routes it. Don't connect to this
    # from outside; use set_function's ret_function.
    _call_done = QtCore.pyqtSignal(object, dict)

    # Message handler signals
    changed_status = QtCore.pyqtSignal(str, int, str)
    raised_error = QtCore.pyqtSignal(str)
    raised_fatal = QtCore.pyqtSignal(str)

    # Event handler signals
    changed_show = QtCore.pyqtSignal(dict)
    changed_show_status = QtCore.pyqtSignal(dict, object)
    changed_list = QtCore.pyqtSignal(dict)
    changed_queue = QtCore.pyqtSignal(int)
    tracker_state = QtCore.pyqtSignal(dict)
    playing_show = QtCore.pyqtSignal(dict, bool, int)
    prompt_for_update = QtCore.pyqtSignal(dict, int)
    prompt_for_add = QtCore.pyqtSignal(dict, int)
    undo_stack_changed = QtCore.pyqtSignal()
    titles_changed = QtCore.pyqtSignal()

    def __init__(self):
        super(EngineWorker, self).__init__()

        self.overrides = {'start': self._start}
        # Calls made while another is in flight are queued and run in
        # order. The old scheme (disconnect finished / reconnect the new
        # callback) let e.g. a tracker-driven details load mid-sync steal
        # the sync's result and drop its own call entirely.
        self._pending = []
        self._ret_function = None
        self._call_done.connect(self._dispatch)

    def _messagehandler(self, classname, msgtype, msg):
        self.changed_status.emit(classname, msgtype, msg)

    def _error(self, msg):
        self.raised_error.emit(str(msg))

    def _fatal(self, msg):
        self.raised_fatal.emit(str(msg))

    def _changed_show(self, show, changes=None):
        self.changed_show.emit(show)

    def _changed_show_status(self, show, old_status=None):
        self.changed_show_status.emit(show, old_status)

    def _changed_list(self, show):
        self.changed_list.emit(show)

    def _changed_queue(self, queue):
        self.changed_queue.emit(len(queue))

    def _tracker_state(self, status):
        self.tracker_state.emit(status)

    def _playing_show(self, show, is_playing, episode):
        self.playing_show.emit(show, is_playing, episode)

    def _prompt_for_update(self, show, episode):
        self.prompt_for_update.emit(show, episode)

    def _prompt_for_add(self, show, episode):
        self.prompt_for_add.emit(show, episode)

    def _undo_stack_changed(self):
        self.undo_stack_changed.emit()

    def _titles_changed(self):
        self.titles_changed.emit()

    def _start(self, account):
        self.engine = Engine(account, self._messagehandler)

        self.engine.connect_signal('episode_changed', self._changed_show)
        self.engine.connect_signal('score_changed', self._changed_show)
        self.engine.connect_signal('tags_changed', self._changed_show)
        self.engine.connect_signal('mal_score_changed', self._changed_show)
        self.engine.connect_signal('status_changed', self._changed_show_status)
        self.engine.connect_signal('playing', self._playing_show)
        self.engine.connect_signal('show_added', self._changed_list)
        self.engine.connect_signal('show_deleted', self._changed_list)
        self.engine.connect_signal('show_synced', self._changed_show)
        self.engine.connect_signal('queue_changed', self._changed_queue)
        self.engine.connect_signal(
            'prompt_for_update', self._prompt_for_update)
        self.engine.connect_signal('prompt_for_add', self._prompt_for_add)
        self.engine.connect_signal('tracker_state', self._tracker_state)
        self.engine.connect_signal(
            'undo_stack_changed', self._undo_stack_changed)
        self.engine.connect_signal('titles_changed', self._titles_changed)

        self.engine.start()

    def set_function(self, function, ret_function, *args, **kwargs):
        # Always called from the main thread (as is _dispatch), so plain
        # list operations are safe. Owns starting the thread: callers
        # must NOT call start() themselves. A caller-side start() could
        # land in the window where the previous run() has returned
        # (isRunning() False) but its queued _call_done hasn't drained a
        # non-empty _pending yet -- and would re-run the stale previous
        # call (e.g. re-uploading a whole sync queue).
        if self.isRunning() or self._pending:
            self._pending.append((function, ret_function, args, kwargs))
            return
        self._set_current(function, ret_function, args, kwargs)
        self.start()

    def _set_current(self, function, ret_function, args, kwargs):
        if function in self.overrides:
            self.function = self.overrides[function]
        else:
            self.function = getattr(self.engine, function)

        self._ret_function = ret_function
        self.args = args
        self.kwargs = kwargs

    def _dispatch(self, ret_function, result):
        try:
            if ret_function:
                ret_function(result)
        except Exception as e:
            # A buggy callback must never take the app down (PyQt
            # aborts on unhandled exceptions in slots) nor strand the
            # calls queued behind it.
            traceback.print_exc()
            self._error('Internal callback error: %r' % e)
        finally:
            if self._pending:
                # run() has already emitted; make sure the thread fully
                # stopped before starting the next queued call.
                self.wait()
                self._set_current(*self._pending.pop(0))
                self.start()

    def __del__(self):
        self.wait()

    def run(self):
        if self.function is None:
            # Defensive: every real start goes through set_function or
            # _dispatch, which stage a call first. A stray QThread
            # start() with nothing staged must not re-run a consumed
            # (stale) function.
            return
        try:
            ret = self.function(*self.args, **self.kwargs)
            self._call_done.emit(self._ret_function,
                                 {'success': True, 'result': ret})
        except utils.HakubunError as e:
            self._error(e)
            self._call_done.emit(self._ret_function, {'success': False})
        except utils.HakubunFatal as e:
            self._fatal(e)
            # Still resolve the call: leaving it unresolved would strand
            # every entry in _pending for the rest of the session.
            self._call_done.emit(self._ret_function, {'success': False})
        except Exception as e:
            # Anything unexpected must still resolve the call: before
            # this, a stray exception (e.g. a TypeError from a malformed
            # API response) silently killed the thread and left the
            # caller waiting on a result forever.
            self._error(e)
            self._call_done.emit(self._ret_function, {'success': False})
        finally:
            # Mark the staged call consumed. Safe: _dispatch wait()s for
            # this thread before staging the next queued call, and the
            # idle path only stages after isRunning() is False (i.e.
            # after this method, finally included, has returned).
            self.function = None
