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
# Thinned automatically for accounts with a finer rating scale -- see
# _ScoreDistributionChart.setData().
_ROW_PITCH = 30
_BAR_THICKNESS = 18
_ROW_PITCH_THIN = 18
_BAR_THICKNESS_THIN = 11
_THIN_ABOVE_ROWS = 10
_BAR_RADIUS = 4
_LABEL_GUTTER = 40  # fixed-width column for the score/bucket label
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
        self._row_pitch = _ROW_PITCH
        self._bar_thickness = _BAR_THICKNESS
        self._label_font = QtGui.QFont(self.font())
        self._label_gutter = _LABEL_GUTTER
        self.setMouseTracking(True)
        self.setMinimumHeight(_ROW_PITCH)

    def setData(self, rows):
        self._rows = rows
        self._max_count = max((count for _label, count in rows), default=0)
        self._hovered_row = -1
        # Finer rating scales (a user-configured AniList 100-point
        # account normalized to 20 buckets, or Kitsu's doubled 0-10/0.5
        # scale) mean more rows in the same chart -- thin the bars to
        # fit rather than letting the chart balloon in height.
        if len(rows) > _THIN_ABOVE_ROWS:
            self._row_pitch, self._bar_thickness = _ROW_PITCH_THIN, _BAR_THICKNESS_THIN
        else:
            self._row_pitch, self._bar_thickness = _ROW_PITCH, _BAR_THICKNESS

        self._label_font = QtGui.QFont(self.font())
        if self._row_pitch < _ROW_PITCH:
            self._label_font.setPointSizeF(max(7.0, self._label_font.pointSizeF() - 1))
        # Measure the actual widest label rather than assuming a fixed
        # gutter -- a range label ("95-100") is wider than a bare score
        # ("10"), and a clipped label is worse than a wide one (mark
        # spec: never clip a label that doesn't fit, measure first).
        metrics = QtGui.QFontMetrics(self._label_font)
        widest = max((metrics.horizontalAdvance(label) for label, _count in rows), default=0)
        self._label_gutter = max(_LABEL_GUTTER, widest + 12)

        self.setMinimumHeight(max(self._row_pitch, len(rows) * self._row_pitch))
        self.update()

    def _row_at(self, y):
        if not self._rows:
            return -1
        row = int(y // self._row_pitch)
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

        pitch, thickness = self._row_pitch, self._bar_thickness
        label_gutter = self._label_gutter
        width = self.width()
        track_left = label_gutter
        track_width = max(1, width - label_gutter - _VALUE_GUTTER)

        painter.setFont(self._label_font)

        for i, (label, count) in enumerate(self._rows):
            top = i * pitch
            bar_top = top + (pitch - thickness) // 2

            if i == self._hovered_row:
                painter.fillRect(
                    QtCore.QRect(0, top, width, pitch), track_color.lighter(106))

            # Score/bucket label, left gutter.
            painter.setPen(text_color)
            painter.drawText(
                QtCore.QRect(0, top, label_gutter - 6, pitch),
                QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight,
                label)

            # Recessive track -- lets a bucket with a small count still
            # show its proportion against the full range, instead of a
            # near-invisible sliver on blank surface.
            track_rect = QtCore.QRect(track_left, bar_top, track_width, thickness)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(track_color)
            radius = min(_BAR_RADIUS, thickness / 2)
            painter.drawRoundedRect(track_rect, radius, radius)

            if self._max_count and count:
                bar_width = max(int(radius) or 1, int(track_width * count / self._max_count))
                painter.setBrush(self._bar_color)
                painter.drawPath(_bar_path(
                    QtCore.QRect(track_left, bar_top, bar_width, thickness), radius))

            # Value at the tip.
            painter.setPen(text_color)
            painter.drawText(
                QtCore.QRect(width - _VALUE_GUTTER + 6, top, _VALUE_GUTTER - 6, pitch),
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


def _format_duration(total_minutes):
    """Renders a minute count as e.g. "12d 4h 30m" / "3h 20m" / "45m"."""
    total_minutes = int(round(total_minutes))
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append('%dd' % days)
    if days or hours:
        parts.append('%dh' % hours)
    parts.append('%dm' % minutes)
    return ' '.join(parts)


class StatisticsWidget(QWidget):
    """Taiga-style Statistics page: an "Anime list" summary (counts,
    score stats, and estimated watch time, as a KPI tile row), a Score
    distribution bar chart, and a "Hakubun" section of app-internal
    numbers real Taiga has no equivalent for -- matching real Taiga's
    own Statistics page in spirit without faking the pieces (torrent
    cache, uptime) that don't map onto hakubun's architecture.

    "Time spent watching"/"Time to complete" use each show's 'duration'
    (minutes/episode), sourced from the API where it provides one
    (MAL's average_episode_duration, AniList's duration, Kitsu's
    episodeLength) -- shows lacking it are simply excluded from the sum
    rather than assumed to have some guessed average length, so the
    total is a real (if sometimes partial) figure, never a fabricated
    one. "Hakubun" numbers (unsynced items, undo history, local library
    files, pinned folders) are read directly off the engine/data
    handler, not estimated.

    Pure computation over already-loaded state, no new engine calls --
    refresh() is called by MainWindow after every engine (re)load
    (mirroring SeasonsWidget.set_context()) and kept current via worker
    signals for episode/score/status/list/queue/undo changes.
    """

    def __init__(self, parent, worker, bar_color=None):
        super().__init__(parent)
        self.worker = worker

        self._tiles = {}
        anime_row = QHBoxLayout()
        anime_row.setSpacing(32)
        for key, caption in (
            ('count', 'Anime count'),
            ('episodes', 'Episode count'),
            ('mean_score', 'Mean score'),
            ('score_deviation', 'Score deviation'),
            ('watch_time', 'Time spent watching'),
            ('complete_time', 'Time to complete'),
        ):
            tile = _StatTile(caption)
            anime_row.addWidget(tile)
            self._tiles[key] = tile
        anime_row.addStretch(1)

        # can_play-gated: local library files/pinned folders only mean
        # anything for a mediatype the engine can actually scan/play
        # (see _refresh_hakubun_section).
        hakubun_row = QHBoxLayout()
        hakubun_row.setSpacing(32)
        for key, caption in (
            ('unsynced', 'Unsynced items'),
            ('undo_history', 'Undo history'),
            ('library_files', 'Local library files'),
            ('pinned_folders', 'Pinned folders'),
        ):
            tile = _StatTile(caption)
            hakubun_row.addWidget(tile)
            self._tiles[key] = tile
        hakubun_row.addStretch(1)

        # Same accent already used for the Anime List's own progress
        # bars (colors['progress_fg']) -- one hue, consistently applied,
        # rather than introducing a second accent just for this chart.
        self._chart = _ScoreDistributionChart(
            QtGui.QColor(bar_color) if bar_color else self.palette().color(
                QtGui.QPalette.ColorRole.Highlight))

        layout = QVBoxLayout()
        layout.addWidget(QLabel('<b>Anime list</b>'))
        layout.addLayout(anime_row)
        layout.addSpacing(12)
        layout.addWidget(QLabel('<b>Score distribution</b>'))
        layout.addWidget(self._chart)
        layout.addSpacing(12)
        layout.addWidget(QLabel('<b>Hakubun</b>'))
        layout.addLayout(hakubun_row)
        layout.addStretch(1)
        self.setLayout(layout)

        self.worker.changed_list.connect(self.refresh)
        self.worker.changed_show.connect(self.refresh)
        self.worker.changed_show_status.connect(self.refresh)
        self.worker.changed_queue.connect(self.refresh)
        self.worker.undo_stack_changed.connect(self.refresh)

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
        display_max, display_step, _decimals = utils.score_display_range(mediainfo)

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

        self._refresh_distribution(rated_scores, display_max, display_step)
        self._refresh_watch_time(showlist, mediainfo)
        self._refresh_hakubun_section(engine, mediainfo)

    def _refresh_watch_time(self, showlist, mediainfo):
        watched_minutes = sum(
            (show.get('my_progress') or 0) * show['duration']
            for show in showlist if show.get('duration'))

        finish_statuses = set(mediainfo.get('statuses_finish', []))
        remaining_minutes = 0
        for show in showlist:
            if not show.get('duration') or not show.get('total'):
                continue
            if show.get('my_status') in finish_statuses:
                continue
            remaining_eps = show['total'] - (show.get('my_progress') or 0)
            if remaining_eps > 0:
                remaining_minutes += remaining_eps * show['duration']

        has_duration = any(show.get('duration') for show in showlist)
        self._tiles['watch_time'].setValue(
            _format_duration(watched_minutes) if has_duration else '-')
        self._tiles['complete_time'].setValue(
            _format_duration(remaining_minutes) if has_duration else '-')

    def _refresh_hakubun_section(self, engine, mediainfo):
        self._tiles['unsynced'].setValue(str(len(engine.get_queue())))
        self._tiles['undo_history'].setValue(str(engine.undo_count()))

        can_play = bool(mediainfo.get('can_play'))
        self._tiles['library_files'].setVisible(can_play)
        self._tiles['pinned_folders'].setVisible(can_play)
        if can_play:
            self._tiles['library_files'].setValue(str(engine.library_file_count()))
            self._tiles['pinned_folders'].setValue(str(engine.show_folder_count()))

    def _refresh_distribution(self, rated_scores, display_max, display_step):
        if not display_max:
            self._chart.setData([])
            return

        # One bucket per value the account's own rating system can
        # actually produce -- respects a 5-star account's 5 buckets, a
        # 3-point (smiley) account's 3, MAL/AniList's 10-point's 10, and
        # Kitsu's doubled 0-10/0.5 scale's 20. The one exception is a
        # scale fine enough to make a bucket-per-value chart unreadable
        # (AniList's 100-point system, or a decimal 10-point one) --
        # those fold to the same 20-bucket resolution Kitsu's 0.5-step
        # scale already uses, rather than 100 razor-thin bars.
        num_buckets = int(round(display_max / display_step)) if display_step else int(
            round(display_max))
        if num_buckets > 20:
            num_buckets = 20
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
