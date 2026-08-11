from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import (QDoubleSpinBox, QStyle, QStyleFactory, QStyleOptionProgressBar,
                             QStyledItemDelegate)

from hakubun import utils
from hakubun.ui.qt.util import IN_LIST_COLOR, getColor

MARGIN = 5
PADDING = 5
WIDTH = 450
# A floor, not a target: sizeHint() measures the rows a card will
# actually draw and grows past this when it needs to. Keeps a card with
# few facts and a short synopsis from collapsing to less than the
# thumbnail's height.
MIN_HEIGHT = 250
# Fact rows every card draws: Season, Type, Episodes.
FIXED_ROWS = 3
# Rows drawn only when the entry has them: Score, Popularity, Airs, In
# List. Cards are sized as if all four are present (see sizeHint).
OPTIONAL_ROWS = 4
# Lines of synopsis a card is sized to fit under its fact rows. The
# synopsis is the last thing painted, into whatever vertical space is
# left over, so without reserving room for it the optional rows (Score,
# Popularity, Airs, In List) push it off the bottom of the card -- which
# is exactly what happened on the Seasons page, where every one of those
# rows is populated.
SYNOPSIS_LINES = 5
COLUMN_A = 100
COLUMN_B = 290


class AddListDelegate(QStyledItemDelegate):
    """ This is the delegate that handles the rendering of cards
    in the List View of the Add show dialog. """

    def __init__(self, parent=None, mylist=None, statuses_dict=None):
        self.results = None
        self.mylist = mylist or {}
        self.statuses_dict = statuses_dict or {}

        self.font = QtGui.QFont()

        fm = QtGui.QFontMetrics(self.font)
        self.fh = fm.height()

        # Get theme colors
        palette = QtGui.QPalette()
        self.alternatebasecolor = palette.color(palette.ColorRole.AlternateBase)
        self.windowtextcolor = palette.color(palette.ColorRole.WindowText)
        self.windowcolor = palette.color(palette.ColorRole.Window)

        super().__init__(parent)

    def set_mylist(self, mylist):
        """Refresh the "already in my list" highlighting. AddDialog is a
        modal that only ever needs one snapshot, but a persistent page
        (e.g. Taiga mode's Seasons page) outlives adds/status changes
        and needs to keep this current."""
        self.mylist = mylist or {}

    def _get_extra(self, extra, key):
        for k, v in extra:
            if k == key:
                return v

    def paint(self, painter, option, index):
        outerRect = option.rect - \
            QtCore.QMargins(MARGIN, MARGIN, MARGIN, MARGIN)

        data = index.data()
        thumb = index.data(QtCore.Qt.ItemDataRole.DecorationRole)

        mylist_entry = self.mylist.get(data.get('id'))
        in_list_label = self.statuses_dict.get(
            mylist_entry['my_status'], '?') if mylist_entry else None
        airing_time = data.get('airing_time')
        score_label = data.get('platform_score')
        popularity_label = data.get('popularity_label')

        painter.save()

        color = index.data(QtCore.Qt.ItemDataRole.BackgroundRole)

        # Draw background box -- tinted if this show is already in the
        # user's list, so it's not confused with a brand new result.
        painter.setPen(QtGui.QPen(self.alternatebasecolor))
        painter.setBrush(QtGui.QBrush(
            IN_LIST_COLOR if mylist_entry else self.windowcolor))
        painter.drawRect(outerRect)

        # Prepare to draw inside
        baseRect = outerRect - \
            QtCore.QMargins(PADDING, PADDING, PADDING, PADDING)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)

        # Draw thumbnail (if any)
        if thumb:
            painter.drawImage(baseRect.topLeft(), thumb)

        # Create text QRect and draw the title background
        textRect = baseRect.adjusted(COLUMN_A+5, 0, 0, 0)
        textRect.setHeight(self.fh + 5)
        painter.setBrush(QtGui.QBrush(color))
        painter.drawRect(textRect)

        # Set our font to bold
        bfont = QtGui.QFont(self.font)
        bfont.setWeight(QtGui.QFont.Weight.Bold)

        painter.setFont(bfont)
        painter.setPen(QtGui.QPen(QtGui.QColor(10, 10, 10)))

        # Make some padding
        textRect -= QtCore.QMargins(5, 0, 5, 0)

        # Draw title
        painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignVCenter, data['title'])

        # The highlight background is always light, so force dark text
        # there regardless of the app's theme -- otherwise light-on-light
        # text from a dark theme becomes unreadable.
        detail_textcolor = QtGui.QColor(30, 30, 30) if mylist_entry else self.windowtextcolor
        painter.setPen(QtGui.QPen(detail_textcolor))

        # Draw the details
        textRect.setHeight(self.fh)
        dataRect = textRect.adjusted(75, 0, 0, 0)

        textRect.translate(0, self.fh + 10)
        painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignTop, "Season")
        textRect.translate(0, self.fh + 5)
        painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignTop, "Type")
        textRect.translate(0, self.fh + 5)
        painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignTop, "Episodes")
        if score_label:
            textRect.translate(0, self.fh + 5)
            painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignTop, "Score")
        if popularity_label:
            textRect.translate(0, self.fh + 5)
            painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignTop, "Popularity")
        if airing_time:
            textRect.translate(0, self.fh + 5)
            painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignTop, "Airs")
        if in_list_label:
            textRect.translate(0, self.fh + 5)
            painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignTop, "In List")

        # Draw data
        painter.setFont(self.font)

        dataRect.translate(0, self.fh + 10)
        painter.drawText(dataRect, QtCore.Qt.AlignmentFlag.AlignTop,
                         utils.get_season_label(data))
        dataRect.translate(0, self.fh + 5)
        painter.drawText(dataRect, QtCore.Qt.AlignmentFlag.AlignTop,
                         str(data.get('type') or '?'))
        dataRect.translate(0, self.fh + 5)
        painter.drawText(dataRect, QtCore.Qt.AlignmentFlag.AlignTop,
                         str(data.get('total') or '?'))
        if score_label:
            dataRect.translate(0, self.fh + 5)
            painter.drawText(dataRect, QtCore.Qt.AlignmentFlag.AlignTop, score_label)
        if popularity_label:
            dataRect.translate(0, self.fh + 5)
            painter.drawText(dataRect, QtCore.Qt.AlignmentFlag.AlignTop, popularity_label)
        if airing_time:
            dataRect.translate(0, self.fh + 5)
            painter.drawText(dataRect, QtCore.Qt.AlignmentFlag.AlignTop, airing_time)
        if in_list_label:
            dataRect.translate(0, self.fh + 5)
            painter.drawText(dataRect, QtCore.Qt.AlignmentFlag.AlignTop, in_list_label)

        # Draw synopsis
        textRect.translate(0, self.fh + 5)
        textRect.setBottomRight(baseRect.bottomRight())

        if 'extra' in data:
            painter.drawText(textRect, QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.TextFlag.TextWordWrap, self._get_extra(
                data['extra'], 'Synopsis'))

        # Draw select box
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Overlay)
            # painter.setOpacity(0.5)
            painter.fillRect(outerRect, option.palette.highlight())

        painter.restore()

    def sizeHint(self, option, index):
        # Mirrors paint()'s vertical layout: a title band of fh+10, one
        # fh+5 line per fact row, then the synopsis into whatever is
        # left. Sized for a card drawing EVERY optional row, not for the
        # rows this particular entry has, because AddCardView sets
        # uniformItemSizes -- Qt asks once and reuses the answer, so a
        # per-entry height would clip every card taller than the first.
        # Costs nothing on sparser cards: the synopsis is painted into
        # the leftover space, so unused fact rows become extra synopsis
        # lines rather than blank card.
        content = ((self.fh + 10)
                   + (FIXED_ROWS + OPTIONAL_ROWS) * (self.fh + 5)
                   + SYNOPSIS_LINES * self.fh)
        return QtCore.QSize(
            WIDTH, max(MIN_HEIGHT, content + 2 * (PADDING + MARGIN)))


class ShowsTableDelegate(QStyledItemDelegate):
    """
    Custom delegate that shows a custom progress bar
    for detailed information about episodes, and editing
    the progress and score.
    """
    # Enum BarStyle
    BarStyleBasic = 0   # Basic native ProgressBar appearance
    BarStyle04 = 1      # Rectangular dual bar of Trackma v0.4
    BarStyleHybrid = 2  # Native ProgressBar with v0.4 library subbar overlaid

    _subvalue = -1
    _episodes = []
    _subheight = 5
    _bar_style = BarStyle04
    _show_text = False
    _text_fraction = False
    _show_buttons = False

    def __init__(self, parent, palette=None):
        self.colors = palette
        # Native styles render CE_ProgressBar/CE_ProgressBarLabel very
        # differently depending on the user's system theme -- some
        # squeeze the bar and push the label out to the side instead of
        # centering it. Force Fusion for this specific control, the way
        # real Taiga's own paintProgressBar() does (QProxyStyle{"fusion"}
        # in painters.cpp), so the bar/text layout is consistent
        # regardless of the desktop theme.
        self._progress_style = QStyleFactory.create('Fusion')

        super().__init__(parent)

    def paint(self, painter, option, index):
        if index.column() == 4:
            rect = option.rect
            data = index.model().data(index)

            if not data:
                return

            (value, maximum, subvalue, episodes, real_total) = data
            m = index.model().sourceModel()

            painter.save()

            # Real Taiga's hover +/- episode buttons (ported from the
            # win32 1.4 codebase's AnimeListDialog::ListView -- the Qt
            # rewrite doesn't have this at all, see dlg_anime_list.cpp)
            # sit flush at the left/right edges of the cell and shrink
            # the bar to make room, rather than floating on top of it.
            hovering = bool(self._show_buttons and
                            (option.state & QStyle.StateFlag.State_MouseOver))
            dec_visible = inc_visible = False
            bar_rect = rect
            if hovering:
                dec_visible, inc_visible = self._button_visibility(value, maximum)
                bar_rect = self._bar_rect(rect, dec_visible, inc_visible)

            if self._bar_style is self.BarStyleBasic:
                prog_options = QStyleOptionProgressBar()
                prog_options.maximum = maximum
                prog_options.progress = value
                prog_options.rect = bar_rect
                prog_options.palette = option.palette
                prog_options.state = option.state
                prog_options.direction = option.direction
                prog_options.fontMetrics = option.fontMetrics
                prog_options.text = self._format_text(value, maximum, real_total)
                prog_options.textVisible = self._show_text
                prog_options.textAlignment = QtCore.Qt.AlignmentFlag.AlignCenter
                self._progress_style.drawControl(
                    QStyle.ControlElement.CE_ProgressBar, prog_options, painter)

            elif self._bar_style is self.BarStyle04:
                painter.setBrush(getColor(self.colors['progress_bg']))
                painter.setPen(QtCore.Qt.GlobalColor.transparent)
                painter.drawRect(bar_rect)
                self.paintSubValue(painter, bar_rect, subvalue, maximum)
                if value > 0:
                    if value >= maximum:
                        painter.setBrush(
                            getColor(self.colors['progress_complete']))
                        mid = bar_rect.width()
                    else:
                        painter.setBrush(getColor(self.colors['progress_fg']))
                        mid = int(bar_rect.width() / float(maximum) * value)
                    progressRect = QtCore.QRect(
                        bar_rect.x(), bar_rect.y(), mid, bar_rect.height())
                    painter.drawRect(progressRect)
                self.paintEpisodes(painter, bar_rect, episodes, maximum)

            elif self._bar_style is self.BarStyleHybrid:
                # Fusion's CE_ProgressBar renders determinate progress as
                # a row of distinct rounded chunks (its normal look for
                # any native/proxy style) -- fine for a real OS progress
                # bar, but reads as a "battery charging" pattern here,
                # unlike real Taiga's smooth solid fill. So the actual
                # colored fill is our own flat rect (identical to
                # BarStyle04's smooth fill).
                painter.setPen(QtCore.Qt.GlobalColor.transparent)
                painter.setBrush(getColor(self.colors['progress_bg']))
                painter.drawRect(bar_rect)
                self.paintSubValue(painter, bar_rect, subvalue, maximum)
                if value > 0:
                    if value >= maximum:
                        painter.setBrush(
                            getColor(self.colors['progress_complete']))
                        mid = bar_rect.width()
                    else:
                        painter.setBrush(getColor(self.colors['progress_fg']))
                        mid = int(bar_rect.width() / float(maximum) * value)
                    painter.drawRect(QtCore.QRect(
                        bar_rect.x(), bar_rect.y(), mid, bar_rect.height()))
                self.paintEpisodes(painter, bar_rect, episodes, maximum)

                if self._show_text:
                    # CE_ProgressBarLabel (tried previously) draws an
                    # embossed two-tone effect tuned for Fusion's own
                    # palette -- against these custom pastel bar colors
                    # it comes out as garbled/doubled text. Plain flat
                    # black text avoids depending on any style's
                    # assumptions entirely.
                    painter.setPen(QtGui.QPen(QtCore.Qt.GlobalColor.black))
                    painter.drawText(
                        bar_rect, QtCore.Qt.AlignmentFlag.AlignCenter,
                        self._format_text(value, maximum, real_total))

            if hovering:
                self._paint_buttons(painter, rect, dec_visible, inc_visible)

            painter.restore()
        else:
            super().paint(painter, option, index)

    def _button_visibility(self, value, maximum):
        dec_visible = value > 0
        inc_visible = not maximum or value < maximum
        return dec_visible, inc_visible

    def _button_rects(self, rect):
        size = rect.height()
        dec_rect = QtCore.QRect(rect.left(), rect.top(), size, rect.height())
        inc_rect = QtCore.QRect(rect.right() - size + 1, rect.top(), size, rect.height())
        return dec_rect, inc_rect

    def _bar_rect(self, rect, dec_visible, inc_visible):
        bar_rect = QtCore.QRect(rect)
        dec_rect, inc_rect = self._button_rects(rect)
        if dec_visible:
            bar_rect.setLeft(dec_rect.right() + 1)
        if inc_visible:
            bar_rect.setRight(inc_rect.left() - 1)
        return bar_rect

    def _paint_buttons(self, painter, rect, dec_visible, inc_visible):
        dec_rect, inc_rect = self._button_rects(rect)

        def draw_button(btn_rect, glyph):
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(getColor(self.colors['progress_sub_bg']))
            painter.drawRect(btn_rect)
            painter.setPen(QtGui.QPen(getColor(self.colors['progress_sub_fg'])))
            font = QtGui.QFont(painter.font())
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(btn_rect, QtCore.Qt.AlignmentFlag.AlignCenter, glyph)

        if dec_visible:
            draw_button(dec_rect, '-')
        if inc_visible:
            draw_button(inc_rect, '+')

    def editorEvent(self, event, model, option, index):
        if (self._show_buttons and index.column() == 4
                and event.type() == QtCore.QEvent.Type.MouseButtonRelease
                and event.button() == QtCore.Qt.MouseButton.LeftButton):
            data = index.model().data(index)
            if data:
                (value, maximum, _subvalue, _episodes, _real_total) = data
                dec_visible, inc_visible = self._button_visibility(value, maximum)
                dec_rect, inc_rect = self._button_rects(option.rect)
                source_row = index.model().mapToSource(index).row()
                show = index.model().sourceModel().showlist[source_row]

                if dec_visible and dec_rect.contains(event.pos()):
                    index.model().sourceModel().progressChanged.emit(
                        show['id'], float(value - 1))
                    return True
                if inc_visible and inc_rect.contains(event.pos()):
                    new_value = value + 1
                    if maximum:
                        new_value = min(new_value, maximum)
                    index.model().sourceModel().progressChanged.emit(
                        show['id'], float(new_value))
                    return True

        return super().editorEvent(event, model, option, index)

    def paintSubValue(self, painter, rect, subvalue, maximum):
        if subvalue and maximum and subvalue <= maximum:
            painter.setBrush(getColor(self.colors['progress_sub_bg']))
            mid = int(rect.width() / float(maximum) * subvalue)
            progressRect = QtCore.QRect(
                rect.x(),
                rect.y()+rect.height()-self._subheight,
                mid,
                rect.height()-(rect.height()-self._subheight)
            )
            painter.drawRect(progressRect)

    def paintEpisodes(self, painter, rect, episodes, maximum):
        if episodes:
            for episode in episodes:
                painter.setBrush(getColor(self.colors['progress_sub_fg']))
                if episode <= maximum:
                    start = int(rect.width() / float(maximum) * (episode - 1))
                    finish = int(rect.width() / float(maximum) * episode)
                    progressRect = QtCore.QRect(
                        rect.x()+start,
                        rect.y()+rect.height()-self._subheight,
                        finish-start,
                        rect.height()-(rect.height()-self._subheight)
                    )
                    painter.drawRect(progressRect)

    def setBarStyle(self, style, show_text, text_fraction=False):
        self._bar_style = style
        self._show_text = show_text
        self._text_fraction = text_fraction

    def setShowButtons(self, enabled):
        self._show_buttons = enabled

    def _format_text(self, value, maximum, real_total=None):
        if self._text_fraction:
            # maximum is only ever a real total or a made-up bar-width
            # denominator (rounded up to the next 12-episode block, see
            # ShowListModel) -- real_total (unset unless it's genuinely
            # known) is what actually belongs in the text, not maximum,
            # or e.g. "7/12" would claim a total that doesn't exist.
            return '%d/%s' % (value, real_total if real_total else '?')
        return '%d%%' % (value*100/maximum)

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), QtGui.QFontMetrics(option.font).height() + 2)

    def createEditor(self, parent, option, index):
        editor = QDoubleSpinBox(parent)
        editor.setFrame(False)

        return editor

    def setEditorData(self, editor, index):
        (value, maximum, decimals, step) = index.model().data(
            index, QtCore.Qt.ItemDataRole.EditRole)

        editor.setMaximum(maximum or 999)
        editor.setDecimals(decimals or 0)
        editor.setSingleStep(step or 1)

        if value:
            editor.setValue(value)

    def setModelData(self, editor, model, index):
        editor.interpretText()
        old_value = index.model().data(index, QtCore.Qt.ItemDataRole.EditRole)[0]
        new_value = editor.value()

        if new_value != old_value:
            model.setData(index, new_value, QtCore.Qt.ItemDataRole.EditRole)
