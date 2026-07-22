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

import html
from datetime import date

from PyQt6 import QtCore
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QMenu,
                             QMessageBox, QPushButton, QRadioButton, QSpinBox, QSplitter, QStackedWidget,
                             QVBoxLayout)

from hakubun import utils
from hakubun.ui.qt.details import DetailsDialog
from hakubun.ui.qt.widgets import AddCardView, AddTableDetailsView


class AddDialog(QDialog):
    worker = None
    selected_show = None
    results = []

    goToRequested = QtCore.pyqtSignal(int)

    def __init__(self, parent, worker, current_status, default=None):
        QDialog.__init__(self, parent)
        self.resize(950, 700)
        self.setWindowTitle('Search/Add from Remote')
        self.worker = worker
        self.current_status = current_status
        self.default = default
        if default:
            self.setWindowTitle(
                'Search/Add from Remote for new show: %s' % default)

        # Get available search methods and default to keyword search if not reported by the API
        search_methods = self.worker.engine.mediainfo.get(
            'search_methods', [utils.SearchMethod.KW])

        # Shows already in the user's list, so search results can be
        # highlighted instead of letting the user accidentally re-add
        # or lose track of something they're already tracking.
        self.mylist = {show['id']: show for show in self.worker.engine.get_list()}
        self.statuses_dict = self.worker.engine.mediainfo['statuses_dict']

        layout = QVBoxLayout()

        # Create top layout
        top_layout = QHBoxLayout()

        if utils.SearchMethod.KW in search_methods:
            self.search_rad = QRadioButton('By keyword:')
            self.search_rad.setChecked(True)
            self.search_txt = QLineEdit()
            self.search_txt.setClearButtonEnabled(True)
            self.search_txt.returnPressed.connect(self.s_search)
            if default:
                self.search_txt.setText(default)
            self.search_btn = QPushButton('Search')
            self.search_btn.clicked.connect(self.s_search)
            top_layout.addWidget(self.search_rad)
            top_layout.addWidget(self.search_txt)
        else:
            top_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

        top_layout.addWidget(self.search_btn)

        # Create filter line
        filters_layout = QHBoxLayout()

        if utils.SearchMethod.SEASON in search_methods:
            self.season_rad = QRadioButton('By season:')
            self.season_combo = QComboBox()
            self.season_combo.addItem('Winter', utils.Season.WINTER)
            self.season_combo.addItem('Spring', utils.Season.SPRING)
            self.season_combo.addItem('Summer', utils.Season.SUMMER)
            self.season_combo.addItem('Fall', utils.Season.FALL)

            self.season_year = QSpinBox()

            today = date.today()
            current_season = (today.month - 1) // 3

            self.season_year.setRange(1900, today.year)
            self.season_year.setValue(today.year)
            self.season_combo.setCurrentIndex(current_season)

            filters_layout.addWidget(self.season_rad)
            filters_layout.addWidget(self.season_combo)
            filters_layout.addWidget(self.season_year)

            filters_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
            filters_layout.addWidget(QSplitter())
        else:
            filters_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

        view_combo = QComboBox()
        view_combo.addItem('Card view')
        view_combo.addItem('Table view')
        view_combo.currentIndexChanged.connect(self.s_change_view)

        filters_layout.addWidget(view_combo)

        # Create central content
        self.contents = QStackedWidget()

        # Set up views
        tableview = AddTableDetailsView(
            None, self.worker, mylist=self.mylist, statuses_dict=self.statuses_dict)
        tableview.changed.connect(self.s_selected)
        tableview.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        tableview.table.customContextMenuRequested.connect(
            lambda pos: self.s_context_menu(tableview.table, pos))

        cardview = AddCardView(
            api_info=self.worker.engine.api_info, mylist=self.mylist, statuses_dict=self.statuses_dict)
        cardview.changed.connect(self.s_selected)
        cardview.doubleClicked.connect(self.s_show_details)
        cardview.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        cardview.customContextMenuRequested.connect(
            lambda pos: self.s_context_menu(cardview, pos))

        self.contents.addWidget(cardview)
        self.contents.addWidget(tableview)

        # Use for testing
        # self.set_results([{'id': 1, 'title': 'Hola', 'image': 'https://omaera.org/icon.png'}])

        bottom_buttons = QDialogButtonBox()
        bottom_buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.add_btn = bottom_buttons.addButton(
            "Add", QDialogButtonBox.ButtonRole.AcceptRole)
        self.add_btn.setEnabled(False)
        bottom_buttons.accepted.connect(self.s_add)
        bottom_buttons.rejected.connect(self.close)

        # Finish layout
        layout.addLayout(top_layout)
        layout.addLayout(filters_layout)
        layout.addWidget(self.contents)
        layout.addWidget(bottom_buttons)
        self.setLayout(layout)

        if utils.SearchMethod.SEASON in search_methods:
            self.search_txt.setFocus()

    def worker_call(self, function, ret_function, *args, **kwargs):
        # Run worker in a thread. set_function owns starting/queueing;
        # don't call worker.start() here (see EngineWorker.set_function).
        self.worker.set_function(function, ret_function, *args, **kwargs)

    def _enable_widgets(self, enable):
        self.search_btn.setEnabled(enable)
        self.contents.currentWidget().setEnabled(enable)

    def set_results(self, results):
        self.results = results
        self.contents.currentWidget().setResults(self.results)

    # Slots
    def s_show_details(self):
        on_go_to = None
        if self.selected_show and self.selected_show['id'] in self.mylist:
            showid = self.selected_show['id']
            on_go_to = lambda: self.s_go_to(showid)

        detailswindow = DetailsDialog(
            self, self.worker, self.selected_show, on_go_to=on_go_to)
        detailswindow.setModal(True)
        detailswindow.show()

    def s_go_to(self, showid):
        self.goToRequested.emit(showid)
        self.close()

    def s_context_menu(self, view, pos):
        # Right-click "Move to" for a search result -- only meaningful
        # for shows already in the user's list, since there's no status
        # to move a not-yet-added show away from.
        index = view.indexAt(pos)
        if not index.isValid():
            return

        source_row = view.model().mapToSource(index).row()
        show = view.model().sourceModel().results[source_row]
        if show['id'] not in self.mylist:
            return

        menu = QMenu(self)
        move_to = menu.addMenu('Move to')
        for status in self.worker.engine.mediainfo['statuses']:
            action = move_to.addAction(self.statuses_dict.get(status, str(status)))
            action.triggered.connect(
                lambda checked=False, s=status, showid=show['id']: self.s_move_to(showid, s))
        menu.exec(view.viewport().mapToGlobal(pos))

    def s_move_to(self, showid, status):
        self.worker_call('set_status', self.r_moved, showid, status)
        if showid in self.mylist:
            self.mylist[showid]['my_status'] = status

    def r_moved(self, result):
        if not result['success']:
            QMessageBox.critical(
                self, 'Error', 'Could not change the show\'s status.')

    def s_change_view(self, item):
        self.contents.currentWidget().getModel().setResults(None)
        self.contents.setCurrentIndex(item)
        self.contents.currentWidget().getModel().setResults(self.results)

    def s_search(self):
        if self.search_rad.isChecked():
            criteria = self.search_txt.text().strip()
            if not criteria:
                return
            method = utils.SearchMethod.KW
        elif self.season_rad.isChecked():
            criteria = (self.season_combo.itemData(
                self.season_combo.currentIndex()), self.season_year.value())
            method = utils.SearchMethod.SEASON

        self.contents.currentWidget().clearSelection()
        self.selected_show = None

        self._enable_widgets(False)
        self.add_btn.setEnabled(False)

        self.worker_call('search', self.r_searched, criteria, method)

    def s_selected(self, show):
        self.selected_show = show
        self.add_btn.setEnabled(True)

    def s_add(self):
        if not self.selected_show:
            return

        mediainfo = self.worker.engine.mediainfo
        statuses = mediainfo['statuses']
        statuses_dict = mediainfo['statuses_dict']

        # Default status is configurable (Settings > Behavior): either the
        # currently active tab, or the API's "Plan to Watch" status (the
        # last entry of `statuses` by convention across every backend --
        # `statuses_start` is a different thing: the status a show flips to
        # once you start watching it, e.g. "watching"/"CURRENT"). Either
        # way we always confirm before adding, so a show is never silently
        # dropped into whatever tab happened to be active.
        default_status = self.current_status
        if self.worker.engine.get_config('add_dialog_default_status') == 'start' \
                and statuses:
            default_status = statuses[-1]

        dialog = AddStatusDialog(
            self,
            show_title=self.selected_show['title'],
            statuses=statuses,
            statuses_dict=statuses_dict,
            default_status=default_status,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        chosen_status = dialog.chosen_status()
        self.worker_call('add_show', self.r_added,
                         self.selected_show, chosen_status)

    # Worker responses
    def r_searched(self, result):
        self._enable_widgets(True)

        if result['success']:
            self.set_results(result['result'])
        else:
            self.set_results(None)

    def r_added(self, result):
        if result['success']:
            if self.default:
                self.accept()
            else:
                title = self.selected_show.get('title', 'Show')
                QMessageBox.information(
                    self, 'Added', f'"{title}" was added to your list.')


class AddStatusDialog(QDialog):
    """
    Confirmation dialog shown before adding a show to the list.

    Lets the user choose which status to add the show under instead of
    silently inheriting whatever tab happens to be active. This prevents
    accidentally adding a show to Dropped or On Hold when auto-sync is on.
    """

    def __init__(self, parent, show_title, statuses, statuses_dict, default_status):
        super().__init__(parent)
        self.setWindowTitle('Add to list')
        self.setMinimumWidth(340)

        layout = QVBoxLayout()
        layout.setSpacing(12)

        # Show title
        title_label = QLabel(f'<b>{html.escape(show_title)}</b>')
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        # Status row
        status_row = QHBoxLayout()
        status_label = QLabel('Add as:')
        status_label.setFixedWidth(56)
        self._status_combo = QComboBox()

        default_index = 0
        for i, status in enumerate(statuses):
            self._status_combo.addItem(statuses_dict[status], userData=status)
            if status == default_status:
                default_index = i

        self._status_combo.setCurrentIndex(default_index)

        status_row.addWidget(status_label)
        status_row.addWidget(self._status_combo, 1)
        layout.addLayout(status_row)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Add')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def chosen_status(self):
        """Returns the status value selected by the user."""
        return self._status_combo.currentData()
