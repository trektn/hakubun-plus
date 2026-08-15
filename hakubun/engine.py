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

import contextlib
import datetime
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from decimal import Decimal
from functools import lru_cache, partial
from pathlib import Path

from hakubun import accounts
from hakubun import data
from hakubun import messenger
from hakubun import utils
from hakubun.i18n import _p
from hakubun.extras import redirections
from hakubun.parser import get_parser_class


def _localize_mediainfo(mediainfo):
    """Returns a copy of a lib adapter's mediainfo dict with
    statuses_dict's values translated for display.

    mediainfo (and the statuses_dict inside it) is the *class-level*
    dict a hakubun/lib/*.py adapter defines (e.g. libanilist.py's
    mediatypes['anime']), shared by every account using that library.
    Two things follow from that:

    - It can't be translated at its definition site. Class bodies run
      at import time, almost always before the UI has called
      i18n.install() with the user's chosen language (module imports
      happen before Qt/GTK have parsed the config to know what that
      choice even is), so any translation baked in there would be
      stuck on whatever _translation was default at that point.
    - It must never be mutated in place here -- every account sharing
      the class would see the translated copy, including a
      hypothetical later account whose own language differs.

    Called once per account/mediatype load (see get_api_info() and
    start()), which happens well after i18n.install(), and returns
    fresh copies both times -- cheap dicts, and it means every UI
    consumer reading self.mediainfo['statuses_dict'] gets a translated
    value for free, without patching each of the many call sites
    individually.

    The status *codes* statuses_dict keys on ('CURRENT', 'watching',
    ...) are provider-internal and untouched -- only the English
    display strings they map to are translated.
    """
    statuses_dict = mediainfo.get('statuses_dict')
    if not statuses_dict:
        return mediainfo

    localized = dict(mediainfo)
    localized['statuses_dict'] = {
        code: _p('status', label) for code, label in statuses_dict.items()
    }
    return localized


class Engine:
    """
    The engine is the controller that handles commands coming from
    the user interface and then queries the Data Handler for the necessary data.
    It doesn't control nor care about how the data is fetched by the Data Handler.

    After instantiating this class, the :func:`start` must be run to initialize the engine.
    Likewise, the :func:`unload` function must be called when you're done using the engine.

    The account and mediatype can be changed later on the fly by calling :func:`reload`.

    The **account** parameter is an account dictionary passed by an Account Manager
    and is used to run the engine.

    The **message_handler** is a reference to a messaging function for the engine
    to send to. Optional.
    """
    data_handler = None
    tracker = None
    redirections = None
    config = {}
    msg = None
    loaded = False
    playing = False
    hooks_available = []

    name = 'Engine'

    signals = {'show_added':        None,
               'show_deleted':      None,
               'episode_changed':   None,
               'score_changed':     None,
               'status_changed':    None,
               'show_synced':       None,
               'sync_complete':     None,
               'queue_changed':     None,
               'playing':           None,
               'prompt_for_update': None,
               'prompt_for_add':    None,
               'tracker_state':     None,
               'episode_missing': None,
               'undo_stack_changed': None,
               'mal_score_changed': None,
               }

    # Maximum number of user actions kept in the undo/redo history.
    UNDO_LIMIT = 50

    # If this many individual MAL score lookups in a row fail, assume MAL
    # is down/throttling us for now rather than grinding through the rest
    # making doomed requests -- rerunning later picks up whatever's still
    # missing.
    MAL_SCORE_CONSECUTIVE_FAILURE_LIMIT = 8

    # AniList's public GraphQL API has the best airing-schedule support of
    # any backend Hakubun+ talks to, and it's queryable unauthenticated by
    # MAL id -- so the airing schedule is always cross-referenced from
    # here regardless of which backend the account itself uses.
    ANILIST_GRAPHQL_URL = 'https://graphql.anilist.co'
    AIRING_SCHEDULE_BATCH = 50

    # How long a cached next-airing answer stays good. An episode airs
    # once a week, so an hour is nowhere near stale enough to mislead,
    # and it keeps repeated details opens off the network.
    NEXT_AIRING_CACHE_SECONDS = 3600

    def __init__(self, account=None, message_handler=None, accountnum=None):
        self.msg = messenger.Messenger(message_handler, self.name)

        # Set by start() if the configured title_parser couldn't be
        # imported and a fallback was used instead -- checked once by the
        # UI after the first successful load so it can surface this
        # somewhere more durable than a status-bar message that's about
        # to be overwritten by "Ready.".
        self.parser_fallback_warning = None

        # mal id -> (fetched_at, episode, aware UTC airing time), or
        # (fetched_at, None, None) for a show AniList has no schedule
        # for. Shared by get_airing_schedule and the per-show lookup the
        # details view does, so opening details for a show right after
        # viewing the schedule costs nothing -- and so a "no schedule"
        # answer isn't re-asked on every single open.
        self._next_airing_cache = {}

        # Utility parameter to get the account from the account manager
        if accountnum:
            account = accounts.AccountManager().get_account(accountnum)

        # Initialize
        self._load(account)
        self._init_data_handler()

    def _load(self, account):
        self.account = account

        # Create home directory
        utils.make_dir(utils.to_config_path())
        self.configfile = utils.to_config_path('config.json')

        # Create user directory
        userfolder = "%s.%s" % (account['username'], account['api'])
        utils.make_dir(utils.to_data_path(userfolder))

        self.msg.info('Hakubun+ v{0} - using account {1}({2}).'.format(
            utils.VERSION, account['username'], account['api']))
        self.msg.info('Reading config files...')
        try:
            self.config = utils.parse_config(
                self.configfile, utils.config_defaults)
        except IOError:
            raise utils.EngineFatal("Couldn't open config file.")

        # Expand media directories and ignore those that don't exist
        if isinstance(self.config['searchdir'], str):
            # Compatibility: Turn a string of a single directory into a list
            self.msg.debug("Fixing string searchdir to list.")
            self.config['searchdir'] = [self.config['searchdir']]

        self.searchdirs = [path for path in utils.expand_paths(
            self.config['searchdir']) if self._searchdir_exists(path)]

    def _init_data_handler(self, mediatype=None):
        # Create data handler
        self.data_handler = data.Data(
            self.msg, self.config, self.account, mediatype)
        self.data_handler.connect_signal('show_synced', self._data_show_synced)
        self.data_handler.connect_signal(
            'sync_complete', self._data_sync_complete)
        self.data_handler.connect_signal(
            'queue_changed', self._data_queue_changed)
        # Gated on the 'auto_add_mal_scores' config setting (Settings ->
        # Interface -> "Add MAL Scores"), off by default. The epoch
        # cancellation in fetch_mal_scores()/_fetch_mal_scores_task
        # handles a re-sync firing this again while a previous run is
        # still going.
        self.data_handler.connect_signal(
            'list_downloaded', self._auto_fetch_mal_scores)

        # Record the API details
        (self.api_info, self.mediainfo) = self.data_handler.get_api_info()
        self.mediainfo = _localize_mediainfo(self.mediainfo)

        # The undo/redo history is tied to this account+mediatype's data,
        # so it's reset whenever that changes (initial load, account
        # switch, or mediatype switch).
        self._undo_stack = deque(maxlen=self.UNDO_LIMIT)
        self._redo_stack = deque(maxlen=self.UNDO_LIMIT)
        self._replaying_undo = False
        # While not None, _record_undo() appends to this list instead of
        # pushing straight to _undo_stack, so a user action that triggers
        # several field changes (e.g. set_episode's auto status/date
        # change) is recorded and undone/redone as one compound entry.
        self._undo_group = None

        # Bumped on every fetch_mal_scores() call so an in-flight fetch
        # batch from a previous sync can tell it's been superseded and
        # stop instead of mutating show objects that download_data() has
        # since orphaned by replacing showlist wholesale.
        self._mal_fetch_epoch = 0

    def _data_show_synced(self, show, changes):
        self._emit_signal('show_synced', show, changes)

    def _data_sync_complete(self, items):
        self._emit_signal('sync_complete', items)

    def _data_queue_changed(self, queue):
        self._emit_signal('queue_changed', queue)

    def _record_undo(self, method, showid, title, old_value, new_value, description):
        """
        Records a reversible user action (an old/new value pair for a
        single field of a single show) onto the undo stack, so it can
        later be undone (and redone) via undo()/redo(). Does nothing while
        an undo/redo is itself being replayed, to avoid the replay being
        recorded as a new action. If called while inside a
        _grouped_undo() block, the entry is added to that group instead
        of being pushed directly.
        """
        if self._replaying_undo:
            return
        entry = (method, showid, title, old_value, new_value, description)
        if self._undo_group is not None:
            self._undo_group.append(entry)
            return
        self._undo_stack.append(entry)
        self._redo_stack.clear()
        self._emit_signal('undo_stack_changed')

    @contextlib.contextmanager
    def _grouped_undo(self):
        """
        Wrap a user-facing action that may internally call more than one
        _record_undo()-ing method (e.g. set_episode's auto status/date
        change) so the whole thing is recorded and undone/redone as a
        single compound entry instead of separate ones the user would
        have to undo one at a time. Nested calls join the outermost
        group rather than starting a new one.
        """
        if self._undo_group is not None:
            yield
            return
        self._undo_group = []
        try:
            yield
        finally:
            group = self._undo_group
            self._undo_group = None
            if not group:
                return
            self._undo_stack.append(group[0] if len(group) == 1 else group)
            self._redo_stack.clear()
            self._emit_signal('undo_stack_changed')

    def can_undo(self):
        return len(self._undo_stack) > 0

    def can_redo(self):
        return len(self._redo_stack) > 0

    def undo_count(self):
        """Number of actions available to undo -- used by the Statistics
        page's Hakubun section, distinct from can_undo()'s plain bool."""
        return len(self._undo_stack)

    def undo(self):
        """
        Reverts the last undoable user action (episode, score, status or
        tags change, possibly a compound of several fields changed
        together) by re-applying its old value(s). The reversal is
        itself queued for the next sync, same as any other change.
        """
        if not self._undo_stack:
            raise utils.EngineError('Nothing to undo.')

        entry = self._undo_stack.pop()
        actions = entry if isinstance(entry, list) else [entry]
        self._replaying_undo = True
        try:
            for method, showid, title, old_value, new_value, description in reversed(actions):
                self.msg.info("Undoing: %s" % description)
                try:
                    getattr(self, method)(showid, old_value)
                except utils.EngineError as e:
                    # The show may no longer be in a state where the old
                    # value applies (e.g. it was deleted). Don't leave the
                    # stacks inconsistent.
                    self.msg.warn("Couldn't undo '%s': %s" % (description, e))
                    raise
        finally:
            self._replaying_undo = False

        self._redo_stack.append(entry)
        self._emit_signal('undo_stack_changed')
        return actions[0][5]

    def redo(self):
        """Re-applies the last user action (or compound of actions) undone via undo()."""
        if not self._redo_stack:
            raise utils.EngineError('Nothing to redo.')

        entry = self._redo_stack.pop()
        actions = entry if isinstance(entry, list) else [entry]
        self._replaying_undo = True
        try:
            for method, showid, title, old_value, new_value, description in actions:
                self.msg.info("Redoing: %s" % description)
                try:
                    getattr(self, method)(showid, new_value)
                except utils.EngineError as e:
                    self.msg.warn("Couldn't redo '%s': %s" % (description, e))
                    raise
        finally:
            self._replaying_undo = False

        self._undo_stack.append(entry)
        self._emit_signal('undo_stack_changed')
        return actions[0][5]

    def _tracker_detected(self, path, filename):
        self.add_to_library(path, filename)

    def _tracker_removed(self, path, filename):
        self.remove_from_library(path, filename)

    def _tracker_playing(self, showid, playing, episode):
        show = self.get_show_info(showid)
        self._emit_signal('playing', show, playing, episode)

    def _tracker_update(self, show, episode):
        if self.config['tracker_update_prompt']:
            self.msg.info("Prompting for update.")
            self._emit_signal('prompt_for_update', show, episode)
        else:
            try:
                self.set_episode(show['id'], episode)
            except utils.HakubunError as e:
                self.msg.warn("Can't update episode: {}".format(e))

    def _tracker_unrecognised(self, show, episode):
        if self.config['tracker_not_found_prompt']:
            self._emit_signal('prompt_for_add', show, episode)

    def _tracker_state(self, status):
        self._emit_signal('tracker_state', status)

    def _emit_signal(self, signal, *args):
        try:
            # Call the signal function
            if self.signals[signal]:
                self.signals[signal](*args)
        except AttributeError:
            pass

        # If there are loaded hooks, call the functions in all of them
        for module in self.hooks_available:
            method = getattr(module, signal, None)
            if method is not None:
                self.msg.debug("Calling hook {}:{}...".format(
                    module.__name__, signal))
                try:
                    method(self, *args)
                except Exception as err:
                    self.msg.warn("Exception on hook {}:{}: {}".format(
                        module.__name__, signal, err))

    def _get_tracker_list(self, filter_num=None):
        tracker_list = {}
        if isinstance(filter_num, type(None)):
            source_list = self.get_list()
        elif isinstance(filter_num, list):
            status_list = [s for s in filter_num if s is not self.mediainfo['statuses_finish']]
            status_list_display = [self.mediainfo['statuses_dict'][s] for s in status_list]
            self.msg.debug(f"Scanning for {', '.join(status_list_display)}")
            source_list = []
            for status in status_list:
                source_list.extend(self.filter_list(status))
        else:
            source_list = self.filter_list(filter_num)

        for show in source_list:
            tracker_list[show['id']] = {
                'id': show['id'],
                'title': show['title'],
                'my_progress': show['my_progress'],
                'total': show['total'],
                'type': None,
                'titles': self.data_handler.get_show_titles(show),
            }

        altnames_map = self.data_handler.get_altnames_map()
        return (tracker_list, altnames_map)

    def _update_tracker(self):
        if self.tracker:
            self.tracker.update_list(self._get_tracker_list())

    def _cleanup(self):
        # If the engine wasn't closed for whatever reason, do it
        if self.loaded:
            self.msg.info("Forcing exit...")
            self.data_handler.unload(True)
            if self.tracker:
                self.tracker.disable()
            self.loaded = False

    def connect_signal(self, signal, callback):
        try:
            self.signals[signal] = callback
        except KeyError:
            raise utils.EngineFatal("Invalid signal.")

    def set_message_handler(self, message_handler):
        """Changes the message handler function on the fly."""
        self.msg = messenger.Messenger(message_handler, self.name)
        self.data_handler.set_message_handler(self.msg)

    def start(self):
        """
        Starts the engine.
        This function should be called before doing anything with the engine,
        as it initializes the data handler.
        """
        if self.loaded:
            raise utils.HakubunError("Already loaded.")

        self.msg.debug("Starting engine...")

        # Temporary deprecation warning for Python 3.9
        if sys.version_info[:2] == (3, 9):
            self.msg.warn("\n==============="
                          "\nDEPRECATION WARNING: Python 3.9 has reached end of life."
                          "\nHakubun+ will drop support for it soon. It is recommended"
                          "\nto upgrade to Python 3.10 or newer."
                          "\n===============")

        # Start the data handler
        try:
            (self.api_info, self.mediainfo) = self.data_handler.start()
            self.mediainfo = _localize_mediainfo(self.mediainfo)
        except utils.DataError as e:
            raise utils.DataFatal(str(e))
        except utils.APIError as e:
            raise utils.APIFatal(str(e))

        # Load redirection file if supported
        api = self.api_info['shortname']
        mediatype = self.data_handler.userconfig['mediatype']
        if redirections.supports(api, mediatype):
            if utils.file_exists(utils.to_config_path('anime-relations.txt')):
                fname = utils.to_config_path('anime-relations.txt')
                self.msg.debug("Using user-provided redirection file.")
            else:
                fname = utils.to_data_path('anime-relations.txt')
                if self.config['redirections_time'] and (
                        not utils.file_exists(fname) or
                        utils.file_older_than(fname, self.config['redirections_time'] * 86400)):
                    self.msg.info("Syncing redirection file...")
                    self.msg.debug("Syncing from: %s" %
                                   self.config['redirections_url'])
                    utils.sync_file(fname, self.config['redirections_url'])

            if not utils.file_exists(fname):
                self.msg.debug("Defaulting to repo provided redirections file.")
                fname = utils.DATADIR + '/anime-relations/anime-relations.txt'

            self.msg.info("Parsing redirection file...")
            try:
                self.redirections = redirections.parse_anime_relations(
                    fname, api)
            except Exception as e:
                self.msg.warn("Error parsing anime-relations.txt!")
                self.msg.debug("{}".format(e))

        # Determine parser library. If the configured parser can't be
        # imported (an optional dependency isn't installed), cascade down
        # through progressively more basic parsers rather than jumping
        # straight to aie -- e.g. anitomy_ng missing should still try
        # anitopy before giving up, not skip past a perfectly good parser
        # the user may already have installed.
        parser_fallback_chain = {
            'anitomy_ng': 'anitopy',
            'anitopy': 'aie',
        }
        requested_parser = self.config['title_parser']
        parser_name = requested_parser
        while True:
            try:
                self.msg.debug(self.name, "Initializing parser...")
                self.parser_class = get_parser_class(self.msg, parser_name)
                break
            except ImportError as e:
                self.msg.warn(self.name, "Couldn't import specified parser: {}; {}".format(
                    parser_name, e))
                next_parser = parser_fallback_chain.get(parser_name, 'aie')
                self.msg.warn(self.name, "Falling back to {}...".format(next_parser))
                parser_name = next_parser

        if parser_name != requested_parser:
            self.parser_fallback_warning = (
                "Couldn't load the configured title parser '{}' (its optional "
                "dependency isn't installed); using '{}' instead.".format(
                    requested_parser, parser_name))
            # Also correct the config value in memory (not persisted to
            # disk), not just self.parser_class -- the tracker looks up
            # config['title_parser'] independently when it starts (see
            # TrackerBase.__init__), and would otherwise retry the same
            # broken import and fail with a misleading "couldn't import
            # the tracker" instead of this already-explained parser issue.
            self.config['title_parser'] = parser_name

        # Rescan library if necessary
        if self.config['library_autoscan']:
            try:
                self._scan_library_if_changed()
            except utils.HakubunError as e:
                self.msg.warn("Can't auto-scan library: {}".format(e))

        # Load hook files
        if self.config['use_hooks']:
            hooks_dir = utils.to_config_path('hooks')
            if os.path.isdir(hooks_dir):
                import importlib.util
                import pkgutil

                self.msg.info("Importing user hooks...")
                for finder, name, _ in pkgutil.iter_modules([hooks_dir]):
                    # List all the hook files in the hooks folder, import them
                    # and call the init() function if they have them
                    # We build the list "hooks available" with the loaded modules
                    # for later calls.
                    try:
                        self.msg.debug("Importing hook {}...".format(name))
                        module_spec = finder.find_spec(name)
                        module = importlib.util.module_from_spec(module_spec)
                        module_spec.loader.exec_module(module)
                        if hasattr(module, 'init'):
                            module.init(self)
                        self.hooks_available.append(module)
                    except ImportError:
                        self.msg.warn("Error importing hook {}.".format(name))
                        self.msg.exception(sys.exc_info())

        # Start tracker
        if self.mediainfo.get('can_play') and self.config['tracker_enabled']:
            self.msg.debug("Initializing tracker...")
            try:
                TrackerClass = self._get_tracker_class(
                    self.config['tracker_type'])

                self.tracker = TrackerClass(self.msg,
                                            self._get_tracker_list(),
                                            self.config,
                                            self.searchdirs,
                                            self.redirections,
                                            )
                self.tracker.connect_signal('detected', self._tracker_detected)
                self.tracker.connect_signal('removed', self._tracker_removed)
                self.tracker.connect_signal('playing', self._tracker_playing)
                self.tracker.connect_signal('update', self._tracker_update)
                self.tracker.connect_signal(
                    'unrecognised', self._tracker_unrecognised)
                self.tracker.connect_signal('state', self._tracker_state)
            except ImportError:
                self.msg.warn("Couldn't import specified tracker: {}".format(
                    self.config['tracker_type']))
                self.msg.exception(sys.exc_info())

        self.loaded = True
        self.msg.debug("Engine started")
        return True

    def unload(self):
        """
        Closes the data handler and closes the engine cleanly.
        This should be called when closing the client application, or when you're
        sure you're not going to use the engine anymore. This does all the necessary
        procedures to close the data handler cleanly and then itself.

        """
        if self.loaded:
            self.msg.info("Unloading...")
            self.data_handler.unload()
            if self.tracker:
                self.tracker.disable()

            # If there are loaded hooks, unload them
            self.msg.info("Unloading user hooks...")
            for module in self.hooks_available.copy():
                self.msg.debug("Unloading hook {}...".format(
                    module.__name__))
                try:
                    if hasattr(module, 'destroy'):
                        module.destroy(self)
                    self.hooks_available.remove(module)
                except Exception as err:
                    self.msg.warn("Error destroying hook {}: {}".format(
                        module.__name__, err))

            self.loaded = False

    def reload(self, account=None, mediatype=None):
        """Changes the API and/or mediatype and reloads itself."""
        if self.loaded:
            self.unload()

        if account:
            self._load(account)

        self._init_data_handler(mediatype)
        self.start()

    def get_config(self, key):
        """Returns the specified key from the configuration."""
        return self.config[key]

    def get_userconfig(self, key):
        return self.data_handler.userconfig[key]

    def set_config(self, key, value):
        """
        Writes the defined key to the configuration.
        Note that this writes the configuration only to memory; when you're
        done doing all necessary changes, make sure to write the configuration file
        with :func:`save_config`."""
        self.config[key] = value

    def save_config(self):
        """Writes all configuration files to disk."""

        # Save config file
        utils.save_config(self.config, self.configfile)

    def get_list(self):
        """
        Returns the full show list requested from the data handler as a list of show dictionaries.
        If you only need shows in a specified status, use :func:`filter_list`.
        """
        return self.data_handler.get().values()

    def get_show_info(self, showid=None, title=None, filename=None):
        """
        Returns the show dictionary for the specified **showid**.
        """
        showdict = self.data_handler.get()

        if showid:
            # Get show by ID
            try:
                return showdict[showid]
            except KeyError:
                raise utils.EngineError("Show not found.")
        elif title:
            showdict = self.data_handler.get()
            # Get show by title, slower
            for show in showdict.values():
                if show['title'] == title:
                    return show
            raise utils.EngineError("Show not found.")
        elif filename:
            # Guess show by filename
            self.msg.debug("Guessing by filename.")

            anime_info = self.parser_class(self.msg, filename)
            (show_title, ep) = anime_info.getName(), anime_info.getEpisode()
            self.msg.debug("Show guess: {}".format(show_title))

            if show_title:
                tracker_list = self._get_tracker_list()

                show = utils.guess_show(show_title, tracker_list)
                if show:
                    return utils.redirect_show((show, ep), self.redirections, tracker_list)
                else:
                    raise utils.EngineError("Show not found.")
            else:
                raise utils.EngineError("File name not recognized.")

    def get_show_details(self, show):
        """
        Returns detailed information about **show** requested from the data handler.
        """
        details = self.data_handler.info_get(show)

        # mal_score isn't part of the detail-fetch response itself.
        # Shows already on the user's list get it from the cached
        # background/manual fetch (see fetch_mal_scores) -- but a show
        # viewed from search results isn't on the list yet, so there's
        # nothing cached for it. This is called from a worker thread in
        # both UIs already (Qt's worker_call / GTK's own thread in
        # ShowInfoBox), so a quick on-demand lookup here is safe to do
        # synchronously rather than needing yet another background job.
        if not show.get('mal_score') and show.get('mal_id'):
            try:
                mal_lib = self._get_mal_auth_lib()
                if mal_lib:
                    # Single attempt: the details view is waiting on this,
                    # and the retry/backoff path can stall it for ~6s.
                    score = self._fetch_mal_score(
                        show['mal_id'], mal_lib, attempts=1)
                    if score is not None:
                        show['mal_score'] = '%.2f' % score
            except Exception as e:
                self.msg.debug("Couldn't fetch MAL score for details: %s" % e)

        # Everything below is appended to 'extra', which both UIs (and
        # the CLI) render generically as label/value rows -- so a row
        # added here shows up in all of them without UI-side work.
        rows = []

        if show.get('mal_score'):
            rows.append(('MAL Score', show['mal_score']))

        # Phrased as a full sentence-fragment label because the value is
        # a duration, not a fact about the show: "Next episode will air
        # in: 3 days (Sat Aug 02, 2026)".
        try:
            next_airing = self.get_next_airing(show)
        except Exception as e:
            self.msg.debug("Couldn't resolve next airing for details: %s" % e)
            next_airing = None
        if next_airing:
            (episode, airing_at) = next_airing
            when = utils.format_next_airing(airing_at)
            if when:
                rows.append(('Next episode will air in', when))
                if episode:
                    rows.append(('Next episode', str(episode)))

        # A manually pinned folder (set_show_folder) is otherwise
        # invisible once set -- nothing in the list marks a show as
        # pinned, so the details view is where you find out.
        folder = self.get_show_folder(show['id'])
        if folder:
            rows.append(('Folder', folder))

        if rows:
            details = dict(details)
            details['extra'] = list(details.get('extra') or []) + rows
        return details

    def _mal_id_of(self, show):
        """The show's MAL id, which is what AniList's public API is
        queried by.

        'mal_id' is only ever populated by the MAL-score cross-reference
        feature (see fetch_mal_scores), which exists for accounts on a
        *different* backend -- it's never set for an actual MAL account,
        since there was never anything to cross-reference against. For
        MAL accounts, 'id' already *is* the MAL id, so use that instead
        of treating every show as unresolvable.
        """
        if self.api_info.get('shortname') == 'mal':
            return show.get('id')
        return show.get('mal_id')

    def _cache_next_airing(self, mal_id, episode, airing_at):
        self._next_airing_cache[mal_id] = (
            time.time(), episode, airing_at)

    def get_next_airing(self, show):
        """When **show**'s next episode airs, as (episode, aware UTC
        datetime), or None if it isn't airing / nothing knows.

        Three sources, cheapest first: the show dict itself (AniList
        accounts get 'next_ep_time' with the list, for free), this
        session's cache (populated by get_airing_schedule and by earlier
        calls here), and finally a single-show query against AniList's
        public API by MAL id -- the same cross-reference the airing
        schedule uses, so it works on every backend.
        """
        if show.get('status') != utils.Status.AIRING:
            return None

        if show.get('next_ep_time'):
            return (show.get('next_ep_number'),
                    utils.as_utc(show['next_ep_time']))

        mal_id = self._mal_id_of(show)
        if not mal_id:
            return None

        cached = self._next_airing_cache.get(mal_id)
        if cached and time.time() - cached[0] < self.NEXT_AIRING_CACHE_SECONDS:
            return (cached[1], cached[2]) if cached[2] else None

        query = '''
        query ($id: Int) {
          Media(idMal: $id, type: ANIME) {
            nextAiringEpisode { airingAt episode }
          }
        }'''
        try:
            data = self._anilist_public_query(query, {'id': mal_id})
        except utils.HakubunError as e:
            # A details view is not worth failing over a schedule
            # lookup; the row is simply omitted.
            self.msg.debug("Couldn't fetch airing time for details: %s" % e)
            return None

        media = (data.get('data') or {}).get('Media') or {}
        next_ep = media.get('nextAiringEpisode')
        if not next_ep:
            self._cache_next_airing(mal_id, None, None)
            return None

        airing_at = datetime.datetime.fromtimestamp(
            next_ep['airingAt'], tz=datetime.timezone.utc)
        self._cache_next_airing(mal_id, next_ep['episode'], airing_at)
        return (next_ep['episode'], airing_at)

    def get_airing_schedule(self):
        """
        Cross-references the airing shows in the list against AniList's
        public API (by MAL id) to find when each one's next episode airs.

        Returns a list of {'show': <show dict>, 'episode': int,
        'airing_at': aware UTC datetime}, sorted by airing_at. Shows
        without a resolvable mal_id, or that AniList doesn't have a
        schedule for, are silently skipped.
        """
        mal_id_of = self._mal_id_of
        shows = [s for s in self.data_handler.get().values()
                if mal_id_of(s) and s.get('status') == utils.Status.AIRING]
        if not shows:
            return []

        # Some shows can share a mal_id after a redirection merge; keep
        # the first one seen rather than fetching duplicates.
        by_mal_id = {}
        for show in shows:
            by_mal_id.setdefault(mal_id_of(show), show)

        query = '''
        query ($ids: [Int], $perPage: Int) {
          Page(page: 1, perPage: $perPage) {
            media(idMal_in: $ids, type: ANIME) {
              idMal
              nextAiringEpisode { airingAt episode }
            }
          }
        }'''

        schedule = []
        mal_ids = list(by_mal_id.keys())
        for offset in range(0, len(mal_ids), self.AIRING_SCHEDULE_BATCH):
            batch = mal_ids[offset:offset + self.AIRING_SCHEDULE_BATCH]
            data = self._anilist_public_query(
                query, {'ids': batch, 'perPage': self.AIRING_SCHEDULE_BATCH})

            # AniList reports query errors in a 200 response with
            # "data": null, which _anilist_public_query can't turn into
            # an EngineError -- guard here so the UI gets a normal error
            # instead of a TypeError that hangs the loading dialog.
            page = (data.get('data') or {}).get('Page') or {}
            if not page:
                raise utils.EngineError(
                    "Could not fetch airing schedule: %s"
                    % (data.get('errors') or 'malformed response'))

            for media in page.get('media') or []:
                next_ep = media.get('nextAiringEpisode')
                if not next_ep:
                    # Remember the negative answer too, so the details
                    # view doesn't go ask again one show at a time.
                    self._cache_next_airing(media['idMal'], None, None)
                    continue
                show = by_mal_id.get(media['idMal'])
                if not show:
                    continue
                airing_at = datetime.datetime.fromtimestamp(
                    next_ep['airingAt'], tz=datetime.timezone.utc)
                self._cache_next_airing(
                    media['idMal'], next_ep['episode'], airing_at)
                schedule.append({
                    'show': show,
                    'episode': next_ep['episode'],
                    'airing_at': airing_at,
                })

        schedule.sort(key=lambda entry: entry['airing_at'])
        return schedule

    def _anilist_public_query(self, query, variables):
        payload = json.dumps(
            {'query': query, 'variables': variables}).encode('utf-8')
        request = urllib.request.Request(
            self.ANILIST_GRAPHQL_URL, payload,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Hakubun-Plus/%s' % utils.VERSION,
            })
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode('utf-8'))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            raise utils.EngineError("Could not fetch airing schedule: %s" % e)

    def regex_list(self, regex):
        """
        It asks the data handler to do a regex search for a show and returns the
        list of show dictionaries with all the matches.
        """
        showlist = self.data_handler.get()
        return list(v for k, v in showlist.items() if re.search(regex, v['title'], re.I))

    def regex_list_titles(self, pattern):
        # TODO : Temporal hack for the client autocomplete function
        showlist = self.data_handler.get()
        newlist = list()
        for v in showlist.values():
            if re.match(pattern, v['title'], re.I):
                if ' ' in v['title']:
                    newlist.append('"' + v['title'] + '" ')
                else:
                    newlist.append(v['title'] + ' ')

        return newlist

    def tracker_status(self):
        """
        Asks the tracker for its current status.
        """

        if self.tracker:
            return self.tracker.get_status()

        return None

    def search(self, criteria, method=utils.SearchMethod.KW):
        """
        Request a remote list of shows matching the criteria
        and returns it as a list of show dictionaries.
        This is useful to add a show.
        """
        if method not in self.mediainfo.get('search_methods', [utils.SearchMethod.KW]):
            raise utils.EngineError(
                'Search method not supported by API or mediatype.')

        results = self.data_handler.search(criteria, method)
        self._prefetch_search_mal_scores(results)
        return results

    def _prefetch_search_mal_scores(self, results):
        """
        Kicks off a background job to cross-reference MyAnimeList scores
        for search results ahead of time, mutating the same show dicts
        the UI already holds -- so by the time the user actually opens a
        show's details, get_show_details()'s own on-demand check finds a
        score already cached instead of having to wait on a fresh
        network round-trip right when details are requested.
        """
        if self.api_info.get('shortname') == 'mal':
            return
        if not self.mediainfo.get('can_reference_mal'):
            return
        shows = [s for s in results if s.get('mal_id')]
        if not shows:
            return

        threading.Thread(
            target=self._prefetch_search_mal_scores_task, args=(shows,),
            daemon=True).start()

    def _prefetch_search_mal_scores_task(self, shows):
        # A short pause first so this doesn't compete with whatever the
        # user does immediately after a search comes back (e.g. clicking
        # into results, or refining the search again right away).
        time.sleep(1)

        mal_lib = self._get_mal_auth_lib()
        if not mal_lib:
            return

        consecutive_failures = 0
        for show in shows:
            try:
                score = self._fetch_mal_score(show['mal_id'], mal_lib)
                consecutive_failures = 0
            except Exception as e:
                self.msg.debug(
                    'MAL score prefetch failed for %s: %s' % (show['title'], e))
                consecutive_failures += 1
                if consecutive_failures >= self.MAL_SCORE_CONSECUTIVE_FAILURE_LIMIT:
                    self.msg.debug(
                        'Too many consecutive prefetch failures, stopping.')
                    return
                continue
            if score is not None:
                show['mal_score'] = '%.2f' % score

    def add_show(self, show, status=None):
        """
        Adds **show** to the list and queues the list update
        for the next sync.
        """
        # Check if operation is supported by the API
        if not self.mediainfo.get('can_add'):
            raise utils.EngineError('Operation not supported by API.')

        # Set to the requested status
        if status:
            if status not in self.mediainfo['statuses']:
                raise utils.EngineError('Invalid status.')

            show['my_status'] = status

        # Add in data handler
        self.data_handler.queue_add(show)

        # Update the tracker with the new information
        self._update_tracker()

        # Emit signal
        self._emit_signal('show_added', show)

    def set_episode(self, showid, newep):
        """
        Updates the progress of the specified **showid** to **newep**
        and queues the list update for the next sync.
        """
        # Check if operation is supported by the API
        if not self.mediainfo.get('can_update'):
            raise utils.EngineError('Operation not supported by API.')

        # Check for the episode number
        try:
            newep = int(newep)
        except ValueError:
            raise utils.EngineError('Episode must be numeric.')

        # Get the show info
        show = self.get_show_info(showid)
        # More checks
        if (show['total'] and newep > show['total']) or newep < 0:
            raise utils.EngineError('Episode out of limits.')
        if show['my_progress'] == newep:
            raise utils.EngineError("Show already at episode %d" % newep)

        # Change episode. Grouped so that an auto status change triggered
        # below is undone/redone together with the episode change as one
        # user-facing action, instead of requiring two separate undos.
        with self._grouped_undo():
            old_progress = show['my_progress']
            self.msg.info("Updating show %s to episode %d..." %
                          (show['title'], newep))
            self.data_handler.queue_update(show, 'my_progress', newep)
            self._record_undo(
                'set_episode', show['id'], show['title'], old_progress, newep,
                "Episode change for %s (%d -> %d)" % (show['title'], old_progress, newep))

            # Emit signal
            self._emit_signal('episode_changed', show)

            # Change status if required
            oldstatus = show['my_status']
            if self.config['auto_status_change'] and self.mediainfo.get('can_status'):
                try:
                    if show['total'] and newep == show['total'] and self.mediainfo.get('statuses_finish'):
                        if (
                                not self.config['auto_status_change_if_scored'] or
                                not self.mediainfo.get('can_score') or
                                show['my_score']
                        ):
                            # Change to finished status
                            self.set_status(
                                show['id'], self._guess_new_finish(show))
                        else:
                            self.msg.warn("Updated episode but status won't be changed until a score is set.")
                    elif newep == 1 and self.mediainfo.get('statuses_start'):
                        # Change to start status
                        self.set_status(show['id'], self._guess_new_start(show))
                except utils.EngineError as e:
                    # Only warn about engine errors since status change here is not critical
                    self.msg.warn('Updated episode but status wasn\'t changed: %s' % e)

            # Change dates if required
            if self.config['auto_date_change'] and self.mediainfo.get('can_date'):
                start_date = finish_date = None

                try:
                    initial_status = self.mediainfo.get('statuses_start')[0]

                    if newep == 1 and show['my_status'] == initial_status:
                        start_date = datetime.date.today()
                    if show['total'] and newep == show['total'] and oldstatus == initial_status:
                        finish_date = datetime.date.today()

                    self.set_dates(show['id'], start_date, finish_date)
                except utils.EngineError as e:
                    # Only warn about engine errors since date change here is not critical
                    self.msg.warn('Updated episode but dates weren\'t changed: %s' % e)

        # Update the tracker with the new information
        self._update_tracker()

        return show

    def set_dates(self, showid, start_date=None, finish_date=None):
        """
        Updates the start date and finish date of a show.
        If any of the two are None, it won't be changed.
        """
        if not self.mediainfo.get('can_date'):
            raise utils.EngineError('Operation not supported by API.')

        show = self.get_show_info(showid)

        # Change the start date if required
        if start_date:
            if not isinstance(start_date, datetime.date):
                raise utils.EngineError('start_date must be a Date object.')
            self.msg.info("Updating show %s start date to %s..." %
                      (show['title'], start_date))
            self.data_handler.queue_update(show, 'my_start_date', start_date)

        if finish_date:
            if not isinstance(finish_date, datetime.date):
                raise utils.EngineError('finish_date must be a Date object.')
            self.msg.info("Updating show %s finish date to %s..." %
                      (show['title'], finish_date))
            self.data_handler.queue_update(show, 'my_finish_date', finish_date)

    def set_score(self, showid, newscore):
        """
        Updates the score of the specified **showid** to **newscore**
        and queues the list update for the next sync.
        """
        # Check if operation is supported by the API
        if not self.mediainfo.get('can_score'):
            raise utils.EngineError('Operation not supported by API.')

        # Check for the correctness of the score
        if (Decimal(str(newscore)) % Decimal(str(self.mediainfo['score_step']))) != 0:
            raise utils.EngineError('Invalid score.')

        # Convert to proper type
        if isinstance(self.mediainfo['score_step'], int):
            newscore = int(newscore)
        else:
            newscore = float(newscore)

        # Get the show and update it
        show = self.get_show_info(showid)
        # More checks
        if newscore > self.mediainfo['score_max']:
            raise utils.EngineError('Score out of limits.')
        if show['my_score'] == newscore:
            raise utils.EngineError("Score already at %s" % newscore)

        # Change score. Grouped for the same reason as set_episode: an
        # auto status change below should undo/redo together with the
        # score change as one action.
        with self._grouped_undo():
            old_score = show['my_score']
            self.msg.info("Updating show %s score to %s..." %
                          (show['title'], newscore))
            self.data_handler.queue_update(show, 'my_score', newscore)
            self._record_undo(
                'set_score', show['id'], show['title'], old_score, newscore,
                "Score change for %s (%s -> %s)" % (show['title'], old_score, newscore))

            # Emit signal
            self._emit_signal('score_changed', show)

            # Change status if required
            if (
                    show['total'] and
                    show['my_progress'] == show['total'] and
                    show['my_score'] and
                    self.mediainfo.get('can_status') and
                    self.config['auto_status_change'] and
                    self.config['auto_status_change_if_scored'] and
                    self.mediainfo.get('statuses_finish')
            ):
                try:
                    self.set_status(show['id'], self._guess_new_finish(show))
                except utils.EngineError as e:
                    # Only warn about engine errors since status change here is not critical
                    self.msg.warn('Updated episode but status wasn\'t changed: %s' % e)

        return show

    def set_status(self, showid, newstatus):
        """
        Updates the score of the specified **showid** to **newstatus** (number)
        and queues the list update for the next sync.
        """
        # Check if operation is supported by the API
        if not self.mediainfo.get('can_status'):
            raise utils.EngineError('Operation not supported by API.')

        try:
            newstatus = int(newstatus)
        except ValueError:
            pass  # It's not necessary for it to be an int

        # Check if the status is valid
        _statuses = self.mediainfo['statuses_dict']
        if newstatus not in _statuses:
            raise utils.EngineError('Invalid status.')

        # Get the show and update it
        show = self.get_show_info(showid)
        # More checks
        if show['my_status'] == newstatus:
            raise utils.EngineError("Show already in %s." %
                                    _statuses[newstatus])

        # Change status
        old_status = show['my_status']
        self.msg.info("Updating show %s status to %s..." %
                      (show['title'], _statuses[newstatus]))
        self.data_handler.queue_update(show, 'my_status', newstatus)
        self._record_undo(
            'set_status', show['id'], show['title'], old_status, newstatus,
            "Status change for %s (%s -> %s)" % (
                show['title'], _statuses.get(old_status, old_status), _statuses[newstatus]))

        # Emit signal
        self._emit_signal('status_changed', show, old_status)

        return show

    def set_tags(self, showid, newtags):
        """
        Updates the tags of the specified **showid** to **newtags**
        and queues the list update for the next sync.
        """
        # Check if operation is supported by the API
        if 'can_tag' not in self.mediainfo or not self.mediainfo.get('can_tag'):
            raise utils.EngineError('Operation not supported by API.')

        # Get the show and update it
        show = self.get_show_info(showid)
        # More checks
        if show['my_tags'] == newtags:
            raise utils.EngineError("Tags already %s" % newtags)

        # Change tags
        old_tags = show['my_tags']
        self.msg.info("Updating show %s to tags '%s'..." %
                      (show['title'], newtags))
        self.data_handler.queue_update(show, 'my_tags', newtags)
        self._record_undo(
            'set_tags', show['id'], show['title'], old_tags, newtags,
            "Tags change for %s" % show['title'])

        # Emit signal
        self._emit_signal('tags_changed', show)

        return show

    def delete_show(self, show):
        """
        Deletes **show** completely from the list and queues the list update for the next sync.
        """
        if not self.mediainfo.get('can_delete'):
            raise utils.EngineError('Operation not supported by API.')

        # Add in data handler
        self.data_handler.queue_delete(show)

        # Update the tracker with the new information
        self._update_tracker()

        # Emit signal
        self._emit_signal('show_deleted', show)

    def library(self):
        return self.data_handler.library_get()

    def _library_dirs_signature(self, paths):
        """
        Cheap fingerprint of the search directories' current state, used to
        detect whether a full library scan is actually necessary. Only
        stats directories (not every file), so it's much faster than a
        full scan_library() call while still catching added/removed/
        renamed files -- any of those changes the mtime of the directory
        that contains them.
        """
        latest = 0
        entries = 0
        for root_path in paths:
            for dirpath, _dirnames, filenames in os.walk(root_path):
                try:
                    mtime = os.stat(dirpath).st_mtime
                except OSError:
                    continue
                if mtime > latest:
                    latest = mtime
                entries += len(filenames)
        return latest, entries

    def _scan_status_scope(self):
        """The list statuses a library scan covers, shared by
        _scan_library_if_changed and scan_library's own default so the two
        can never disagree about what's in scope."""
        if self.config['scan_whole_list']:
            return self.mediainfo['statuses']
        return self.mediainfo.get(
            'statuses_library', self.mediainfo['statuses_start'])

    def _tracker_list_signature(self, tracker_list):
        """Fingerprint of the tracker list content a scan matched against
        (every in-scope show's id and titles/aliases), so that adding,
        removing, or retitling a show is detected the same way a changed
        file on disk is -- not just "did the directory mtimes move".
        Uses a stable hash (not Python's randomized hash()) since this is
        persisted across process restarts and compared against on the
        next run.
        """
        showlist = tracker_list[0]
        parts = []
        for show_id in sorted(showlist):
            show = showlist[show_id]
            parts.append(str(show_id))
            parts.extend(sorted(show['titles']))
        return hashlib.sha256(
            '\x1f'.join(parts).encode('utf-8', 'surrogatepass')).hexdigest()

    def _scan_library_if_changed(self):
        """
        Runs scan_library() only if the search directories or the tracker
        list itself look like they may have changed since the last scan,
        instead of unconditionally re-walking and re-matching the whole
        library every time the engine starts -- e.g. on every account or
        mediatype switch, which previously re-did this even when nothing
        on disk had changed at all.

        A pure filesystem change (new/removed/renamed files) still uses
        the incremental per-file cache. But a tracker list change (a show
        added/removed, retitled, or an alias/redirection edited) can
        invalidate an already-cached guess for a file whose own mtime
        never moved -- e.g. a file that didn't match anything before the
        show was added to the list would otherwise stay "unmatched"
        forever -- so that case forces every file to be re-guessed from
        scratch (rescan=True), not just files under a changed directory.
        """
        show_folders = self.data_handler.show_folders_get()
        if not self.mediainfo.get('can_play') \
                or not (self.config['searchdir'] or show_folders):
            return

        my_status = self._scan_status_scope()
        tracker_list = self._get_tracker_list(my_status)
        dirs_signature = self._library_dirs_signature(
            self.searchdirs + list(show_folders.values()))
        list_signature = self._tracker_list_signature(tracker_list)
        signature = (dirs_signature, list_signature)
        cached_signature = self.data_handler.library_scan_signature_get()

        if cached_signature is not None \
                and tuple(cached_signature) == signature \
                and self.data_handler.library_get():
            self.msg.info("Local library unchanged, skipping scan.")
            return

        list_changed = cached_signature is not None \
            and tuple(cached_signature)[1:] != signature[1:]
        self.scan_library(my_status=my_status, signature=signature,
                           rescan=list_changed)

    def scan_library(self, my_status=None, rescan=False, path=None,
                     signature=None):
        """`signature`, when given, is a precomputed
        (_library_dirs_signature(), _tracker_list_signature()) pair the
        caller already has (see _scan_library_if_changed) -- reusing it
        skips redundantly recomputing the same values again after the
        scan completes."""
        # Check if operation is supported by the API
        if not self.mediainfo.get('can_play'):
            raise utils.EngineError(
                'Operation not supported by current site or mediatype.')
        show_folders = self.data_handler.show_folders_get()
        if not path and not self.config['searchdir'] and not show_folders:
            raise utils.EngineError('Media directories not set.')

        t = time.time()
        library = {}
        library_cache = self.data_handler.library_cache_get()

        if not my_status:
            my_status = self._scan_status_scope()

        if rescan:
            self.msg.info("Scanning local library (overriding cache)...")
        else:
            self.msg.info("Scanning local library...")

        tracker_list = self._get_tracker_list(my_status)
        guess_show = lru_cache(partial(utils.guess_show, tracker_list=tracker_list))

        if path:
            # An explicit single directory (CLI's --scan-library-dir):
            # unchanged behaviour, still guessed by title.
            scan_targets = [(path, None)]
        else:
            scan_targets = [(d, None) for d in self.searchdirs]
            # Manually pinned show folders (set_show_folder) are swept on
            # every full scan too, not just when first assigned -- they
            # may live outside searchdir entirely, and a show removed
            # from the list since is simply skipped.
            for show_id, folder in show_folders.items():
                try:
                    forced_show = self.get_show_info(show_id)
                except utils.EngineError:
                    continue
                scan_targets.append((folder, forced_show))

        for searchdir, forced_show in scan_targets:
            self.msg.debug("Directory: %s" % searchdir)

            # Do a full listing of the media directory
            for fullpath, filename in utils.regex_find_videos(searchdir):
                if self.config['library_full_path']:
                    filename = self._get_relative_path_or_basename(searchdir, fullpath)
                (library, library_cache) = self._add_show_to_library(
                    library, library_cache, rescan, fullpath, filename, tracker_list,
                    guess_show, forced_show=forced_show)

            self.msg.debug(f"Time: {time.time() - t:.3}s")

        # library/library_cache accumulate across every scan target, so
        # only the final save reflects anything the earlier ones don't
        # -- saving after each target (once per searchdir/pinned-folder)
        # just re-pickles a growing superset of the same data.
        self.data_handler.library_save(library)
        self.data_handler.library_cache_save(library_cache)

        if path is None:
            # Only a full scan (all searchdirs + pinned show folders)
            # produces a signature that's safe to cache -- a single-
            # directory scan wouldn't reflect the true state of the
            # others. Must match _scan_library_if_changed's own inputs.
            if signature is None:
                signature = (
                    self._library_dirs_signature(
                        self.searchdirs + list(show_folders.values())),
                    self._tracker_list_signature(tracker_list),
                )
            self.data_handler.library_scan_signature_save(signature)

        return library

    def set_show_folder(self, show_id, path):
        """Manually pin a show to a specific local folder: every video
        file under it is attributed directly to this show (only the
        episode number is still parsed from the filename), bypassing
        title-based guessing entirely -- the escape hatch for a folder
        whose name the parser/guesser can't (or shouldn't have to)
        make sense of. Scanned immediately so it takes effect now, and
        swept again on every future full library scan (scan_library)."""
        if not self.mediainfo.get('can_play'):
            raise utils.EngineError(
                'Operation not supported by current site or mediatype.')
        show = self.get_show_info(show_id)
        self.data_handler.show_folder_set(show_id, path)

        library = self.data_handler.library_get()
        library_cache = self.data_handler.library_cache_get()
        tracker_list = self._get_tracker_list()
        for fullpath, filename in utils.regex_find_videos(path):
            if self.config['library_full_path']:
                filename = self._get_relative_path_or_basename(path, fullpath)
            (library, library_cache) = self._add_show_to_library(
                library, library_cache, True, fullpath, filename, tracker_list,
                None, forced_show=show)
        self.data_handler.library_save(library)
        self.data_handler.library_cache_save(library_cache)

    def unset_show_folder(self, show_id):
        """Un-pin a show's manually assigned folder. Already-cached
        library entries from it are left alone (harmless either way)
        until the next full library scan re-derives them normally."""
        self.data_handler.show_folder_clear(show_id)

    def get_show_folder(self, show_id):
        return self.data_handler.show_folder_get(show_id)

    def show_folder_count(self):
        """Number of shows manually pinned to a local folder -- used by
        the Statistics page's Hakubun section."""
        return len(self.data_handler.show_folders_get())

    def library_file_count(self):
        """Total tracked episode files across the whole local library
        -- used by the Statistics page's Hakubun section."""
        return sum(len(episodes) for episodes in self.data_handler.library_get().values())

    def remove_from_library(self, path, filename):
        library = self.data_handler.library_get()
        library_cache = self.data_handler.library_cache_get()
        tracker_list = self._get_tracker_list()
        fullpath = path+"/"+filename
        # Only remove if the filename matches library entry
        if filename in library_cache and library_cache[filename]:
            (show_id, show_ep) = library_cache[filename]
            if show_id and show_ep and show_id \
                    and library.get(show_id, {}).get(show_ep) == fullpath:
                self.msg.debug("File removed from local library: %s" % fullpath)
                library_cache.pop(filename, None)
                library[show_id].pop(show_ep, None)

    def add_to_library(self, path, filename, rescan=False):
        # The inotify tracker tells us when files are created in
        # or moved within our library directory, so we call this.
        library = self.data_handler.library_get()
        library_cache = self.data_handler.library_cache_get()
        tracker_list = self._get_tracker_list()
        fullpath = path+"/"+filename
        guess_show = partial(utils.guess_show, tracker_list=tracker_list)
        # A newly-created file inside a manually pinned show folder
        # (set_show_folder) is attributed straight to that show, same
        # as a full scan would -- the whole point of pinning a folder
        # is to never depend on the guesser for it.
        forced_show = None
        for show_id, folder in self.data_handler.show_folders_get().items():
            if path == folder or path.startswith(folder.rstrip('/') + '/'):
                try:
                    forced_show = self.get_show_info(show_id)
                except utils.EngineError:
                    pass
                break
        self._add_show_to_library(
            library, library_cache, rescan, fullpath, filename, tracker_list, guess_show,
            forced_show=forced_show)

    def _add_show_to_library(self, library, library_cache, rescan, fullpath, filename,
                             tracker_list, guess_show, forced_show=None):
        show_id = None
        if not rescan and filename in library_cache:
            # If the filename was already seen before
            # use the cached information, if there's no information (None)
            # then it means it doesn't correspond to any show in the list
            # and can be safely skipped.
            if library_cache[filename]:
                (show_id, show_ep) = library_cache[filename]
                if type(show_ep) is tuple:
                    (show_ep_start, show_ep_end) = show_ep
                else:
                    show_ep_start = show_ep_end = show_ep
            else:
                return library, library_cache
        else:
            # If the filename has not been seen, extract
            # the information from the filename and do a fuzzy search
            # on the user's list. Cache the information.
            # If it fails, cache it as None.
            anime_info = self.parser_class(self.msg, filename)
            show_title = anime_info.getName()
            (show_ep_start, show_ep_end) = anime_info.getEpisodeNumbers(True)
            # A manually pinned folder (forced_show) already tells us
            # the show -- skip guessing by title entirely, since that's
            # precisely the step a pinned folder exists to bypass.
            show = forced_show
            if show is None and show_title:
                show = guess_show(show_title)
            if show:
                self.msg.debug("Adding to library: {}".format(fullpath))
                self.msg.debug("Show guess: {}".format(show_title))

                if show_ep_start == show_ep_end:
                    # TODO : Support redirections for episode ranges
                    (show, show_ep) = utils.redirect_show(
                        (show, show_ep_start), self.redirections, tracker_list)
                    show_ep_end = show_ep_start = show_ep

                    self.msg.debug("Redirected to: {} - {}".format(
                        show['title'], show_ep))
                    library_cache[filename] = (show['id'], show_ep)
                else:
                    library_cache[filename] = (
                        show['id'], (show_ep_start, show_ep_end))

                show_id = show['id']
            else:
                self.msg.debug("Unable to match '{}', skipping: {}"
                               .format(show_title, fullpath))
                library_cache[filename] = None

        # After we got our information, add it to our library
        if show_id:
            if show_id not in library:
                library[show_id] = {}
            for show_ep in range(show_ep_start, show_ep_end+1):
                library[show_id][show_ep] = fullpath

        return library, library_cache

    def get_episode_path(self, show, episode=0):
        """
        This function returns the full path of the requested episode from the requested show.
        If the episode is unspecified, it will return any episode from the requested show.
        """
        library = self.library()
        showid = show['id']

        if showid not in library:
            raise utils.EngineError('Show not in library.')

        # Get the specified episode, otherwise get any if unspecified
        if episode:
            if episode not in library[showid]:
                raise utils.EngineError('Episode not in library.')
            return library[showid][episode]
        else:
            return next(iter(library[showid].values()))

    def play_random(self):
        """
        This function will pick a random show that has a new episode to watch
        and return the arguments to play it.
        """
        library = self.library()
        newep = []

        self.msg.info('Looking for random episode.')

        for showid, eps in library.items():
            try:
                show = self.get_show_info(showid)
            except utils.EngineError:
                continue # In library but not available
            if show['my_progress'] + 1 in eps:
                newep.append(show)

        if not newep:
            raise utils.EngineError('No new episodes found to pick from.')

        show = random.choice(newep)
        return self.play_episode(show)

    def play_episode(self, show, playep=0):
        """
        Does a local search in the hard disk (in the folder specified by the config file)
        for the specified episode (**playep**) for the specified **show**.

        If no **playep** is specified, the next episode of the show will be returned.
        """
        # Check if operation is supported by the API
        if not self.mediainfo.get('can_play'):
            raise utils.EngineError(
                'Operation not supported by current site or mediatype.')

        try:
            playep = int(playep)
        except ValueError:
            raise utils.EngineError('Episode must be numeric.')

        if not show:
            raise utils.EngineError('Show given is invalid')

        if playep <= 0:
            playep = show['my_progress'] + 1

        if show['total'] and playep > show['total']:
            raise utils.EngineError('Episode beyond limits.')

        self.msg.info("Getting '%s' episode '%s' from library..." %
                        (show['title'], playep))

        try:
            filename = self.get_episode_path(show, playep)
        except utils.EngineError:
            self.msg.info("Episode not found. Calling hooks...")
            self._emit_signal("episode_missing", show, playep)
            return []

        self.msg.info('Found. Starting player...')

        if self.config.get('use_subminer'):
            subminer_bin = shutil.which('subminer')
            if not subminer_bin:
                raise utils.EngineError(
                    'SubMiner not found. Install it or disable '
                    '"Open episodes with SubMiner" in settings.')

            if self.config['player_reuse_mpv_instance']:
                # SubMiner manages its own single mpv+overlay instance
                # under its own fixed socket (not our mpv_ipc_socket_path)
                # -- ask it where that is and hand off the same way we do
                # for a plain mpv player below.
                subminer_socket = utils.subminer_mpv_socket_path(subminer_bin)
                if subminer_socket and utils.mpv_ipc_loadfile(filename, subminer_socket):
                    self.msg.info('Handed off to the running SubMiner instance.')
                    return []

            return [subminer_bin, filename]

        args = shlex.split(self.config['player'])

        if not args:
            raise utils.EngineError('Player not set up, check your config.json')

        args[0] = shutil.which(args[0])

        if not args[0]:
            raise utils.EngineError('Player not found, check your config.json')

        reuse_mpv = self.config['player_reuse_mpv_instance'] and utils.is_mpv_player(args[0])
        if reuse_mpv and utils.mpv_ipc_loadfile(filename):
            # Handed off to an already-running mpv instance -- an empty
            # arg list tells every caller (GTK/CLI/Qt) not to spawn a
            # new process, same as when no player is configured at all.
            self.msg.info('Handed off to the running mpv instance.')
            return []

        if reuse_mpv:
            # Nothing was listening on the socket, so this spawn becomes
            # the new reusable instance -- future calls will find it here.
            utils.make_dir(utils.to_cache_path())
            args.append('--input-ipc-server=' + utils.mpv_ipc_socket_path())

        args.append(filename)
        return args

    def open_show_folder(self, show_id):
        show = self.get_show_info(show_id)
        filename = self.get_episode_path(show)
        try:
            utils.open_folder(os.path.dirname(filename))
        except OSError:
            raise utils.EngineError("Could not open folder.")

    def queue_clear(self):
        """Clears the data handler queue and discards any unsynced change."""
        return self.data_handler.queue_clear()

    def altname(self, showid, newname=None):
        """
        If **newname** is specified, it gets the alternate name of **showid**.
        Otherwise, it sets the alternate name of **showid** to **newname**.
        """
        if newname is not None:
            if newname == '':
                self.data_handler.altname_clear(showid)
                self.msg.info('Cleared alternate name.')
            else:
                self.data_handler.altname_set(showid, newname)
                self.msg.info('Changed alternate name to %s.' % newname)
            # Update the tracker with the new altname
            self._update_tracker()
        else:
            return self.data_handler.altname_get(showid)

    def altnames(self):
        """
        Gets a dictionary of all set alternative names.
        """
        return self.data_handler.altnames_get()

    def filter_list(self, status_num):
        """
        Returns a show list with the shows in the specified **status_num** status.
        If you need a list with all the shows, use :func:`get_list`.
        """
        showlist = self.data_handler.get()
        return list(v for k, v in showlist.items() if v['my_status'] == status_num)

    def list_download(self):
        """Asks the data handler to download the remote list."""
        self.data_handler.queue_clear()
        self.data_handler.download_data()
        self._update_tracker()
        # MAL score fetching is triggered by the data handler's
        # 'list_downloaded' signal instead of directly here, so it also
        # fires for downloads the data handler does on its own (e.g. the
        # auto-retrieve download on startup), not just this explicit path.

    def list_upload(self):
        """Asks the data handler to upload the unsynced changes in the queue."""
        self.data_handler.process_queue()
        # for show in result:
        #    self._emit_signal('episode_changed', show)

    def get_queue(self):
        """Asks the data handler for the items in the current queue."""
        return self.data_handler.queue

    def _auto_fetch_mal_scores(self):
        """Runs fetch_mal_scores() after a sync if the user has opted
        into it (Settings -> Interface -> "Add MAL Scores"), swallowing
        the "not applicable to this account" errors that are meant to be
        shown directly to the user when they trigger this manually."""
        if not self.get_config('auto_add_mal_scores'):
            return
        try:
            self.fetch_mal_scores()
        except utils.EngineError as e:
            self.msg.debug('Skipping automatic MAL score fetch: %s' % e)

    @staticmethod
    def _find_mal_account():
        """Returns the first MAL account configured in Hakubun+, or None."""
        return next(
            (acc for _, acc in accounts.AccountManager().get_accounts()
             if acc['api'] == 'mal'),
            None)

    def fetch_mal_scores(self):
        """
        Starts a background job to cross-reference MyAnimeList's
        community score for the current list, using a MAL account
        already configured in Hakubun+. Runs either automatically after a
        sync (when the "Add MAL Scores" setting is enabled -- see
        _auto_fetch_mal_scores) or on demand. Uses a single bulk request
        for the user's whole MAL library (see libmal.fetch_list) instead
        of one request per show, which is drastically faster for a large
        list; only shows missing from the user's own MAL list fall back
        to individual lookups.

        Raises EngineError (safe to show directly to the user) if this
        account doesn't need MAL cross-referencing, or if no MAL account
        is configured.
        """
        if self.api_info.get('shortname') == 'mal':
            raise utils.EngineError(
                "This account is already MyAnimeList -- its own score "
                "is already shown as Platform Score.")
        if not self.mediainfo.get('can_reference_mal'):
            raise utils.EngineError(
                'MAL score cross-referencing is not supported for this API.')
        if not self._find_mal_account():
            raise utils.EngineError(
                'No MyAnimeList account is configured in Hakubun+. Add '
                'one first (Switch Account), then try again.')

        shows = [s for s in self.data_handler.get().values() if s.get('mal_id')]
        if not shows:
            raise utils.EngineError('No shows with a known MAL id to fetch scores for.')

        # A re-sync while this is still running replaces
        # data_handler.showlist wholesale with a fresh dict from the API,
        # orphaning whatever show objects this batch is still holding --
        # mutating those doesn't help since they're no longer part of
        # what gets saved. Bumping this and having the task bail out the
        # moment it sees a newer one means an old batch cancels itself
        # within one loop iteration instead of grinding away on discarded
        # data.
        self._mal_fetch_epoch += 1
        epoch = self._mal_fetch_epoch

        self.msg.debug('Fetching MAL scores for %d shows...' % len(shows))
        threading.Thread(
            target=self._fetch_mal_scores_task, args=(shows, epoch),
            daemon=True).start()

    def _get_mal_auth_lib(self):
        """
        Returns an authenticated libmal instance reusing a MAL account
        already configured in Hakubun+. Returns None if authentication
        fails for any reason (e.g. a revoked authorization) -- the caller
        is expected to have already confirmed a MAL account exists (see
        fetch_mal_scores), so this is just about the auth itself, not
        finding the account.
        """
        mal_account = self._find_mal_account()
        if not mal_account:
            return None

        try:
            mal_data = data.Data(
                self.msg, self.config, mal_account, self.api_info['mediatype'])
            mal_data.api.check_credentials()
            return mal_data.api
        except (utils.HakubunError, KeyError) as e:
            self.msg.debug('Could not authenticate with the existing MAL account: %s' % e)
            return None

    def _fetch_mal_scores_task(self, shows, epoch):
        mal_lib = self._get_mal_auth_lib()
        if not mal_lib:
            self.msg.warn(
                "Couldn't authenticate with your MyAnimeList account. "
                "Try re-authorizing it (Switch Account).")
            return

        # Bulk path: one (or a few, paginated) request for the user's
        # whole MAL library covers the vast majority of shows at once,
        # instead of one request per show.
        try:
            mal_list = mal_lib.fetch_list()
        except utils.HakubunError as e:
            self.msg.warn("Couldn't fetch your MAL list: %s" % e)
            return

        if epoch != self._mal_fetch_epoch:
            self.msg.debug('MAL score fetch superseded by a newer request, stopping.')
            return

        bulk_scores = {mal_id: s['platform_score']
                       for mal_id, s in mal_list.items() if s.get('platform_score')}

        found = 0
        for show in shows:
            score = bulk_scores.get(show['mal_id'])
            if score:
                show['mal_score'] = score
                self._emit_signal('mal_score_changed', show)
                found += 1

        self.data_handler.save_cache()
        self.msg.debug(
            'Found %d of %d scores via your MAL list.' % (found, len(shows)))

        # Whatever's left isn't on the user's own MAL list (so the bulk
        # fetch above couldn't cover it) -- fall back to individual
        # lookups for those.
        remaining = [s for s in shows if not s.get('mal_score')]
        if not remaining:
            self.msg.debug('Finished fetching MAL scores.')
            return

        self.msg.debug(
            '%d shows not on your MAL list, looking them up individually...'
            % len(remaining))
        consecutive_failures = 0
        unsaved_scores = 0
        for show in remaining:
            if epoch != self._mal_fetch_epoch:
                self.msg.debug(
                    'MAL score fetch superseded by a newer request, stopping.')
                if unsaved_scores:
                    # Persist what this batch already fetched -- the
                    # scores are applied to the show dicts and emitted
                    # to the UI, and the superseding task may fail
                    # before reaching its own save.
                    self.data_handler.save_cache()
                return

            try:
                score = self._fetch_mal_score(show['mal_id'], mal_lib)
                consecutive_failures = 0
            except Exception as e:
                # Deliberately broad: a single show's lookup failing for
                # any reason (timeout, bad response, whatever) must never
                # take down the whole background batch -- a bare
                # TimeoutError from a mid-read socket timeout isn't even
                # a URLError, so this used to silently kill the thread on
                # the very first hiccup.
                self.msg.debug('MAL score fetch failed for %s: %s' % (show['title'], e))
                score = None
                consecutive_failures += 1

            if score is not None:
                show['mal_score'] = '%.2f' % score
                self._emit_signal('mal_score_changed', show)
                unsaved_scores += 1
                # Flush periodically so an interrupted batch keeps most of
                # its work, without pickling the whole list once per show.
                if unsaved_scores >= 10:
                    self.data_handler.save_cache()
                    unsaved_scores = 0

            if consecutive_failures >= self.MAL_SCORE_CONSECUTIVE_FAILURE_LIMIT:
                self.msg.debug(
                    'Too many consecutive MAL score fetch failures, stopping for now.')
                break

        self.data_handler.save_cache()
        self.msg.debug('Finished fetching MAL scores.')

    def _fetch_mal_score(self, mal_id, mal_lib, attempts=3):
        """
        Looks up a single show's MAL community score via an authenticated
        request. Only used as a fallback for shows not covered by the
        bulk fetch_list() call in _fetch_mal_scores_task. Retries a
        couple of times with backoff.
        """
        for attempt in range(attempts):
            try:
                result = mal_lib._request(
                    'GET', '%s/%s/%d' % (mal_lib.query_url, mal_lib.mediatype, mal_id),
                    get={'fields': 'mean'}, auth=True)
                return result.get('mean')
            except Exception:
                if attempt == attempts - 1:
                    raise
                time.sleep(2 * (attempt + 1))

    def _get_relative_path_or_basename(self, searchdir, fullpath):
        """Determine the path relative to a directory or the basename if not a sub-path.

        Used for including the folder name(s) for show detection.
        """
        path = Path(fullpath)
        try:
            return str(path.relative_to(searchdir))
        except ValueError:
            return path.basename

    def _searchdir_exists(self, path):
        """Variation of dir_exists that warns the user if the path doesn't exist."""
        if not utils.dir_exists(path):
            self.msg.warn("The specified media directory {} doesn't exist!".format(path))
            return False
        return True

    def _guess_new_finish(self, show):
        try:
            # Use corresponding finish status if we're already in a start status
            new_index = self.mediainfo['statuses_start'].index(
                show['my_status'])
            new_status = self.mediainfo['statuses_finish'][new_index]
        except ValueError:
            new_status = self.mediainfo['statuses_finish'][0]
        except IndexError:
            new_status = self.mediainfo['statuses_finish'][-1]

        return new_status

    def _guess_new_start(self, show):
        try:
            # Use following start status if we're already in a finish status
            new_index = self.mediainfo['statuses_finish'].index(
                show['my_status'])
            new_status = self.mediainfo['statuses_start'][new_index+1]
        except ValueError:
            new_status = self.mediainfo['statuses_start'][0]
        except IndexError:
            new_status = self.mediainfo['statuses_start'][-1]

        return new_status

    def _get_tracker_class(self, ttype):
        # Choose the tracker we want to start
        if ttype == 'plex':
            from hakubun.tracker.plex import PlexTracker
            return PlexTracker
        if ttype == 'jellyfin':
            from hakubun.tracker.jellyfin import JellyfinTracker
            return JellyfinTracker
        elif ttype == 'kodi':
            from hakubun.tracker.kodi import KodiTracker
            return KodiTracker
        elif ttype == 'mpris':
            from hakubun.tracker.mpris import MprisTracker
            return MprisTracker
        elif ttype == 'inotify_auto':
            try:
                return self._get_tracker_class('pyinotify')
            except ImportError:
                return self._get_tracker_class('inotify')
        elif ttype == 'pyinotify':
            from hakubun.tracker.pyinotify import pyinotifyTracker
            return pyinotifyTracker
        elif ttype == 'inotify':
            from hakubun.tracker.inotify import inotifyTracker
            return inotifyTracker
        elif ttype == 'win32':
            from hakubun.tracker.win32 import Win32Tracker
            return Win32Tracker
        elif ttype == 'polling':
            from hakubun.tracker.polling import PollingTracker
            return PollingTracker
        else:
            # Guess the working tracker
            if os.name == 'nt':
                return self._get_tracker_class('win32')

            # Try trackers in this order: MPRIS, pyinotify, inotify, polling.
            # MPRIS first since it needs no configuration (no search
            # directories to set up) and works for any MPRIS-capable
            # player, not just ones playing from a watched directory.
            try:
                return self._get_tracker_class('mpris')
            except ImportError:
                pass

            try:
                return self._get_tracker_class('inotify_auto')
            except ImportError:
                return self._get_tracker_class('polling')
