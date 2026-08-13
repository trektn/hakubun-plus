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

"""The Mirror preview, as a wall of covers.

A Mirror plan is a few hundred works, and a tree of a few hundred rows
is a wall of text: you cannot see at a glance which shows are affected,
and every title has to be READ rather than recognized. The art is the
thing people actually navigate their lists by.

So each work is a tile -- its cover, its title underneath -- and the
changes live behind the cover: point at a tile and the picture fades
out from under the list of what Mirror will do to that work. Nothing is
hidden behind a disclosure the user has to find; the detail is simply
where the pointer already is.

The tiles render `present.MirrorCard`, the same toolkit-agnostic
projection the GTK grid draws, so the two windows say the same words.
"""

from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel,
                             QLayout, QScrollArea, QVBoxLayout, QWidget)

from hakubun.sync import present
from hakubun.ui import posters

# Roughly the proportions of a poster, at a size that fits a useful
# number of them on a laptop screen and still leaves the changes
# readable when they come up over the art.
COVER_W = 200
COVER_H = 285

_ADD_COLOR = '#66bb6a'
_REMOVE_COLOR = '#ef5350'
_UPDATE_COLOR = '#42a5f5'
_LOCAL_COLOR = '#66bb6a'
_CONFLICT_COLOR = '#ffa726'
_OWNERSHIP_COLOR = '#9ccc65'
_MUTED_COLOR = '#9e9e9e'


class FlowLayout(QLayout):
    """Left-to-right wrapping layout (Qt's own flow layout example).

    Qt ships no wrapping layout, and the alternatives both fail here: a
    QGridLayout has to be re-columned by hand on every resize, and the
    icon-mode item views cannot host a widget per item.
    """

    def __init__(self, parent=None, margin=0, spacing=10):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def __del__(self):
        while self.count():
            self.takeAt(0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QtCore.QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QtCore.QSize(margins.left() + margins.right(),
                                   margins.top() + margins.bottom())

    def _do_layout(self, rect, test):
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(),
                                  -margins.right(), -margins.bottom())
        x, y, line_height = effective.x(), effective.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self.spacing()
            if next_x - self.spacing() > effective.right() \
                    and line_height > 0:
                x = effective.x()
                y = y + line_height + self.spacing()
                next_x = x + hint.width() + self.spacing()
                line_height = 0
            if not test:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


def _sizes(widget):
    """Body and aside point sizes, derived from the user's own font.

    The body is the interface font, full stop. An earlier revision ran
    it under -- to fit more rows in a tile -- and simply made the
    preview hard to read; a tile that has more to say than fits scrolls
    instead. Only the reason underneath is set smaller, because it is
    deliberately the quieter half of the row.
    """
    base = widget.font().pointSizeF()
    if base <= 0:                      # font set in pixels
        base = 10.0
    return base, max(7.0, base * 0.86)


def _row_markup(change, color, body_pt, why_pt):
    """A row on two levels: what happens, then why underneath.

    The reason is the part you only read when the change surprises you,
    so it is smaller and quieter -- otherwise every row is a paragraph
    and none of them is scannable.
    """
    parts = ['<span style="color:%s;">%s</span>'
             % (color, _escape(change.head))]
    if change.detail:
        parts.append('<span style="color:#e8eaed;">%s</span>'
                     % _escape(change.detail))
    line = ('<span style="font-size:%.1fpt;">%s</span>'
            % (body_pt, ' '.join(parts)))
    if change.why:
        line += ('<br><span style="color:%s; font-size:%.1fpt;">%s</span>'
                 % (_MUTED_COLOR, why_pt, _escape(change.why)))
    return line


def _escape(text):
    return (text.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


class MirrorRow(QWidget):
    """One line of the overlay: a change, or a note about one.

    Carries the objects the window's context menu branches on, so the
    menu can stay where it already is rather than being reimplemented
    per row type.
    """

    def __init__(self, change, color, issue=None, provider_label='',
                 parent=None):
        super().__init__(parent)
        self.change = change
        self.op = change.op
        self.issue = issue
        self.provider_label = provider_label
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        if self.op is not None and hasattr(self.op, 'selected'):
            # The text is a LABEL beside the box, never the box's own
            # caption: a checkbox does not wrap its caption, and these
            # rows are read in a tile two hundred pixels wide.
            self.check = QCheckBox()
            self.check.setChecked(bool(self.op.selected))
            self.check.toggled.connect(self._on_toggled)
            layout.addWidget(self.check, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        else:
            # Informational: a settled membership decision, or a
            # tracker this work has not been matched on. There is
            # nothing to tick, but the way back to that decision is
            # this row's context menu.
            self.check = None

        mark = QLabel(present.MIRROR_MARKS.get(change.kind, '·'))
        mark.setStyleSheet('color: %s;' % color)
        mark.setFixedWidth(12)
        mark.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop
                          | QtCore.Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(mark, 0)

        body_pt, why_pt = _sizes(self)
        self.label = QLabel(_row_markup(change, color, body_pt, why_pt))
        self.label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        # No context menu policy of its own: the right-click belongs to
        # the tile, which routes it back down to whichever row it
        # landed on. A policy here would swallow the event instead.

    def _on_toggled(self, checked):
        self.op.selected = checked
        tile = self.parent()
        while tile is not None and not isinstance(tile, MirrorTile):
            tile = tile.parent()
        if tile is not None:
            tile.refresh_check()

    def set_checked(self, checked):
        if self.check is None:
            return
        self.check.blockSignals(True)
        self.check.setChecked(checked)
        self.check.blockSignals(False)

    def text(self):
        """The flat sentence -- what the row SAYS, independent of how
        it is laid out."""
        return self.change.text


class MirrorTile(QFrame):
    """One work: its cover, its title, and its changes behind them."""

    # Cover, then two lines of title and one of summary. Fixed, so the
    # grid is a grid: a tile that grew with its title would stagger
    # every row it appeared in.
    TILE_H = COVER_H + 58

    def __init__(self, card, parent=None):
        super().__init__(parent)
        self.card = card
        self._hovered = False
        self._details = None
        self.rows = []
        self.setFixedSize(COVER_W, self.TILE_H)
        self.setObjectName('mirrorTile')
        self.setStyleSheet(self._frame_style())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.cover_holder = QWidget(self)
        self.cover_holder.setFixedSize(COVER_W, COVER_H)
        self.cover_holder.setStyleSheet('background: #1b1f27;')
        self.cover = QLabel(self.cover_holder)
        self.cover.setGeometry(0, 0, COVER_W, COVER_H)
        self.cover.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.cover.setWordWrap(True)
        self.cover.setText(card.title)
        self.cover.setStyleSheet('color: #6b7280; padding: 12px;')
        layout.addWidget(self.cover_holder)

        self.check = QCheckBox(self.cover_holder)
        self.check.setTristate(True)
        self.check.move(6, 6)
        self.check.setToolTip('Apply every change to this title')
        self.check.setStyleSheet(
            'QCheckBox { background: rgba(0, 0, 0, 140); padding: 2px; }')
        self.check.setVisible(bool(card.ops))
        self.check.clicked.connect(self._on_card_clicked)
        self.refresh_check()

        self.title = QLabel(card.title)
        self.title.setWordWrap(True)
        self.title.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter
                                | QtCore.Qt.AlignmentFlag.AlignTop)
        self.title.setToolTip(card.title)
        # Fixed, not merely maximum: a long title's wrapped label still
        # reports a wider minimum, and a QVBoxLayout honours it -- the
        # text then hangs out over the neighbouring tiles.
        self.title.setFixedSize(COVER_W - 6, 34)
        layout.addWidget(self.title)

        headline = present.mirror_card_headline(card)
        self.headline = QLabel()
        self.headline.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignHCenter
            | QtCore.Qt.AlignmentFlag.AlignTop)
        font = self.headline.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.85))
        self.headline.setFont(font)
        self.headline.setStyleSheet('color: %s;' % self._accent())
        self.headline.setFixedSize(COVER_W - 6, 18)
        # One line, always: this is the glance, and the full account of
        # it is a pointer-move away.
        self.headline.setText(QtGui.QFontMetrics(font).elidedText(
            headline, QtCore.Qt.TextElideMode.ElideRight, COVER_W - 12))
        self.headline.setToolTip(headline)
        layout.addWidget(self.headline)

        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)

    # -- appearance ----------------------------------------------------

    def _accent(self):
        """The colour of the biggest thing happening to this work.

        Deletion outranks everything: it is the only irreversible
        thing Mirror does, and a grid is scanned, not read.
        """
        if any(not hasattr(op, 'field') and not hasattr(op, 'values')
               for op in self.card.ops):
            return _REMOVE_COLOR
        if self.card.conflicts:
            return _CONFLICT_COLOR
        if any(hasattr(op, 'values') for op in self.card.ops):
            return _ADD_COLOR
        return _UPDATE_COLOR

    def _frame_style(self):
        return ('#mirrorTile { border: 1px solid %s; border-radius: 4px; }'
                % self._accent())

    def set_cover(self, pixmap):
        scaled = pixmap.scaled(
            COVER_W, COVER_H,
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        self.cover.setText('')
        self.cover.setPixmap(scaled)

    # -- hover ---------------------------------------------------------

    def enterEvent(self, event):
        self.set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        # Qt sends Leave when the pointer moves onto a CHILD widget, so
        # the overlay itself would keep flickering the details away
        # from under the cursor. Ask where the pointer actually is.
        self.set_hovered(self._pointer_inside())
        super().leaveEvent(event)

    def _pointer_inside(self):
        pos = self.mapFromGlobal(QtGui.QCursor.pos())
        return self.rect().contains(pos)

    def set_hovered(self, hovered):
        if hovered == self._hovered:
            return
        self._hovered = hovered
        if hovered:
            self.ensure_details()
        # The fade IS the panel: its background is nearly-but-not-quite
        # opaque, so the cover reads through it at about a tenth --
        # still the backdrop of its own changes, which is what tells
        # you at a glance which tile you are reading.
        #
        # This used to be a QGraphicsOpacityEffect on the cover, and
        # that is what made scrolling tear: a QScrollArea scrolls by
        # blitting its viewport, and a widget carrying a graphics
        # effect renders through an offscreen pixmap that the blit
        # leaves behind at the old offset.
        if self._details is not None:
            self._details.setVisible(hovered)
        if self.cover.pixmap() is None or self.cover.pixmap().isNull():
            # No art: the title is standing in for the picture, and
            # reading the changes through it is unpleasant.
            self.cover.setText('' if hovered else self.card.title)
        self.check.raise_()

    @property
    def hovered(self):
        return self._hovered

    # -- the changes ---------------------------------------------------

    def ensure_details(self):
        """Build the overlay the first time it is needed.

        A preview can be several hundred tiles; building every one's
        rows up front is a visible pause for detail nobody has asked to
        see yet.
        """
        if self._details is not None:
            return self._details
        panel = QWidget(self.cover_holder)
        panel.setGeometry(0, 0, COVER_W, COVER_H)
        panel.setStyleSheet('background: rgba(12, 15, 20, 225);')
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('background: transparent;')
        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(8, 26, 8, 8)
        inner.setSpacing(7)

        if self.card.desired:
            # What ownership says this work SHOULD be -- the row every
            # delta below is read against. Without it "Score 7 -> 8" is
            # a number the reader has to reconstruct a target from.
            # Set as a heading rather than a sentence: it is the one
            # row that is not a change.
            body_pt, small_pt = _sizes(self)
            says = QLabel(_desired_markup(self.card.desired,
                                          body_pt, small_pt))
            says.setTextFormat(QtCore.Qt.TextFormat.RichText)
            says.setWordWrap(True)
            inner.addWidget(says)

        for change in self.card.rows:
            row = MirrorRow(change, _row_color(change),
                            issue=self.card.issue,
                            provider_label=(change.head
                                            if change.op is None else ''))
            self.rows.append(row)
            inner.addWidget(row)

        for conflict in self.card.conflicts:
            note = QLabel(
                '<span style="color:%s;">%s&nbsp; %s needs your decision'
                '</span>' % (_CONFLICT_COLOR,
                             present.MIRROR_MARKS['conflict'],
                             _escape(present.field_label(conflict.field))))
            note.setTextFormat(QtCore.Qt.TextFormat.RichText)
            note.setWordWrap(True)
            inner.addWidget(note)

        inner.addStretch()
        scroll.setWidget(holder)
        outer.addWidget(scroll)
        panel.setVisible(False)
        self._details = panel
        return panel

    # -- selection -----------------------------------------------------

    def _on_card_clicked(self, _checked):
        """The tile's own tick answers the whole title at once.

        Qt's tristate cycles through Partially on click; a card is
        either all in or all out, so the click is read as "the opposite
        of what it was", never as a third state.
        """
        selected = not all(op.selected for op in self.card.ops)
        for op in self.card.ops:
            op.selected = selected
        for row in self.rows:
            row.set_checked(selected)
        self.refresh_check()

    def refresh_check(self):
        states = {op.selected for op in self.card.ops}
        if states == {True}:
            state = QtCore.Qt.CheckState.Checked
        elif states == {False} or not states:
            state = QtCore.Qt.CheckState.Unchecked
        else:
            state = QtCore.Qt.CheckState.PartiallyChecked
        self.check.blockSignals(True)
        self.check.setCheckState(state)
        self.check.blockSignals(False)

    def set_selected(self, selected):
        for op in self.card.ops:
            op.selected = selected
        for row in self.rows:
            row.set_checked(selected)
        self.refresh_check()


_ROW_COLORS = {'add': _ADD_COLOR, 'remove': _REMOVE_COLOR,
               'update': _UPDATE_COLOR, 'local': _LOCAL_COLOR}


def _row_color(change):
    return _ROW_COLORS.get(change.kind, _MUTED_COLOR)


def _desired_markup(desired, body_pt, small_pt):
    """What ownership says the work should be, as a list of
    field/value pairs rather than one long run-on line."""
    parts = ['<span style="color:%s; font-size:%.1fpt;">OWNERSHIP SAYS'
             '</span>' % (_MUTED_COLOR, small_pt)]
    for name, value, owner, _why in desired:
        line = ('<span style="color:%s;">%s</span> '
                '<span style="color:#e8eaed;">%s</span>'
                % (_OWNERSHIP_COLOR, _escape(name), _escape(value)))
        if owner:
            line += ('<span style="color:%s; font-size:%.1fpt;"> %s</span>'
                     % (_MUTED_COLOR, small_pt, _escape(owner)))
        parts.append('<span style="font-size:%.1fpt;">%s</span>'
                     % (body_pt, line))
    return '<br>'.join(parts)


class MirrorGrid(QScrollArea):
    """The wall of tiles, and the poster downloads that fill it."""

    menu_requested = QtCore.pyqtSignal(object, QtCore.QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._holder = QWidget()
        self._flow = FlowLayout(self._holder, margin=8)
        self.setWidget(self._holder)
        self.tiles = []
        self._by_url = {}
        self._posters = posters.PosterCache(self._poster_ready)
        self._pixmaps = {}

    # -- content -------------------------------------------------------

    def clear(self):
        for tile in self.tiles:
            tile.setParent(None)
            tile.deleteLater()
        self.tiles = []
        self._by_url = {}
        while self._flow.count():
            self._flow.takeAt(0)

    def set_cards(self, cards):
        self.clear()
        for card in cards:
            tile = MirrorTile(card, self._holder)
            tile.setContextMenuPolicy(
                QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            tile.customContextMenuRequested.connect(
                lambda pos, t=tile: self._on_menu(t, pos))
            self._flow.addWidget(tile)
            self.tiles.append(tile)
            self._request_cover(tile)

    def tile_for(self, title):
        for tile in self.tiles:
            if tile.card.title == title:
                return tile
        return None

    def set_all_selected(self, selected):
        for tile in self.tiles:
            tile.set_selected(selected)

    # -- covers --------------------------------------------------------

    def _request_cover(self, tile):
        url = tile.card.image
        if not url:
            return
        self._by_url.setdefault(url, []).append(tile)
        pixmap = self._pixmaps.get(url)
        if pixmap is not None:
            tile.set_cover(pixmap)
            return
        path = self._posters.get(url)
        if path is not None:
            self._show_cover(url, path)

    def _poster_ready(self, url, path):
        """Called from a download thread: hop to the GUI thread before
        touching a widget."""
        QtCore.QMetaObject.invokeMethod(
            self, '_on_poster_ready', QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(str, url), QtCore.Q_ARG(str, path))

    @QtCore.pyqtSlot(str, str)
    def _on_poster_ready(self, url, path):
        self._show_cover(url, path)

    def _show_cover(self, url, path):
        pixmap = self._pixmaps.get(url)
        if pixmap is None:
            pixmap = QtGui.QPixmap(path)
            if pixmap.isNull():
                return
            self._pixmaps[url] = pixmap
        for tile in self._by_url.get(url, []):
            try:
                tile.set_cover(pixmap)
            except RuntimeError:
                # The tile was destroyed by a re-preview between the
                # download starting and finishing.
                pass

    def stop(self):
        self._posters.stop()

    # -- menus ---------------------------------------------------------

    def _on_menu(self, tile, pos):
        """Route a right-click to the row under it.

        The window owns what the menu SAYS (membership decisions are
        its business, not the grid's); the grid only works out which
        row was clicked.
        """
        target = tile.childAt(pos)
        while target is not None and not isinstance(target, MirrorRow):
            target = target.parent()
            if target is tile:
                target = None
        self.menu_requested.emit(target or tile, tile.mapToGlobal(pos))
