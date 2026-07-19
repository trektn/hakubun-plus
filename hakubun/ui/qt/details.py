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

from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QTabWidget, QVBoxLayout

from hakubun.ui.qt.widgets import DetailsWidget


class DetailsDialog(QDialog):
    def __init__(self, parent, worker, show, edit_widget=None, on_go_to=None):
        QDialog.__init__(self, parent)
        self.setMinimumSize(530, 550)
        self.setWindowTitle('Details')
        self.worker = worker
        # In Taiga mode this is MainWindow's own progress/score/status/tags
        # widget, on loan for as long as this dialog is open (see
        # MainWindow.s_show_details) -- detach it on close so it survives
        # this dialog's destruction and can be reused next time.
        self.edit_widget = edit_widget
        self.on_go_to = on_go_to

        main_layout = QVBoxLayout()
        details = DetailsWidget(self, worker)

        if edit_widget is not None:
            tabs = QTabWidget()
            tabs.addTab(details, 'Details')
            tabs.addTab(edit_widget, 'Edit')
            main_layout.addWidget(tabs)
        else:
            main_layout.addWidget(details)

        bottom_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bottom_buttons.setCenterButtons(True)
        bottom_buttons.rejected.connect(self.close)

        # Only set when this show is already in the user's list (see
        # AddDialog.s_show_details) -- lets the user jump straight to it
        # in the main list instead of going through Add again.
        if on_go_to is not None:
            go_to_btn = bottom_buttons.addButton(
                'Go to', QDialogButtonBox.ButtonRole.ActionRole)
            go_to_btn.clicked.connect(self.s_go_to)

        main_layout.addWidget(bottom_buttons)

        self.setLayout(main_layout)
        details.load(show)

    def s_go_to(self):
        self.on_go_to()
        self.close()

    def closeEvent(self, event):
        if self.edit_widget is not None:
            self.edit_widget.setParent(None)
        super().closeEvent(event)
