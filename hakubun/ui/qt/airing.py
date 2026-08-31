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

import datetime
import os

from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QScrollArea,
                             QSizePolicy, QVBoxLayout, QWidget)

from hakubun import utils
from hakubun.i18n import _
from hakubun.ui.qt.workers import ImageWorker

_CARD_WIDTH = 130
_IMAGE_SIZE = (110, 155)


class AiringShowCard(QFrame):
    """One show's upcoming episode: poster, title, relative air time
    (absolute time on hover), and whether the user is caught up."""

    def __init__(self, entry, api_info):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedWidth(_CARD_WIDTH)

        show = entry['show']
        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.image_label = QLabel(_('...'))
        self.image_label.setFixedSize(*_IMAGE_SIZE)
        self.image_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet(
            "border: 1px solid palette(mid); border-radius: 3px;")
        layout.addWidget(self.image_label, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

        title_label = QLabel(show['title'])
        title_label.setWordWrap(True)
        title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        title_font = QtGui.QFont()
        title_font.setBold(True)
        title_font.setPointSize(9)
        title_label.setFont(title_font)
        layout.addWidget(title_label)

        when_label = QLabel(_('Ep %d — %s') % (
            entry['episode'], utils.format_relative_airtime(entry['airing_at'])))
        when_label.setWordWrap(True)
        when_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        when_label.setToolTip(utils.format_local_time(entry['airing_at']))
        layout.addWidget(when_label)

        behind_by = (entry['episode'] - 1) - (show.get('my_progress') or 0)
        status_label = QLabel(
            _('Up to date') if behind_by <= 0 else _('Behind by %d') % behind_by)
        status_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        status_label.setStyleSheet(
            'color: #4CAF50; font-weight: bold;' if behind_by <= 0
            else 'color: #E5A400; font-weight: bold;')
        layout.addWidget(status_label)

        self.setLayout(layout)

        if not (show.get('image_thumb') or show.get('image')):
            self.image_label.setText(_('No image'))

    def set_pixmap_from_file(self, filename):
        self.image_label.setPixmap(QtGui.QPixmap(filename).scaled(
            self.image_label.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation))


class _DayColumn(QWidget):
    def __init__(self, date):
        super().__init__()
        self.setFixedWidth(_CARD_WIDTH + 14)

        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)

        header = QLabel(utils.format_airing_day(date))
        header.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        header_font = QtGui.QFont()
        header_font.setBold(True)
        header.setFont(header_font)
        today = datetime.date.today()
        if date == today:
            header.setStyleSheet('color: #74C0FA;')
        layout.addWidget(header)

        self.cards_layout = QVBoxLayout()
        self.cards_layout.setSpacing(8)
        layout.addLayout(self.cards_layout)
        layout.addStretch(1)

        self.setLayout(layout)

    def add_card(self, entry, api_info):
        card = AiringShowCard(entry, api_info)
        self.cards_layout.addWidget(card)
        return card


class AiringScheduleDialog(QDialog):
    """
    A week-view (today + the next 6 days) of the airing shows in the
    user's list, cross-referenced from AniList's public API -- see
    Engine.get_airing_schedule(). Each show gets a card (poster, title,
    relative air time, caught-up/behind status) under the day it airs.
    """

    def __init__(self, parent, worker):
        QDialog.__init__(self, parent)
        self.worker = worker
        self.setWindowTitle(_('Airing Schedule'))
        self.resize(1080, 420)

        layout = QVBoxLayout()

        self.status_label = QLabel(_('Loading airing schedule...'))
        layout.addWidget(self.status_label)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

        self.week_widget = QWidget()
        self.week_layout = QHBoxLayout()
        self.week_layout.setSpacing(4)
        self.week_widget.setLayout(self.week_layout)
        self.week_widget.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)

        scroll_area.setWidget(self.week_widget)
        layout.addWidget(scroll_area, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self.setLayout(layout)

        # Cards don't manage their own image download -- kept here instead
        # so the worker threads are unambiguously owned by something with
        # a stable lifetime for as long as this dialog is open.
        self._image_workers = []

        self.worker_call('get_airing_schedule', self.r_schedule_loaded)

    def worker_call(self, function, ret_function, *args, **kwargs):
        # set_function owns starting/queueing; don't call worker.start()
        # here (see EngineWorker.set_function).
        self.worker.set_function(function, ret_function, *args, **kwargs)

    def r_schedule_loaded(self, result):
        if not result['success']:
            self.status_label.setText(_('Could not load airing schedule.'))
            return

        schedule = result['result']
        api_info = self.worker.engine.api_info

        today = datetime.date.today()
        days = [today + datetime.timedelta(days=i) for i in range(7)]
        by_day = {day: [] for day in days}
        for entry in schedule:
            day = entry['airing_at'].astimezone().date()
            if day in by_day:
                by_day[day].append(entry)

        total_this_week = sum(len(entries) for entries in by_day.values())
        if not schedule:
            self.status_label.setText(
                _('No airing shows with a known schedule in your list.'))
        elif not total_this_week:
            self.status_label.setText(
                _('Nothing airing in the next 7 days -- %d upcoming episode(s) further out.')
                % len(schedule))
        else:
            self.status_label.setText(
                _('%d episode(s) airing this week.') % total_this_week)

        for i, day in enumerate(days):
            if i > 0:
                separator = QFrame()
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setFrameShadow(QFrame.Shadow.Sunken)
                self.week_layout.addWidget(separator)

            column = _DayColumn(day)
            entries = sorted(by_day[day], key=lambda e: e['airing_at'])
            for entry in entries:
                card = column.add_card(entry, api_info)
                self._load_card_image(card, entry['show'], api_info)
            self.week_layout.addWidget(column)

    def _load_card_image(self, card, show, api_info):
        if not (show.get('image_thumb') or show.get('image')):
            return

        utils.make_dir(utils.to_cache_path())
        filename = utils.to_cache_path("%s_%s_f_%s.jpg" % (
            api_info['shortname'], api_info['mediatype'], show['id']))

        if os.path.isfile(filename):
            card.set_pixmap_from_file(filename)
            return

        image_worker = ImageWorker(
            show['image_thumb'] or show['image'], filename, _IMAGE_SIZE)
        image_worker.finished.connect(card.set_pixmap_from_file)
        self._image_workers.append(image_worker)
        image_worker.start()
