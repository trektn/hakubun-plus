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

from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QToolTip, QVBoxLayout, QWidget

from hakubun import utils

# Row geometry for the score-distribution chart: the bar itself is capped
# well under the row's pitch (mark spec: a bar is never as thick as its
# slot -- the leftover height is intentional air, not slack to fill).
_ROW_PITCH = 30
_BAR_THICKNESS = 18
_BAR_RADIUS = 4
_LABEL_GUTTER = 34  # fixed-width column for the score/bucket label
_VALUE_GUTTER = 40  # fixed-width column reserved for the count, right of the bar


class _StatTile(QWidget):
    """One KPI tile: a muted caption over a large value -- the "handful
    of headline numbers" case calls for a tile row, not a label:value
    form (see dataviz skill, choosing-a-form.md)."""

    def __init__(self, caption):
        super().__init__()
        self._value_label = QLabel('-')
        value_font = QtGui.QFont()
        value_font.setPointSize(value_font.pointSize() + 6)
        value_font.setBold(True)
        self._value_label.setFont(value_font)

        caption_label = QLabel(caption)
        caption_label.setStyleSheet('color: palette(placeholder-text);')

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(caption_label)
        layout.addWidget(self._value_label)
        self.setLayout(layout)

    def setValue(self, text):
        self._value_label.setText(text)


class _ScoreDistributionChart(QWidget):
    """Horizontal bar chart of show counts per score bucket -- a real
    chart (custom-painted marks, direct value labels, a recessive track
    so proportions read even for near-empty buckets, a hover tooltip)
    rather than a stack of native QProgressBars in a form, which is
    what this replaced.
    """

    def __init__(self, bar_color):
        super().__init__()
        self._bar_color = bar_color
        self._rows = []  # [(label, count), ...], already highest-first
        self._max_count = 0
        self._hovered_row = -1
        self.setMouseTracking(True)
        self.setMinimumHeight(_ROW_PITCH)

    def setData(self, rows):
        self._rows = rows
        self._max_count = max((count for _label, count in rows), default=0)
        self._hovered_row = -1
        self.setMinimumHeight(max(_ROW_PITCH, len(rows) * _ROW_PITCH))
        self.update()

    def _row_at(self, y):
        if not self._rows:
            return -1
        row = int(y // _ROW_PITCH)
        return row if 0 <= row < len(self._rows) else -1

    def mouseMoveEvent(self, event):
        row = self._row_at(event.position().y())
        if row != self._hovered_row:
            self._hovered_row = row
            self.update()
        if row >= 0:
            label, count = self._rows[row]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                '%s: %d show%s' % (label, count, '' if count == 1 else 's'),
                self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event):
        self._hovered_row = -1
        QToolTip.hideText()
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        muted = self.palette().color(QtGui.QPalette.ColorRole.PlaceholderText)
        text_color = self.palette().color(QtGui.QPalette.ColorRole.WindowText)
        track_color = self.palette().color(QtGui.QPalette.ColorRole.AlternateBase)

        if not self._rows:
            painter.setPen(muted)
            painter.drawText(
                self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, 'No scores recorded yet.')
            return

        width = self.width()
        track_left = _LABEL_GUTTER
        track_width = max(1, width - _LABEL_GUTTER - _VALUE_GUTTER)

        for i, (label, count) in enumerate(self._rows):
            top = i * _ROW_PITCH
            bar_top = top + (_ROW_PITCH - _BAR_THICKNESS) // 2

            if i == self._hovered_row:
                painter.fillRect(
                    QtCore.QRect(0, top, width, _ROW_PITCH), track_color.lighter(106))

            # Score/bucket label, left gutter.
            painter.setPen(text_color)
            painter.drawText(
                QtCore.QRect(0, top, _LABEL_GUTTER - 6, _ROW_PITCH),
                QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight,
                label)

            # Recessive track -- lets a bucket with a small count still
            # show its proportion against the full range, instead of a
            # near-invisible sliver on blank surface.
            track_rect = QtCore.QRect(track_left, bar_top, track_width, _BAR_THICKNESS)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(track_color)
            painter.drawRoundedRect(track_rect, _BAR_RADIUS, _BAR_RADIUS)

            if self._max_count and count:
                bar_width = max(_BAR_RADIUS, int(track_width * count / self._max_count))
                painter.setBrush(self._bar_color)
                painter.drawPath(_bar_path(
                    QtCore.QRect(track_left, bar_top, bar_width, _BAR_THICKNESS), _BAR_RADIUS))

            # Value at the tip.
            painter.setPen(text_color)
            painter.drawText(
                QtCore.QRect(width - _VALUE_GUTTER + 6, top, _VALUE_GUTTER - 6, _ROW_PITCH),
                QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft,
                str(count))


def _bar_path(rect, radius):
    """A bar mark: rounded data-end on the right, square at the
    baseline (left) -- never a fully rounded pill, per the mark spec."""
    r = min(radius, rect.height() / 2, rect.width() / 2)
    path = QtGui.QPainterPath()
    if r <= 0:
        path.addRect(QtCore.QRectF(rect))
        return path
    path.moveTo(rect.left(), rect.top())
    path.lineTo(rect.right() - r, rect.top())
    path.arcTo(rect.right() - 2 * r, rect.top(), 2 * r, 2 * r, 90, -90)
    path.lineTo(rect.right(), rect.bottom() - r)
    path.arcTo(rect.right() - 2 * r, rect.bottom() - 2 * r, 2 * r, 2 * r, 0, -90)
    path.lineTo(rect.left(), rect.bottom())
    path.closeSubpath()
    return path


class StatisticsWidget(QWidget):
    """Taiga-style Statistics page: an "Anime list" summary (Anime
    count / Episode count / Mean score / Score deviation, as a KPI
    tile row) plus a Score distribution bar chart -- matching real
    Taiga's own Statistics page.

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

    def __init__(self, parent, worker, bar_color=None):
        super().__init__(parent)
        self.worker = worker

        self._tiles = {}
        tiles_row = QHBoxLayout()
        tiles_row.setSpacing(32)
        for key, caption in (
            ('count', 'Anime count'),
            ('episodes', 'Episode count'),
            ('mean_score', 'Mean score'),
            ('score_deviation', 'Score deviation'),
        ):
            tile = _StatTile(caption)
            tiles_row.addWidget(tile)
            self._tiles[key] = tile
        tiles_row.addStretch(1)

        # Same accent already used for the Anime List's own progress
        # bars (colors['progress_fg']) -- one hue, consistently applied,
        # rather than introducing a second accent just for this chart.
        self._chart = _ScoreDistributionChart(
            QtGui.QColor(bar_color) if bar_color else self.palette().color(
                QtGui.QPalette.ColorRole.Highlight))

        layout = QVBoxLayout()
        layout.addWidget(QLabel('<b>Anime list</b>'))
        layout.addLayout(tiles_row)
        layout.addSpacing(12)
        layout.addWidget(QLabel('<b>Score distribution</b>'))
        layout.addWidget(self._chart)
        layout.addStretch(1)
        self.setLayout(layout)

        self.worker.changed_list.connect(self.refresh)
        self.worker.changed_show.connect(self.refresh)
        self.worker.changed_show_status.connect(self.refresh)

    def refresh(self, *_args):
        engine = self.worker.engine
        mediainfo = engine.mediainfo
        showlist = list(engine.get_list())

        self._tiles['count'].setValue(str(len(showlist)))

        episodes = sum(show.get('my_progress') or 0 for show in showlist)
        self._tiles['episodes'].setValue(str(episodes))

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
            self._tiles['mean_score'].setValue('%.2f' % mean_score)
            self._tiles['score_deviation'].setValue('%.2f' % statistics.pstdev(rated_scores))
        else:
            self._tiles['mean_score'].setValue('-')
            self._tiles['score_deviation'].setValue('-')

        self._refresh_distribution(rated_scores, display_max)

    def _refresh_distribution(self, rated_scores, display_max):
        if not display_max:
            self._chart.setData([])
            return

        # One bar per whole point on scales small enough for that to
        # make sense (MAL/Kitsu-style 1-10); coarser scales (AniList's
        # 0-100 percentage system) get grouped into ~10 equal ranges
        # instead of 100 individual bars.
        if display_max <= 10:
            num_buckets = int(round(display_max))
        else:
            num_buckets = 10
        bucket_size = display_max / num_buckets

        counts = [0] * num_buckets
        for score in rated_scores:
            idx = min(num_buckets - 1, max(0, int((score - 1e-9) // bucket_size)))
            counts[idx] += 1

        rows = []
        for i in reversed(range(num_buckets)):
            upper = (i + 1) * bucket_size
            label = str(int(upper)) if bucket_size == 1 else '%g-%g' % (
                i * bucket_size, upper)
            rows.append((label, counts[i]))

        self._chart.setData(rows)
