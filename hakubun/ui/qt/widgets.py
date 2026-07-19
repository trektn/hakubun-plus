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
import os

from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import (QAbstractItemView, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView,
                             QLabel, QListView, QProgressBar, QScrollArea, QSlider, QSplitter, QTableView,
                             QToolButton, QVBoxLayout, QWidget)

from hakubun import utils
from hakubun.ui.qt.delegates import AddListDelegate, ShowsTableDelegate
from hakubun.ui.qt.models import AddListModel, AddListProxy, AddTableModel, ShowListModel, ShowListProxy
from hakubun.ui.qt.util import getIcon
from hakubun.ui.qt.workers import ImageWorker


class PlaybackBar(QWidget):
    """
    A compact "now playing" row: a play/pause indicator, a progress bar
    for how far into the episode playback is, and the percentage.
    Driven directly off a tracker status dict (see TrackerBase.get_status).
    """

    def __init__(self, parent=None, progress_color=None):
        super().__init__(parent)

        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)

        self.play_icon = QLabel()
        self.play_icon.setFixedWidth(18)
        self.play_icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.play_icon)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        # The OS-theme default chunk color reads as near-black on most
        # dark themes -- use the same accent the show list's own
        # completion bar uses (colors['progress_fg']) instead.
        self.bar.setStyleSheet(
            'QProgressBar { background-color: palette(base); border: none; border-radius: 3px; }'
            'QProgressBar::chunk { background-color: %s; border-radius: 3px; }'
            % (progress_color or '#74C0FA'))
        layout.addWidget(self.bar, 1)

        self.percent_label = QLabel('')
        self.percent_label.setMinimumWidth(36)
        self.percent_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.percent_label)

        self.setLayout(layout)

        self._play_pixmap = getIcon('media-playback-start').pixmap(16, 16)
        self._pause_pixmap = getIcon('media-playback-pause').pixmap(16, 16)

    def update_status(self, status):
        state = status.get('state')
        # IGNORED is a recognized, still-playing episode the tracker just
        # isn't going to submit progress for (e.g. a rewatch) -- position
        # and the play/pause indicator should still reflect it.
        playing = state in (utils.Tracker.PLAYING, utils.Tracker.IGNORED)

        if playing:
            paused = bool(status.get('paused'))
            self.play_icon.setPixmap(
                self._pause_pixmap if paused else self._play_pixmap)
        else:
            self.play_icon.setPixmap(QtGui.QPixmap())

        length = status.get('length')
        offset = status.get('viewOffset')
        if playing and length and offset is not None:
            percent = min(100, round(offset / length * 100))
            self.bar.setValue(percent)
            self.percent_label.setText('%d%%' % percent)
        else:
            self.bar.setValue(0)
            self.percent_label.setText('')

    def clear(self):
        self.update_status({'state': None})


class DetailsWidget(QWidget):
    # Shown as a full-width prose block with its own heading, instead of
    # a "Label: Value" row -- everything else from the API's 'extra' list
    # (Type, Episodes, Status, Score, Studios, alternate titles, etc.) is
    # backend-specific and can't be known in advance, so it's rendered
    # generically as an aligned facts table rather than guessing which
    # ones deserve special treatment.
    PROSE_KEYS = {'Synopsis'}

    def __init__(self, parent, worker, image_on_right=False):
        self.worker = worker

        QWidget.__init__(self, parent)

        # Build layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.show_title = QLabel()
        show_title_font = QtGui.QFont()
        show_title_font.setBold(True)
        show_title_font.setPointSize(13)
        self.show_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.show_title.setFont(show_title_font)
        self.show_title.setWordWrap(True)

        content = QWidget()
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(14, 14, 14, 14)
        content_layout.setSpacing(16)

        top_row = QHBoxLayout()
        top_row.setSpacing(18)

        self.show_image = QLabel()
        self.show_image.setFixedSize(200, 280)
        self.show_image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.show_image.setStyleSheet(
            "border: 1px solid palette(mid); border-radius: 3px;")

        self.facts_layout = QFormLayout()
        self.facts_layout.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.facts_layout.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.facts_layout.setVerticalSpacing(8)
        self.facts_layout.setHorizontalSpacing(12)
        facts_widget = QWidget()
        facts_widget.setLayout(self.facts_layout)

        if image_on_right:
            top_row.addWidget(facts_widget, 1)
            top_row.addWidget(self.show_image)
        else:
            top_row.addWidget(self.show_image)
            top_row.addWidget(facts_widget, 1)

        self.show_description = QLabel()
        self.show_description.setWordWrap(True)
        self.show_description.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.show_description.setTextFormat(QtCore.Qt.TextFormat.RichText)

        content_layout.addLayout(top_row)
        content_layout.addWidget(self.show_description)
        content_layout.addStretch(1)
        content.setLayout(content_layout)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_area.setWidget(content)

        main_layout.addWidget(self.show_title)
        main_layout.addWidget(scroll_area)

        self.setLayout(main_layout)

    def worker_call(self, function, ret_function, *args, **kwargs):
        # Run worker in a thread
        self.worker.set_function(function, ret_function, *args, **kwargs)
        self.worker.start()

    def _clear_facts(self):
        while self.facts_layout.rowCount() > 0:
            self.facts_layout.removeRow(0)

    def load(self, show):
        metrics = QtGui.QFontMetrics(self.show_title.font())
        title = metrics.elidedText(
            show['title'], QtCore.Qt.TextElideMode.ElideRight, self.show_title.width())

        self.show_title.setText("<a href=\"%s\">%s</a>" % (
            html.escape(show['url']), html.escape(title)))
        self.show_title.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.show_title.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextBrowserInteraction)
        self.show_title.setOpenExternalLinks(True)

        # Load show info
        self._clear_facts()
        self.facts_layout.addRow(QLabel('Loading details...'))
        self.show_description.setText('')
        self.worker_call('get_show_details', self.r_details_loaded, show)
        api_info = self.worker.engine.api_info

        # Load show image
        if show.get('image'):
            filename = utils.to_cache_path("%s_%s_f_%s.jpg" % (
                api_info['shortname'], api_info['mediatype'], show['id']))

            if os.path.isfile(filename):
                self.s_show_image(filename)
            else:
                utils.make_dir(utils.to_cache_path())
                self.show_image.setText('Downloading...')
                self.image_worker = ImageWorker(
                    show['image'], filename, (200, 280))
                self.image_worker.finished.connect(self.s_show_image)
                self.image_worker.start()
        else:
            self.show_image.setText('No image')

    def s_show_image(self, filename):
        self.show_image.setPixmap(QtGui.QPixmap(filename).scaled(
            self.show_image.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation))

    def r_details_loaded(self, result):
        self._clear_facts()

        if not result['success']:
            self.facts_layout.addRow(
                QLabel('There was an error while getting details.'))
            return

        details = result['result']
        prose_blocks = []

        for key, value in details['extra']:
            if not key or not value:
                continue
            str_value = ', '.join(value) if isinstance(value, list) else str(value)
            if not str_value:
                continue

            if key in self.PROSE_KEYS:
                # str_value's paragraph breaks are real '\n's (see
                # utils.clean_synopsis) -- escape first, then turn those
                # into <br> since RichText otherwise collapses them.
                body = html.escape(str_value).replace('\n', '<br>')
                prose_blocks.append(f"<h3>{html.escape(key)}</h3><p>{body}</p>")
                continue

            value_label = QLabel(str_value)
            value_label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            self.facts_layout.addRow(f'<b>{key}:</b>', value_label)

        if self.facts_layout.rowCount() == 0:
            self.facts_layout.addRow(QLabel('No details available.'))

        self.show_description.setText(''.join(prose_blocks))


class ShowsTableView(QTableView):
    """
    Regular table widget with context menu for show actions.

    """
    middleClicked = QtCore.pyqtSignal()

    def __init__(self, parent=None, palette=None):
        QTableView.__init__(self, parent)

        model = ShowListModel(palette=palette)
        proxymodel = ShowListProxy()
        proxymodel.setSourceModel(model)
        proxymodel.setFilterKeyColumn(-1)
        self.setModel(proxymodel)

        self.setItemDelegate(ShowsTableDelegate(self, palette=palette))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setSectionsMovable(True)
        self.horizontalHeader().setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.verticalHeader().hide()
        self.setGridStyle(QtCore.Qt.PenStyle.NoPen)

    def contextMenuEvent(self, event):
        action = self.context_menu.exec(event.globalPos())

    def mousePressEvent(self, event):
        super().mousePressEvent(event)

        if event.button() == QtCore.Qt.MouseButton.MiddleButton:
            self.middleClicked.emit()


class AddCardView(QListView):
    changed = QtCore.pyqtSignal(dict)

    def __init__(self, parent=None, api_info=None, mylist=None, statuses_dict=None):
        super().__init__(parent)

        m = AddListModel(api_info=api_info)
        proxy = AddListProxy()
        proxy.setSourceModel(m)
        proxy.sort(0, QtCore.Qt.SortOrder.AscendingOrder)

        self.setItemDelegate(AddListDelegate(mylist=mylist, statuses_dict=statuses_dict))
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setModel(proxy)

        self.selectionModel().currentRowChanged.connect(self.s_show_selected)

    def s_show_selected(self, new, old=None):
        if not new:
            return

        index = self.model().mapToSource(new).row()
        selected_show = self.getModel().results[index]

        self.changed.emit(selected_show)

    def setResults(self, results):
        self.getModel().setResults(results)

    def getModel(self):
        return self.model().sourceModel()


class AddTableDetailsView(QSplitter):
    """ This is a splitter widget that contains a table and
    a details widget. Used in the Add Show dialog. """

    changed = QtCore.pyqtSignal(dict)

    def __init__(self, parent=None, worker=None, mylist=None, statuses_dict=None):
        super().__init__(parent)

        self.table = QTableView()
        m = AddTableModel(mylist=mylist, statuses_dict=statuses_dict)
        proxy = QtCore.QSortFilterProxyModel()
        proxy.setSourceModel(m)

        self.table.setGridStyle(QtCore.Qt.PenStyle.NoPen)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setModel(proxy)

        # Allow sorting but don't sort by default
        self.table.horizontalHeader().setSortIndicator(-1, QtCore.Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)

        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self.table.selectionModel().currentRowChanged.connect(self.s_show_selected)
        self.addWidget(self.table)

        self.details = DetailsWidget(parent, worker)
        self.addWidget(self.details)

        self.setSizes([1, 1])

    def s_show_selected(self, new, old=None):
        if not new:
            return

        index = self.table.model().mapToSource(new).row()
        selected_show = self.getModel().results[index]
        self.details.load(selected_show)

        self.changed.emit(selected_show)

    def setResults(self, results):
        self.getModel().setResults(results)

    def getModel(self):
        return self.table.model().sourceModel()

    def clearSelection(self):
        return self.table.clearSelection()


class HoverProgressBar(QWidget):
    """A progress bar with +/- buttons overlaid on its left/right edges
    instead of sitting beside it -- Taiga mode's compact episode
    increment control. The buttons stay real (never hidden -- their
    keyboard shortcuts would stop firing if they were), just styled
    transparent until the bar is hovered.

    Exposes the same setFormat/setMaximum/setValue/setEnabled surface
    as a plain QProgressBar so callers don't need to know which one
    they have.
    """

    incremented = QtCore.pyqtSignal()
    decremented = QtCore.pyqtSignal()

    _BTN_HIDDEN = "QToolButton { border: none; background: transparent; color: transparent; }"
    _BTN_VISIBLE = "QToolButton { border: none; background: palette(mid); color: palette(text); }"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)

        self.bar = QProgressBar(self)

        self.dec_btn = QToolButton(self)
        self.dec_btn.setText('-')
        self.dec_btn.setAutoRaise(True)
        self.dec_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.dec_btn.setStyleSheet(self._BTN_HIDDEN)
        self.dec_btn.clicked.connect(self.decremented.emit)

        self.inc_btn = QToolButton(self)
        self.inc_btn.setText('+')
        self.inc_btn.setAutoRaise(True)
        self.inc_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.inc_btn.setStyleSheet(self._BTN_HIDDEN)
        self.inc_btn.clicked.connect(self.incremented.emit)

        self.setMinimumHeight(self.bar.sizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.bar.setGeometry(0, 0, self.width(), self.height())
        btn_w = max(20, min(28, self.width() // 5))
        self.dec_btn.setGeometry(0, 0, btn_w, self.height())
        self.inc_btn.setGeometry(self.width() - btn_w, 0, btn_w, self.height())

    def enterEvent(self, event):
        self.dec_btn.setStyleSheet(self._BTN_VISIBLE)
        self.inc_btn.setStyleSheet(self._BTN_VISIBLE)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.dec_btn.setStyleSheet(self._BTN_HIDDEN)
        self.inc_btn.setStyleSheet(self._BTN_HIDDEN)
        super().leaveEvent(event)

    def setFormat(self, fmt):
        self.bar.setFormat(fmt)

    def setMaximum(self, value):
        self.bar.setMaximum(value)

    def setValue(self, value):
        self.bar.setValue(value)

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        self.bar.setEnabled(enabled)
        self.dec_btn.setEnabled(enabled)
        self.inc_btn.setEnabled(enabled)


class ScoreSlider(QWidget):
    """
    Score control combining a slider (for quick, approximate rating) with
    a small spin box (for exact values and fine-grained scales like
    AniList's 0-100). Both stay in sync and operate on the API's raw
    score scale internally, but present a friendlier display scale via
    utils.score_display_range/score_display_factor (see there for why:
    e.g. Kitsu's 0-5 in 0.25 increments is shown as 0-10 in 0.5
    increments, matching the convention of larger-scale APIs).

    A value of zero is always "no score set yet" and is shown as such
    rather than a literal 0.
    """

    valueChanged = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._raw_step = 1
        self._raw_decimals = 0
        self._factor = 1
        self._syncing = False

        self.slider = QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setMinimumWidth(160)
        self.slider.valueChanged.connect(self._on_slider_changed)

        self.spin = QDoubleSpinBox()
        self.spin.setMinimumWidth(70)
        self.spin.setSpecialValueText('Unrated')
        self.spin.valueChanged.connect(self._on_spin_changed)

        # The slider needs real width to be usable (fine-grained scales
        # like AniList's 0-100 need every pixel of resolution they can
        # get), so it gets its own full-width row; the spin box and any
        # extra widgets (e.g. a "Set" button) sit on a second row below
        # instead of competing with the slider for horizontal space.
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.slider)

        self._extra_row = QHBoxLayout()
        self._extra_row.setContentsMargins(0, 0, 0, 0)
        self._extra_row.addWidget(self.spin)
        self._extra_row.addStretch(1)
        layout.addLayout(self._extra_row)

        self.setLayout(layout)

    def add_extra_widget(self, widget):
        """
        Appends an external widget (e.g. a "Set" button) into the row
        below the slider, so a caller can give the whole group a single
        form-layout row without the slider having to compete with it for
        horizontal space.
        """
        self._extra_row.addWidget(widget)

    def setMediaInfo(self, mediainfo):
        self._raw_step = mediainfo['score_step']
        self._raw_decimals = utils.decimal_places(self._raw_step)
        self._factor = utils.score_display_factor(mediainfo)
        display_max, display_step, display_decimals = utils.score_display_range(mediainfo)

        ticks = round(mediainfo['score_max'] / self._raw_step)

        self._syncing = True
        self.slider.setRange(0, ticks)
        self.spin.setDecimals(display_decimals)
        self.spin.setSingleStep(display_step)
        self.spin.setRange(0, display_max)
        self.slider.setValue(0)
        self.spin.setValue(0)
        self._syncing = False

    def setValue(self, raw_score):
        tick = round((raw_score or 0) / self._raw_step) if self._raw_step else 0
        self._set_tick(tick)

    def value(self):
        raw = self.slider.value() * self._raw_step
        return round(raw, self._raw_decimals) if self._raw_decimals else int(raw)

    def _tick_to_display(self, tick):
        raw = tick * self._raw_step
        raw = round(raw, self._raw_decimals) if self._raw_decimals else raw
        return raw * self._factor

    def _display_to_tick(self, display_value):
        raw = display_value / self._factor
        return round(raw / self._raw_step) if self._raw_step else 0

    def _set_tick(self, tick):
        self._syncing = True
        self.slider.setValue(tick)
        self.spin.setValue(self._tick_to_display(tick))
        self._syncing = False

    def _on_slider_changed(self, tick):
        if self._syncing:
            return
        self._set_tick(tick)
        self.valueChanged.emit()

    def _on_spin_changed(self, display_value):
        if self._syncing:
            return
        self._set_tick(self._display_to_tick(display_value))
        self.valueChanged.emit()
