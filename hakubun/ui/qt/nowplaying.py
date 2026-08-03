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

from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from hakubun import utils
from hakubun.ui.qt.widgets import DetailsWidget, PlaybackBar


class NowPlayingWidget(QWidget):
    """
    Taiga mode's dedicated "Now Playing" screen: what the tracker
    currently sees playing, at the top -- poster, title, episode, a
    play/pause + progress bar row, Last/Next Episode buttons --
    followed by that show's normal details view (facts + synopsis),
    reused as-is (minus its own title/poster, which would just
    duplicate the ones above).

    When nothing is recognized as playing, the details view is hidden
    entirely (it used to just keep showing whatever was last loaded)
    and replaced with a "play something at random" recommendation. The
    poster stays visible either way (blank until something's playing).
    """

    playRequested = QtCore.pyqtSignal(int, int)  # show_id, episode
    playRandomRequested = QtCore.pyqtSignal()

    def __init__(self, parent, worker, progress_color=None):
        QWidget.__init__(self, parent)
        self.worker = worker
        self._show = None
        self._episode = None

        layout = QVBoxLayout()

        # Built before the rest of the layout so its poster (image_on_right
        # keeps it out of the way of the facts table below, which reads
        # more naturally flush left) can be pulled out and shown up here,
        # above the title -- same position GTK's NowPlayingView shows its
        # own image_box in. Reusing this QLabel instead of loading a
        # second, independent one avoids two ImageWorker threads racing
        # to download and write the same cache file for the same show.
        self.details = DetailsWidget(self, worker, image_on_right=True)
        # The title label below already covers this -- showing it again
        # here just duplicates (and, since this label measures its own
        # not-yet-laid-out width to elide against, truncates) what's
        # already on screen.
        self.details.show_title.hide()

        self.image_label = self.details.show_image
        # addWidget() alone would move image_label's QObject parent to
        # this layout but leave a stale QLayoutItem for it in details'
        # own top_row -- remove it there first.
        self.details.top_row.removeWidget(self.image_label)
        image_row = QHBoxLayout()
        image_row.addStretch(1)
        image_row.addWidget(self.image_label)
        image_row.addStretch(1)
        layout.addLayout(image_row)

        self.title_label = QLabel('Nothing playing')
        title_font = QtGui.QFont()
        title_font.setBold(True)
        title_font.setPointSize(14)
        self.title_label.setFont(title_font)
        self.title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.status_label = QLabel('')
        self.status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        # Play/pause + progress bar and the elapsed/total time, in one row.
        position_row = QHBoxLayout()
        self.playback_bar = PlaybackBar(progress_color=progress_color)
        self.time_label = QLabel('')
        self.time_label.setStyleSheet('color: palette(placeholder-text);')
        position_row.addWidget(self.playback_bar, 1)
        position_row.addWidget(self.time_label)
        layout.addLayout(position_row)

        btn_row = QHBoxLayout()
        self.watch_last_btn = QPushButton('Watch Previous Episode')
        self.watch_last_btn.setEnabled(False)
        self.watch_last_btn.clicked.connect(self._on_watch_last)
        self.watch_next_btn = QPushButton('Watch Next Episode')
        self.watch_next_btn.setEnabled(False)
        self.watch_next_btn.clicked.connect(self._on_watch_next)
        btn_row.addStretch(1)
        btn_row.addWidget(self.watch_last_btn)
        btn_row.addWidget(self.watch_next_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        layout.addWidget(self.details, 1)

        # Shown instead of self.details when nothing is recognized.
        self.empty_widget = QWidget()
        empty_layout = QVBoxLayout()
        empty_layout.addStretch(1)
        empty_label = QLabel("Nothing to show details for yet.")
        empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        empty_label.setStyleSheet('color: palette(mid);')
        empty_layout.addWidget(empty_label)
        random_row = QHBoxLayout()
        self.random_btn = QPushButton('Watch a Random Episode')
        self.random_btn.clicked.connect(
            lambda: self.playRandomRequested.emit())
        random_row.addStretch(1)
        random_row.addWidget(self.random_btn)
        random_row.addStretch(1)
        empty_layout.addLayout(random_row)
        empty_layout.addStretch(1)
        self.empty_widget.setLayout(empty_layout)
        layout.addWidget(self.empty_widget, 1)

        self.setLayout(layout)
        self._show_empty_state()

    def _show_empty_state(self):
        self.details.hide()
        self.empty_widget.show()

    def _show_details_state(self):
        self.empty_widget.hide()
        self.details.show()

    def update_status(self, status):
        state = status.get('state')

        # IGNORED means the tracker recognized the show/episode fine but
        # isn't updating progress for it (e.g. replaying an
        # already-watched episode from the player's own playlist) -- it
        # should still show details, just labeled differently, rather
        # than falling through to the empty state as if nothing were
        # recognized at all.
        if state in (utils.Tracker.PLAYING, utils.Tracker.IGNORED) and status.get('show'):
            self._update_recognized(status)
        else:
            self._show = None
            self._episode = None
            self.watch_last_btn.setEnabled(False)
            self.watch_next_btn.setEnabled(False)
            self.playback_bar.clear()
            self.time_label.setText('')
            self.image_label.clear()
            self._show_empty_state()
            if state == utils.Tracker.UNRECOGNIZED:
                self.title_label.setText('Unrecognized video')
                self.status_label.setText(
                    "The file that's playing doesn't match anything in your list.")
            elif state == utils.Tracker.NOT_FOUND:
                self.title_label.setText('Not in your list')
                self.status_label.setText(
                    "Playing, but this show isn't in your list.")
            else:
                self.title_label.setText('Nothing playing')
                self.status_label.setText('')

    def _update_recognized(self, status):
        tracker_show, episode = status['show']

        show = None
        try:
            show = self.worker.engine.get_show_info(tracker_show['id'])
        except utils.HakubunError:
            pass
        show = show or tracker_show

        is_new_show = self._show is None or self._show['id'] != show['id']
        self._show = show
        self._episode = episode

        self.title_label.setText(show['title'])
        if status.get('state') == utils.Tracker.IGNORED:
            self.status_label.setText('Already watched -- Episode %d' % episode)
        else:
            self.status_label.setText('Now Playing -- Episode %d' % episode)
        self.playback_bar.update_status(status)
        self.time_label.setText(
            utils.format_playback_position(
                status.get('viewOffset'), status.get('length'),
                include_percent=False))
        self.watch_last_btn.setEnabled(episode > 1)
        self.watch_next_btn.setEnabled(True)
        self._show_details_state()

        if is_new_show:
            # Loads facts + poster together -- see the comment in
            # __init__ on why the poster ends up shown above the title
            # instead of where DetailsWidget itself puts it.
            self.details.load(show)

    def _on_watch_last(self):
        if self._show and self._episode and self._episode > 1:
            self.playRequested.emit(self._show['id'], self._episode - 1)

    def _on_watch_next(self):
        if self._show and self._episode:
            self.playRequested.emit(self._show['id'], self._episode + 1)
