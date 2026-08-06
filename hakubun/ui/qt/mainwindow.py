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

import base64
import os
import urllib.parse

from PyQt6 import QtCore, QtGui
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                             QDateEdit, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                             QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
                             QPushButton, QSizePolicy, QSpinBox, QStackedWidget, QStyle,
                             QStyleOptionButton, QSystemTrayIcon, QTabBar, QToolButton, QVBoxLayout,
                             QWidget)

from hakubun import messenger
from hakubun import utils
from hakubun.accounts import AccountManager
from hakubun.sync import present
from hakubun.ui.qt.accounts import AccountDialog
from hakubun.ui.qt.add import AddDialog
from hakubun.ui.qt.airing import AiringScheduleDialog
from hakubun.ui.qt.delegates import ShowsTableDelegate
from hakubun.ui.qt.details import DetailsDialog
from hakubun.ui.qt.models import ShowListModel
from hakubun.ui.qt.nowplaying import NowPlayingWidget
from hakubun.ui.qt.search import SearchWidget
from hakubun.ui.qt.seasons import SeasonsWidget
from hakubun.ui.qt.settings import SettingsDialog
from hakubun.ui.qt.stats import StatisticsWidget
from hakubun.ui.qt.util import FilterBar, getIcon
from hakubun.ui.qt.widgets import HoverProgressBar, PlaybackBar, ScoreSlider, ShowsTableView
from hakubun.ui.qt.workers import EngineWorker, ImageWorker

# Not a real version bump -- just a build identifier for this working
# branch, so it's obvious at a glance (in About) whether you're running
# this in-progress Taiga-mode revamp or an actual release build.
DEV_BUILD_ID = 'retaiga'


class MainWindow(QMainWindow):
    """
    Main GUI class

    """
    debug = False
    config = None
    tray = None
    mediainfo = None
    accountman = None
    accountman_widget = None
    worker = None
    image_worker = None
    started = False
    selected_show_id = None
    show_lists = None
    finish = False
    was_maximized = False

    # Taiga mode's row context menu "Search" submenu (res/menu.xml,
    # "Search" -- ported minus the two torrent-search entries). {title}
    # is URL-quoted before formatting, see s_search_online().
    _SEARCH_ONLINE_SITES = (
        ('AniDB', 'https://anidb.net/perl-bin/animedb.pl?show=animelist&adb.search={title}'
                  '&noalias=1&do.update=update'),
        ('AniList', 'https://anilist.co/search/anime?sort=SEARCH_MATCH&search={title}'),
        ('Anime News Network', 'https://www.animenewsnetwork.com/search?q={title}'),
        ('Kitsu', 'https://kitsu.io/anime?text={title}'),
        ('MyAnimeList', 'https://myanimelist.net/anime.php?q={title}'),
        ('Reddit', 'https://www.reddit.com/search?sort=new&q=subreddit%3Aanime+title%3A'
                   '{title}+episode+discussion'),
        ('Wikipedia', 'https://en.wikipedia.org/wiki/Special:Search?search={title}'),
        ('YouTube', 'https://www.youtube.com/results?search_query={title}'),
    )

    # Multi-sync owner-score editing state. When the selected show's
    # Score is owned by another provider (and the entry is shared), the
    # bottom-bar score slider adopts that owner's rating system and its
    # Set writes to multisync local instead of the signed-in account.
    _score_owner_mode = None            # owning provider, or None
    _score_editor_provider = None       # provider the slider is set for
    _score_owner_mediainfo = {}         # {provider: mediainfo}
    _multisync_media_type = None

    def __init__(self, debug=False, force_taiga=False):
        QMainWindow.__init__(self, None)
        self.debug = debug
        # r_engine_loaded fires on every account/mediatype reload, not
        # just the initial start -- only show the parser-fallback dialog
        # once per session, not on every switch.
        self._parser_fallback_shown = False

        # Load QT specific configuration
        self.configfile = utils.to_config_path('ui-qt.json')
        self.config = utils.parse_config(self.configfile, utils.qt_defaults)
        if force_taiga:
            # --taiga: this run only, not written back to disk.
            self.config['taiga_mode'] = True
        # Snapshotted once here rather than read live from self.config
        # everywhere below: the settings dialog mutates self.config
        # in place immediately on Apply/OK (before any restart happens,
        # since taiga_mode is restart-gated), which would otherwise make
        # post-startup code believe it's in taiga mode -- and start
        # touching taiga-only widgets that were never built because the
        # window itself was constructed for the other mode.
        self._taiga_mode = self.config['taiga_mode']

        # Build UI
        self.app_name = 'Taiga-qt' if self._taiga_mode else 'Hakubun+-qt'
        if self._taiga_mode:
            QApplication.setWindowIcon(
                QtGui.QIcon(utils.DATADIR + '/taiga_icon.png'))
        elif os.name != "nt":
            QApplication.setWindowIcon(QtGui.QIcon(utils.DATADIR + '/icon.png'))
        else:
            QApplication.setWindowIcon(QtGui.QIcon(utils.DATADIR + '/icon.ico'))
        self.setWindowTitle(self.app_name)

        self.accountman = AccountManager()

        # Go directly into the application if a default account is set
        # Open the selection dialog otherwise
        default = self.accountman.get_default()
        if default:
            self.start(default)
        else:
            self.accountman_create()
            self.accountman_widget.show()

    def accountman_create(self):
        self.accountman_widget = AccountDialog(None, self.accountman)
        self.accountman_widget.selected.connect(self.accountman_selected)

    def accountman_selected(self, account_num, remember):
        account = self.accountman.get_account(account_num)

        if remember:
            self.accountman.set_default(account_num)
        else:
            self.accountman.set_default(None)

        if self.started:
            self.reload(account)
        else:
            self.start(account)

    def start(self, account):
        """
        Start engine and everything

        """
        # Workers
        self.worker = EngineWorker()
        self.account = account

        # Get API specific configuration
        self.api_config = self._get_api_config(account['api'])

        # Timers
        self.image_timer = QtCore.QTimer()
        self.image_timer.setInterval(500)
        self.image_timer.setSingleShot(True)
        self.image_timer.timeout.connect(self.s_download_image)

        self.busy_timer = QtCore.QTimer()
        self.busy_timer.setInterval(100)
        self.busy_timer.setSingleShot(True)
        self.busy_timer.timeout.connect(self.s_busy)

        # Build menus
        self.action_play_next = QAction(
            getIcon('media-playback-start'), 'Play &Next', self)
        self.action_play_next.setStatusTip('Play the next unwatched episode.')
        self.action_play_next.setShortcut('Ctrl+N')
        self.action_play_next.triggered.connect(lambda: self.s_play(True))
        self.action_play_dialog = QAction('Play Episode...', self)
        self.action_play_dialog.setStatusTip('Select an episode to play.')
        self.action_play_dialog.triggered.connect(self.s_play_number)
        # Text shows the current state ('SubMiner: On'/'Off') rather than
        # a static label -- see _apply_subminer_state, which keeps it in
        # sync with the 'use_subminer' config value. One QAction shared
        # between the toolbar (normal mode) and the Tools menu (Taiga
        # mode, see menu_tools below) instead of a separate control per
        # look.
        self.action_use_subminer = QAction('SubMiner: Off', self)
        self.action_use_subminer.setCheckable(True)
        if utils.subminer_available():
            self.action_use_subminer.setStatusTip(
                'Open episodes with SubMiner instead of the configured '
                'player.')
            self.action_use_subminer.setToolTip(
                'Open episodes with SubMiner instead of the configured '
                'player.')
        else:
            self.action_use_subminer.setEnabled(False)
            self.action_use_subminer.setToolTip(
                'SubMiner was not found on PATH. Install it to enable this.')
        self.action_use_subminer.toggled.connect(self.s_toggle_subminer)
        # Visibility (Settings > User Interface) doesn't depend on the
        # engine being loaded, unlike the checked state/text -- apply it
        # here rather than waiting for r_engine_loaded.
        self.action_use_subminer.setVisible(
            self.config['show_subminer_toggle'])
        self.action_details = QAction('Show &details...', self)
        self.action_details.setStatusTip(
            'Show detailed information about the selected show.')
        self.action_details.triggered.connect(self.s_show_details)
        self.action_altname = QAction('Change &alternate name...', self)
        self.action_altname.setStatusTip(
            'Set an alternate title for the tracker.')
        self.action_altname.triggered.connect(self.s_altname)
        action_play_random = QAction('Play &random show', self)
        action_play_random.setStatusTip(
            'Pick a random show with a new episode and play it.')
        action_play_random.setShortcut('Ctrl+R')
        action_play_random.triggered.connect(self.s_play_random)
        self.action_add = QAction(
            getIcon('edit-find'), 'Search', self)
        self.action_add.setShortcut('Ctrl+A')
        self.action_add.triggered.connect(self.s_add)
        self.action_airing_schedule = QAction(
            getIcon('view-calendar'), 'Airing Schedule', self)
        self.action_airing_schedule.setStatusTip(
            'See when the airing shows in your list air next.')
        self.action_airing_schedule.triggered.connect(self.s_airing_schedule)
        self.action_multisync = QAction('&Multi-provider Sync...', self)
        self.action_multisync.setStatusTip(
            'Reconcile your lists across every configured provider.')
        self.action_multisync.triggered.connect(self.s_multisync)
        self.action_delete = QAction(getIcon('edit-delete'), '&Delete', self)
        self.action_delete.setStatusTip('Remove this show from your list.')
        self.action_delete.setShortcut(QtCore.Qt.Key.Key_Delete)
        self.action_delete.triggered.connect(self.s_delete)
        action_quit = QAction(getIcon('application-exit'), '&Quit', self)
        action_quit.setShortcut('Ctrl+Q')
        action_quit.setStatusTip('Exit Hakubun+.')
        action_quit.triggered.connect(self._exit)

        self.action_undo = QAction(getIcon('edit-undo'), '&Undo', self)
        self.action_undo.setStatusTip('Undo the last episode/score/status/tags change.')
        self.action_undo.setShortcut('Ctrl+Z')
        self.action_undo.setEnabled(False)
        self.action_undo.triggered.connect(self.s_undo)
        self.action_redo = QAction(getIcon('edit-redo'), '&Redo', self)
        self.action_redo.setStatusTip('Redo the last undone change.')
        self.action_redo.setShortcuts(['Ctrl+Shift+Z', 'Ctrl+Y'])
        self.action_redo.setEnabled(False)
        self.action_redo.triggered.connect(self.s_redo)

        self.action_sync = QAction(getIcon('view-refresh'), '&Sync', self)
        self.action_sync.setShortcut('Ctrl+S')
        self.action_sync.triggered.connect(self.s_sync_button)
        self._apply_sync_action_label()
        self.action_send = QAction('S&end changes', self)
        self.action_send.setShortcut('Ctrl+E')
        self.action_send.setStatusTip(
            'Upload any changes made to the list immediately.')
        self.action_send.triggered.connect(self.s_send)
        self.action_retrieve = QAction('Re&download list', self)
        self.action_retrieve.setShortcut('Ctrl+D')
        self.action_retrieve.setStatusTip(
            'Discard any changes made to the list and re-download it.')
        self.action_retrieve.triggered.connect(self.s_retrieve)
        action_scan_library = self.action_scan_library = QAction(
            'Rescan &Library (quick)', self)
        action_scan_library.setShortcut('Ctrl+L')
        action_scan_library.triggered.connect(self.s_scan_library)
        action_rescan_library = self.action_rescan_library = QAction(
            'Rescan &Library (full)', self)
        action_rescan_library.triggered.connect(self.s_rescan_library)
        action_open_folder = QAction('Open containing folder', self)
        action_open_folder.triggered.connect(self.s_open_folder)
        self.action_set_folder = QAction('Set folder...', self)
        self.action_set_folder.setStatusTip(
            'Manually point this show at a local folder, bypassing '
            'filename guessing -- for folders the parser can\'t match.')
        self.action_set_folder.triggered.connect(self.s_set_folder)
        self.action_clear_folder = QAction('Clear folder', self)
        self.action_clear_folder.triggered.connect(self.s_clear_folder)

        self.action_reload = QAction('Switch &Account', self)
        self.action_reload.setStatusTip('Switch to a different account.')
        self.action_reload.triggered.connect(self.s_switch_account)
        action_settings = QAction(getIcon('preferences-system'), '&Settings...', self)
        action_settings.triggered.connect(self.s_settings)

        action_about = QAction(getIcon('help-about'), 'About...', self)
        action_about.triggered.connect(self.s_about)
        action_about_qt = QAction('About Qt...', self)
        action_about_qt.triggered.connect(self.s_about_qt)

        menubar = self.menuBar()

        if self._taiga_mode:
            # Taiga's own menu bar/toolbar are much simpler than
            # Hakubun+'s -- File/Services/Tools/Help instead of
            # Show/List/Mediatype/Options/Help. Actions that don't have
            # a natural home in that structure (undo/redo, rescan,
            # mediatype switch) stay reachable via File > Library folders
            # or their existing shortcuts rather than disappearing
            # outright.
            self.menu_library_folders = QMenu('Library folders', self)
            self.menu_mediatype = QMenu('Mediatype', self)
            self.mediatype_actiongroup = QActionGroup(self)
            self.mediatype_actiongroup.setExclusive(True)
            self.mediatype_actiongroup.triggered.connect(self.s_mediatype)

            menu_file = menubar.addMenu('&File')
            menu_file.addMenu(self.menu_library_folders)
            menu_file.addMenu(self.menu_mediatype)
            menu_file.addAction(action_play_random)
            menu_file.addSeparator()
            menu_file.addAction(action_quit)

            self.menu_services = menubar.addMenu('&Services')
            self.menu_services.addAction(self.action_sync)
            # Switch Account lives here rather than File -- it's account
            # management, same as everything else in this menu.
            self.menu_services.addAction(self.action_reload)
            self.menu_services.addSeparator()
            # The per-service section (profile/stats/history links) is
            # built in _rebuild_services_menu() once the active
            # account's API and username are known.

            menu_tools = menubar.addMenu('&Tools')
            menu_tools.addAction(self.action_altname)
            menu_tools.addAction(self.action_add)
            menu_tools.addAction(self.action_airing_schedule)
            menu_tools.addAction(self.action_multisync)
            menu_tools.addSeparator()
            menu_tools.addAction(self.action_use_subminer)

            menu_help = menubar.addMenu('&Help')
            menu_help.addAction(action_about)
            menu_help.addAction(action_about_qt)

            # Keep these working by shortcut even with no menu entry.
            for action in (self.action_undo, self.action_redo,
                          action_scan_library, action_rescan_library):
                self.addAction(action)

            self.taiga_toolbar = toolbar = self.addToolBar('Main')
            toolbar.setMovable(False)
            toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            toolbar.addAction(self.action_sync)
            self.library_folders_btn = QToolButton()
            self.library_folders_btn.setText('Library folders')
            self.library_folders_btn.setMenu(self.menu_library_folders)
            self.library_folders_btn.setPopupMode(
                QToolButton.ToolButtonPopupMode.InstantPopup)
            toolbar.addWidget(self.library_folders_btn)
            toolbar.addAction(action_settings)
        else:
            self.menu_show = menubar.addMenu('&Show')
            self.menu_show.addAction(self.action_play_next)
            self.menu_show.addAction(self.action_play_dialog)
            self.menu_show.addAction(self.action_details)
            self.menu_show.addAction(self.action_altname)
            self.menu_show.addSeparator()
            self.menu_show.addAction(action_play_random)
            self.menu_show.addSeparator()
            self.menu_show.addAction(self.action_add)
            self.menu_show.addAction(self.action_airing_schedule)
            self.menu_show.addAction(self.action_delete)
            self.menu_show.addSeparator()
            self.menu_show.addAction(action_quit)

            # Search, Sync, and Settings are common enough to need more
            # than a buried menu item.
            toolbar = self.addToolBar('Main')
            toolbar.setMovable(False)
            toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            toolbar.addAction(self.action_add)
            toolbar.addAction(self.action_airing_schedule)
            toolbar.addAction(self.action_sync)
            toolbar.addSeparator()
            toolbar.addAction(self.action_undo)
            toolbar.addAction(self.action_redo)
            toolbar.addSeparator()
            toolbar.addAction(action_settings)
            toolbar.addAction(self.action_use_subminer)

        self.menu_play = QMenu('Play')
        # Populated per-account in _rebuild_statuses(), once the API's
        # statuses are known.
        self.menu_move_to = QMenu('Move to', self)

        # Context menu for right click on list item
        self.menu_show_context = QMenu()
        self.menu_show_context.addMenu(self.menu_play)
        self.menu_show_context.addAction(self.action_details)
        if self._taiga_mode:
            # Real Taiga's row context menu (res/menu.xml, "RightClick")
            # has a "Search" submenu of external sites here, using the
            # show's title -- ported the non-torrent entries (skips
            # "Custom RSS feed"/"Nyaa.si", out of scope). Pure URL-
            # opening, no engine dependency.
            self.menu_search_online = QMenu('Search', self)
            for site_name, url_template in self._SEARCH_ONLINE_SITES:
                action = self.menu_search_online.addAction(site_name)
                action.triggered.connect(
                    lambda checked=False, u=url_template: self.s_search_online(u))
            self.menu_show_context.addMenu(self.menu_search_online)

            # Real Taiga's Edit submenu has a quick-pick "Set score"
            # radio list (res/menu.xml, "EditScore") -- fixed 0-10
            # there since Taiga is MAL/Kitsu-centric, but hakubun also
            # has AniList's 100-point scale, so this is populated (and
            # re-populated on reload) in _rebuild_statuses() once
            # mediainfo['score_max'/'score_step'] are known, rather
            # than hardcoded here.
            self.menu_quick_score = QMenu('Set score', self)
            self.menu_show_context.addMenu(self.menu_quick_score)

            # Real Taiga's Edit submenu also has "Set date started"/"Set
            # date completed" quick submenus (res/menu.xml,
            # "EditDateStarted"/"EditDateCompleted") offering shortcuts
            # like "Set to date started airing" -- skips "Clear" (the
            # engine's set_dates() has no clear capability, same
            # limitation already noted on the Details tab's date
            # checkboxes). Content is per-show, so it's rebuilt just
            # before the menu is shown rather than once here.
            self.menu_quick_date_started = QMenu('Set date started', self)
            self.action_quick_date_started = self.menu_show_context.addMenu(
                self.menu_quick_date_started)
            self.menu_quick_date_completed = QMenu('Set date completed', self)
            self.action_quick_date_completed = self.menu_show_context.addMenu(
                self.menu_quick_date_completed)
            self.menu_show_context.aboutToShow.connect(self._rebuild_quick_date_menus)
        self.menu_show_context.addMenu(self.menu_move_to)
        self.menu_show_context.addAction(action_open_folder)
        self.menu_show_context.addAction(self.action_set_folder)
        self.menu_show_context.addAction(self.action_clear_folder)
        self.menu_show_context.addAction(self.action_altname)
        self.menu_show_context.addSeparator()
        self.menu_show_context.addAction(self.action_delete)
        self.menu_show_context.aboutToShow.connect(self._update_folder_actions)

        # Make icons for viewed episodes
        rect = QtCore.QSize(16, 16)
        buffer = QtGui.QPixmap(rect)
        ep_icon_states = {'all': QStyle.StateFlag.State_On,
                          'part': QStyle.StateFlag.State_NoChange,
                          'none': QStyle.StateFlag.State_Off}
        self.ep_icons = {}
        for key, state in ep_icon_states.items():
            buffer.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(buffer)
            opt = QStyleOptionButton()
            opt.state = state
            self.style().drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorMenuCheckMark, opt, painter)
            self.ep_icons[key] = QtGui.QIcon(buffer)
            painter.end()

        if not self._taiga_mode:
            menu_list = menubar.addMenu('&List')
            menu_list.addAction(self.action_undo)
            menu_list.addAction(self.action_redo)
            menu_list.addSeparator()
            menu_list.addAction(self.action_sync)
            menu_list.addSeparator()
            menu_list.addAction(self.action_send)
            menu_list.addAction(self.action_retrieve)
            menu_list.addSeparator()
            # Menu-only on purpose: the toolbar stays reserved for the
            # everyday actions. (Taiga mode surfaces this under Tools.)
            menu_list.addAction(self.action_multisync)
            menu_list.addSeparator()
            menu_list.addAction(action_scan_library)
            menu_list.addAction(action_rescan_library)
            self.menu_mediatype = menubar.addMenu('&Mediatype')
            self.mediatype_actiongroup = QActionGroup(self)
            self.mediatype_actiongroup.setExclusive(True)
            self.mediatype_actiongroup.triggered.connect(self.s_mediatype)
            menu_options = menubar.addMenu('&Options')
            menu_options.addAction(self.action_reload)
            menu_options.addSeparator()
            menu_options.addAction(action_settings)
            menu_help = menubar.addMenu('&Help')
            menu_help.addAction(action_about)
            menu_help.addAction(action_about_qt)

        # Build layout
        main_layout = QVBoxLayout()
        top_hbox = QHBoxLayout()
        main_hbox = QHBoxLayout()
        self.list_box = QVBoxLayout()
        filter_bar_box_layout = QHBoxLayout()
        self.filter_bar_box = QWidget()
        left_box = QFormLayout()
        small_btns_hbox = QHBoxLayout()

        self.show_title = QLabel(self.app_name)
        show_title_font = QtGui.QFont()
        show_title_font.setBold(True)
        show_title_font.setPointSize(12)
        self.show_title.setFont(show_title_font)

        self.api_icon = QLabel('icon')
        self.api_user = QLabel('user')

        top_hbox.addWidget(self.show_title, 1)
        top_hbox.addWidget(self.api_icon)
        top_hbox.addWidget(self.api_user)

        # Create main models and view
        self.notebook = QTabBar()
        self.notebook.currentChanged.connect(self.s_tab_changed)

        self.view = ShowsTableView(palette=self.config['colors'])
        self.view.context_menu = self.menu_show_context
        self.view.horizontalHeader().customContextMenuRequested.connect(
            self.s_show_menu_columns)
        self.view.horizontalHeader().sortIndicatorChanged.connect(self.s_update_sort)
        self.view.selectionModel().currentRowChanged.connect(self.s_show_selected)
        if self._taiga_mode:
            # Real Taiga always shows the episode count (e.g. "7/12",
            # not a percentage) directly on its progress bars, with
            # markers for downloaded episodes -- not a user preference
            # there the way episodebar_style/episodebar_text are here.
            # BarStyle04 (the usual default) never draws text at all
            # regardless of show_text, so this needs BarStyleHybrid
            # specifically to get both the episode markers and text.
            self.view.itemDelegate().setBarStyle(
                ShowsTableDelegate.BarStyleHybrid, True, text_fraction=True)
            # Hover +/- episode buttons, ported from the win32 1.4
            # codebase's AnimeListDialog::ListView (dlg_anime_list.cpp)
            # -- the current Qt rewrite doesn't have these at all.
            self.view.itemDelegate().setShowButtons(True)
        else:
            self.view.itemDelegate().setBarStyle(
                self.config['episodebar_style'], self.config['episodebar_text'])
        self.view.middleClicked.connect(lambda: self.s_play(True))
        self.view.doubleClicked.connect(self.s_show_details)
        self._apply_view()

        self.view.model().sourceModel().progressChanged.connect(self.s_set_episode)
        self.view.model().sourceModel().scoreChanged.connect(self.s_set_score)

        # Context menu for right click on list header
        self.menu_columns = QMenu()
        self.column_keys = {'id': 0,
                            'title': 1,
                            'progress': 2,
                            'score': 3,
                            'percent': 4,
                            'next_ep': 5,
                            'date_start': 6,
                            'date_end': 7,
                            'my_start': 8,
                            'my_end': 9,
                            'tag': 10,
                            'my_last_update': 12,
                            'season': 13,
                            'type': 14,
                            'platform_score': 15,
                            'mal_score': 16}

        for i, column_name in enumerate(self.view.model().sourceModel().columns):
            if self._taiga_mode and i == ShowListModel.COL_MY_PROGRESS:
                # Hidden by default in Taiga mode (redundant with
                # Percent, itself renamed to "Progress" -- see
                # _init_view()) -- leaving this in the menu would offer
                # two same-named, easy-to-confuse "Progress" entries.
                continue

            # This menu is built before _init_view() renames Percent to
            # "Progress" for Taiga mode -- match that name here too, or
            # this entry would read "Percent" while the header (and the
            # column it toggles) actually says "Progress".
            label = 'Progress' if (self._taiga_mode and i == ShowListModel.COL_PERCENT) \
                else column_name
            action = QAction(label, self, checkable=True)
            action.setData(i)
            if column_name in self.api_config['visible_columns']:
                action.setChecked(True)

            action.triggered.connect(self.s_toggle_column)
            self.menu_columns.addAction(action)

        # Create filter list
        self.show_filter = QLineEdit()
        self.show_filter.setClearButtonEnabled(True)
        self.show_filter.textChanged.connect(self.s_filter_text_changed)
        # Filters the current list as you type, but Enter jumps straight
        # to a full remote search/add.
        self.show_filter.returnPressed.connect(
            lambda: self.s_add(self.show_filter.text().strip() or None))
        filter_tooltip = (
            "General Search: All fields (columns) of each show will be matched against the search term."
            "\nAdvanced Searching: A field can be specified by using its key followed by a colon"
            " e.g. 'title:My_Show date_start:2016'."
            "\n  Any field may be specified multiple times to match terms in any order e.g. 'tag:Battle+Shounen tag:Ecchi'. "
            "\n  + and _ are replaced with spaces when searching specific fields."
            "\n  If colon is used after something that is not a column key, it will treat it as a general term."
            "\n  ALL terms not attached to a field will be combined into a single general search term"
            "\n         - 'My date_end:2016 Show' will match shows that have 'My Show' in any field and 2016 in the End Date field."
            "\n  Available field keys are: "
        )
        colkeys = ', '.join(sorted(self.column_keys.keys()))
        self.show_filter.setToolTip(filter_tooltip + colkeys + '.')
        self.show_filter_invert = QCheckBox()
        self.show_filter_invert.stateChanged.connect(self.s_filter_invert_changed)
        self.show_filter_casesens = QCheckBox()
        self.show_filter_casesens.stateChanged.connect(self.s_filter_changed)

        if self.config['remember_geometry']:
            self.resize(self.config['last_width'], self.config['last_height'])
            self.move(self.config['last_x'], self.config['last_y'])
        else:
            self.resize(740, 480)

        spinbox_width = 75
        self.show_image = QLabel()
        self.show_image.setFixedHeight(149)
        self.show_image.setMinimumWidth(100)
        self.show_image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._set_default_poster()
        show_progress_label = QLabel('Progress:')
        self.show_progress = QSpinBox()
        self.show_progress.setMinimumWidth(spinbox_width)
        # Taiga mode overlays the +/- controls on the bar itself
        # (revealed on hover) instead of using separate buttons beside it.
        self.show_progress_bar = (
            HoverProgressBar() if self._taiga_mode else QProgressBar())
        self.show_progress_btn = QPushButton('Update')
        self.show_progress_btn.setToolTip(
            'Set number of episodes watched to the value entered above')
        self.show_progress_btn.clicked.connect(self.s_set_episode)
        self.show_play_btn = QToolButton()
        self.show_play_btn.setIcon(getIcon('media-playback-start'))
        self.show_play_btn.setToolTip(
            'Play the next unwatched episode\nHold to play other episodes')
        self.show_play_btn.clicked.connect(lambda: self.s_play(True))
        self.show_play_btn.setMenu(self.menu_play)
        self.show_play_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup)

        if self._taiga_mode:
            self.show_inc_btn = self.show_progress_bar.inc_btn
            self.show_dec_btn = self.show_progress_bar.dec_btn
            self.show_progress_bar.incremented.connect(self.s_plus_episode)
            self.show_progress_bar.decremented.connect(self.s_rem_episode)
        else:
            self.show_inc_btn = QToolButton()
            self.show_inc_btn.setIcon(getIcon('list-add'))
            self.show_inc_btn.clicked.connect(self.s_plus_episode)
            self.show_dec_btn = QToolButton()
            self.show_dec_btn.setIcon(getIcon('list-remove'))
            self.show_dec_btn.clicked.connect(self.s_rem_episode)

        self.show_inc_btn.setShortcut('Ctrl+Right')
        self.show_inc_btn.setToolTip('Increment number of episodes watched')
        self.show_dec_btn.setShortcut('Ctrl+Left')
        self.show_dec_btn.setToolTip('Decrement number of episodes watched')
        show_score_label = QLabel('Score:')
        self.show_score = ScoreSlider()
        self.show_score.setMinimumWidth(220)
        self.show_score_btn = QPushButton('Set')
        self.show_score_btn.setToolTip('Set score to the value entered above')
        self.show_score_btn.clicked.connect(self.s_set_score)
        self.show_score.add_extra_widget(self.show_score_btn)
        # Synced/Platform score-system switch (multisync): for a shared
        # entry whose Score is owned by another tracker, flip the slider
        # between the owner's synced rating system and this account's own
        # system. Hidden unless the selected entry is owned elsewhere and
        # the 'edit owned scores in owner's system' setting is on.
        self.show_score_system = QCheckBox('Synced score')
        self.show_score_system.setToolTip(
            'Checked: rate this cross-tracker entry in its owner\'s synced '
            'rating system (e.g. AniList\'s 8.4). Unchecked: rate it in '
            'this account\'s own system.')
        self.show_score_system.setChecked(True)
        self.show_score_system.setVisible(False)
        self.show_score_system.toggled.connect(
            lambda _checked: self._resync_score_editor())
        self.show_tags_btn = QPushButton('Edit Tags...')
        self.show_tags_btn.setToolTip(
            'Open a dialog to edit your tags for this show')
        self.show_tags_btn.clicked.connect(self.s_set_tags)
        self.show_status = QComboBox()
        self.show_status.setToolTip('Change your watching status of this show')
        self.show_status.currentIndexChanged.connect(self.s_set_status)

        if self._taiga_mode:
            # Never wired into any UI before Taiga mode's Edit tab --
            # the engine's set_dates() already supports this, it just
            # had nowhere to live. Unlike Rewatching/Times rewatched/
            # Notes (also on real Taiga's equivalent screen), there's no
            # engine support for those at all, so they're left out
            # rather than faked. Folder *is* now backed by the engine
            # (set_show_folder/unset_show_folder) -- see below.
            #
            # A checkbox gates each date, matching real Taiga's own
            # dialog -- QDateEdit has no "empty" state, so without this
            # an unset date (e.g. "Date completed" on a show still being
            # watched) would otherwise always show *some* date and look
            # like it was actually recorded. Unchecked = disabled/unset,
            # checked = editable and committed via set_dates(). Note the
            # engine only supports setting a date, not clearing one (see
            # set_dates()'s docstring), so unchecking never tries to
            # clear it server-side -- it just stops implying a date
            # exists locally.
            self.show_start_date_check = QCheckBox()
            self.show_start_date_check.setToolTip('Whether a start date is recorded')
            self.show_start_date = QDateEdit()
            self.show_start_date.setCalendarPopup(True)
            self.show_start_date.setDisplayFormat('yyyy-MM-dd')
            self.show_start_date.setToolTip('Date you started watching this show')
            self.show_start_date_check.toggled.connect(self.s_toggle_start_date)
            self.show_start_date.dateChanged.connect(self.s_set_start_date)

            self.show_finish_date_check = QCheckBox()
            self.show_finish_date_check.setToolTip('Whether a completion date is recorded')
            self.show_finish_date = QDateEdit()
            self.show_finish_date.setCalendarPopup(True)
            self.show_finish_date.setDisplayFormat('yyyy-MM-dd')
            self.show_finish_date.setToolTip('Date you finished watching this show')
            self.show_finish_date_check.toggled.connect(self.s_toggle_finish_date)
            self.show_finish_date.dateChanged.connect(self.s_set_finish_date)

            # Manually pin this show to a local folder, bypassing
            # filename-based guessing entirely -- the escape hatch for a
            # folder whose naming the parser/guesser can't match. Real
            # Taiga's own Anime Information dialog has this as an
            # editable path field; ours is read-only (set only via the
            # folder picker) since typing a path needs to actually
            # trigger a scan (set_show_folder), not just change text.
            self.show_folder_edit = QLineEdit()
            self.show_folder_edit.setReadOnly(True)
            self.show_folder_edit.setPlaceholderText('Not set')
            self.show_folder_edit.setToolTip(
                "Local folder manually pinned to this show, bypassing "
                "filename guessing -- for folders the parser can't match.")
            self.show_folder_browse_btn = QPushButton('Browse...')
            self.show_folder_browse_btn.clicked.connect(self.s_set_folder)
            self.show_folder_clear_btn = QPushButton('Clear')
            self.show_folder_clear_btn.clicked.connect(self.s_clear_folder)

        # Hidden entirely until something's actually playing -- see
        # _update_now_playing_sidebar().
        self.now_playing_group = QGroupBox('Now Playing')
        self.now_playing_group.setFlat(True)
        self.now_playing_status = QLabel('Nothing playing')
        self.now_playing_status.setWordWrap(True)
        now_playing_status_font = QtGui.QFont()
        now_playing_status_font.setBold(True)
        self.now_playing_status.setFont(now_playing_status_font)

        self.now_playing_bar = PlaybackBar(progress_color=self.config['colors']['progress_fg'])

        now_playing_btn_row = QHBoxLayout()
        self.now_playing_last_btn = QPushButton('Previous Episode')
        self.now_playing_last_btn.clicked.connect(
            lambda: self._now_playing_play(-1))
        self.now_playing_next_btn = QPushButton('Next Episode')
        self.now_playing_next_btn.clicked.connect(
            lambda: self._now_playing_play(1))
        now_playing_btn_row.addWidget(self.now_playing_last_btn)
        now_playing_btn_row.addWidget(self.now_playing_next_btn)

        self.now_playing_position = QLabel('')
        self.now_playing_position.setWordWrap(True)
        # A theme's own de-emphasized-text role, not a fixed color --
        # stays legible whether the theme is light or dark.
        self.now_playing_position.setStyleSheet(
            'color: palette(placeholder-text);')

        now_playing_layout = QVBoxLayout()
        now_playing_layout.addWidget(self.now_playing_status)
        now_playing_layout.addWidget(self.now_playing_bar)
        now_playing_layout.addLayout(now_playing_btn_row)
        now_playing_layout.addWidget(self.now_playing_position)
        self.now_playing_group.setLayout(now_playing_layout)
        self.now_playing_group.hide()
        self._now_playing_show = None
        self._now_playing_episode = None

        left_box.addRow(self.show_image)

        if self._taiga_mode:
            # Taiga's sidebar only shows the picture and whatever's
            # playing (progress + play controls). Page navigation lives
            # in the nav column (self.taiga_nav) instead of here. Every
            # other editing control (progress spinbox, score, status,
            # tags) moves into a second "Edit" tab in the Details
            # dialog instead -- see s_show_details().

            # show_dec_btn/show_inc_btn are already overlaid on the bar
            # itself (see HoverProgressBar) -- just add the bar.
            left_box.addRow(self.show_progress_bar)

            small_btns_hbox.addWidget(self.show_play_btn)
            small_btns_hbox.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            left_box.addRow(small_btns_hbox)

            # Single-column label+field form, matching real Taiga's own
            # Anime Information dialog (win32 1.4's dlg_anime_info_page
            # and the unreleased Qt rewrite's media_dialog are both
            # plain top-down forms, not a side-by-side field grid) --
            # a QGridLayout here previously let one row's expanding
            # widget (e.g. the Status combo) stretch its column and
            # drag unrelated fields in other rows out of place.
            # Skips Times rewatched/Rewatching/Notes -- real Taiga has
            # these too, but hakubun's engine has no equivalent data for
            # them, and faking fields with nothing behind them would be
            # worse than not having them. Folder *is* backed by the
            # engine (set_show_folder/unset_show_folder).
            section_label = QLabel('<b>Anime list</b>')

            form = QFormLayout()
            form.setContentsMargins(0, 8, 0, 0)
            form.setHorizontalSpacing(12)
            form.setVerticalSpacing(8)

            # Each row ends in addStretch(1) -- QAbstractSpinBox/
            # QPushButton/QComboBox default to a Preferred horizontal
            # size policy (not Fixed), so without something to soak up
            # the field column's leftover width, QFormLayout stretches
            # them individually across the row instead of leaving them
            # at their natural size.
            progress_row = QHBoxLayout()
            progress_row.addWidget(self.show_progress)
            progress_row.addWidget(self.show_progress_btn)
            progress_row.addStretch(1)
            form.addRow(show_progress_label, progress_row)

            score_row = QHBoxLayout()
            score_row.addWidget(self.show_score)
            score_row.addWidget(self.show_score_btn)
            score_row.addStretch(1)
            form.addRow(show_score_label, score_row)
            # Hidden until _set_score_editor finds a shared entry whose
            # Score is owned by another provider -- same widget the
            # classic-mode sidebar uses (left_box.addRow below), just
            # not previously reachable from Taiga mode's Edit tab at all.
            form.addRow(self.show_score_system)

            self.show_status.setMinimumWidth(150)
            status_row = QHBoxLayout()
            status_row.addWidget(self.show_status)
            status_row.addStretch(1)
            form.addRow(QLabel('Status:'), status_row)

            start_date_row = QHBoxLayout()
            start_date_row.addWidget(self.show_start_date_check)
            start_date_row.addWidget(self.show_start_date)
            start_date_row.addStretch(1)
            form.addRow(QLabel('Date started:'), start_date_row)

            finish_date_row = QHBoxLayout()
            finish_date_row.addWidget(self.show_finish_date_check)
            finish_date_row.addWidget(self.show_finish_date)
            finish_date_row.addStretch(1)
            form.addRow(QLabel('Date completed:'), finish_date_row)

            folder_row = QHBoxLayout()
            folder_row.addWidget(self.show_folder_edit, 1)
            folder_row.addWidget(self.show_folder_browse_btn)
            folder_row.addWidget(self.show_folder_clear_btn)
            form.addRow(QLabel('Folder:'), folder_row)

            edit_layout = QVBoxLayout()
            edit_layout.addWidget(section_label)
            edit_layout.addLayout(form)
            edit_layout.addSpacing(8)
            edit_layout.addWidget(self.show_tags_btn)
            edit_layout.addStretch(1)

            self.taiga_edit_widget = QWidget()
            self.taiga_edit_widget.setLayout(edit_layout)
        else:
            small_btns_hbox.addWidget(self.show_dec_btn)
            small_btns_hbox.addWidget(self.show_play_btn)
            small_btns_hbox.addWidget(self.show_inc_btn)
            small_btns_hbox.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

            left_box.addRow(self.show_progress_bar)
            left_box.addRow(small_btns_hbox)

            left_box.addRow(show_progress_label)
            left_box.addRow(self.show_progress, self.show_progress_btn)
            left_box.addRow(show_score_label)
            left_box.addRow(self.show_score)
            left_box.addRow(self.show_score_system)
            left_box.addRow(self.now_playing_group)
            left_box.addRow(self.show_status)
            left_box.addRow(self.show_tags_btn)

        if self._taiga_mode:
            # Real Taiga's search box lives in the toolbar, top-right --
            # not a separate row above/below the list, and with no
            # Invert/Case Sensitive toggles (not part of Taiga's UI at
            # all). The underlying proxy model still defaults to
            # non-inverted, case-insensitive filtering; show_filter_invert/
            # show_filter_casesens are simply never shown or checked in
            # this mode, so they stay at that default.
            self.show_filter.setFixedWidth(200)
            spacer = QWidget()
            spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self.taiga_toolbar.addWidget(spacer)
            self.taiga_toolbar.addWidget(self.show_filter)

            self.list_box.addWidget(self.notebook)
            self.list_box.addWidget(self.view)
        else:
            filter_bar_box_layout.addWidget(QLabel('Filter:'))
            filter_bar_box_layout.addWidget(self.show_filter)
            filter_bar_box_layout.addWidget(QLabel('Invert'))
            filter_bar_box_layout.addWidget(self.show_filter_invert)
            filter_bar_box_layout.addWidget(QLabel('Case Sensitive'))
            filter_bar_box_layout.addWidget(self.show_filter_casesens)
            self.filter_bar_box.setLayout(filter_bar_box_layout)

            if self.config['filter_bar_position'] is FilterBar.PositionHidden:
                self.list_box.addWidget(self.notebook)
                self.list_box.addWidget(self.view)
                self.filter_bar_box.hide()
            elif self.config['filter_bar_position'] is FilterBar.PositionAboveLists:
                self.list_box.addWidget(self.filter_bar_box)
                self.list_box.addWidget(self.notebook)
                self.list_box.addWidget(self.view)
            elif self.config['filter_bar_position'] is FilterBar.PositionBelowLists:
                self.list_box.addWidget(self.notebook)
                self.list_box.addWidget(self.view)
                self.list_box.addWidget(self.filter_bar_box)

        if self._taiga_mode:
            list_page = QWidget()
            list_page.setLayout(self.list_box)

            self.now_playing_widget = NowPlayingWidget(
                self, self.worker, progress_color=self.config['colors']['progress_fg'])
            self.now_playing_widget.playRequested.connect(self.s_play_show_episode)
            self.now_playing_widget.playRandomRequested.connect(self.s_play_random)

            self.content_stack = QStackedWidget()
            self._taiga_pages = {}

            # Persistent page-navigation column, separate from the
            # per-show info panel in left_box -- mirrors real Taiga's
            # sidebar (Anime List / Now Playing / ... as destinations,
            # not editing controls).
            self.taiga_nav = QListWidget()
            self.taiga_nav.setObjectName('taiga_nav')
            self.taiga_nav.setFrameShape(QFrame.Shape.NoFrame)
            self.taiga_nav.setFixedWidth(160)
            self.taiga_nav.currentItemChanged.connect(self._on_taiga_nav_changed)

            self.seasons_widget = SeasonsWidget(self, self.worker)
            self.search_widget = SearchWidget(self, self.worker)
            self.stats_widget = StatisticsWidget(
                self, self.worker, bar_color=self.config['colors']['progress_fg'])

            # Grouping matches real Taiga's sidebar: Now Playing on its
            # own, then list-management pages, then discovery pages
            # (History/Torrents omitted -- no data model for the
            # former, the latter out of scope entirely).
            self._add_taiga_page('now_playing', 'Now Playing', 'media-playback-start',
                                  self.now_playing_widget)
            self._add_taiga_separator()
            self._add_taiga_page('list', 'Anime List', 'view-list-details', list_page)
            self._add_taiga_page('stats', 'Statistics', 'view-statistics', self.stats_widget)
            self._add_taiga_separator()
            self._add_taiga_page('search', 'Search', 'edit-find', self.search_widget)
            # 'view-calendar' is already used by the Airing Schedule action
            # below and there's no calendar icon in Taiga's own set to give
            # this row instead -- 'view-grid' matches the Seasons page's
            # actual card-grid layout and has a real Taiga equivalent.
            self._add_taiga_page('seasons', 'Seasons', 'view-grid', self.seasons_widget)

            # Only the Anime List page has a "currently selected show" to
            # show quick info/actions for -- Now Playing already shows a
            # full-size version of the same thing via now_playing_widget,
            # and Seasons has no selected-show relationship at all. Kept
            # as a widget (not added straight as a layout) so it can be
            # hidden per-page without the empty-poster "double sidebar"
            # look on pages it's irrelevant to.
            self.taiga_info_panel = QWidget()
            self.taiga_info_panel.setLayout(left_box)

            main_hbox.addWidget(self.taiga_nav)
            main_hbox.addWidget(self.taiga_info_panel)
            main_hbox.addWidget(self.content_stack, 1)

            self._set_taiga_page('list')
        else:
            main_hbox.addLayout(left_box)
            main_hbox.addLayout(self.list_box, 1)

        main_layout.addLayout(top_hbox)
        main_layout.addLayout(main_hbox)

        self.main_widget = QWidget(self)
        self.main_widget.setLayout(main_layout)
        self.setCentralWidget(self.main_widget)

        # Statusbar
        self.status_text = QLabel(self.app_name)
        self.tracker_text = QLabel('Tracker: N/A')
        self.tracker_text.setMinimumWidth(120)
        self.queue_text = QLabel('Unsynced items: N/A')
        self.statusBar().addWidget(self.status_text, 1)
        self.statusBar().addPermanentWidget(self.tracker_text)
        self.statusBar().addPermanentWidget(self.queue_text)

        # Tray icon
        tray_menu = QMenu(self)
        action_hide = QAction('Show/Hide', self)
        action_hide.triggered.connect(self.s_hide)
        tray_menu.addAction(action_hide)
        if self._taiga_mode:
            # Real Taiga's tray menu (res/menu.xml, "Tray") also carries
            # Folders/Services/Settings -- reuses the same menu objects
            # already built and kept live for the menu bar (a QMenu can
            # be referenced as a submenu from more than one parent),
            # rather than building a second copy that would need its
            # own separate refresh calls.
            tray_menu.addSeparator()
            tray_menu.addMenu(self.menu_library_folders)
            tray_menu.addMenu(self.menu_services)
            tray_menu.addSeparator()
            tray_menu.addAction(action_settings)
            tray_menu.addSeparator()
        tray_menu.addAction(action_quit)

        self.tray = QSystemTrayIcon(self.windowIcon())
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self.s_tray_clicked)
        self._apply_tray()

        # Connect worker signals
        self.worker.changed_status.connect(self.ws_changed_status)
        self.worker.raised_error.connect(self.error)
        self.worker.raised_fatal.connect(self.fatal)
        self.worker.changed_show.connect(self.ws_changed_show)
        self.worker.changed_show_status.connect(self.ws_changed_show_status)
        self.worker.changed_list.connect(self.ws_changed_list)
        self.worker.changed_queue.connect(self.ws_changed_queue)
        self.worker.tracker_state.connect(self.ws_tracker_state)
        self.worker.playing_show.connect(self.ws_changed_show)
        self.worker.prompt_for_update.connect(self.ws_prompt_update)
        self.worker.prompt_for_add.connect(self.ws_prompt_add)
        self.worker.undo_stack_changed.connect(self.ws_undo_stack_changed)

        # Show main window
        if not (self.config['show_tray'] and self.config['start_in_tray']):
            self.show()

        # Start loading engine
        self.started = True
        self._busy(False)
        self.worker_call('start', self.r_engine_loaded, account)

    def reload(self, account=None, mediatype=None):
        if self.config['remember_columns']:
            self._store_columnstate()

        if account:
            self.account = account

            # Get API specific configuration
            self.api_config = self._get_api_config(account['api'])

        self.menu_columns.setEnabled(False)
        for action in self.menu_columns.actions():
            action.setChecked(
                action.text() in self.api_config['visible_columns'])
        self.menu_columns.setEnabled(True)

        self.show()
        self._busy(False)
        self.worker_call('reload', self.r_engine_loaded, account, mediatype)

    def closeEvent(self, event):
        if not self.started or not self.worker.engine.loaded:
            event.accept()
            if self.finish:
                QApplication.instance().quit()
        elif self.config['show_tray'] and self.config['close_to_tray']:
            event.ignore()
            self.s_hide()
        else:
            event.ignore()
            self._exit()

    def status(self, message):
        self.status_text.setText(message)
        print(message)

    def error(self, msg):
        self.status('Error: {}'.format(msg))
        QMessageBox.critical(self, 'Error', str(msg), QMessageBox.StandardButton.Ok)

    def fatal(self, msg):
        QMessageBox.critical(
            self, 'Fatal Error', "Fatal Error! Reason:\n\n{0}".format(msg), QMessageBox.StandardButton.Ok)
        self.accountman.set_default(None)
        self._busy()
        self.finish = False
        self.worker_call('unload', self.r_engine_unloaded)

    def worker_call(self, function, ret_function, *args, **kwargs):
        # Run worker in a thread. set_function owns starting/queueing;
        # don't call worker.start() here (see EngineWorker.set_function).
        self.worker.set_function(function, ret_function, *args, **kwargs)

    # GUI Functions
    def _get_api_config(self, api):
        if self.config['columns_per_api']:
            if 'api' not in self.config:
                self.config['api'] = {}
            if api not in self.config['api']:
                self.config['api'][api] = dict(utils.qt_per_api_defaults)
            return self.config['api'][api]
        else:
            # API settings are universal
            return self.config

    def _save_config(self):
        utils.save_config(self.config, self.configfile)

    def _exit(self):
        self._busy()
        if self.config['remember_geometry']:
            self._store_geometry()
        if self.config['remember_columns']:
            self._store_columnstate()
        self.finish = True
        self.worker_call('unload', self.r_engine_unloaded)

    def _store_geometry(self):
        self.config['last_x'] = self.x()
        self.config['last_y'] = self.y()
        self.config['last_width'] = self.width()
        self.config['last_height'] = self.height()
        utils.save_config(self.config, self.configfile)

    def _store_columnstate(self):
        columns_state = {}

        state = self.view.horizontalHeader().saveState()
        columns_state = base64.b64encode(state).decode('ascii')

        self.api_config['columns_state'] = columns_state
        self._save_config()

    def _enable_widgets(self, enable):
        self.view.setEnabled(enable)
        self._enable_show_widgets(bool(self.selected_show_id and enable))

        self.action_add.setEnabled(enable)
        self.action_airing_schedule.setEnabled(enable)
        self.action_sync.setEnabled(enable)
        self.action_send.setEnabled(enable)
        self.action_retrieve.setEnabled(enable)
        self.action_reload.setEnabled(enable)

        self.show_filter.setEnabled(enable)
        self.show_filter_invert.setEnabled(enable)
        self.show_filter_casesens.setEnabled(enable)

    def _enable_show_widgets(self, enable):
        self.show_progress.setEnabled(enable)
        self.show_score.setEnabled(enable)
        self.show_progress_btn.setEnabled(enable)
        self.show_score_btn.setEnabled(enable)
        self.show_tags_btn.setEnabled(
            bool(self.mediainfo and self.mediainfo.get('can_tag') and enable))
        self.show_inc_btn.setEnabled(enable)
        self.show_dec_btn.setEnabled(enable)
        self.show_play_btn.setEnabled(enable)
        self.show_status.setEnabled(enable)
        if self._taiga_mode:
            date_enabled = bool(self.mediainfo and self.mediainfo.get('can_date') and enable)
            self.show_start_date_check.setEnabled(date_enabled)
            self.show_finish_date_check.setEnabled(date_enabled)
            # The date picker itself only unlocks once its checkbox says
            # a date actually exists -- see _select_show/s_toggle_*_date.
            self.show_start_date.setEnabled(date_enabled and self.show_start_date_check.isChecked())
            self.show_finish_date.setEnabled(date_enabled and self.show_finish_date_check.isChecked())
            folder_enabled = bool(self.mediainfo and self.mediainfo.get('can_play') and enable)
            self.show_folder_browse_btn.setEnabled(folder_enabled)
            self.show_folder_clear_btn.setEnabled(
                folder_enabled and bool(self.show_folder_edit.text()))
        self.action_play_next.setEnabled(enable)
        self.action_play_dialog.setEnabled(enable)
        self.action_altname.setEnabled(enable)
        self.action_delete.setEnabled(enable)
        self.action_details.setEnabled(enable)

    def _update_queue_counter(self, queue):
        self.queue_text.setText("Unsynced items: %d" % queue)

    def _update_tracker_info(self, status):
        state = status['state']
        timer = status['timer']
        paused = status['paused']

        if state == utils.Tracker.NOVIDEO:
            st = 'Listen'
        elif state == utils.Tracker.PLAYING:
            (m, s) = divmod(timer or 0, 60)
            st = "+{0}:{1:02d}{2}".format(m, s, ' [P]' if paused else '')
        elif state == utils.Tracker.UNRECOGNIZED:
            st = 'Unrecognized'
        elif state == utils.Tracker.NOT_FOUND:
            st = 'Not found'
        elif state == utils.Tracker.IGNORED:
            st = 'Ignored'
        else:
            st = '???'

        self.tracker_text.setText("Tracker: {}".format(st))

    def _update_config(self):
        self._apply_view()
        self._apply_tray()
        self._apply_filter_bar()
        self._apply_sync_action_label()
        self._apply_subminer_state()
        # Refresh the multi-sync overlay so it follows the Settings
        # toggle immediately (on or off), instead of lingering until
        # the next full list rebuild.
        try:
            self._apply_multisync_overlay(self.worker.engine.get_list())
        except utils.HakubunError:
            pass
        # TODO: Reload listviews?
        if self._taiga_mode:
            self._rebuild_library_folders_menu()
        if self.worker.engine.get_config('sync_on_settings_apply'):
            self.s_send(False)

    def _apply_sync_action_label(self):
        if self.config['multisync_enabled']:
            self.action_sync.setText('&Multi-sync (BETA)')
            self.action_sync.setStatusTip(
                'Reconcile your list across every configured provider '
                '(mode set in Settings > Behavior); opens for review '
                'only if something needs your decision.')
        else:
            self.action_sync.setText('&Sync')
            self.action_sync.setStatusTip(
                'Send queued changes and download the current list.')

    def _apply_view(self):
        if self.config['inline_edit']:
            self.view.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        else:
            self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    def _apply_tray(self):
        if self.tray.isVisible() and not self.config['show_tray']:
            self.tray.hide()
        elif not self.tray.isVisible() and self.config['show_tray']:
            self.tray.show()
        if self.tray.isVisible():
            if self.config['tray_api_icon']:
                self.tray.setIcon(QtGui.QIcon(
                    utils.available_libs[self.account['api']][1]))
            else:
                self.tray.setIcon(self.windowIcon())

    def _apply_filter_bar(self):
        if self._taiga_mode:
            # Taiga mode's search box lives permanently in the toolbar,
            # not the classic filter_bar_box -- filter_bar_position
            # isn't applicable here.
            return
        self.list_box.removeWidget(self.filter_bar_box)
        self.list_box.removeWidget(self.notebook)
        self.list_box.removeWidget(self.view)
        self.filter_bar_box.show()
        if self.config['filter_bar_position'] is FilterBar.PositionHidden:
            self.list_box.addWidget(self.notebook)
            self.list_box.addWidget(self.view)
            self.filter_bar_box.hide()
        elif self.config['filter_bar_position'] is FilterBar.PositionAboveLists:
            self.list_box.addWidget(self.filter_bar_box)
            self.list_box.addWidget(self.notebook)
            self.list_box.addWidget(self.view)
        elif self.config['filter_bar_position'] is FilterBar.PositionBelowLists:
            self.list_box.addWidget(self.notebook)
            self.list_box.addWidget(self.view)
            self.list_box.addWidget(self.filter_bar_box)

    def _busy(self, wait=False):
        if wait:
            self.busy_timer.start()
        else:
            self._enable_widgets(False)

    def _unbusy(self):
        if self.busy_timer.isActive():
            self.busy_timer.stop()
        else:
            self._enable_widgets(True)

    def _rebuild_statuses(self):
        # Rebuild statuses
        self.show_status.blockSignals(True)
        self.notebook.blockSignals(True)

        self.show_status.clear()
        self.menu_move_to.clear()

        # Clear notebook
        while self.notebook.count() > 0:
            self.notebook.removeTab(0)

        # Add one page per status
        for i, status in enumerate(self.mediainfo['statuses']):
            name = self.mediainfo['statuses_dict'][status]

            self.notebook.addTab(name)
            self.notebook.setTabData(i, status)

            self.show_status.addItem(name)

            action = self.menu_move_to.addAction(name)
            action.triggered.connect(
                lambda checked=False, s=status: self._set_show_status_from_menu(s))

        self.notebook.addTab("All")

        if self._taiga_mode:
            self._rebuild_quick_score_menu()

        self.show_status.blockSignals(False)
        self.notebook.blockSignals(False)

    def _rebuild_library_folders_menu(self):
        self.menu_library_folders.clear()
        self.menu_library_folders.addAction(self.action_scan_library)
        self.menu_library_folders.addAction(self.action_rescan_library)
        self.menu_library_folders.addSeparator()

        folders = self.worker.engine.get_config('searchdir')
        if not folders:
            empty = self.menu_library_folders.addAction('(no folders configured)')
            empty.setEnabled(False)
            return

        for folder in folders:
            action = self.menu_library_folders.addAction(folder)
            action.triggered.connect(
                lambda checked=False, f=folder: utils.open_folder(f))

    def _rebuild_services_menu(self):
        # Trim back to the Sync/Switch Account actions + separator built
        # in start(); everything after that is the per-service section,
        # rebuilt here once the active account's API/username are known.
        for action in self.menu_services.actions()[3:]:
            self.menu_services.removeAction(action)

        api = self.account['api']
        username = self.worker.engine.get_userconfig('username')
        if not username:
            return

        sections = {
            'anilist': [
                ('Go to my Profile', 'https://anilist.co/user/%s' % username),
                ('Stats', 'https://anilist.co/user/%s/stats' % username),
            ],
            'kitsu': [
                ('Go to my library', 'https://kitsu.app/users/%s/library' % username),
                ('Go to my Profile', 'https://kitsu.app/users/%s' % username),
            ],
            'mal': [
                ('Go to my Profile', 'https://myanimelist.net/profile/%s' % username),
                ('Go to my History', 'https://myanimelist.net/history/%s' % username),
            ],
        }

        entries = sections.get(api)
        if not entries:
            return

        for label, url in entries:
            action = self.menu_services.addAction(label)
            action.triggered.connect(
                lambda checked=False, u=url: QtGui.QDesktopServices.openUrl(QtCore.QUrl(u)))

    def _set_show_status_from_menu(self, status):
        if self.selected_show_id:
            self._set_show_status(self.selected_show_id, status)

    def _rebuild_quick_score_menu(self):
        self.menu_quick_score.clear()

        display_max, _step, decimals = utils.score_display_range(self.mediainfo)
        if not display_max:
            return

        # 11 evenly-spaced picks (0 through display_max) regardless of
        # scale, rather than Taiga's fixed 0-10 -- that assumes a
        # 10-point scale, which doesn't hold for e.g. AniList's 0-100.
        for i in range(11):
            display_value = display_max * i / 10
            label = ('No Score' if i == 0 else '(%s)' % (
                ('%.*f' % (decimals, display_value)).rstrip('0').rstrip('.')
                if decimals else str(int(display_value))))
            raw_value = utils.score_to_raw(display_value, self.mediainfo)
            action = self.menu_quick_score.addAction(label)
            action.triggered.connect(
                lambda checked=False, v=raw_value: self.s_set_quick_score(v))

    def s_set_quick_score(self, raw_value):
        if self.selected_show_id:
            self.s_set_score(self.selected_show_id, raw_value)

    def _rebuild_quick_date_menus(self):
        self.menu_quick_date_started.clear()
        self.menu_quick_date_completed.clear()

        date_enabled = bool(
            self.selected_show_id and self.mediainfo and self.mediainfo.get('can_date'))
        self.action_quick_date_started.setEnabled(date_enabled)
        self.action_quick_date_completed.setEnabled(date_enabled)
        if not date_enabled:
            return

        show = self.worker.engine.get_show_info(self.selected_show_id)

        if show.get('start_date'):
            action = self.menu_quick_date_started.addAction('Set to date started airing')
            action.triggered.connect(
                lambda checked=False, d=show['start_date']: self._quick_set_start_date(d))
        self.action_quick_date_started.setEnabled(bool(self.menu_quick_date_started.actions()))

        if show.get('end_date'):
            action = self.menu_quick_date_completed.addAction('Set to date finished airing')
            action.triggered.connect(
                lambda checked=False, d=show['end_date']: self._quick_set_finish_date(d))
        if show.get('my_last_update'):
            action = self.menu_quick_date_completed.addAction('Set to last updated')
            action.triggered.connect(
                lambda checked=False, d=show['my_last_update']: self._quick_set_finish_date(d))
        self.action_quick_date_completed.setEnabled(bool(self.menu_quick_date_completed.actions()))

    def _quick_set_start_date(self, date):
        # Drives the same checkbox+QDateEdit pair the Details tab uses,
        # so both stay in sync regardless of which one changed the
        # date -- blockSignals to commit once with the real target date
        # instead of twice (once for the checkbox's own "just checked"
        # commit, once for the date itself).
        self.show_start_date_check.blockSignals(True)
        self.show_start_date.blockSignals(True)
        self.show_start_date_check.setChecked(True)
        self.show_start_date.setEnabled(True)
        self.show_start_date.setDate(QtCore.QDate(date.year, date.month, date.day))
        self.show_start_date_check.blockSignals(False)
        self.show_start_date.blockSignals(False)
        self.s_set_start_date(self.show_start_date.date())

    def _quick_set_finish_date(self, date):
        self.show_finish_date_check.blockSignals(True)
        self.show_finish_date.blockSignals(True)
        self.show_finish_date_check.setChecked(True)
        self.show_finish_date.setEnabled(True)
        self.show_finish_date.setDate(QtCore.QDate(date.year, date.month, date.day))
        self.show_finish_date_check.blockSignals(False)
        self.show_finish_date.blockSignals(False)
        self.s_set_finish_date(self.show_finish_date.date())

    def _recalculate_counts(self):
        showlist = self.worker.engine.get_list()

        self.counts = {status: 0 for status in self.mediainfo['statuses']}
        self.counts['!ALL'] = 0

        for show in showlist:
            self.counts[show['my_status']] += 1
            self.counts['!ALL'] += 1

        self._update_counts()

    def _update_counts(self):
        for page in range(self.notebook.count()):
            status = self.notebook.tabData(page)
            if status is not None:
                status_name = self.mediainfo['statuses_dict'][status]
            else:
                status_name = "All"
                status = "!ALL"

            self.notebook.setTabText(page, "{} ({})".format(
                status_name, self.counts[status]))

    def _rebuild_view(self):
        """
        Using a full showlist, rebuilds main view

        """
        showlist = self.worker.engine.get_list()
        altnames = self.worker.engine.altnames()
        library = self.worker.engine.library()

        # Set allowed ranges (this will be reported by the engine later)
        self.show_score.setMediaInfo(self.mediainfo)

        # Get the new list and pass it to our model
        self.view.setSortingEnabled(False)
        self.view.model().setFilterStatus(
            self.notebook.tabData(self.notebook.currentIndex()))
        self.view.model().sourceModel().setMediaInfo(self.mediainfo)
        self.view.model().sourceModel().setShowList(showlist, altnames, library)
        self._apply_multisync_overlay(showlist)
        self.view.resizeRowsToContents()
        self.view.setSortingEnabled(True)

        self.s_filter_changed()

    def _apply_multisync_overlay(self, showlist, quiet=False):
        """When multi-sync is on, display each show's reconciled
        per-field value (episodes from one provider, rating from
        another, ...) instead of just this account's raw value. Purely
        a display overlay -- read-only, gated, and a no-op (identical
        to before) whenever it's disabled or has nothing to show, so it
        can never destabilise the main list. Edits still go to the
        signed-in account, which multi-sync reconciles on the next
        sync.

        `quiet` refreshes the overlay in place (no model reset), so a
        refresh triggered mid-interaction -- e.g. right after setting an
        owned score -- doesn't drop the current selection."""
        model = self.view.model().sourceModel()
        # Reset owner-score editing state; re-established below when on.
        self._score_owner_mediainfo = {}
        self._multisync_media_type = None
        if not self.config.get('multisync_enabled') \
                or not getattr(self, 'account', None):
            model.set_overlay({})
            self._resync_score_editor()
            return
        try:
            from hakubun.sync import uibridge
            media_type = self.worker.engine.data_handler.userconfig.get(
                'mediatype') or 'anime'
            self._multisync_media_type = media_type
            msg = messenger.Messenger(None, 'Overlay')
            # Shared with the GTK front-end (hakubun.sync.uibridge) so the
            # two UIs build the overlay identically.
            overlay, pmi = uibridge.build_list_overlay(
                self.accountman.get_accounts(), self.account['api'],
                self.mediainfo, media_type, msg,
                show_ids=[s['id'] for s in showlist])
            self._score_owner_mediainfo = pmi
            if quiet:
                model.refresh_overlay(overlay)
            else:
                model.set_overlay(overlay)
        except Exception:
            import traceback
            traceback.print_exc()
            model.set_overlay({})   # never break the list over this
        self._resync_score_editor()

    def _resync_score_editor(self):
        """Re-derive the bottom-bar score editor's owner-mode state from
        the CURRENT overlay. Called after every overlay rebuild/clear so
        the editor can never be left in a stale owner mode (owner scale
        on the slider, no owner mediainfo behind it) -- which would
        silently discard the next Set."""
        if self.selected_show_id:
            try:
                show = self.worker.engine.get_show_info(
                    self.selected_show_id)
            except utils.EngineError:
                show = None
            if show:
                self._set_score_editor(show)
                return
        # Nothing (valid) selected: drop owner mode and restore the
        # active account's own rating system.
        if self._score_editor_provider is not None:
            self.show_score.setMediaInfo(self.mediainfo)
        self._score_editor_provider = None
        self._score_owner_mode = None
        self.show_score_system.setVisible(False)

    def _init_view(self):
        # Set view options
        self.view.sortByColumn(self.config['sort_index'], QtCore.Qt.SortOrder(self.config['sort_order']))

        # Hide invisible columns
        for i, column in enumerate(self.view.model().sourceModel().columns):
            if column not in self.api_config['visible_columns']:
                self.view.setColumnHidden(i, True)

        self.view.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.view.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)

        # Recover column state
        if self.config['remember_columns'] and isinstance(self.api_config['columns_state'], str):
            state = QtCore.QByteArray(base64.b64decode(
                self.api_config['columns_state']))
            self.view.horizontalHeader().restoreState(state)
        else:
            self.view.horizontalHeader().resizeSection(3, 70)
            self.view.horizontalHeader().resizeSection(4, 100)

        if self._taiga_mode:
            # Taiga mode's own column set/order, overriding whatever the
            # classic api_config['visible_columns']-driven loop above
            # did: status dot, Title, Progress (renamed from Percent --
            # the plain-text "Progress" column duplicates it now that
            # the bar shows the count as centered text), Score,
            # Platform Score, Type, Season. Everything else (ID, the
            # old Progress column, Next Episode, Start/End date, My
            # start/finish, Tags, Status, Last updated, MAL Score) is
            # hidden -- real Taiga doesn't show any of these (status is
            # already conveyed by the nav/tabs, not a text column).
            model = self.view.model().sourceModel()
            model.columns = list(model.columns)
            model.columns[ShowListModel.COL_PERCENT] = 'Progress'

            header = self.view.horizontalHeader()
            shown_columns = [
                ShowListModel.COL_RELEASE_STATUS,
                ShowListModel.COL_TITLE,
                ShowListModel.COL_PERCENT,
                ShowListModel.COL_MY_SCORE,
                ShowListModel.COL_PLATFORM_SCORE,
                ShowListModel.COL_TYPE,
                ShowListModel.COL_SEASON,
            ]
            hidden_columns = [
                ShowListModel.COL_ID,
                ShowListModel.COL_MY_PROGRESS,
                ShowListModel.COL_NEXT_EP,
                ShowListModel.COL_START_DATE,
                ShowListModel.COL_END_DATE,
                ShowListModel.COL_MY_START,
                ShowListModel.COL_MY_FINISH,
                ShowListModel.COL_MY_TAGS,
                ShowListModel.COL_MY_STATUS,
                ShowListModel.COL_LAST_UPDATED,
                ShowListModel.COL_MAL_SCORE,
            ]
            for col in hidden_columns:
                self.view.setColumnHidden(col, True)
            for target_index, col in enumerate(shown_columns):
                self.view.setColumnHidden(col, False)
                header.moveSection(header.visualIndex(col), target_index)

            header.setSectionResizeMode(
                ShowListModel.COL_RELEASE_STATUS, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(ShowListModel.COL_RELEASE_STATUS, 20)
            # COL_TITLE (1) is already set to Stretch unconditionally
            # above, which happens to match its Taiga-mode position too.

    def _set_default_poster(self):
        # With no show selected, fill the poster box with a placeholder
        # logo rather than bare "<app name>" text: the hanko mark for
        # Hakubun+, Taiga's own icon in Taiga mode (mirrors the window
        # icon set on the QApplication).
        logo = 'taiga_icon.png' if self._taiga_mode else 'hanko.png'
        self.show_image.setPixmap(QtGui.QPixmap(
            utils.DATADIR + '/' + logo).scaledToHeight(
                100, QtCore.Qt.TransformationMode.SmoothTransformation))

    def _select_show(self, show):
        # Stop any running image timer
        if self.image_timer is not None:
            self.image_timer.stop()

        # Unselect show
        if not show:
            self.selected_show_id = None

            self.show_title.setText(self.app_name)
            self._set_default_poster()
            self.show_progress.setValue(0)
            self.show_score.setValue(0)
            self._score_owner_mode = None
            self.show_progress_bar.setValue(0)
            self.show_progress_bar.setFormat('?/?')
            if self._taiga_mode:
                self.show_folder_edit.setText('')
            self._enable_show_widgets(False)
            self._update_folder_label()

            return

        # Block signals
        self.show_status.blockSignals(True)

        # Set proper ranges
        if show['total']:
            self.show_progress.setMaximum(show['total'])
            self.show_progress_bar.setFormat(
                '%p%' if self._taiga_mode else '%v/%m')
            self.show_progress_bar.setMaximum(show['total'])
            # Regenerate Play Episode Menu
            self.generate_episode_menus(
                self.menu_play, show['total'], show['my_progress'])
        else:
            self.show_progress.setMaximum(
                utils.estimate_aired_episodes(show) or 10000)
            self.generate_episode_menus(
                self.menu_play, utils.estimate_aired_episodes(show), show['my_progress'])
            self.show_progress_bar.setFormat(
                '{}/?'.format(show['my_progress']))

        # Update information
        metrics = QtGui.QFontMetrics(self.show_title.font())
        title = metrics.elidedText(
            show['title'], QtCore.Qt.TextElideMode.ElideRight, self.show_title.width())
        self.show_title.setText(title)

        self.show_progress.setValue(show['my_progress'])
        self.show_status.setCurrentIndex(
            self.mediainfo['statuses'].index(show['my_status']))
        self._set_score_editor(show)

        if self._taiga_mode:
            # blockSignals so populating these from the selected show
            # doesn't itself trigger s_set_start_date/s_set_finish_date
            # (dateChanged fires on programmatic setDate() too, not
            # just user interaction).
            for check, widget, key in (
                    (self.show_start_date_check, self.show_start_date, 'my_start_date'),
                    (self.show_finish_date_check, self.show_finish_date, 'my_finish_date')):
                check.blockSignals(True)
                widget.blockSignals(True)
                date = show.get(key)
                if date:
                    check.setChecked(True)
                    widget.setDate(QtCore.QDate(date.year, date.month, date.day))
                else:
                    # No date recorded -- default the (disabled) picker
                    # to today only as a starting point for if the user
                    # checks the box, not as a claim this date is set.
                    check.setChecked(False)
                    widget.setDate(QtCore.QDate.currentDate())
                widget.setEnabled(bool(date))
                check.blockSignals(False)
                widget.blockSignals(False)

            # self.worker.engine can still be None here -- a show gets
            # selected (e.g. a stale queued selection-changed signal)
            # before the engine has finished loading, or after it's
            # been torn down (account switch, fatal error unload).
            engine = self.worker.engine
            self.show_folder_edit.setText(
                (engine and engine.get_show_folder(show['id'])) or '')

        # Enable relevant buttons
        self._enable_show_widgets(True)

        # Download image or use cache
        if show.get('image_thumb') or show.get('image'):
            if self.image_worker is not None:
                self.image_worker.cancel()

            utils.make_dir(utils.to_cache_path())
            filename = utils.to_cache_path("%s_%s_%s.jpg" % (
                self.api_info['shortname'], self.api_info['mediatype'], show['id']))

            if os.path.isfile(filename):
                self.s_show_image(filename)
            else:
                if "imaging_available" in os.environ:
                    self.show_image.setText('Waiting...')
                    self.image_timer.start()
                else:
                    self.show_image.setText('Not available')
        else:
            self.show_image.setText('No image')

        if show['total']:
            self.show_progress_bar.setValue(show['my_progress'])
        else:
            self.show_progress_bar.setValue(0)

        # Make it global
        self.selected_show_id = show['id']
        self._update_folder_label()

        # Unblock signals
        self.show_status.blockSignals(False)

    def _set_score_editor(self, show):
        """Configure the bottom-bar score slider for the selected show.

        For a SHARED entry whose Score is owned by another provider, the
        slider adopts the OWNER's rating system -- so you can rate in
        AniList's decimals (slide to 8.4) even while signed into Kitsu,
        which only offers 0.5 steps -- seeded from local's reconciled
        score; the Set then writes to multisync local (see s_set_score).
        Otherwise the slider stays on the active account's own system and
        seeds from this account's raw my_score, exactly as before -- so a
        platform-specific entry keeps the signed-in account's steps."""
        over = self.view.model().sourceModel().overlay_for(show['id'])
        owner = over.get('_score_owner')
        owner_mi = self._score_owner_mediainfo.get(owner) if owner else None
        # The owner-system editor is only offered when the setting allows
        # it; the Synced/Platform switch appears only for an owned entry.
        can_synced = bool(owner_mi) and self.config.get(
            'multisync_edit_owned_score', True)
        self.show_score_system.setVisible(can_synced)
        if can_synced:
            self.show_score_system.setText(
                'Synced score (%s)' % owner.capitalize()
                if self.show_score_system.isChecked() else 'Platform score')
        use_synced = can_synced and self.show_score_system.isChecked()
        if use_synced:
            if self._score_editor_provider != owner:
                self.show_score.setMediaInfo(owner_mi)
                self._score_editor_provider = owner
            self.show_score.setValue(over.get('_score_owner_raw') or 0)
            self._score_owner_mode = owner
        else:
            # Always re-apply the active account's own scale here, not
            # only when transitioning off an owner scale: flipping the
            # switch to Platform must reliably restore the signed-in
            # tracker's system, regardless of any stale editor state.
            self.show_score.setMediaInfo(self.mediainfo)
            self._score_editor_provider = None
            self.show_score.setValue(show['my_score'])
            self._score_owner_mode = None

    def generate_episode_menus(self, menu, max_eps=1, watched_eps=0):
        bp_top = 5  # No more than this many submenus/episodes in the root menu
        bp_mid = 10  # No more than this many submenus in submenus
        bp_btm = 13  # No more than this many episodes in the submenus
        # The number of episodes where we ditch the submenus entirely since Qt doesn't deserve this abuse
        breakpoint_no_menus = bp_top * bp_btm * bp_mid * bp_mid

        menu.clear()
        # Make basic actions
        action_play_next = QAction(
            getIcon('media-skip-forward'), 'Play &Next Episode', self)
        action_play_next.triggered.connect(lambda: self.s_play(True))
        # Was 'view-refresh' (a sync/refresh icon, unrelated to replaying
        # an episode) -- 'history' actually matches what this action does.
        action_play_last = QAction(
            getIcon('history'), 'Play Last Watched Ep (#%d)' % watched_eps, self)
        action_play_last.triggered.connect(lambda: self.s_play(False))
        action_play_dialog = QAction('Play Episode...', self)
        action_play_dialog.setStatusTip('Select an episode to play.')
        action_play_dialog.triggered.connect(self.s_play_number)

        menu.addAction(action_play_next)
        menu.addAction(action_play_last)

        if max_eps < 1 or max_eps > breakpoint_no_menus:
            menu.addAction(action_play_dialog)
            return menu

        if max_eps > 60:
            # Typing a number beats digging through nested submenus once a
            # show has this many episodes.
            menu.addAction(action_play_dialog)

        menu.addSeparator()

        ep_actions = []
        for ep in range(1, max_eps+1):
            action = QAction('Ep. %d' % ep, self)
            action.triggered.connect(self.s_play_ep_number(action, ep))
            if ep <= watched_eps:
                action.setIcon(self.ep_icons['all'])
            else:
                action.setIcon(self.ep_icons['none'])
            ep_actions.append(action)

        if max_eps <= bp_top:
            # Just put the eps in the root menu
            for action in ep_actions:
                menu.addAction(action)

        else:
            # We need to go deeper. For now, put all the episodes into bottom-level submenus.
            # I don't like this scoping. If you find a way to transfer ownership of the submenu to the menu feel free to fix this.
            self.play_ep_submenus = []
            # A bit hacky but avoids a special case for the first submenu
            current_actions = bp_btm + 1
            for action in ep_actions:
                if current_actions >= bp_btm:
                    current_actions = 0
                    length = len(self.play_ep_submenus)
                    self.play_ep_submenus.append(
                        QMenu('Episodes %d-%d:' % (length*bp_btm + 1, min((length+1)*bp_btm, max_eps)), menu))
                    if watched_eps > min((length+1)*bp_btm, max_eps):
                        self.play_ep_submenus[-1].setIcon(self.ep_icons['all'])
                    elif watched_eps > length*bp_btm:
                        self.play_ep_submenus[-1].setIcon(
                            self.ep_icons['part'])
                    else:
                        self.play_ep_submenus[-1].setIcon(
                            self.ep_icons['none'])
                self.play_ep_submenus[-1].addAction(action)
                current_actions += 1

            # Now to put the bottom level menus into other things
            if len(self.play_ep_submenus) <= bp_top:  # Straight into the root menu, easy!
                for submenu in self.play_ep_submenus:
                    menu.addMenu(submenu)
            else:  # For now, put them into another level of submenus
                self.play_ep_sub2menus = []
                current_menus = bp_mid + 1
                for s in self.play_ep_submenus:
                    if current_menus >= bp_mid:
                        current_menus = 0
                        length = len(self.play_ep_sub2menus)
                        self.play_ep_sub2menus.append(QMenu(
                            'Episodes %d-%d:' % (length*bp_btm*bp_mid + 1, min((length+1)*bp_btm*bp_mid, max_eps)), s))
                    self.play_ep_sub2menus[-1].addMenu(s)
                    if watched_eps > min((length+1)*bp_btm*bp_mid, max_eps):
                        self.play_ep_sub2menus[-1].setIcon(
                            self.ep_icons['all'])
                    elif watched_eps > length*bp_btm*bp_mid:
                        self.play_ep_sub2menus[-1].setIcon(
                            self.ep_icons['part'])
                    else:
                        self.play_ep_sub2menus[-1].setIcon(
                            self.ep_icons['none'])
                    current_menus += 1

                if len(self.play_ep_sub2menus) <= bp_top:
                    for submenu in self.play_ep_sub2menus:
                        menu.addMenu(submenu)
                else:
                    # I seriously hope this additional level is not needed, but maybe someone will want to set smaller breakpoints.
                    self.play_ep_sub3menus = []
                    current_menus = bp_mid + 1
                    for s in self.play_ep_sub2menus:
                        if current_menus >= bp_mid:
                            current_menus = 0
                            length = len(self.play_ep_sub3menus)
                            self.play_ep_sub3menus.append(QMenu(
                                'Episodes %d-%d:' % (length*bp_btm*bp_mid*bp_mid + 1, min((length+1)*bp_btm*bp_mid*bp_mid, max_eps)), s))
                        self.play_ep_sub3menus[-1].addMenu(s)
                        if watched_eps > min((length+1)*bp_btm*bp_mid*bp_mid, max_eps):
                            self.play_ep_sub3menus[-1].setIcon(
                                self.ep_icons['all'])
                        elif watched_eps > length*bp_btm*bp_mid*bp_mid:
                            self.play_ep_sub3menus[-1].setIcon(
                                self.ep_icons['part'])
                        else:
                            self.play_ep_sub3menus[-1].setIcon(
                                self.ep_icons['none'])
                        current_menus += 1
                    # No more levels, our sanity check earlier ensured that.
                    for submenu in self.play_ep_sub3menus:
                        menu.addMenu(submenu)
        return menu

    # Slots
    def s_hide(self):
        if self.isVisible():
            self.was_maximized = self.isMaximized()
            self.hide()
        else:
            self.setGeometry(self.geometry())
            if self.was_maximized:
                self.showMaximized()
            else:
                self.show()

    def s_tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.s_hide()

    def s_busy(self):
        self._enable_widgets(False)

    def s_show_selected(self, new, old=None):
        if new:
            index = self.view.model().index(new.row(), 0)
            selected_id = self.view.model().data(index)

            if selected_id:
                show = self.worker.engine.get_show_info(selected_id)
                self._select_show(show)
        else:
            self._select_show(None)

    def s_update_sort(self, index, order):
        self.config['sort_index'] = index
        self.config['sort_order'] = order.value

    def s_download_image(self):
        show = self.worker.engine.get_show_info(self.selected_show_id)
        self.show_image.setText('Downloading...')
        filename = utils.to_cache_path("%s_%s_%s.jpg" % (
            self.api_info['shortname'], self.api_info['mediatype'], show['id']))

        self.image_worker = ImageWorker(
            show.get('image_thumb') or show['image'], filename, (100, 140))
        self.image_worker.finished.connect(self.s_show_image)
        self.image_worker.start()

    def s_tab_changed(self, index):
        # Change the filter of the main view to the specified status
        status = self.notebook.tabData(index)
        self.view.model().setFilterStatus(status)
        self.view.resizeRowsToContents()  # TODOMVC : Find a faster way

        self.s_show_selected(None)
        self.s_filter_changed()  # Refresh filter

    def s_filter_changed(self):
        # TODOMVC DEPRECATED
        expression = self.show_filter.text()

        # Determine if a show matches a filter. True -> match -> do not hide
        # Advanced search: Separate the expression into specific field terms, fail if any are not met
        if ':' in expression:
            exprs = expression.split(' ')
            expr_dict = {}
            expr_list = []
            for expr in exprs:
                if ':' in expr:
                    expr_terms = expr.split(':', 1)
                    if expr_terms[0] in self.column_keys:
                        col = self.column_keys[expr_terms[0]]
                        sub_expr = expr_terms[1].replace(
                            '_', ' ').replace('+', ' ')
                        expr_dict[col] = sub_expr
                    else:  # If it's not a field key, let it be a regular search term
                        expr_list.append(expr)
                else:
                    expr_list.append(expr)
            expression = ' '.join(expr_list)
            self.view.model().setFilterColumns(expr_dict)

        if self.show_filter_casesens.isChecked():
            self.view.model().setFilterCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseSensitive)
        else:
            self.view.model().setFilterCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
        self.view.model().setFilterFixedString(expression)

    def s_filter_text_changed(self):
        # Separate from s_filter_changed (also invoked by s_tab_changed to
        # re-apply the existing filter after a tab switch) so switching to
        # All only happens in response to the user actually typing, not
        # every time the filter gets reapplied.
        raw_query = self.show_filter.text()
        self.s_filter_changed()

        if raw_query and self.config['filter_global']:
            all_tab = self.notebook.count() - 1
            if self.notebook.currentIndex() != all_tab:
                self.notebook.setCurrentIndex(all_tab)

    def s_filter_invert_changed(self):
        self.view.model().setFilterInvert(self.show_filter_invert.isChecked())

    def s_plus_episode(self):
        self._busy(True)
        self.worker_call('set_episode', self.r_generic,
                         self.selected_show_id, self.show_progress.value()+1)

    def s_rem_episode(self):
        if not self.show_progress.value() <= 0:
            self._busy(True)
            self.worker_call('set_episode', self.r_generic,
                             self.selected_show_id, self.show_progress.value()-1)

    def s_set_episode(self, showid=None, ep=None):
        self._busy(True)
        self.worker_call('set_episode', self.r_generic,
                         showid or self.selected_show_id, ep if ep is not None else self.show_progress.value())

    def s_set_score(self, showid=None, score=None):
        if not showid:
            showid = self.selected_show_id

        # Owner-mode: the bottom-bar slider adopted another provider's
        # rating system for this shared entry, so the value the user
        # picked is in THAT system. Persist it to multisync local (as
        # intent) and let the next sync push it to the owner and every
        # tracker, rather than forcing it into the signed-in account's
        # coarser scale. Inline list edits (score passed in) are not in
        # owner mode and keep the normal active-account path below. If
        # the local write can't happen we ABORT rather than fall through
        # -- the slider value is in the owner's scale and would be
        # misread by the active account.
        if score is None and showid == self.selected_show_id \
                and self._score_owner_mode:
            if not self._set_owned_score(showid, self.show_score.value()):
                self.status('Could not set the owned score locally; '
                            'nothing changed.')
            return

        self._busy(True)
        if score is None:
            score = self.show_score.value()

        self.worker_call('set_score', self.r_generic, showid, score)

    def _set_owned_score(self, showid, owner_raw):
        """Persist a score entered in the OWNER's rating system to
        multisync local, as user intent, so the next multi-sync pushes
        it to the owner and (per the ownership matrix) every other
        tracker. Returns True when the score was handled here; False to
        fall back to the normal active-account set_score path."""
        owner = self._score_owner_mode
        owner_mi = self._score_owner_mediainfo.get(owner)
        media_type = self._multisync_media_type
        if not owner or not owner_mi or not media_type:
            return False
        try:
            from hakubun.sync import uibridge
            # Shared with the GTK front-end so both write owned scores
            # to multisync local identically.
            if not uibridge.write_owned_score(
                    media_type, self.account['api'], showid, owner_raw,
                    owner_mi):
                return False
        except Exception:
            import traceback
            traceback.print_exc()
            return False
        # Reflect immediately: refresh the overlay in place (no model
        # reset -> selection kept) so the list shows the new owner-system
        # score, and say it's staged for the next sync.
        self._apply_multisync_overlay(self.worker.engine.get_list(),
                                      quiet=True)
        self.status("Score set in %s's rating system; the next multi-sync "
                    "applies it." % owner.capitalize())
        return True

    def s_set_status(self, index):
        if self.selected_show_id:
            self._set_show_status(
                self.selected_show_id, self.mediainfo['statuses'][index])

    def _set_show_status(self, showid, status):
        self._busy(True)
        self.worker_call('set_status', self.r_generic, showid, status)

    def s_set_start_date(self, qdate):
        if self.selected_show_id:
            self._busy(True)
            self.worker_call(
                'set_dates', self.r_generic, self.selected_show_id,
                start_date=qdate.toPyDate())

    def s_set_finish_date(self, qdate):
        if self.selected_show_id:
            self._busy(True)
            self.worker_call(
                'set_dates', self.r_generic, self.selected_show_id,
                finish_date=qdate.toPyDate())

    def s_toggle_start_date(self, checked):
        self.show_start_date.setEnabled(checked)
        # Checking it on records whatever date is currently showing
        # (defaults to today -- see _select_show) as the start date.
        # Unchecking never clears it server-side (the engine can't --
        # see set_dates()), it just stops showing a date that was never
        # actually confirmed.
        if checked:
            self.s_set_start_date(self.show_start_date.date())

    def s_toggle_finish_date(self, checked):
        self.show_finish_date.setEnabled(checked)
        if checked:
            self.s_set_finish_date(self.show_finish_date.date())

    def s_set_folder(self):
        if not self.selected_show_id:
            return
        current = self.worker.engine.get_show_folder(self.selected_show_id)
        folder = QFileDialog.getExistingDirectory(
            self, 'Select folder', current or os.path.expanduser('~'))
        if not folder:
            return
        self._busy(True)
        self.worker_call('set_show_folder', self.r_folder_set,
                         self.selected_show_id, folder)

    def r_folder_set(self, result):
        if result['success']:
            self._update_folder_display()
            self._rebuild_view()
            self.status('Ready.')
        self._unbusy()

    def s_clear_folder(self):
        if not self.selected_show_id:
            return
        self.worker.engine.unset_show_folder(self.selected_show_id)
        self._update_folder_display()
        self.status('Folder cleared.')

    def _update_folder_display(self):
        engine = self.worker.engine
        folder = self.selected_show_id and engine and engine.get_show_folder(
            self.selected_show_id)
        self.show_folder_edit.setText(folder or '')
        self.show_folder_clear_btn.setEnabled(bool(folder))

    def s_undo(self):
        self._busy(True)
        self.worker_call('undo', self.r_generic)

    def s_redo(self):
        self._busy(True)
        self.worker_call('redo', self.r_generic)

    def s_set_tags(self):
        show = self.worker.engine.get_show_info(self.selected_show_id)
        if 'my_tags' in show and show['my_tags']:
            tags = show['my_tags']
        else:
            tags = ''
        tags, ok = QInputDialog.getText(self, 'Edit Tags',
                                        'Enter desired tags (comma separated)',
                                        text=tags)
        if ok:
            self.s_edit_tags(show, tags)

    def s_edit_tags(self, show, tags):
        self._busy(True)
        self.worker_call('set_tags', self.r_generic, show['id'], tags)

    def s_play(self, play_next, episode=0):
        if self.selected_show_id:
            show = self.worker.engine.get_show_info(self.selected_show_id)

            # episode = 0 # Engine plays next unwatched episode
            if not play_next and not episode:
                episode = self.show_progress.value()

            self._busy(True)
            self.worker_call('play_episode', self.r_play_episode, show, episode)

    def s_play_random(self):
        self._busy(True)
        self.worker_call('play_random', self.r_play_episode)

    def s_play_show_episode(self, show_id, episode):
        # Like s_play, but for an explicit show rather than the one
        # selected in the list (used by Taiga mode's Now Playing screen,
        # whose show usually isn't the current list selection).
        show = self.worker.engine.get_show_info(show_id)
        self._busy(True)
        self.worker_call('play_episode', self.r_play_episode, show, episode)

    def _add_taiga_page(self, key, label, icon_name, widget):
        """Register a Taiga-mode nav destination backed by a page in
        self.content_stack. Later phases (Seasons, Statistics) add more
        pages through this same method."""
        self.content_stack.addWidget(widget)
        self._taiga_pages[key] = widget
        item = QListWidgetItem(getIcon(icon_name), label)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
        self.taiga_nav.addItem(item)

    def _add_taiga_separator(self):
        """A non-selectable spacer row, matching real Taiga's grouping
        of its sidebar into Now Playing / list-management / discovery
        sections."""
        item = QListWidgetItem()
        item.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
        item.setSizeHint(QtCore.QSize(0, 8))
        self.taiga_nav.addItem(item)

    # Pages where the "currently selected show" info panel makes sense.
    # Real Taiga's Anime List is a full-width table with no such panel
    # at all -- that quick-info sidebar is a trackma/hakubun-only
    # concept, not something Taiga mode should keep just because
    # classic mode has it. Now Playing already shows a full-size version
    # of the same info via now_playing_widget, and pages like Seasons
    # have no selected-show relationship at all. taiga_info_panel is
    # still built (its child widgets, e.g. taiga_edit_widget, are reused
    # by the Details dialog's Edit tab) -- it's just never shown.
    _TAIGA_INFO_PANEL_PAGES = frozenset()

    def _set_taiga_page(self, key):
        """Single entry point for switching Taiga-mode pages, so the
        nav column's selection and the content stack's current widget
        never desync as more pages get added."""
        widget = self._taiga_pages.get(key)
        if widget is None:
            return
        self.content_stack.setCurrentWidget(widget)
        self.taiga_info_panel.setVisible(key in self._TAIGA_INFO_PANEL_PAGES)
        for i in range(self.taiga_nav.count()):
            item = self.taiga_nav.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == key:
                self.taiga_nav.blockSignals(True)
                self.taiga_nav.setCurrentItem(item)
                self.taiga_nav.blockSignals(False)
                break

    def _on_taiga_nav_changed(self, current, previous):
        if current is None:
            return
        self._set_taiga_page(current.data(QtCore.Qt.ItemDataRole.UserRole))

    def s_play_number(self):
        if self.selected_show_id:
            show = self.worker.engine.get_show_info(self.selected_show_id)
            ep_default = 1
            ep_min = 1
            ep_max = utils.estimate_aired_episodes(show)
            if not ep_max:
                # If we don't know the total just allow anything
                ep_max = show['total'] or 10000

            episode, ok = QInputDialog.getInt(self, 'Play Episode',
                                              'Enter an episode number of %s to play:' % show['title'],
                                              ep_default, ep_min, ep_max)

            if ok:
                self.s_play(False, episode)

    def s_play_ep_number(self, action, number):
        return lambda: [action.setIcon(self.ep_icons['part']), self.s_play(False, number)]

    def s_delete(self):
        if self.selected_show_id:
            show = self.worker.engine.get_show_info(self.selected_show_id)
            reply = QMessageBox.question(self, 'Confirmation',
                                         'Are you sure you want to delete %s?' % show['title'],
                                         QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No)

            if reply == QMessageBox.StandardButton.Yes:
                self.worker_call('delete_show', self.r_generic, show)

    def s_scan_library(self):
        self.worker_call('scan_library', self.r_library_scanned, rescan=False)

    def s_rescan_library(self):
        self.worker_call('scan_library', self.r_library_scanned, rescan=True)

    def s_altname(self):
        show = self.worker.engine.get_show_info(self.selected_show_id)
        current_altname = self.worker.engine.altname(self.selected_show_id)

        new_altname, ok = QInputDialog.getText(self, 'Alternative title',
                                               'Set the new alternative title for %s (blank to remove):' % show[
                                                   'title'],
                                               text=current_altname)

        if ok:
            self.worker.engine.altname(self.selected_show_id, str(new_altname))
            self.ws_changed_show(show, altname=new_altname)

    def s_search_online(self, url_template):
        if not self.selected_show_id:
            return
        show = self.worker.engine.get_show_info(self.selected_show_id)
        title = urllib.parse.quote(show['title'])
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url_template.format(title=title)))

    def s_open_folder(self):
        try:
            self.worker.engine.open_show_folder(self.selected_show_id)
        except utils.EngineError as e:
            self.error(e.args[0])

    def s_set_folder(self):
        if not self.selected_show_id:
            return
        current = self.worker.engine.get_show_folder(self.selected_show_id)
        folder = QFileDialog.getExistingDirectory(
            self, 'Select folder', current or os.path.expanduser('~'))
        if not folder:
            return
        # No _update_folder_label() here: set_show_folder runs on the
        # worker thread, so the pin may not be written yet. The Edit tab
        # only exists inside the details dialog, and s_show_details
        # refreshes the row on the way in -- which is always after this.
        self.worker_call('set_show_folder', self.r_library_scanned,
                         self.selected_show_id, folder)

    def s_clear_folder(self):
        if not self.selected_show_id:
            return
        self.worker.engine.unset_show_folder(self.selected_show_id)
        self._update_folder_label()
        self.status('Folder cleared.')

    def _update_folder_actions(self):
        has_folder = bool(self.selected_show_id and self.worker.engine.get_show_folder(
            self.selected_show_id))
        self.action_clear_folder.setEnabled(has_folder)
        self.action_set_folder.setText(
            'Change folder...' if has_folder else 'Set folder...')

    def _apply_subminer_state(self):
        # Visibility (Settings > User Interface) is independent of
        # whether the engine has finished loading, but the checked
        # state/text mirror the 'use_subminer' config value, which does
        # need a loaded engine -- called from r_engine_loaded and
        # _update_config, not at __init__ time.
        self.action_use_subminer.setVisible(
            self.config['show_subminer_toggle'])
        if not self.action_use_subminer.isEnabled():
            return
        checked = bool(self.worker.engine.get_config('use_subminer'))
        self.action_use_subminer.blockSignals(True)
        self.action_use_subminer.setChecked(checked)
        self.action_use_subminer.blockSignals(False)
        self._set_subminer_text(checked)

    def _set_subminer_text(self, checked):
        self.action_use_subminer.setText(
            'SubMiner: On' if checked else 'SubMiner: Off')

    def s_toggle_subminer(self, checked):
        self.worker.engine.set_config('use_subminer', checked)
        self.worker.engine.save_config()
        self._set_subminer_text(checked)

    def _update_folder_label(self):
        """Keep the Taiga Edit tab's Folder row in step with the pin.
        Only exists in Taiga mode -- everywhere else the folder shows up
        in the details view alone (see engine.get_show_details)."""
        # getattr: _select_show can fire before the Taiga branch of the
        # sidebar build has created this label.
        label = getattr(self, 'taiga_folder_label', None)
        if label is None:
            return
        folder = self.worker.engine.get_show_folder(self.selected_show_id) \
            if self.selected_show_id else None
        label.setText(folder or 'Not set')
        label.setToolTip(folder or '')

    def s_retrieve(self, result=None):
        # `result` present because this is also used as a worker_call
        # callback (the upload-then-retrieve chain). The old signal
        # dispatch silently dropped extra arguments for zero-arg slots;
        # the direct dispatch does not.
        queue = self.worker.engine.get_queue()

        if queue:
            reply = QMessageBox.question(self, 'Confirmation',
                                         'There are %d unsynced changes. Do you want to send them first? (Choosing No will discard them!)' % len(
                                             queue),
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)

            if reply == QMessageBox.StandardButton.Yes:
                self.s_send(True)
            elif reply == QMessageBox.StandardButton.No:
                self._busy(True)
                self.worker_call('list_download', self.r_list_retrieved)
        else:
            self._busy(True)
            self.worker_call('list_download', self.r_list_retrieved)

    def s_send(self, retrieve=False):
        self._busy(True)
        if retrieve:
            self.worker_call('list_upload', self.s_retrieve)
        else:
            self.worker_call('list_upload', self.r_generic_ready)

    def s_switch_account(self):
        if not self.accountman_widget:
            self.accountman_create()
        else:
            self.accountman_widget.rebuild()

        self.accountman_widget.setModal(True)
        self.accountman_widget.show()

    def s_show_image(self, filename):
        self.show_image.setPixmap(QtGui.QPixmap(filename))

    def s_show_details(self):
        if not self.selected_show_id:
            return

        show = self.worker.engine.get_show_info(self.selected_show_id)

        self._update_folder_label()
        edit_widget = self.taiga_edit_widget if self._taiga_mode else None
        self.detailswindow = DetailsDialog(
            None, self.worker, show, edit_widget=edit_widget)
        self.detailswindow.setModal(True)
        self.detailswindow.show()

    def s_add(self, query=None):
        current_status = self.notebook.tabData(self.notebook.currentIndex())

        self.addwindow = AddDialog(
            None, self.worker, current_status, default=query or None)
        self.addwindow.goToRequested.connect(self.s_go_to_show)
        self.addwindow.setModal(True)
        self.addwindow.show()
        if query:
            self.addwindow.s_search()

    def s_go_to_show(self, showid):
        show = self.worker.engine.get_show_info(showid)
        for i in range(self.notebook.count() - 1):  # exclude the All tab
            if self.notebook.tabData(i) == show['my_status']:
                self.notebook.setCurrentIndex(i)
                break

        source_model = self.view.model().sourceModel()
        if showid not in source_model.id_map:
            return
        source_index = source_model.index(source_model.id_map[showid], 0)
        proxy_index = self.view.model().mapFromSource(source_index)
        if proxy_index.isValid():
            self.view.setCurrentIndex(proxy_index)
            self.view.scrollTo(proxy_index)

    def s_airing_schedule(self):
        self.airingwindow = AiringScheduleDialog(None, self.worker)
        self.airingwindow.setModal(True)
        self.airingwindow.show()

    def _get_syncwindow(self):
        """The one SyncWindow instance: reused (and its already-fetched
        plan preserved) across both the manual 'Multi-provider Sync...'
        entry and the toolbar button's headless attempt -- but only
        for as long as the loaded account's media type doesn't change.
        Anime and manga use separate multisync databases (independent
        provider id spaces -- see SyncWindow.__init__), so switching
        media type (Settings, then reloading the account) must open a
        fresh window against the OTHER database, never keep reusing
        the stale one."""
        from hakubun.ui.qt.syncwindow import SyncWindow
        media_type = self.worker.engine.data_handler.userconfig.get(
            'mediatype') or 'anime'
        existing = getattr(self, 'syncwindow', None)
        if existing is not None and getattr(existing, '_closed', False):
            # The user closed it (store already closed with it); never
            # reuse a dead window -- that would operate on a closed
            # database. Rebuild fresh.
            self.syncwindow = existing = None
        # The signed-in account is the app's editing surface; the sync
        # engine treats its changes as local intent (the "primary
        # fold"). Switching accounts therefore changes what 'local'
        # MEANS -- a window built for the old account would keep folding
        # the wrong tracker's edits in and label conflicts with the
        # wrong platform -- so it is rebuilt, exactly like a media-type
        # change.
        active_api = (self.account or {}).get('api') \
            if getattr(self, 'account', None) else None
        if existing is not None and (existing.media_type != media_type
                                     or existing.active_api != active_api):
            existing.close()
            self.syncwindow = existing = None
        if existing is None:
            self.syncwindow = SyncWindow(None, self.accountman,
                                         active_api=active_api,
                                         media_type=media_type)
        return self.syncwindow

    def s_multisync(self):
        win = self._get_syncwindow()
        win.show()
        win.raise_()
        win.activateWindow()

    def s_sync_button(self):
        """The toolbar/menu Sync action. Classic single-account sync
        when multi-sync is disabled in Settings > Behavior; otherwise
        fetch+plan happens off the GUI thread and, if there's work, the
        Sync window is surfaced to apply it WITH its progress bar and
        Cancel button -- never behind a disabled main window (that was
        the multi-minute 'hang')."""
        if not self.config['multisync_enabled']:
            self.s_send(True)
            return

        win = self._get_syncwindow()
        if not win.engine.adapters:
            detail = ('; '.join(win._adapter_errors)
                      if win._adapter_errors else
                      'no provider accounts could be loaded')
            self.error('Multi-sync: %s.' % detail)
            return
        if win.is_busy():
            # Never drop the click invisibly (the window's own guard
            # only updates its -- possibly hidden -- status label):
            # surface the window so its progress/status is visible.
            self._surface_syncwindow(win)
            self.status('Multi-sync: an operation is already running '
                        '(see the sync window).')
            return

        (mode, plan_only) = present.settings_sync_mode(self.config)
        idx = win.mode_combo.findData(mode)
        if idx >= 0:
            win.mode_combo.setCurrentIndex(idx)

        # No self._busy(): the main window stays usable while the sync
        # runs on the window's worker thread.
        self.status('Multi-syncing (%s%s)...' % (
            mode.name.lower(), ', review' if plan_only else ''))
        win._run(win._fetch_and_plan,
                 lambda plan, error: self._r_multisync_planned(
                     win, plan, error),
                 'Fetching provider lists...')

    def _surface_syncwindow(self, win):
        win.show()
        win.raise_()
        win.activateWindow()

    def _r_multisync_planned(self, win, plan, error):
        # `win` is captured from the call that started the fetch --
        # never re-resolved via _get_syncwindow(), which can close the
        # old window and build a fresh one against the OTHER media
        # type's database. If the loaded account's media type changed
        # mid-fetch, this window is no longer current: discard the
        # stale plan rather than rendering/applying it against the
        # wrong database.
        if error is not None:
            self.error('Multi-sync failed: %s' % error)
            return
        media_type = self.worker.engine.data_handler.userconfig.get(
            'mediatype') or 'anime'
        active_api = (self.account or {}).get('api') \
            if getattr(self, 'account', None) else None
        if win is not getattr(self, 'syncwindow', None) \
                or win.media_type != media_type \
                or win.active_api != active_api:
            self.status('Multi-sync: the loaded account changed '
                        'mid-sync; results discarded. Sync again.')
            return
        # Render this exact plan into the (still hidden) window.
        win.r_planned(plan, None)
        if plan.conflicts:
            self._surface_syncwindow(win)
            self.status('Multi-sync needs your decision on %d '
                       'conflict(s).' % len(plan.conflicts))
            return
        (_mode, plan_only) = present.settings_sync_mode(self.config)
        if plan_only:
            # Checked "Fetch & plan only": the point of the setting is
            # seeing the plan, so surface the window even when the plan
            # is empty -- reporting "already in sync" into the status
            # bar and leaving the window shut made the setting look like
            # it did nothing at all.
            self._surface_syncwindow(win)
            if not plan.changes:
                self.status('Multi-sync: already in sync.')
            else:
                self.status('Multi-sync: %d change(s) planned -- review '
                            'and apply from the sync window.'
                            % len(plan.changes))
            return
        if not plan.changes:
            self.status('Multi-sync: already in sync.')
            return
        if any(c.first_sync for c in plan.changes):
            # First contact with at least one tracker for some field:
            # nothing has changed since a shared base because there IS
            # no shared base, so the "winner" is just whichever list was
            # read first. Those rows are planned unticked; a headless
            # Sync must never apply them on the user's behalf.
            self._surface_syncwindow(win)
            self.status('Multi-sync: first sync for some fields -- '
                        'review what would be overwritten before '
                        'applying.')
            return
        # Clean changes: apply IN the window so its progress bar, log
        # and Cancel button are visible, and the main window is free.
        self._surface_syncwindow(win)
        self.status('Multi-sync: applying %d change(s) -- see the sync '
                    'window.' % len(plan.changes))
        win.s_apply()

    def s_mediatype(self, action):
        index = action.data()
        if index is not None:
            mediatype = self.api_info['supported_mediatypes'][index]
            self.reload(None, mediatype)

    def s_settings(self):
        dialog = SettingsDialog(
            None, self.worker, self.config, self.configfile)
        dialog.saved.connect(self._update_config)
        dialog.exec()

    def s_about(self):
        # The window/taskbar icon is the plain hanko mark (see the
        # QIcon set on QApplication near the top of __init__) -- About
        # gets the fuller "Hakubun+" wordmark instead, since it has the
        # room to show it.
        version = '%s-%s' % (utils.VERSION, DEV_BUILD_ID)
        QMessageBox.about(self, 'About %s %s' % (self.app_name, version),
                          ('<p align="center"><img src="%s" width="128" height="128"></p>'
                          '<p><b>About %s %s</b></p><p>Hakubun+ is an open source client for media tracking websites, an independent fork of Trackma.</p>'
                          '<p>This program is licensed under the GPLv3, for more information read COPYING file.</p>'
                          '<p>Thanks to all contributors. To see all contributors see AUTHORS file.</p>'
                          '<p>Filename parsing uses <a href="https://github.com/igorcmoura/anitopy">Anitopy</a> and '
                          '<a href="https://github.com/tylergibbs2/anitomy-ng">anitomy-ng</a>, '
                          'both licensed under the Mozilla Public License 2.0.</p>'
                          '<p>Optional fuzzy title matching uses '
                          '<a href="https://github.com/rapidfuzz/RapidFuzz">RapidFuzz</a>, '
                          'licensed under the MIT license.</p>'
                          '<p>Copyright (C) z411</p>'
                          '<p><a href="https://github.com/trektn/hakubun-plus">https://github.com/trektn/hakubun-plus</a></p>') % (
                              utils.DATADIR + '/about_logo.png', self.app_name, version))

    def s_about_qt(self):
        QMessageBox.aboutQt(self, 'About Qt')

    def s_show_menu_columns(self, pos):
        globalPos = self.sender().mapToGlobal(pos)
        globalPos += QtCore.QPoint(3, 3)
        self.menu_columns.exec(globalPos)

    def s_toggle_column(self, visible):
        w = self.sender()
        index, column_name = w.data(), w.text()
        MIN_WIDTH = 30  # Width to restore columns to if too small to see

        if visible:
            if column_name not in self.api_config['visible_columns']:
                self.api_config['visible_columns'].append(str(column_name))
        else:
            if column_name in self.api_config['visible_columns']:
                self.api_config['visible_columns'].remove(column_name)

        self._save_config()

        self.view.setColumnHidden(index, not visible)
        if visible and self.view.columnWidth(index) < MIN_WIDTH:
            self.view.setColumnWidth(index, MIN_WIDTH)

    # Worker slots
    def ws_changed_status(self, classname, msgtype, msg):
        if msgtype != messenger.TYPE_DEBUG:
            self.status('{}: {}'.format(classname, msg))
        elif self.debug:
            print('[D] {}: {}'.format(classname, msg))

    def ws_changed_show(self, show, is_playing=False, episode=None, altname=None):
        if show:
            if not self.view:
                return  # List not built yet; can be safely avoided

            # Update the view of the updated show
            self.view.model().sourceModel().update(show['id'], is_playing)

            if show['id'] == self.selected_show_id:
                self._select_show(show)

            if is_playing and self.config['show_tray'] and self.config['notifications']:
                if episode == (show['my_progress'] + 1):
                    tracker_status = self.worker.engine.tracker_status() or {}
                    prefix = "Playing %s (%s / %s). " % (
                        show['title'], episode, show['total'] or '?')

                    # The "will update in N seconds" countdown measures
                    # wall-clock time since the file was detected, not how
                    # much of the episode you've actually watched. When the
                    # backend reports a real playback position that's
                    # already past the update threshold (e.g. resuming a
                    # nearly-finished episode), quoting that countdown is
                    # misleading -- you've effectively watched it already.
                    offset = tracker_status.get('viewOffset')
                    length = tracker_status.get('length')
                    pct = self.worker.engine.get_config('tracker_update_percentage')
                    if length and offset is not None and (offset / length) * 100 >= pct:
                        body = prefix + "Already watched -- will update on completion."
                    else:
                        delay = tracker_status.get('wait_s') or \
                            self.worker.engine.get_config('tracker_update_wait_s')
                        body = prefix + "Will update in %d seconds." % delay

                    self.tray.showMessage('Hakubun+ Tracker', body)

    def ws_changed_show_status(self, show, old_status=None):
        # Update the view of the new show
        self.view.model().sourceModel().update(show['id'])

        # Update counts
        self.counts[show['my_status']] += 1
        self.counts[old_status] -= 1
        self._update_counts()

        # Set notebook to the new page
        self.notebook.setCurrentIndex(
            self.mediainfo['statuses'].index(show['my_status']))
        # Refresh filter
        self.s_filter_changed()

    def ws_changed_list(self, show):
        self._rebuild_view()
        self._recalculate_counts()
        self.s_filter_changed()

    def ws_changed_queue(self, queue):
        self._update_queue_counter(queue)

    def ws_tracker_state(self, status):
        self._update_tracker_info(status)
        if self._taiga_mode:
            self.now_playing_widget.update_status(status)
        else:
            self._update_now_playing_sidebar(status)

    def _update_now_playing_sidebar(self, status):
        state = status['state']

        # IGNORED means the tracker recognized the show/episode just fine
        # but isn't going to update progress for it (e.g. replaying an
        # already-watched episode from the player's own playlist) -- it
        # should still show up here, just labeled differently, rather
        # than the section disappearing as if nothing were playing.
        if state in (utils.Tracker.PLAYING, utils.Tracker.IGNORED) and status.get('show'):
            show, episode = status['show']
            self._now_playing_show = show
            self._now_playing_episode = episode
            if state == utils.Tracker.IGNORED:
                self.now_playing_status.setText(
                    '%s -- Episode %d (already watched)' % (show['title'], episode))
            else:
                self.now_playing_status.setText(
                    '%s -- Episode %d' % (show['title'], episode))
            self.now_playing_bar.update_status(status)
            self.now_playing_position.setText(
                utils.format_playback_position(
                    status.get('viewOffset'), status.get('length'),
                    include_percent=False))
            self.now_playing_last_btn.setEnabled(episode > 1)
            self.now_playing_group.show()
        else:
            self._now_playing_show = None
            self._now_playing_episode = None
            self.now_playing_group.hide()

    def _now_playing_play(self, delta):
        if self._now_playing_show and self._now_playing_episode:
            episode = self._now_playing_episode + delta
            if episode >= 1:
                self.s_play_show_episode(self._now_playing_show['id'], episode)

    def ws_undo_stack_changed(self):
        self.action_undo.setEnabled(self.worker.engine.can_undo())
        self.action_redo.setEnabled(self.worker.engine.can_redo())

    def ws_prompt_update(self, show, episode):
        box = QMessageBox(self)
        box.setWindowTitle("Update prompt")
        box.setText(f"Do you want to update {show['title']} to {episode}?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        box.setModal(False)
        box.accepted.connect(lambda:
                self.worker_call('set_episode', self.r_generic,
                show['id'], episode))
        box.show()

    def ws_prompt_add(self, show, episode):
        page = self.notebook.currentIndex()
        current_status = self.mediainfo['statuses'][page]

        addwindow = AddDialog(
            None, self.worker, current_status, default=show['title'])
        addwindow.setModal(True)
        if addwindow.exec():
            self.worker_call('set_episode', self.r_generic,
                             addwindow.selected_show['id'], episode)

    # Responses from the engine thread
    def r_generic(self, result=None):
        self._unbusy()

    def r_generic_ready(self, result=None):
        self._unbusy()
        self.status('Ready.')

    def r_engine_loaded(self, result):
        if result['success']:
            # The status bar already shows the underlying warning, but it's
            # transient and gets overwritten within the same startup
            # sequence -- easy to miss even when printed to console. Show
            # it once, durably, as an actual dialog.
            parser_warning = self.worker.engine.parser_fallback_warning
            if parser_warning and not self._parser_fallback_shown:
                self._parser_fallback_shown = True
                QMessageBox.warning(self, 'Title parser fallback', parser_warning)

            showlist = self.worker.engine.get_list()
            altnames = self.worker.engine.altnames()
            library = self.worker.engine.library()

            # Set globals
            self.api_info = self.worker.engine.api_info
            self.mediainfo = self.worker.engine.mediainfo

            # Rebuild statuses
            self._rebuild_statuses()

            # Build mediatype menu
            for action in self.mediatype_actiongroup.actions():
                self.mediatype_actiongroup.removeAction(action)

            for n, mediatype in enumerate(self.api_info['supported_mediatypes']):
                action = QAction(mediatype, self, checkable=True)
                if mediatype == self.api_info['mediatype']:
                    action.setChecked(True)
                else:
                    action.setData(n)
                self.mediatype_actiongroup.addAction(action)
                self.menu_mediatype.addAction(action)

            # Show API info
            self.api_icon.setPixmap(QtGui.QPixmap(
                utils.available_libs[self.account['api']][1]))
            if self.config['tray_api_icon']:
                self.tray.setIcon(QtGui.QIcon(
                    utils.available_libs[self.account['api']][1]))
            self.api_user.setText(
                self.worker.engine.get_userconfig('username'))
            self.setWindowTitle("%s %s [%s (%s)]" % (
                self.app_name, utils.VERSION, self.api_info['name'], self.api_info['mediatype']))

            if self._taiga_mode:
                self._rebuild_services_menu()
                self._rebuild_library_folders_menu()
                self.seasons_widget.set_context(self.api_info, self.mediainfo)
                self.search_widget.set_context(self.api_info, self.mediainfo)
                self.stats_widget.refresh()
                self.show_filter.setPlaceholderText(
                    'Filter list or search %s' % self.api_info['name'])

            self._apply_subminer_state()

            # Show tracker info
            tracker_info = self.worker.engine.tracker_status()
            if tracker_info:
                self._update_tracker_info(tracker_info)

            # Build our main view and show total counts
            self._rebuild_view()
            self._init_view()
            self._recalculate_counts()

            self.s_show_selected(None)

            self.status('Ready.')

        self._unbusy()

    def r_list_retrieved(self, result):
        if result['success']:
            self._rebuild_view()
            self._recalculate_counts()

            self.status('Ready.')

        self._unbusy()

    def r_library_scanned(self, result):
        if result['success']:
            self._rebuild_view()

            self.status('Ready.')

        self._unbusy()

    def r_engine_unloaded(self, result):
        if result['success']:
            self.close()
            if not self.finish:
                self.s_switch_account()

    def r_play_episode(self, result):
        if result['success']:
            args = result['result']
            if len(args) > 1:
                # QtCore.QProcess.startDetached(args[0], args[1:])
                process = QtCore.QProcess()
                for attr in ['setStandardErrorFile', 'setStandardOutputFile']:
                    getattr(process, attr)(QtCore.QProcess.nullDevice())
                process.setProgram(args[0])
                process.setArguments(args[1:])
                process.startDetached()

        self._unbusy()
