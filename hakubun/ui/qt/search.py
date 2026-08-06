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

from PyQt6 import QtCore
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
                             QPushButton, QVBoxLayout, QWidget)

from hakubun import utils
from hakubun.ui.qt.add import AddStatusDialog
from hakubun.ui.qt.widgets import AddTableDetailsView


class SearchWidget(QWidget):
    """Taiga-style keyword search page.

    Real Taiga's own Search dialog (win32 1.4, dlg_search.cpp) is a
    plain grouped table -- "Not in list"/"In list" sections, columns
    Title/Type/Episodes/Score/Season -- with a separate Anime
    Information dialog opened on double-click for details. Reuses
    AddTableDetailsView -- the Add dialog's table+inline-details split
    -- for a persistent page instead of a one-shot modal; the inline
    details pane it already provides is a straight improvement over
    needing a double-click for every result, so it's kept rather than
    reproduced minus that feature. In-list membership is shown via
    AddTableModel's existing "In Your List" column/tint rather than a
    true grouped-list (a native-listview-specific feature with no
    direct Qt equivalent), matching the same tradeoff already made for
    the Seasons page.

    Like Seasons, the actual search UI can't be built until the
    account's api_info/search_methods are known -- see set_context().
    """

    def __init__(self, parent, worker):
        super().__init__(parent)
        self.worker = worker
        self.selected_show = None
        self.mylist = {}
        self.statuses = []
        self.statuses_dict = {}
        self.results_view = None
        self.search_box = None

        self._layout = QVBoxLayout()
        self._unavailable_label = QLabel(
            "Search isn't supported for this account/media type.")
        self._unavailable_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(self._unavailable_label)
        self.setLayout(self._layout)

    def set_context(self, api_info, mediainfo):
        """Called once the engine has (re)loaded -- see MainWindow's
        r_engine_loaded(), which already re-runs this on account and
        mediatype reloads."""
        search_methods = mediainfo.get('search_methods', [utils.SearchMethod.KW])
        if utils.SearchMethod.KW not in search_methods:
            return

        self.statuses = mediainfo['statuses']
        self.statuses_dict = mediainfo['statuses_dict']
        self.mylist = {show['id']: show for show in self.worker.engine.get_list()}

        if self.results_view is None:
            self._build_search_ui()
        else:
            self.results_view.set_mylist(self.mylist)

    def _build_search_ui(self):
        self._unavailable_label.hide()
        self._layout.removeWidget(self._unavailable_label)

        self.search_box = QLineEdit()
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setPlaceholderText('Search title')
        self.search_box.returnPressed.connect(self.s_search)

        search_btn = QPushButton('Search')
        search_btn.clicked.connect(self.s_search)

        search_row = QHBoxLayout()
        search_row.addWidget(self.search_box, 1)
        search_row.addWidget(search_btn)

        self.results_view = AddTableDetailsView(
            None, self.worker, mylist=self.mylist, statuses_dict=self.statuses_dict)
        self.results_view.changed.connect(self.s_selected)
        self.results_view.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.results_view.table.customContextMenuRequested.connect(self.s_context_menu)

        self._layout.addLayout(search_row)
        self._layout.addWidget(self.results_view, 1)

        self.worker.changed_list.connect(self._refresh_mylist)

        self.search_box.setFocus()

    def worker_call(self, function, ret_function, *args, **kwargs):
        self.worker.set_function(function, ret_function, *args, **kwargs)
        self.worker.start()

    # Slots
    def s_search(self):
        query = self.search_box.text().strip()
        if not query:
            return

        self.results_view.setEnabled(False)
        self.results_view.clearSelection()
        self.selected_show = None

        self.worker_call('search', self.r_searched, query, utils.SearchMethod.KW)

    def s_selected(self, show):
        self.selected_show = show

    def s_context_menu(self, pos):
        view = self.results_view.table
        index = view.indexAt(pos)
        if not index.isValid():
            return

        source_row = view.model().mapToSource(index).row()
        show = self.results_view.getModel().results[source_row]

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
        menu.exec(view.viewport().mapToGlobal(pos))

    def s_move_to(self, showid, status):
        self.worker_call('set_status', self.r_moved, showid, status)
        if showid in self.mylist:
            self.mylist[showid]['my_status'] = status

    def r_moved(self, result):
        if not result['success']:
            QMessageBox.critical(
                self, 'Error', "Could not change the show's status.")

    def s_add(self, show):
        # No "currently active tab" to fall back to on a browse page --
        # Plan to Watch (statuses[-1] by convention) is the only
        # sensible default, same as Seasons.
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
        self.results_view.setEnabled(True)

        if result['success']:
            self.results_view.setResults(result['result'])
        else:
            self.results_view.setResults(None)

    def _refresh_mylist(self, *_args):
        self.mylist = {show['id']: show for show in self.worker.engine.get_list()}
        if self.results_view is not None:
            self.results_view.set_mylist(self.mylist)
