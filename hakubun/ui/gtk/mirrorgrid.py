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

"""The Mirror preview as a wall of covers -- GTK twin of the Qt grid.

Same idea, same wording, same `present.MirrorCard` behind it: a tile
per work, its cover with the title underneath, and the changes revealed
over the fading cover when the pointer is on it. See
hakubun/ui/qt/mirrorgrid.py for why the preview is shaped this way.
"""

from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

from hakubun.sync import present
from hakubun.ui import posters

COVER_W = 200
COVER_H = 285

_ADD_COLOR = '#66bb6a'
_REMOVE_COLOR = '#ef5350'
_UPDATE_COLOR = '#42a5f5'
_LOCAL_COLOR = '#66bb6a'
_CONFLICT_COLOR = '#ffa726'
_OWNERSHIP_COLOR = '#9ccc65'
_MUTED_COLOR = '#9e9e9e'

_CSS = b"""
.mirror-cover { background-color: #1b1f27; }
.mirror-details { background-color: rgba(12, 15, 20, 0.86); }
.mirror-tile { border: 1px solid rgba(255, 255, 255, 0.12);
               border-radius: 4px; }
.mirror-add { border-color: #66bb6a; }
.mirror-remove { border-color: #ef5350; }
.mirror-update { border-color: #42a5f5; }
.mirror-conflict { border-color: #ffa726; }
"""

_css_loaded = False


def _load_css():
    global _css_loaded
    if _css_loaded:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def _markup(text, color):
    return '<span foreground="%s">%s</span>' % (
        color, GLib.markup_escape_text(text))


class MirrorRow(Gtk.EventBox):
    """One line of a tile's changes: a change, or a note about one."""

    def __init__(self, text, color, op=None, issue=None,
                 provider_label='', on_toggled=None):
        super().__init__()
        self.op = op
        self.issue = issue
        self.provider_label = provider_label
        self._text = text
        self._on_toggled = on_toggled
        self.set_visible_window(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        if op is not None and hasattr(op, 'selected'):
            self.check = Gtk.CheckButton()
            self.check.set_active(bool(op.selected))
            self.check.set_valign(Gtk.Align.START)
            self.check.connect('toggled', self._toggled)
            box.pack_start(self.check, False, False, 0)
        else:
            # Informational: a settled membership decision, or a
            # tracker this work has not been matched on. Nothing to
            # tick -- but the way back to that decision is the context
            # menu on this row.
            self.check = None
        label = Gtk.Label(xalign=0)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_markup(_markup(text, color))
        box.pack_start(label, True, True, 0)
        self.add(box)

    def _toggled(self, _button):
        self.op.selected = self.check.get_active()
        if self._on_toggled is not None:
            self._on_toggled()

    def set_checked(self, checked):
        if self.check is None:
            return
        self.check.handler_block_by_func(self._toggled)
        self.check.set_active(checked)
        self.check.handler_unblock_by_func(self._toggled)

    def text(self):
        return self._text


class MirrorTile(Gtk.EventBox):
    """One work: its cover, its title, and its changes behind them."""

    def __init__(self, card, on_menu=None):
        super().__init__()
        _load_css()
        self.card = card
        self._on_menu = on_menu
        self._hovered = False
        self._details = None
        self.rows = []
        self.set_visible_window(False)
        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect('enter-notify-event', self._on_enter)
        self.connect('leave-notify-event', self._on_leave)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        # Hug the cover: a FlowBox hands its children whatever width the
        # row divides into, and a tile stretched past its art draws its
        # border out in space with bands of background beside the
        # picture.
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_size_request(COVER_W, -1)
        outer.get_style_context().add_class('mirror-tile')
        outer.get_style_context().add_class(self._accent_class())

        self.overlay = Gtk.Overlay()
        self.overlay.set_size_request(COVER_W, COVER_H)
        self.cover = Gtk.Image()
        self.cover.set_size_request(COVER_W, COVER_H)
        self.cover.get_style_context().add_class('mirror-cover')
        self.overlay.add(self.cover)

        self.check = Gtk.CheckButton()
        self.check.set_halign(Gtk.Align.START)
        self.check.set_valign(Gtk.Align.START)
        self.check.set_margin_start(6)
        self.check.set_margin_top(6)
        self.check.set_tooltip_text('Apply every change to this title')
        self.check.set_no_show_all(not card.ops)
        self.check.connect('button-press-event', self._on_check_clicked)
        self.overlay.add_overlay(self.check)
        outer.pack_start(self.overlay, False, False, 0)

        title = Gtk.Label(label=card.title)
        title.set_line_wrap(True)
        title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title.set_lines(2)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_justify(Gtk.Justification.CENTER)
        # A label's NATURAL width is what a Box asks for, so a long
        # title would widen its own tile out of line with the rest.
        # max_width_chars(1) makes it ask for nothing and take the
        # width the tile already fixed.
        title.set_max_width_chars(1)
        title.set_tooltip_text(card.title)
        outer.pack_start(title, False, False, 0)

        headline = Gtk.Label()
        headline.set_markup(
            '<small>%s</small>'
            % _markup(present.mirror_card_headline(card), self._accent()))
        headline.set_ellipsize(Pango.EllipsizeMode.END)
        headline.set_max_width_chars(1)
        headline.set_tooltip_text(present.mirror_card_headline(card))
        outer.pack_start(headline, False, False, 0)

        self.add(outer)
        self.refresh_check()

    # -- appearance ----------------------------------------------------

    def _accent(self):
        """The colour of the biggest thing happening to this work --
        deletion first, since it is the only irreversible one."""
        if any(not hasattr(op, 'field') and not hasattr(op, 'values')
               for op in self.card.ops):
            return _REMOVE_COLOR
        if self.card.conflicts:
            return _CONFLICT_COLOR
        if any(hasattr(op, 'values') for op in self.card.ops):
            return _ADD_COLOR
        return _UPDATE_COLOR

    def _accent_class(self):
        return {_REMOVE_COLOR: 'mirror-remove',
                _CONFLICT_COLOR: 'mirror-conflict',
                _ADD_COLOR: 'mirror-add'}.get(self._accent(),
                                              'mirror-update')

    def set_cover(self, pixbuf):
        self.cover.set_from_pixbuf(pixbuf)

    # -- hover ---------------------------------------------------------

    def _on_enter(self, _widget, _event):
        self.set_hovered(True)
        return False

    def _on_leave(self, _widget, event):
        # A leave whose detail is INFERIOR means the pointer moved onto
        # a CHILD of this tile -- the overlay it just asked for. Hiding
        # the details there would take them away from under the cursor.
        if event.detail == Gdk.NotifyType.INFERIOR:
            return False
        self.set_hovered(False)
        return False

    def set_hovered(self, hovered):
        if hovered == self._hovered:
            return
        self._hovered = hovered
        if hovered:
            self.ensure_details()
        # Not 0: the cover still reads as the backdrop of its own
        # changes, which is what says which tile you are reading.
        self.cover.set_opacity(0.12 if hovered else 1.0)
        if self._details is not None:
            self._details.set_visible(hovered)

    @property
    def hovered(self):
        return self._hovered

    # -- the changes ---------------------------------------------------

    def ensure_details(self):
        """Build the overlay the first time it is needed -- a preview
        is hundreds of tiles, and building every one's rows up front is
        a visible pause for detail nobody has asked to see."""
        if self._details is not None:
            return self._details
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(COVER_W, COVER_H)
        scroll.get_style_context().add_class('mirror-details')
        scroll.set_no_show_all(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(6)
        box.set_margin_top(22)

        if self.card.desired:
            # What ownership says this work SHOULD be: the row every
            # delta below is read against.
            says = Gtk.Label(xalign=0)
            says.set_line_wrap(True)
            says.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            says.set_markup(
                '<small>%s</small>' % _markup(
                    'Ownership says:  '
                    + '   ·   '.join('%s %s (%s)' % (name, value, owner)
                                     if owner else '%s %s' % (name, value)
                                     for name, value, owner, _why
                                     in self.card.desired),
                    _OWNERSHIP_COLOR))
            box.pack_start(says, False, False, 0)

        for op, text in self.card.rows:
            row = MirrorRow(text, _row_color(op), op=op,
                            issue=self.card.issue,
                            provider_label=('' if op is not None
                                            else text.split(' — ')[0]
                                            .strip()),
                            on_toggled=self.refresh_check)
            row.connect('button-press-event', self._on_row_button)
            self.rows.append(row)
            box.pack_start(row, False, False, 0)

        for conflict in self.card.conflicts:
            note = Gtk.Label(xalign=0)
            note.set_line_wrap(True)
            note.set_markup(_markup(
                '%s needs your decision — see Decisions'
                % present.field_label(conflict.field), _CONFLICT_COLOR))
            box.pack_start(note, False, False, 0)

        scroll.add(box)
        # show_all() first, THEN opt out of it: set_no_show_all also
        # suppresses the call made HERE, which left the panel on screen
        # with nothing in it.
        scroll.set_no_show_all(False)
        scroll.show_all()
        scroll.set_no_show_all(True)
        scroll.set_visible(False)
        self.overlay.add_overlay(scroll)
        # Keep the tile's own tick on top of the details it now covers.
        self.overlay.reorder_overlay(self.check, -1)
        self._details = scroll
        return scroll

    # -- selection -----------------------------------------------------

    def _on_check_clicked(self, _widget, _event):
        """The tile's own tick answers the whole title at once.

        Handled on the button press rather than 'toggled', because the
        widget's own state is a three-way display (some / all / none)
        and the click means "the opposite of what it was", never "the
        third state".
        """
        self.set_selected(not all(op.selected for op in self.card.ops))
        return True

    def refresh_check(self):
        states = {op.selected for op in self.card.ops}
        self.check.set_inconsistent(len(states) > 1)
        self.check.set_active(states == {True})

    def set_selected(self, selected):
        for op in self.card.ops:
            op.selected = selected
        for row in self.rows:
            row.set_checked(selected)
        self.refresh_check()

    # -- menus ---------------------------------------------------------

    def _on_row_button(self, row, event):
        if event.button != 3 or self._on_menu is None:
            return False
        return bool(self._on_menu(row, event))


def _row_color(op):
    if op is None:
        return _MUTED_COLOR
    if hasattr(op, 'values'):
        return _ADD_COLOR
    if not hasattr(op, 'field'):
        return _REMOVE_COLOR
    if op.target == 'local':
        return _LOCAL_COLOR
    return _UPDATE_COLOR


class MirrorGrid(Gtk.ScrolledWindow):
    """The wall of tiles, and the poster downloads that fill it."""

    def __init__(self, on_menu=None):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._on_menu = on_menu
        self._flow = Gtk.FlowBox()
        self._flow.set_valign(Gtk.Align.START)
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_homogeneous(True)
        self._flow.set_row_spacing(10)
        self._flow.set_column_spacing(10)
        self._flow.set_border_width(8)
        self._flow.set_max_children_per_line(12)
        self.add(self._flow)
        self.tiles = []
        self._by_url = {}
        self._pixbufs = {}
        self._posters = posters.PosterCache(self._poster_ready)

    # -- content -------------------------------------------------------

    def clear(self):
        for child in self._flow.get_children():
            self._flow.remove(child)
            child.destroy()
        self.tiles = []
        self._by_url = {}

    def set_cards(self, cards):
        self.clear()
        for card in cards:
            tile = MirrorTile(card, on_menu=self._on_menu)
            self._flow.add(tile)
            self.tiles.append(tile)
            self._request_cover(tile)
        self._flow.show_all()

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
        pixbuf = self._pixbufs.get(url)
        if pixbuf is not None:
            tile.set_cover(pixbuf)
            return
        path = self._posters.get(url)
        if path is not None:
            self._show_cover(url, path)

    def _poster_ready(self, url, path):
        """Called from a download thread: back to the main loop before
        touching a widget."""
        GLib.idle_add(self._show_cover, url, path)

    def _show_cover(self, url, path):
        pixbuf = self._pixbufs.get(url)
        if pixbuf is None:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, COVER_W, COVER_H, True)
            except GLib.Error:
                return False
            self._pixbufs[url] = pixbuf
        for tile in self._by_url.get(url, []):
            tile.set_cover(pixbuf)
        return False

    def stop(self):
        self._posters.stop()
