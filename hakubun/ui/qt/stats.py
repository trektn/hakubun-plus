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

import statistics

from PyQt6 import QtCore
from PyQt6.QtWidgets import QFormLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from hakubun import utils


class StatisticsWidget(QWidget):
    """Taiga-style Statistics page: an "Anime list" summary block (Anime
    count / Episode count / Mean score / Score deviation) plus a Score
    distribution bar chart -- matching real Taiga's own Statistics page.

    Real Taiga also shows "Time spent watching"/"Time to complete",
    computed from each anime's episode-length metadata. Hakubun doesn't
    track per-episode duration anywhere in its show model (checked
    libanilist.py/libmal.py -- no 'duration' field is ever populated),
    so those two lines are left out rather than faked with a guessed
    average length. Also left out: Taiga's "Local database"/app-uptime
    section, which is Taiga-internal (torrent cache size, connection
    counts) and doesn't map onto hakubun's architecture.

    Pure computation over the already-loaded show list, no new engine
    calls -- refresh() is called by MainWindow after every engine
    (re)load (mirroring SeasonsWidget.set_context()) and kept current
    via worker signals for episode/score/status/list changes.
    """

    def __init__(self, parent, worker):
        super().__init__(parent)
        self.worker = worker

        self._summary_form = QFormLayout()
        self._summary_labels = {}
        for key, caption in (
            ('count', 'Anime count:'),
            ('episodes', 'Episode count:'),
            ('mean_score', 'Mean score:'),
            ('score_deviation', 'Score deviation:'),
        ):
            label = QLabel('-')
            self._summary_form.addRow(caption, label)
            self._summary_labels[key] = label

        self._distribution_layout = QFormLayout()

        layout = QVBoxLayout()
        layout.addWidget(QLabel('<b>Anime list</b>'))
        layout.addLayout(self._summary_form)
        layout.addWidget(QLabel('<b>Score distribution</b>'))
        layout.addLayout(self._distribution_layout)
        layout.addStretch(1)
        self.setLayout(layout)

        self.worker.changed_list.connect(self.refresh)
        self.worker.changed_show.connect(self.refresh)
        self.worker.changed_show_status.connect(self.refresh)

    def refresh(self, *_args):
        engine = self.worker.engine
        mediainfo = engine.mediainfo
        showlist = list(engine.get_list())

        self._summary_labels['count'].setText(str(len(showlist)))

        episodes = sum(show.get('my_progress') or 0 for show in showlist)
        self._summary_labels['episodes'].setText(str(episodes))

        rated_scores = [
            utils.score_to_display(show['my_score'], mediainfo)
            for show in showlist if show.get('my_score')
        ]
        display_max, _step, _step_decimals = utils.score_display_range(mediainfo)

        if rated_scores:
            mean_score = sum(rated_scores) / len(rated_scores)
            # Fixed 2 decimals regardless of the account's score step,
            # matching real Taiga's Statistics page (e.g. "7.13"/"2.00")
            # rather than the coarser precision used for score *input*.
            self._summary_labels['mean_score'].setText('%.2f' % mean_score)
            self._summary_labels['score_deviation'].setText(
                '%.2f' % statistics.pstdev(rated_scores))
        else:
            self._summary_labels['mean_score'].setText('-')
            self._summary_labels['score_deviation'].setText('-')

        self._refresh_distribution(rated_scores, display_max)

    def _refresh_distribution(self, rated_scores, display_max):
        while self._distribution_layout.rowCount():
            self._distribution_layout.removeRow(0)

        if not display_max:
            return

        # One bar per whole point on scales small enough for that to
        # make sense (MAL/Kitsu-style 1-10); coarser scales (AniList's
        # 0-100 percentage system) get grouped into ~10 equal ranges
        # instead of 100 individual bars.
        if display_max <= 10:
            num_buckets = int(round(display_max))
            bucket_size = display_max / num_buckets
        else:
            num_buckets = 10
            bucket_size = display_max / num_buckets

        counts = [0] * num_buckets
        for score in rated_scores:
            idx = min(num_buckets - 1, max(0, int((score - 1e-9) // bucket_size)))
            counts[idx] += 1

        max_count = max(counts) if counts else 0

        for i in reversed(range(num_buckets)):
            upper = (i + 1) * bucket_size
            label = str(int(upper)) if bucket_size == 1 else '%g-%g' % (
                i * bucket_size, upper)

            bar = QProgressBar()
            bar.setMaximum(max_count or 1)
            bar.setValue(counts[i])
            bar.setFormat(str(counts[i]))
            bar.setTextVisible(True)
            bar.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

            self._distribution_layout.addRow(label, bar)
