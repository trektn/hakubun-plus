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

import subprocess
import sys

from PyQt6 import QtCore
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                             QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QRadioButton,
                             QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from hakubun import i18n
from hakubun import utils
from hakubun.sync import present
from hakubun.ui.qt.delegates import ShowsTableDelegate
from hakubun.ui.qt.themedcolorpicker import ThemedColorPicker
from hakubun.ui.qt.util import FilterBar, getColor, getIcon


class SettingsDialog(QDialog):
    worker = None
    config = None
    configfile = None

    saved = QtCore.pyqtSignal()

    def __init__(self, parent, worker, config, configfile):
        QDialog.__init__(self, parent)

        self.worker = worker
        self.config = config
        self.configfile = configfile
        self.setStyleSheet("QGroupBox { font-weight: bold; } ")
        self.setWindowTitle(_('Settings'))
        layout = QGridLayout()

        # Categories
        self.category_list = QListWidget()
        category_media = QListWidgetItem(
            getIcon('media-playback-start'), _('Media'), self.category_list)
        category_library = QListWidgetItem(
            getIcon('folder'), _('Library'), self.category_list)
        category_sync = QListWidgetItem(
            getIcon('view-refresh'), _('Sync'), self.category_list)
        category_behavior = QListWidgetItem(
            getIcon('preferences-system'), _('Behavior'), self.category_list)
        category_ui = QListWidgetItem(
            getIcon('window-new'), _('User Interface'), self.category_list)
        category_theme = QListWidgetItem(
            getIcon('applications-graphics'), _('Theme'), self.category_list)
        self.category_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.category_list.setCurrentRow(0)
        self.category_list.setMaximumWidth(
            self.category_list.sizeHintForColumn(0) + 15)
        self.category_list.setFocus()
        self.category_list.currentItemChanged.connect(self.s_switch_page)

        # Media tab
        page_media = QWidget()
        page_media_layout = QVBoxLayout()
        page_media_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        # Group: Media settings
        g_media = QGroupBox(_('Media settings'))
        g_media.setFlat(True)
        g_media_layout = QFormLayout()
        self.tracker_enabled = QCheckBox()
        self.tracker_enabled.toggled.connect(self.tracker_type_change)

        self.tracker_type = QComboBox()
        for (n, label) in utils.available_trackers:
            self.tracker_type.addItem(label, n)
        self.tracker_type.currentIndexChanged.connect(self.tracker_type_change)

        self.tracker_interval = QSpinBox()
        self.tracker_interval.setRange(5, 1000)
        self.tracker_interval.setMaximumWidth(60)
        self.tracker_process = QLineEdit()
        self.tracker_update_wait = QSpinBox()
        self.tracker_update_wait.setRange(0, 1000)
        self.tracker_update_wait.setMaximumWidth(60)
        self.tracker_update_close = QCheckBox()

        self.mpris_update_mode = QComboBox()
        self.mpris_update_mode.addItem(_('Fixed wait (above)'), True)
        self.mpris_update_mode.addItem(_('Percentage complete'), False)
        self.mpris_update_mode.setToolTip(_(
            "How the tracker decides an episode counts as watched: "
            "after the fixed wait above, or once playback reaches a "
            "percentage of the episode's actual duration. The latter "
            "needs the player to report its length over MPRIS, so it "
            "only applies when Tracker type is MPRIS."))
        self.mpris_update_mode.currentIndexChanged.connect(
            self.s_mpris_update_mode)

        self.tracker_update_percentage = QSpinBox()
        self.tracker_update_percentage.setRange(1, 100)
        self.tracker_update_percentage.setMaximumWidth(60)
        self.tracker_update_percentage.setSuffix('%')

        self.tracker_update_prompt = QCheckBox()
        self.tracker_not_found_prompt = QCheckBox()

        self.streaming_obey_wait = QCheckBox()
        self.streaming_obey_wait.setToolTip(_(
            "Plex and Kodi normally estimate when an episode is \"watched\" "
            "from playback position instead of a fixed timer. Enable this "
            "to make the currently selected one (Plex or Kodi) use the "
            "fixed \"Wait before updating\" time above instead. Has no "
            "effect for other tracker types."))
        self.streaming_obey_wait.toggled.connect(self.s_streaming_obey_wait)
        self._plex_obey_wait = False
        self._kodi_obey_wait = False

        g_media_layout.addRow(_('Enable tracker'), self.tracker_enabled)
        g_media_layout.addRow(_('Tracker type'), self.tracker_type)
        g_media_layout.addRow(
            _('Tracker interval (seconds)'), self.tracker_interval)
        g_media_layout.addRow(_('Process name (regex)'), self.tracker_process)
        g_media_layout.addRow(
            _('Wait before updating (seconds)'), self.tracker_update_wait)
        g_media_layout.addRow(
            _('Use this wait time for Plex/Kodi trackers'),
            self.streaming_obey_wait)
        g_media_layout.addRow(
            _('Update when (MPRIS)'), self.mpris_update_mode)
        g_media_layout.addRow(
            _('Completion percentage (MPRIS)'), self.tracker_update_percentage)
        g_media_layout.addRow(
            _('Wait until the player is closed'), self.tracker_update_close)
        g_media_layout.addRow(_('Ask before updating'),
                              self.tracker_update_prompt)
        g_media_layout.addRow(_('Ask to add new shows'),
                              self.tracker_not_found_prompt)

        g_media.setLayout(g_media_layout)

        # Group: Jellyfin settings
        g_jellyfin = QGroupBox(_('Jellyfin'))
        g_jellyfin.setFlat(True)
        self.jellyfin_host = QLineEdit()
        self.jellyfin_port = QLineEdit()
        self.jellyfin_user = QLineEdit()
        self.jellyfin_api_key = QLineEdit()

        g_jellyfin_layout = QGridLayout()
        g_jellyfin_layout.addWidget(
            QLabel(_('Host and Port')),                   0, 0, 1, 1)
        g_jellyfin_layout.addWidget(self.jellyfin_host,
                                0, 1, 1, 1)
        g_jellyfin_layout.addWidget(self.jellyfin_port,
                                0, 2, 1, 2)
        g_jellyfin_layout.addWidget(
            QLabel(_('API and User')),                   1, 0, 1, 1)
        g_jellyfin_layout.addWidget(self.jellyfin_api_key,
                                1, 1, 1, 1)
        g_jellyfin_layout.addWidget(self.jellyfin_user,
                                1, 2, 1, 2)

        g_jellyfin.setLayout(g_jellyfin_layout)

        # Group: Kodi settings
        g_kodi = QGroupBox(_('Kodi'))
        g_kodi.setFlat(True)
        self.kodi_host = QLineEdit()
        self.kodi_port = QLineEdit()
        self.kodi_user = QLineEdit()
        self.kodi_passw = QLineEdit()
        self.kodi_passw.setEchoMode(QLineEdit.EchoMode.Password)

        g_kodi_layout = QGridLayout()
        g_kodi_layout.addWidget(
            QLabel(_('Host and Port')),                   0, 0, 1, 1)
        g_kodi_layout.addWidget(self.kodi_host,
                                0, 1, 1, 1)
        g_kodi_layout.addWidget(self.kodi_port,
                                0, 2, 1, 2)
        g_kodi_layout.addWidget(QLabel(_('Kodi login')),   1, 0, 1, 1)
        g_kodi_layout.addWidget(self.kodi_user,
                                1, 1, 1, 1)
        g_kodi_layout.addWidget(self.kodi_passw,
                                1, 2, 1, 2)

        g_kodi.setLayout(g_kodi_layout)

        # Group: Plex settings
        g_plex = QGroupBox(_('Plex Media Server'))
        g_plex.setFlat(True)
        self.plex_host = QLineEdit()
        self.plex_port = QLineEdit()
        self.plex_user = QLineEdit()
        self.plex_passw = QLineEdit()
        self.plex_passw.setEchoMode(QLineEdit.EchoMode.Password)
        self.plex_ssl = QCheckBox()

        g_plex_layout = QGridLayout()
        g_plex_layout.addWidget(
            QLabel(_('Host and Port')),                   0, 0, 1, 1)
        g_plex_layout.addWidget(self.plex_host,
                                0, 1, 1, 1)
        g_plex_layout.addWidget(self.plex_port,
                                0, 2, 1, 2)
        g_plex_layout.addWidget(
            QLabel(_('Use SSL')), 1, 0, 1, 1)
        g_plex_layout.addWidget(self.plex_ssl,
                                1, 2, 1, 1)
        g_plex_layout.addWidget(
            QLabel(_('myPlex login (claimed server)')),   2, 0, 1, 1)
        g_plex_layout.addWidget(self.plex_user,
                                2, 1, 1, 1)
        g_plex_layout.addWidget(self.plex_passw,
                                2, 2, 1, 2)

        g_plex.setLayout(g_plex_layout)

        # Streaming Credentials header -- Jellyfin/Kodi/Plex share a tracker
        # type selector above and only one is ever active at a time, so
        # their login fields are grouped together at the bottom instead of
        # being interleaved with the general tracker options.
        lbl_streaming_credentials = QLabel(
            '<b style="font-size: 12pt;">%s</b>' % _('Streaming Credentials'))

        # Media form
        page_media_layout.addWidget(g_media)
        page_media_layout.addWidget(lbl_streaming_credentials)
        page_media_layout.addWidget(g_jellyfin)
        page_media_layout.addWidget(g_kodi)
        page_media_layout.addWidget(g_plex)
        page_media.setLayout(page_media_layout)

        # Library tab
        page_library = QWidget()
        page_library_layout = QVBoxLayout()
        page_library_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        # Group: Library
        g_playnext = QGroupBox(_('Library'))
        g_playnext.setFlat(True)
        self.title_parser = QComboBox()
        for (n, label) in utils.available_parsers:
            self.title_parser.addItem(label, n)
        self.player = QLineEdit()
        self.player_browse = QPushButton(_('Browse...'))
        self.player_browse.clicked.connect(self.s_player_browse)
        self.player_reuse_mpv = QCheckBox()
        self.player_reuse_mpv.setToolTip(_(
            'Only applies when the player above is mpv. Hands off playback '
            'to the already-running mpv window via its IPC socket instead '
            'of starting a new process each time.'))
        lbl_searchdirs = QLabel(_('Media directories'))
        lbl_searchdirs.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.searchdirs = QListWidget()
        self.searchdirs_add = QPushButton(_('Add...'))
        self.searchdirs_add.clicked.connect(self.s_searchdirs_add)
        self.searchdirs_remove = QPushButton(_('Remove'))
        self.searchdirs_remove.clicked.connect(self.s_searchdirs_remove)
        self.searchdirs_buttons = QVBoxLayout()
        self.searchdirs_buttons.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.searchdirs_buttons.addWidget(self.searchdirs_add)
        self.searchdirs_buttons.addWidget(self.searchdirs_remove)
        self.searchdirs_buttons.addWidget(QSplitter())
        self.library_autoscan = QCheckBox()
        self.scan_whole_list = QCheckBox()
        self.scan_whole_list.setToolTip(_(
            "When library scanning matches your media directories against "
            "the list, only Watching/Plan to Watch shows are considered by "
            "default. Enable this to also match against Completed, "
            "Dropped, and other statuses."))
        self.library_full_path = QCheckBox()

        g_playnext_layout = QGridLayout()
        g_playnext_layout.addWidget(
            QLabel(_('Parser')),                    0, 0, 1, 1)
        g_playnext_layout.addWidget(
            self.title_parser,                   0, 1, 1, 1)
        g_playnext_layout.addWidget(
            QLabel(_('Player')),                    1, 0, 1, 1)
        g_playnext_layout.addWidget(self.player,
                                                 1, 1, 1, 1)
        g_playnext_layout.addWidget(
            self.player_browse,                  1, 2, 1, 1)
        g_playnext_layout.addWidget(
            QLabel(_('Reuse existing mpv window')), 2, 0, 1, 2)
        g_playnext_layout.addWidget(
            self.player_reuse_mpv,               2, 2, 1, 1)
        g_playnext_layout.addWidget(
            lbl_searchdirs,                      3, 0, 1, 1)
        g_playnext_layout.addWidget(
            self.searchdirs,                     3, 1, 1, 1)
        g_playnext_layout.addLayout(
            self.searchdirs_buttons,             3, 2, 1, 1)
        g_playnext_layout.addWidget(
            QLabel(_('Rescan Library at startup')), 4, 0, 1, 2)
        g_playnext_layout.addWidget(
            self.library_autoscan,               4, 2, 1, 1)
        g_playnext_layout.addWidget(
            QLabel(_('Scan through whole list')),   5, 0, 1, 2)
        g_playnext_layout.addWidget(
            self.scan_whole_list,                5, 2, 1, 1)
        g_playnext_layout.addWidget(
            QLabel(_('Take subdirectory name into account')), 6, 0, 1, 2)
        g_playnext_layout.addWidget(
            self.library_full_path,              6, 2, 1, 1)

        g_playnext.setLayout(g_playnext_layout)

        page_library_layout.addWidget(g_playnext)
        page_library.setLayout(page_library_layout)

        # Sync tab
        page_sync = QWidget()
        page_sync_layout = QVBoxLayout()
        page_sync_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        # Group: Autoretrieve
        g_autoretrieve = QGroupBox(_('Autoretrieve'))
        g_autoretrieve.setFlat(True)
        self.autoretrieve_off = QRadioButton(_('Disabled'))
        self.autoretrieve_always = QRadioButton(_('Always at start'))
        self.autoretrieve_days = QRadioButton(_('After n days'))
        self.autoretrieve_days.toggled.connect(self.s_autoretrieve_days)
        self.autoretrieve_days_n = QSpinBox()
        self.autoretrieve_days_n.setRange(1, 100)
        g_autoretrieve_layout = QGridLayout()
        g_autoretrieve_layout.setColumnStretch(0, 1)
        g_autoretrieve_layout.addWidget(self.autoretrieve_off,    0, 0, 1, 1)
        g_autoretrieve_layout.addWidget(self.autoretrieve_always, 1, 0, 1, 1)
        g_autoretrieve_layout.addWidget(self.autoretrieve_days,   2, 0, 1, 1)
        g_autoretrieve_layout.addWidget(self.autoretrieve_days_n, 2, 1, 1, 1)
        g_autoretrieve.setLayout(g_autoretrieve_layout)

        # Group: Autosend
        g_autosend = QGroupBox(_('Autosend'))
        g_autosend.setFlat(True)
        self.autosend_off = QRadioButton(_('Disabled'))
        self.autosend_always = QRadioButton(_('Immediately after every change'))
        self.autosend_minutes = QRadioButton(_('After n minutes'))
        self.autosend_minutes.toggled.connect(self.s_autosend_minutes)
        self.autosend_minutes_n = QSpinBox()
        self.autosend_minutes_n.setRange(1, 1000)
        self.autosend_size = QRadioButton(_('After the queue reaches n items'))
        self.autosend_size.toggled.connect(self.s_autosend_size)
        self.autosend_size_n = QSpinBox()
        self.autosend_size_n.setRange(2, 20)
        self.autosend_at_exit = QCheckBox(_('At exit'))
        g_autosend_layout = QGridLayout()
        g_autosend_layout.setColumnStretch(0, 1)
        g_autosend_layout.addWidget(self.autosend_off,      0, 0, 1, 1)
        g_autosend_layout.addWidget(self.autosend_always,   1, 0, 1, 1)
        g_autosend_layout.addWidget(self.autosend_minutes,    2, 0, 1, 1)
        g_autosend_layout.addWidget(self.autosend_minutes_n,  2, 1, 1, 1)
        g_autosend_layout.addWidget(self.autosend_size,     3, 0, 1, 1)
        g_autosend_layout.addWidget(self.autosend_size_n,   3, 1, 1, 1)
        g_autosend_layout.addWidget(self.autosend_at_exit,  4, 0, 1, 1)
        g_autosend.setLayout(g_autosend_layout)

        # Group: Extra
        g_extra = QGroupBox(_('Additional options'))
        g_extra.setFlat(True)
        self.auto_status_change = QCheckBox(_('Change status automatically'))
        self.auto_status_change.setToolTip(_(
            "Moves a show to its start status (e.g. Watching) when you set "
            "episode 1, and to its finish status (e.g. Completed) when you "
            "set its last episode."))
        self.auto_status_change.toggled.connect(self.s_auto_status_change)
        self.auto_status_change_if_scored = QCheckBox(
            _('Change status automatically only if scored'))
        self.auto_status_change_if_scored.setToolTip(_(
            "Only applies to the finish-status change above: hold off "
            "moving the show to its finish status until it has a score, "
            "instead of doing it as soon as the last episode is set."))
        self.auto_date_change = QCheckBox(
            _('Change start and finish dates automatically'))
        g_extra_layout = QVBoxLayout()
        g_extra_layout.addWidget(self.auto_status_change)
        g_extra_layout.addWidget(self.auto_status_change_if_scored)
        g_extra_layout.addWidget(self.auto_date_change)
        g_extra.setLayout(g_extra_layout)

        # Sync layout
        page_sync_layout.addWidget(g_autoretrieve)
        page_sync_layout.addWidget(g_autosend)
        page_sync_layout.addWidget(g_extra)
        page_sync.setLayout(page_sync_layout)

        # Behavior tab
        page_behavior = QWidget()
        page_behavior_layout = QVBoxLayout()
        page_behavior_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        # Group: Adding shows
        g_add_dialog = QGroupBox(_('Adding shows'))
        g_add_dialog.setFlat(True)
        self.add_dialog_default_status = QComboBox()
        self.add_dialog_default_status.addItem(
            _('Currently active tab'), 'current')
        self.add_dialog_default_status.addItem(
            _('Plan to Watch'), 'start')
        g_add_dialog_layout = QFormLayout()
        g_add_dialog_layout.addRow(
            _('Default status when adding a show:'), self.add_dialog_default_status)
        g_add_dialog.setLayout(g_add_dialog_layout)

        page_behavior_layout.addWidget(g_add_dialog)

        # Group: Apply behavior
        g_apply = QGroupBox(_('Apply behavior'))
        g_apply.setFlat(True)
        self.sync_on_settings_apply = QCheckBox(
            _('Push a sync when applying settings'))
        self.sync_on_settings_apply.setToolTip(_(
            "Sends any queued changes to the API right after you click "
            "Apply/OK here, instead of waiting for the next scheduled or "
            "manual sync. This only pushes -- it doesn't also pull a fresh "
            "copy of the list from the API."))
        g_apply_layout = QVBoxLayout()
        g_apply_layout.addWidget(self.sync_on_settings_apply)
        g_apply.setLayout(g_apply_layout)

        page_behavior_layout.addWidget(g_apply)

        # Group: Sync
        g_sync = QGroupBox(_('Sync'))
        g_sync.setFlat(True)
        self.multisync_enabled = QCheckBox(
            _('Enable multi-sync (BETA)'))
        self.multisync_enabled.setToolTip(_(
            'BETA: Sync will now multi-sync across every configured '
            'provider by default, reconciling them with your local '
            'list instead of just uploading/downloading one account. '
            'Turn this off to fall back to the classic single-account '
            'sync.'))
        self.multisync_mode = QComboBox()
        self.multisync_mode.addItem(_('Merge (reconcile every provider)'),
                                    'merge')
        self.multisync_mode.addItem(_('Pull (providers update local)'),
                                    'pull')
        self.multisync_mode.addItem(_('Push (local overwrites providers)'),
                                    'push')
        self.multisync_plan_only = QCheckBox(
            _('Fetch && plan only (review before applying)'))
        self.multisync_plan_only.setToolTip(_(
            'Sync fetches every provider and works out what would '
            'change in the mode above, then always opens the sync '
            'window so you can review and apply it yourself. Turn this '
            'off to let Sync apply clean changes headlessly -- '
            'conflicts and first-time syncs still always stop for '
            'review either way.'))
        self.multisync_edit_owned_score = QCheckBox(
            _('Edit owned scores in the owner\'s rating system'))
        self.multisync_edit_owned_score.setToolTip(_(
            'When a show\'s Score is owned by another tracker, let the '
            'sidebar score editor rate it in that owner\'s own system '
            '(e.g. AniList\'s 8.4 while signed into Kitsu), toggled live '
            'via the Synced/Platform switch under the slider. Turn this '
            'off to always edit in the signed-in account\'s own system.'))
        self.multisync_enabled.toggled.connect(
            self.multisync_mode.setEnabled)
        self.multisync_enabled.toggled.connect(
            self.multisync_plan_only.setEnabled)
        self.multisync_enabled.toggled.connect(
            self.multisync_edit_owned_score.setEnabled)
        g_sync_layout = QVBoxLayout()
        g_sync_layout.addWidget(self.multisync_enabled)
        mode_row = QFormLayout()
        mode_row.addRow('Mode:', self.multisync_mode)
        g_sync_layout.addLayout(mode_row)
        g_sync_layout.addWidget(self.multisync_plan_only)
        g_sync_layout.addWidget(self.multisync_edit_owned_score)
        g_sync.setLayout(g_sync_layout)

        page_behavior_layout.addWidget(g_sync)

        # Group: Taiga Mode
        g_taiga = QGroupBox(_('Taiga Mode'))
        g_taiga.setFlat(True)
        self.taiga_mode = QCheckBox(
            _('Emulate Taiga\'s look (Qt only, requires restart)'))
        g_taiga_layout = QVBoxLayout()
        g_taiga_layout.addWidget(self.taiga_mode)
        g_taiga.setLayout(g_taiga_layout)

        page_behavior_layout.addWidget(g_taiga)

        # Group: Kitsu
        g_kitsu = QGroupBox(_('Kitsu'))
        g_kitsu.setFlat(True)
        self.kitsu_api = QComboBox()
        self.kitsu_api.addItem(_('Legacy (REST)'), 'legacy')
        self.kitsu_api.addItem(_('New (GraphQL)'), 'graphql')
        self.kitsu_api.setToolTip(_(
            'Which Kitsu API to use for Kitsu accounts. Takes effect the '
            'next time the account is loaded. Both use the same Kitsu '
            'login.'))
        g_kitsu_layout = QFormLayout()
        g_kitsu_layout.addRow(_('Kitsu API:'), self.kitsu_api)
        g_kitsu.setLayout(g_kitsu_layout)

        page_behavior_layout.addWidget(g_kitsu)
        page_behavior.setLayout(page_behavior_layout)

        # UI tab
        page_ui = QWidget()
        page_ui_layout = QFormLayout()
        page_ui_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        # Group: Language
        g_language = QGroupBox(_('Language'))
        g_language.setFlat(True)
        self.ui_language = QComboBox()
        for (code, label) in i18n.SUPPORTED_LANGUAGES:
            self.ui_language.addItem(label, code)
        self.ui_language.setToolTip(_('Takes effect after restarting Hakubun+.'))
        g_language_layout = QFormLayout()
        g_language_layout.addRow(_('UI language:'), self.ui_language)
        g_language.setLayout(g_language_layout)

        # Group: Icon
        g_icon = QGroupBox(_('Notification Icon'))
        g_icon.setFlat(True)
        self.tray_icon = QCheckBox(_('Show tray icon'))
        self.tray_icon.toggled.connect(self.s_tray_icon)
        self.close_to_tray = QCheckBox(_('Close to tray'))
        self.start_in_tray = QCheckBox(_('Start minimized to tray'))
        self.tray_api_icon = QCheckBox(_('Use API icon as tray icon'))
        self.notifications = QCheckBox(
            _('Show notification when tracker detects new media'))
        g_icon_layout = QVBoxLayout()
        g_icon_layout.addWidget(self.tray_icon)
        g_icon_layout.addWidget(self.close_to_tray)
        g_icon_layout.addWidget(self.start_in_tray)
        g_icon_layout.addWidget(self.tray_api_icon)
        g_icon_layout.addWidget(self.notifications)
        g_icon.setLayout(g_icon_layout)

        # Group: Window
        g_window = QGroupBox(_('Window'))
        g_window.setFlat(True)
        self.remember_geometry = QCheckBox(_('Remember window size and position'))
        self.remember_columns = QCheckBox(_('Remember column layouts and widths'))
        self.columns_per_api = QCheckBox(
            _('Use different visible columns per API'))
        self.columns_per_api.setToolTip(_(
            "Remember visible columns (and, if \"Remember column layouts "
            "and widths\" above is also on, their widths/order) "
            "separately for each API/account, instead of sharing one "
            "column layout across all of them."))
        g_window_layout = QVBoxLayout()
        g_window_layout.addWidget(self.remember_geometry)
        g_window_layout.addWidget(self.remember_columns)
        g_window_layout.addWidget(self.columns_per_api)
        g_window.setLayout(g_window_layout)

        # Group: Lists
        g_lists = QGroupBox(_('Lists'))
        g_lists.setFlat(True)
        self.filter_bar_position = QComboBox()
        filter_bar_positions = [(FilterBar.PositionHidden,     _('Hidden')),
                                (FilterBar.PositionAboveLists, _('Above lists')),
                                (FilterBar.PositionBelowLists, _('Below lists'))]
        for (n, label) in filter_bar_positions:
            self.filter_bar_position.addItem(label, n)
        self.inline_edit = QCheckBox(_('Enable in-line editing'))
        self.filter_global = QCheckBox(_('Switch to the All category when filtering'))
        self.filter_global.setToolTip(_(
            "When typing in the filter bar while on a specific status "
            "tab (e.g. Watching), automatically switch to the All tab so "
            "the filter searches your whole list instead of just that "
            "status."))
        g_lists_layout = QFormLayout()
        g_lists_layout.addRow(_('Filter bar position:'), self.filter_bar_position)
        g_lists_layout.addRow(self.inline_edit)
        g_lists_layout.addRow(self.filter_global)
        g_lists.setLayout(g_lists_layout)

        # Group: MAL Scores
        g_mal_scores = QGroupBox(_('MAL Scores'))
        g_mal_scores.setFlat(True)
        self.add_mal_scores = QCheckBox(
            _('Add MAL Scores (must be signed into MAL)'))
        self.add_mal_scores.setToolTip(_(
            "Automatically cross-references MyAnimeList's community score "
            "for shows on your list after every sync, using a MyAnimeList "
            "account added in Hakubun+. Not needed if this account already "
            "is MyAnimeList."))
        self.load_mal_scores_btn = QPushButton(_('Load MAL Scores Now'))
        self.load_mal_scores_btn.setToolTip(_(
            "Fetch MAL scores for the current list right now, without "
            "waiting for a sync or enabling the automatic option above."))
        self.load_mal_scores_btn.clicked.connect(self.s_load_mal_scores)
        g_mal_scores_layout = QVBoxLayout()
        g_mal_scores_layout.addWidget(self.add_mal_scores)
        g_mal_scores_layout.addWidget(
            self.load_mal_scores_btn, alignment=QtCore.Qt.AlignmentFlag.AlignLeft)
        g_mal_scores.setLayout(g_mal_scores_layout)

        # Group: SubMiner
        g_subminer = QGroupBox(_('SubMiner'))
        g_subminer.setFlat(True)
        self.show_subminer_toggle = QCheckBox(
            _('Show the SubMiner toggle (toolbar / Tools menu)'))
        self.show_subminer_toggle.setToolTip(_(
            'SubMiner itself is switched on and off from that toggle, not '
            'here -- this only controls whether the toggle is shown at '
            'all.'))
        g_subminer_layout = QVBoxLayout()
        g_subminer_layout.addWidget(self.show_subminer_toggle)
        g_subminer.setLayout(g_subminer_layout)

        # UI layout
        page_ui_layout.addWidget(g_language)
        page_ui_layout.addWidget(g_icon)
        page_ui_layout.addWidget(g_window)
        page_ui_layout.addWidget(g_lists)
        page_ui_layout.addWidget(g_mal_scores)
        page_ui_layout.addWidget(g_subminer)
        page_ui.setLayout(page_ui_layout)

        # Theming tab
        page_theme = QWidget()
        page_theme_layout = QFormLayout()
        page_theme_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)

        # Group: Episode Bar
        g_ep_bar = QGroupBox(_('Episode Bar'))
        g_ep_bar.setFlat(True)
        self.ep_bar_style = QComboBox()
        ep_bar_styles = [(ShowsTableDelegate.BarStyleBasic,  _('Basic')),
                         (ShowsTableDelegate.BarStyle04,     'Trackma'),
                         (ShowsTableDelegate.BarStyleHybrid, _('Hybrid'))]
        for (n, label) in ep_bar_styles:
            self.ep_bar_style.addItem(label, n)
        self.ep_bar_style.currentIndexChanged.connect(self.s_ep_bar_style)
        self.ep_bar_text = QCheckBox(_('Show text label'))
        g_ep_bar_layout = QFormLayout()
        g_ep_bar_layout.addRow(_('Style:'), self.ep_bar_style)
        g_ep_bar_layout.addRow(self.ep_bar_text)
        g_ep_bar.setLayout(g_ep_bar_layout)

        # Group: Colour scheme
        g_scheme = QGroupBox(_('Color Scheme'))
        g_scheme.setFlat(True)
        col_tabs = [('rows',     _('&Row highlights')),
                    ('progress', _('&Progress widget'))]
        self.colors = {}
        self.colors['rows'] = [('is_playing',  _('Playing')),
                               ('is_queued',   _('Queued')),
                               ('new_episode', _('New episode')),
                               ('is_airing',   _('Airing')),
                               ('not_aired',   _('Unaired'))]
        self.colors['progress'] = [('progress_bg',       _('Background')),
                                   ('progress_fg',       _('Watched bar')),
                                   ('progress_sub_bg',   _('Aired episodes')),
                                   ('progress_sub_fg',   _('Stored episodes')),
                                   ('progress_complete', _('Complete'))]
        self.color_buttons = []
        self.syscolor_buttons = []
        g_scheme_layout = QGridLayout()
        tw_scheme = QTabWidget()
        for (key1, tab_title) in col_tabs:
            page = QFrame()
            page_layout = QGridLayout()
            col = 0
            # Generate widgets from the keys and values
            for (key2, label) in self.colors[key1]:
                self.color_buttons.append(QPushButton())
                # self.color_buttons[-1].setStyleSheet('background-color: ' + getColor(self.config['colors'][key]).name())
                self.color_buttons[-1].setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
                self.color_buttons[-1].clicked.connect(
                    self.s_color_picker(key2, False))
                self.syscolor_buttons.append(QPushButton(_('System Colors')))
                self.syscolor_buttons[-1].clicked.connect(
                    self.s_color_picker(key2, True))
                page_layout.addWidget(QLabel(label),             col, 0, 1, 1)
                page_layout.addWidget(self.color_buttons[-1],    col, 1, 1, 1)
                page_layout.addWidget(self.syscolor_buttons[-1], col, 2, 1, 1)
                col += 1
            page.setLayout(page_layout)
            tw_scheme.addTab(page, tab_title)
        g_scheme_layout.addWidget(tw_scheme)
        g_scheme.setLayout(g_scheme_layout)

        # UI layout
        page_theme_layout.addWidget(g_ep_bar)
        page_theme_layout.addWidget(g_scheme)
        page_theme.setLayout(page_theme_layout)

        # Content
        self.contents = QStackedWidget()
        for page in (page_media, page_library, page_sync, page_behavior, page_ui, page_theme):
            scrollable_page = QScrollArea()
            scrollable_page.setWidgetResizable(True)
            scrollable_page.setWidget(page)
            self.contents.addWidget(scrollable_page)

        # Bottom buttons
        bottombox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        bottombox.accepted.connect(self.s_save)
        bottombox.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._save)
        bottombox.rejected.connect(self.reject)

        # Main layout finish
        layout.addWidget(self.category_list,  0, 0, 1, 1)
        layout.addWidget(self.contents,       0, 1, 1, 1)
        layout.addWidget(bottombox,           1, 0, 1, 2)
        layout.setColumnStretch(1, 1)

        self._load()
        self.update_colors()

        self.setLayout(layout)

    def _add_dir(self, path):
        self.searchdirs.addItem(path)

    def _load(self):
        engine = self.worker.engine
        tracker_type = self.tracker_type.findData(
            engine.get_config('tracker_type'))
        title_parser = self.title_parser.findData(
            engine.get_config('title_parser'))
        autoretrieve = engine.get_config('autoretrieve')
        autosend = engine.get_config('autosend')

        self.tracker_enabled.setChecked(engine.get_config('tracker_enabled'))
        self.tracker_type.setCurrentIndex(max(0, tracker_type))
        self.tracker_interval.setValue(engine.get_config('tracker_interval'))
        self.tracker_process.setText(engine.get_config('tracker_process'))
        self.tracker_update_wait.setValue(
            engine.get_config('tracker_update_wait_s'))
        self.mpris_update_mode.setCurrentIndex(
            max(0, self.mpris_update_mode.findData(
                engine.get_config('mpris_obey_update_wait_s'))))
        self.tracker_update_percentage.setValue(
            engine.get_config('tracker_update_percentage'))
        self.tracker_update_percentage.setEnabled(
            not engine.get_config('mpris_obey_update_wait_s'))
        self.tracker_update_close.setChecked(
            engine.get_config('tracker_update_close'))
        self.tracker_update_prompt.setChecked(
            engine.get_config('tracker_update_prompt'))
        self.tracker_not_found_prompt.setChecked(
            engine.get_config('tracker_not_found_prompt'))

        self.title_parser.setCurrentIndex(max(0, title_parser))
        self.player.setText(engine.get_config('player'))
        self.player_reuse_mpv.setChecked(
            engine.get_config('player_reuse_mpv_instance'))
        self.library_autoscan.setChecked(engine.get_config('library_autoscan'))
        self.scan_whole_list.setChecked(engine.get_config('scan_whole_list'))
        self.library_full_path.setChecked(
            engine.get_config('library_full_path'))

        self.plex_host.setText(engine.get_config('plex_host'))
        self.plex_port.setText(engine.get_config('plex_port'))
        self.plex_ssl.setChecked(
            engine.get_config('plex_ssl'))
        self._plex_obey_wait = engine.get_config('plex_obey_update_wait_s')
        self.plex_user.setText(engine.get_config('plex_user'))
        self.plex_passw.setText(engine.get_config('plex_passwd'))

        self.jellyfin_host.setText(engine.get_config('jellyfin_host'))
        self.jellyfin_port.setText(engine.get_config('jellyfin_port'))
        self.jellyfin_user.setText(engine.get_config('jellyfin_user'))
        self.jellyfin_api_key.setText(engine.get_config('jellyfin_api_key'))

        self.kodi_host.setText(engine.get_config('kodi_host'))
        self.kodi_port.setText(engine.get_config('kodi_port'))
        self._kodi_obey_wait = engine.get_config('kodi_obey_update_wait_s')
        self.kodi_user.setText(engine.get_config('kodi_user'))
        self.kodi_passw.setText(engine.get_config('kodi_passwd'))

        for path in engine.get_config('searchdir'):
            self._add_dir(path)

        if autoretrieve == 'always':
            self.autoretrieve_always.setChecked(True)
        elif autoretrieve == 'days':
            self.autoretrieve_days.setChecked(True)
        else:
            self.autoretrieve_off.setChecked(True)

        self.autoretrieve_days_n.setValue(
            engine.get_config('autoretrieve_days'))

        if autosend == 'always':
            self.autosend_always.setChecked(True)
        elif autosend in ('minutes', 'hours'):
            self.autosend_minutes.setChecked(True)
        elif autosend == 'size':
            self.autosend_size.setChecked(True)
        else:
            self.autosend_off.setChecked(True)

        self.autosend_minutes_n.setValue(engine.get_config('autosend_minutes'))
        self.autosend_size_n.setValue(engine.get_config('autosend_size'))

        self.autosend_at_exit.setChecked(engine.get_config('autosend_at_exit'))
        self.auto_status_change.setChecked(
            engine.get_config('auto_status_change'))
        self.auto_status_change_if_scored.setChecked(
            engine.get_config('auto_status_change_if_scored'))
        self.auto_date_change.setChecked(engine.get_config('auto_date_change'))
        self.add_mal_scores.setChecked(engine.get_config('auto_add_mal_scores'))

        self.add_dialog_default_status.setCurrentIndex(
            max(0, self.add_dialog_default_status.findData(
                engine.get_config('add_dialog_default_status'))))

        self.sync_on_settings_apply.setChecked(
            engine.get_config('sync_on_settings_apply'))

        self.kitsu_api.setCurrentIndex(
            max(0, self.kitsu_api.findData(engine.get_config('kitsu_api'))))

        self.ui_language.setCurrentIndex(
            max(0, self.ui_language.findData(self.config.get('language', 'auto'))))
        self.tray_icon.setChecked(self.config['show_tray'])
        self.close_to_tray.setChecked(self.config['close_to_tray'])
        self.start_in_tray.setChecked(self.config['start_in_tray'])
        self.tray_api_icon.setChecked(self.config['tray_api_icon'])
        self.notifications.setChecked(self.config['notifications'])
        self.remember_geometry.setChecked(self.config['remember_geometry'])
        self.remember_columns.setChecked(self.config['remember_columns'])
        self.columns_per_api.setChecked(self.config['columns_per_api'])
        self.filter_bar_position.setCurrentIndex(
            self.filter_bar_position.findData(self.config['filter_bar_position']))
        self.show_subminer_toggle.setChecked(
            self.config['show_subminer_toggle'])
        self.inline_edit.setChecked(self.config['inline_edit'])
        self.filter_global.setChecked(self.config['filter_global'])
        self.multisync_enabled.setChecked(self.config['multisync_enabled'])
        # A config still on the retired 'plan_only' mode resolves to
        # merge + the review checkbox, which is what it always meant.
        (mode, plan_only) = present.settings_sync_mode(self.config)
        mode_key = next((k for k, v in present.SETTINGS_MODES.items()
                         if v == mode), 'merge')
        self.multisync_mode.setCurrentIndex(
            max(0, self.multisync_mode.findData(mode_key)))
        self.multisync_mode.setEnabled(self.config['multisync_enabled'])
        self.multisync_plan_only.setChecked(plan_only)
        self.multisync_plan_only.setEnabled(self.config['multisync_enabled'])
        self.multisync_edit_owned_score.setChecked(
            self.config['multisync_edit_owned_score'])
        self.multisync_edit_owned_score.setEnabled(
            self.config['multisync_enabled'])
        self.taiga_mode.setChecked(self.config['taiga_mode'])

        self.ep_bar_style.setCurrentIndex(
            self.ep_bar_style.findData(self.config['episodebar_style']))
        self.ep_bar_text.setChecked(self.config['episodebar_text'])

        self.autoretrieve_days_n.setEnabled(self.autoretrieve_days.isChecked())
        self.autosend_minutes_n.setEnabled(self.autosend_minutes.isChecked())
        self.autosend_size_n.setEnabled(self.autosend_size.isChecked())
        self.close_to_tray.setEnabled(self.tray_icon.isChecked())
        self.start_in_tray.setEnabled(self.tray_icon.isChecked())
        self.notifications.setEnabled(self.tray_icon.isChecked())

        self.color_values = self.config['colors'].copy()

        self.tracker_type_change(None)

    def _save(self):
        engine = self.worker.engine
        old_taiga_mode = self.config['taiga_mode']

        engine.set_config('tracker_enabled',
                          self.tracker_enabled.isChecked())
        engine.set_config('tracker_type',          self.tracker_type.itemData(
            self.tracker_type.currentIndex()))
        engine.set_config('tracker_interval',
                          self.tracker_interval.value())
        engine.set_config('tracker_process',       str(
            self.tracker_process.text()))
        engine.set_config('tracker_update_wait_s',
                          self.tracker_update_wait.value())
        engine.set_config('mpris_obey_update_wait_s',
                          self.mpris_update_mode.itemData(
                              self.mpris_update_mode.currentIndex()))
        engine.set_config('tracker_update_percentage',
                          self.tracker_update_percentage.value())
        engine.set_config('tracker_update_close',
                          self.tracker_update_close.isChecked())
        engine.set_config('tracker_update_prompt',
                          self.tracker_update_prompt.isChecked())
        engine.set_config('tracker_not_found_prompt',
                          self.tracker_not_found_prompt.isChecked())

        engine.set_config('title_parser',          self.title_parser.itemData(
            self.title_parser.currentIndex()))
        engine.set_config('player',            self.player.text())
        engine.set_config('player_reuse_mpv_instance',
                           self.player_reuse_mpv.isChecked())
        engine.set_config('library_autoscan',
                          self.library_autoscan.isChecked())
        engine.set_config('scan_whole_list', self.scan_whole_list.isChecked())
        engine.set_config('library_full_path',
                          self.library_full_path.isChecked())

        engine.set_config('plex_host',         self.plex_host.text())
        engine.set_config('plex_port',         self.plex_port.text())
        engine.set_config('plex_obey_update_wait_s', self._plex_obey_wait)
        engine.set_config('plex_ssl',          self.plex_ssl.isChecked())
        engine.set_config('plex_user',         self.plex_user.text())
        engine.set_config('plex_passwd',       self.plex_passw.text())

        engine.set_config('jellyfin_host',         self.jellyfin_host.text())
        engine.set_config('jellyfin_port',         self.jellyfin_port.text())
        engine.set_config('jellyfin_user',         self.jellyfin_user.text())
        engine.set_config('jellyfin_api_key',       self.jellyfin_api_key.text())

        engine.set_config('kodi_host',         self.kodi_host.text())
        engine.set_config('kodi_port',         self.kodi_port.text())
        engine.set_config('kodi_obey_update_wait_s', self._kodi_obey_wait)
        engine.set_config('kodi_user',         self.kodi_user.text())
        engine.set_config('kodi_passwd',       self.kodi_passw.text())

        engine.set_config('searchdir',         [self.searchdirs.item(
            i).text() for i in range(self.searchdirs.count())])

        if self.autoretrieve_always.isChecked():
            engine.set_config('autoretrieve', 'always')
        elif self.autoretrieve_days.isChecked():
            engine.set_config('autoretrieve', 'days')
        else:
            engine.set_config('autoretrieve', 'off')

        engine.set_config('autoretrieve_days',
                          self.autoretrieve_days_n.value())

        if self.autosend_always.isChecked():
            engine.set_config('autosend', 'always')
        elif self.autosend_minutes.isChecked():
            engine.set_config('autosend', 'minutes')
        elif self.autosend_size.isChecked():
            engine.set_config('autosend', 'size')
        else:
            engine.set_config('autosend', 'off')

        engine.set_config('autosend_minutes', self.autosend_minutes_n.value())
        engine.set_config('autosend_size',  self.autosend_size_n.value())

        engine.set_config('autosend_at_exit',
                          self.autosend_at_exit.isChecked())
        engine.set_config('auto_status_change',
                          self.auto_status_change.isChecked())
        engine.set_config('auto_status_change_if_scored',
                          self.auto_status_change_if_scored.isChecked())
        engine.set_config('auto_date_change',
                          self.auto_date_change.isChecked())
        engine.set_config('auto_add_mal_scores',
                          self.add_mal_scores.isChecked())

        engine.set_config('add_dialog_default_status',
                          self.add_dialog_default_status.itemData(
                              self.add_dialog_default_status.currentIndex()))

        engine.set_config('sync_on_settings_apply',
                          self.sync_on_settings_apply.isChecked())

        engine.set_config('kitsu_api',
                          self.kitsu_api.itemData(self.kitsu_api.currentIndex()))

        engine.save_config()

        self.config['language'] = self.ui_language.itemData(
            self.ui_language.currentIndex())
        self.config['show_tray'] = self.tray_icon.isChecked()
        self.config['close_to_tray'] = self.close_to_tray.isChecked()
        self.config['start_in_tray'] = self.start_in_tray.isChecked()
        self.config['tray_api_icon'] = self.tray_api_icon.isChecked()
        self.config['notifications'] = self.notifications.isChecked()
        self.config['remember_geometry'] = self.remember_geometry.isChecked()
        self.config['remember_columns'] = self.remember_columns.isChecked()
        self.config['columns_per_api'] = self.columns_per_api.isChecked()
        self.config['filter_bar_position'] = self.filter_bar_position.itemData(
            self.filter_bar_position.currentIndex())
        self.config['show_subminer_toggle'] = \
            self.show_subminer_toggle.isChecked()
        self.config['inline_edit'] = self.inline_edit.isChecked()
        self.config['filter_global'] = self.filter_global.isChecked()
        self.config['multisync_enabled'] = self.multisync_enabled.isChecked()
        self.config['multisync_mode'] = self.multisync_mode.itemData(
            self.multisync_mode.currentIndex())
        self.config['multisync_plan_only'] = \
            self.multisync_plan_only.isChecked()
        self.config['multisync_edit_owned_score'] = \
            self.multisync_edit_owned_score.isChecked()

        self.config['episodebar_style'] = self.ep_bar_style.itemData(
            self.ep_bar_style.currentIndex())
        self.config['episodebar_text'] = self.ep_bar_text.isChecked()

        self.config['colors'] = self.color_values

        self.config['taiga_mode'] = self.taiga_mode.isChecked()

        utils.save_config(self.config, self.configfile)

        self.saved.emit()

        if self.config['taiga_mode'] != old_taiga_mode:
            self._prompt_restart()

    def _prompt_restart(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(_('Restart required'))
        box.setText(
            _('Taiga Mode takes effect after restarting Hakubun+.'))
        restart_btn = box.addButton(
            _('Restart Now'), QMessageBox.ButtonRole.AcceptRole)
        box.addButton(_('Later'), QMessageBox.ButtonRole.RejectRole)
        box.exec()

        if box.clickedButton() is restart_btn:
            subprocess.Popen([sys.executable] + sys.argv)
            QApplication.instance().quit()

    def s_save(self):
        self._save()
        self.accept()

    def switch_kodi_state(self, state):
        self.kodi_host.setEnabled(state)
        self.kodi_port.setEnabled(state)
        self.kodi_user.setEnabled(state)
        self.kodi_passw.setEnabled(state)

    def switch_plex_state(self, state):
        self.plex_host.setEnabled(state)
        self.plex_port.setEnabled(state)
        self.plex_user.setEnabled(state)
        self.plex_passw.setEnabled(state)
        self.plex_ssl.setEnabled(state)

    def switch_jellyfin_state(self, state):
        self.jellyfin_host.setEnabled(state)
        self.jellyfin_port.setEnabled(state)
        self.jellyfin_user.setEnabled(state)
        self.jellyfin_api_key.setEnabled(state)

    def tracker_type_change(self, checked):
        if self.tracker_enabled.isChecked():
            self.tracker_interval.setEnabled(True)
            self.tracker_update_wait.setEnabled(True)
            self.tracker_type.setEnabled(True)
            current_type = self.tracker_type.itemData(
                self.tracker_type.currentIndex())
            if current_type == 'plex':
                self.switch_jellyfin_state(False)
                self.switch_plex_state(True)
                self.switch_kodi_state(False)
                self.tracker_process.setEnabled(False)
            elif current_type == 'jellyfin':
                self.switch_jellyfin_state(True)
                self.switch_kodi_state(False)
                self.switch_plex_state(False)
                self.tracker_process.setEnabled(False)
            elif current_type == 'kodi':
                self.switch_jellyfin_state(False)
                self.switch_kodi_state(True)
                self.switch_plex_state(False)
                self.tracker_process.setEnabled(False)
            else:
                self.tracker_process.setEnabled(True)
                self.switch_jellyfin_state(False)
                self.switch_plex_state(False)
                self.switch_kodi_state(False)
            self._update_streaming_obey_wait(current_type)
        else:
            self.tracker_type.setEnabled(False)
            self.switch_jellyfin_state(False)
            self.switch_plex_state(False)
            self.switch_kodi_state(False)

            self.tracker_process.setEnabled(False)
            self.tracker_interval.setEnabled(False)
            self.tracker_update_wait.setEnabled(False)
            self._update_streaming_obey_wait(None)

    def _update_streaming_obey_wait(self, current_type):
        if current_type == 'plex':
            self.streaming_obey_wait.setEnabled(True)
            self.streaming_obey_wait.setChecked(self._plex_obey_wait)
        elif current_type == 'kodi':
            self.streaming_obey_wait.setEnabled(True)
            self.streaming_obey_wait.setChecked(self._kodi_obey_wait)
        else:
            self.streaming_obey_wait.setEnabled(False)

    def s_streaming_obey_wait(self, checked):
        current_type = self.tracker_type.itemData(
            self.tracker_type.currentIndex())
        if current_type == 'plex':
            self._plex_obey_wait = checked
        elif current_type == 'kodi':
            self._kodi_obey_wait = checked

    def s_autoretrieve_days(self, checked):
        self.autoretrieve_days_n.setEnabled(checked)

    def s_autosend_minutes(self, checked):
        self.autosend_minutes_n.setEnabled(checked)

    def s_autosend_size(self, checked):
        self.autosend_size_n.setEnabled(checked)

    def s_mpris_update_mode(self, index):
        is_percentage = not self.mpris_update_mode.itemData(index)
        self.tracker_update_percentage.setEnabled(is_percentage)

    def s_tray_icon(self, checked):
        self.close_to_tray.setEnabled(checked)
        self.start_in_tray.setEnabled(checked)
        self.tray_api_icon.setEnabled(checked)
        self.notifications.setEnabled(checked)

    def s_ep_bar_style(self, index):
        if self.ep_bar_style.itemData(index) == ShowsTableDelegate.BarStyle04:
            self.ep_bar_text.setEnabled(False)
        else:
            self.ep_bar_text.setEnabled(True)

    def s_auto_status_change(self, checked):
        self.auto_status_change_if_scored.setEnabled(checked)

    def s_player_browse(self):
        self.player.setText(QFileDialog.getOpenFileName(
            caption=_('Choose player executable'))[0])

    def s_load_mal_scores(self):
        try:
            self.worker.engine.fetch_mal_scores()
        except utils.EngineError as e:
            QMessageBox.warning(self, _('MAL Scores'), str(e))
        else:
            QMessageBox.information(
                self, _('MAL Scores'), _('Fetching MAL scores in the background.'))

    def s_searchdirs_add(self):
        self._add_dir(QFileDialog.getExistingDirectory(
            caption=_('Choose media directory')))

    def s_searchdirs_remove(self):
        row = self.searchdirs.currentRow()
        if row != -1:
            self.searchdirs.takeItem(row)

    def s_switch_page(self, new, old):
        if not new:
            new = old

        self.contents.setCurrentIndex(self.category_list.row(new))

    def s_color_picker(self, key, system):
        return lambda: self.color_picker(key, system)

    def color_picker(self, key, system):
        if system is True:
            current = self.color_values[key]
            result = ThemedColorPicker.do()
            if result is not None and result is not current:
                self.color_values[key] = result
                self.update_colors()
        else:
            current = getColor(self.color_values[key])
            result = QColorDialog.getColor(current)
            if result.isValid() and result is not current:
                self.color_values[key] = str(result.name())
                self.update_colors()

    def update_colors(self):
        for ((key, label), color) in zip(self.colors['rows']+self.colors['progress'], self.color_buttons):
            color.setStyleSheet('background-color: ' +
                                getColor(self.color_values[key]).name())
