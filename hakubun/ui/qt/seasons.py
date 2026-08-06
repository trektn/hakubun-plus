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

from datetime import date

from PyQt6 import QtCore
from PyQt6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QMenu, QMessageBox,
                             QPushButton, QSpinBox, QVBoxLayout, QWidget)

from hakubun import utils
from hakubun.ui.qt.add import AddStatusDialog
from hakubun.ui.qt.details import DetailsDialog
from hakubun.ui.qt.widgets import AddCardView


class SeasonsWidget(QWidget):
    """Taiga-style seasonal browse page.

    Reuses AddCardView/AddListModel/AddListDelegate -- the Add dialog's
    card-grid machinery -- for a persistent page instead of a one-shot
    modal. The card grid itself can't be built until we know whether
    the active account/mediatype even supports season search and what
    its api_info is (see set_context()), which isn't available until
    after the engine has loaded -- so construction just shows a
    placeholder, and set_context() swaps in the real search UI once
    that information exists.
    """

    def __init__(self, parent, worker):
        super().__init__(parent)
        self.worker = worker
        self.selected_show = None
        self.mylist = {}
        self.statuses = []
        self.statuses_dict = {}
        self.card_view = None
        self._details_window = None

        self._layout = QVBoxLayout()
        self._unavailable_label = QLabel(
            "Season browsing isn't supported for this account/media type.")
        self._unavailable_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self._unavailable_label)
        self.setLayout(self._layout)

    def set_context(self, api_info, mediainfo):
        """Called once the engine has (re)loaded -- see MainWindow's
        r_engine_loaded(), which already re-runs this on account and
        mediatype reloads. Builds the search UI on first call if the
        account supports season search, and keeps mylist/statuses
        current afterward.

        The nav row itself stays clickable regardless (MainWindow builds
        it once, unconditionally) -- an account/mediatype switch that
        drops SEASON support (e.g. a Kitsu account moving from anime to
        manga) must revert this page back to the placeholder rather than
        leaving a stale, still-searchable UI pointed at a mediatype that
        no longer supports it."""
        search_methods = mediainfo.get('search_methods', [])
        if utils.SearchMethod.SEASON not in search_methods:
            if self.card_view is not None:
                self._teardown_search_ui()
            return

        self.statuses = mediainfo['statuses']
        self.statuses_dict = mediainfo['statuses_dict']
        self.mylist = {show['id']: show for show in self.worker.engine.get_list()}

        if self.card_view is None:
            self._build_search_ui(api_info)
        else:
            self.card_view.set_mylist(self.mylist)

    def _build_search_ui(self, api_info):
        self._unavailable_label.hide()
        self._layout.removeWidget(self._unavailable_label)

        self.season_combo = QComboBox()
        self.season_combo.addItem('Winter', utils.Season.WINTER)
        self.season_combo.addItem('Spring', utils.Season.SPRING)
        self.season_combo.addItem('Summer', utils.Season.SUMMER)
        self.season_combo.addItem('Fall', utils.Season.FALL)

        today = date.today()
        current_season = (today.month - 1) // 3
        self.season_combo.setCurrentIndex(current_season)

        self.year_spin = QSpinBox()
        self.year_spin.setRange(1900, today.year + 1)
        self.year_spin.setValue(today.year)

        self.search_btn = QPushButton('Refresh')
        self.search_btn.clicked.connect(self.s_search)

        filters_layout = QHBoxLayout()
        filters_layout.addWidget(self.season_combo)
        filters_layout.addWidget(self.year_spin)
        filters_layout.addWidget(self.search_btn)
        filters_layout.addStretch(1)

        self.card_view = AddCardView(
            api_info=api_info, mylist=self.mylist, statuses_dict=self.statuses_dict)
        self.card_view.changed.connect(self.s_selected)
        self.card_view.doubleClicked.connect(self.s_show_details)
        self.card_view.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.card_view.customContextMenuRequested.connect(self.s_context_menu)

        self._filters_layout = filters_layout
        self._layout.addLayout(filters_layout)
        self._layout.addWidget(self.card_view, 1)

        self.worker.changed_list.connect(self._refresh_mylist)

        self.s_search()

    def _teardown_search_ui(self):
        """Reverses _build_search_ui() -- see set_context()'s docstring."""
        self.worker.changed_list.disconnect(self._refresh_mylist)

        self._layout.removeWidget(self.card_view)
        self.card_view.deleteLater()
        self.card_view = None

        self._layout.removeItem(self._filters_layout)
        while self._filters_layout.count():
            # addStretch(1) added a spacer item with no widget() -- only
            # season_combo/year_spin/search_btn need explicit cleanup.
            widget = self._filters_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self._filters_layout = None

        self._unavailable_label.show()
        self._layout.addWidget(self._unavailable_label)

    def worker_call(self, function, ret_function, *args, **kwargs):
        self.worker.set_function(function, ret_function, *args, **kwargs)
        self.worker.start()

    # Slots
    def s_search(self):
        criteria = (self.season_combo.currentData(), self.year_spin.value())

        self.card_view.setEnabled(False)
        self.search_btn.setEnabled(False)

        self.worker_call('search', self.r_searched, criteria, utils.SearchMethod.SEASON)

    def s_selected(self, show):
        self.selected_show = show

    def s_show_details(self):
        if not self.selected_show:
            return

        self._details_window = DetailsDialog(self, self.worker, self.selected_show)
        self._details_window.setModal(True)
        self._details_window.show()

    def s_context_menu(self, pos):
        index = self.card_view.indexAt(pos)
        if not index.isValid():
            return

        source_row = self.card_view.model().mapToSource(index).row()
        show = self.card_view.getModel().results[source_row]

        menu = QMenu(self)
        if show['id'] in self.mylist:
            move_to = menu.addMenu('Move to')
            for status in self.statuses:
                action = move_to.addAction(self.statuses_dict.get(status, str(status)))
                action.triggered.connect(
                    lambda checked=False, s=status, showid=show['id']: self.s_move_to(showid, s))
        else:
            add_action = menu.addAction('Add to list...')
            add_action.triggered.connect(lambda checked=False, s=show: self.s_add(s))
        menu.exec(self.card_view.viewport().mapToGlobal(pos))

    def s_move_to(self, showid, status):
        self.worker_call('set_status', self.r_moved, showid, status)
        if showid in self.mylist:
            self.mylist[showid]['my_status'] = status

    def r_moved(self, result):
        if not result['success']:
            QMessageBox.critical(
                self, 'Error', "Could not change the show's status.")

    def s_add(self, show):
        # Unlike add.py's AddDialog, there's no "currently active tab"
        # here to fall back to -- Plan to Watch (statuses[-1] by
        # convention) is the only sensible default for a browse page.
        default_status = self.statuses[-1] if self.statuses else None

        dialog = AddStatusDialog(
            self,
            show_title=show['title'],
            statuses=self.statuses,
            statuses_dict=self.statuses_dict,
            default_status=default_status,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        chosen_status = dialog.chosen_status()
        self.worker_call(
            'add_show', lambda result, s=show: self.r_added(result, s), show, chosen_status)

    def r_added(self, result, show):
        if result['success']:
            title = show.get('title', 'Show')
            QMessageBox.information(
                self, 'Added', f'"{title}" was added to your list.')
        else:
            QMessageBox.critical(
                self, 'Error', 'Could not add the show to your list.')

    def r_searched(self, result):
        self.card_view.setEnabled(True)
        self.search_btn.setEnabled(True)

        if result['success']:
            self.card_view.setResults(result['result'])
        else:
            self.card_view.setResults(None)

    def _refresh_mylist(self, *_args):
        self.mylist = {show['id']: show for show in self.worker.engine.get_list()}
        if self.card_view is not None:
            self.card_view.set_mylist(self.mylist)
