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
from PyQt6.QtWidgets import QFormLayout, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from hakubun import utils


class StatisticsWidget(QWidget):
    """Taiga-style Statistics page.

    Pure computation over the already-loaded show list -- no new
    persistence or engine calls beyond what's already fetched for the
    main Anime List, which is why this is the cheapest real Taiga
    destination to add. refresh() is called by MainWindow after every
    engine (re)load (mirroring SeasonsWidget.set_context()) and on any
    signal that could change the numbers (episode/score/status/list
    changes).
    """

    def __init__(self, parent, worker):
        super().__init__(parent)
        self.worker = worker

        self._summary_labels = {}
        self._status_layout = QFormLayout()

        summary_row = QHBoxLayout()
        for key, caption in (
            ('total', 'Total anime'),
            ('episodes', 'Episodes watched'),
            ('mean_score', 'Mean score'),
        ):
            box = QVBoxLayout()
            value_label = QLabel('-')
            value_font = value_label.font()
            value_font.setPointSize(value_font.pointSize() + 6)
            value_font.setBold(True)
            value_label.setFont(value_font)
            value_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

            caption_label = QLabel(caption)
            caption_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

            box.addWidget(value_label)
            box.addWidget(caption_label)
            summary_row.addLayout(box)

            self._summary_labels[key] = value_label

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)

        layout = QVBoxLayout()
        layout.addLayout(summary_row)
        layout.addWidget(divider)
        layout.addLayout(self._status_layout)
        layout.addStretch(1)
        self.setLayout(layout)

        self.worker.changed_list.connect(self.refresh)
        self.worker.changed_show.connect(self.refresh)
        self.worker.changed_show_status.connect(self.refresh)

    def refresh(self, *_args):
        engine = self.worker.engine
        mediainfo = engine.mediainfo
        showlist = list(engine.get_list())

        self._summary_labels['total'].setText(str(len(showlist)))

        episodes = sum(show.get('my_progress') or 0 for show in showlist)
        self._summary_labels['episodes'].setText(str(episodes))

        rated_scores = [
            utils.score_to_display(show['my_score'], mediainfo)
            for show in showlist if show.get('my_score')
        ]
        if rated_scores:
            display_max, _step, step_decimals = utils.score_display_range(mediainfo)
            # A mean is fractional even over a whole-number scale (MAL's
            # 1-10 has step_decimals == 0) -- always show at least one
            # decimal place so e.g. an 8/9 average doesn't silently
            # round to a plain "8".
            decimals = max(1, step_decimals)
            mean_score = sum(rated_scores) / len(rated_scores)
            self._summary_labels['mean_score'].setText(
                '%.*f / %g' % (decimals, mean_score, display_max))
        else:
            self._summary_labels['mean_score'].setText('-')

        counts = {status: 0 for status in mediainfo['statuses']}
        for show in showlist:
            if show['my_status'] in counts:
                counts[show['my_status']] += 1

        while self._status_layout.rowCount():
            self._status_layout.removeRow(0)

        statuses_dict = mediainfo['statuses_dict']
        for status in mediainfo['statuses']:
            self._status_layout.addRow(
                statuses_dict.get(status, str(status)), QLabel(str(counts[status])))
