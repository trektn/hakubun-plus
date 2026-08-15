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
from gi.repository import GObject, Gdk, Gtk, Pango

from hakubun import utils

# Used to drag a show from a status tab's list onto a different status
# tab's label to change its status (see ShowTreeView / MainView).
DRAG_TARGETS = [Gtk.TargetEntry.new('application/x-hakubun-showid', 0, 0)]


def _fmt_owner_score(value):
    """Format an owner-system score for the Score cell -- '8.4', '8',
    '8.5' -- trimming trailing zeros so it reads the way the owner shows
    it, not '8.40'."""
    return ("%.2f" % value).rstrip('0').rstrip('.')


def overlay_cells(show, over, decimals, factor):
    """Compute a row's display cells, applying the multi-sync overlay
    entry `over` (or None) on top of the show's own values.

    Pure (no GTK), so it's unit-tested directly. The overlay lets the
    list show reconciled per-field state -- episodes from one provider,
    an owned Score in the OWNER's rating system -- while signed into a
    different account. Returns a dict:
      my_progress    reconciled progress (feeds the bar + colour)
      my_score       reconciled score in the ACTIVE scale (float; sorts)
      episodes_str   'watched / total'
      score_str      owner-system text when Score is owned elsewhere
                     ('8.4'), else the active-scale text ('4.0')
      score_italic   True when the Score cell is a reconciled/owned value
      score_owner    owning provider, '' when Score is not owned elsewhere
      synced_score_str  dedicated 'Synced Score' column: the owner-system
                     score for a SHARED (owned-elsewhere) entry ('8.4'),
                     '–' when owned-elsewhere but unrated, '' for a
                     platform-specific entry (nothing else owns it, so it
                     has no synced score) -- lets the column itself say
                     whether a row is cross-tracker or platform-specific
      percent        progress %
      my_start_date / my_finish_date  reconciled dates (or the show's own)
    """
    my_progress = show['my_progress']
    my_score = show['my_score']
    my_start = show['my_start_date']
    my_finish = show['my_finish_date']
    score_italic = False
    score_owner = ''
    if over:
        if over.get('my_progress') is not None:
            my_progress = over['my_progress']
        if over.get('my_score') is not None:
            my_score = over['my_score']
            score_italic = True
        if 'my_start_date' in over:
            my_start = over['my_start_date']
        if 'my_finish_date' in over:
            my_finish = over['my_finish_date']
        if over.get('_score_owner'):
            score_owner = over['_score_owner']
    # Score text: the owner's own system when owned elsewhere (the whole
    # point -- 8.4 vs Kitsu's 8.5), else this account's scaled rendering.
    if over and over.get('_score_display') is not None:
        score_str = _fmt_owner_score(over['_score_display'])
        score_italic = True
    else:
        score_str = "%0.*f" % (decimals, my_score * factor)
    # Synced Score column: only a SHARED entry (owned on another tracker)
    # has a "synced" score, shown in the OWNER's own system; a platform-
    # specific entry leaves it blank, so the column reads as the
    # owned-vs-platform-specific indicator itself.
    if over and over.get('_score_owner'):
        disp = over.get('_score_display')
        synced_score_str = _fmt_owner_score(disp) if disp is not None else '–'
    else:
        synced_score_str = ''
    episodes_str = "{} / {}".format(my_progress, show['total'] or '?')
    if show['total'] and my_progress <= show['total']:
        percent = (float(my_progress) / show['total']) * 100
    else:
        percent = 0
    return {'my_progress': my_progress, 'my_score': my_score,
            'episodes_str': episodes_str, 'score_str': score_str,
            'score_italic': score_italic, 'score_owner': score_owner,
            'synced_score_str': synced_score_str,
            'percent': percent, 'my_start_date': my_start,
            'my_finish_date': my_finish}


# The show's RELEASE status, as the 'Airing Status' column shows it.
# utils.Status' own values ('Ongoing', 'Not yet started') describe
# publishing in general; these read the way an anime list talks about
# the same three states.
_STATUS_LABELS = {
    utils.Status.ONGOING: 'Airing',
    utils.Status.FINISHED: 'Completed',
    utils.Status.NOTYET: 'Upcoming',
}


def status_label(status):
    """Display label for a show's release status. Anything without a
    nicer name (Cancelled, Other, Unknown) falls back to the utils.Status
    value itself, so the cell still reads as something rather than
    silently blank."""
    try:
        status = utils.Status(status)
    except ValueError:
        return ''
    return _STATUS_LABELS.get(status, status.value)


def sort_by_season(model, iter1, iter2, data):
    """TreeSortable sort func for the 'season' column. The column holds
    a display string ('Summer 2026'); a plain text sort compares the
    season name before the year, so this parses it back into a
    (year, season) key instead (see utils.season_sort_key).

    Installed on the ShowTreeView's Gtk.TreeModelSort in mainview.py,
    not on the underlying ShowListStore -- TreeModelSort implements
    GtkTreeSortable independently of its child model, so a sort func
    set on the store itself is never consulted once a TreeModelSort
    sits between it and the view.
    """
    season_col = ShowListStore.column('season')
    ka = utils.season_sort_key(model.get_value(iter1, season_col))
    kb = utils.season_sort_key(model.get_value(iter2, season_col))
    return (ka > kb) - (ka < kb)


class ShowListStore(Gtk.ListStore):
    __cols = (
        ('id', int),
        ('title', str),
        ('stat', int),
        ('score', float),
        ('stat-text', str),
        ('score-text', str),
        ('total-eps', int),
        ('subvalue', int),
        ('avail-eps', GObject.TYPE_PYOBJECT),
        ('color', str),
        ('stat-pcent', int),
        ('start', str),
        ('end', str),
        ('my-start', str),
        ('my-end', str),
        ('my-status', str),
        ('status', int),
        ('last-updated', str),
        ('last-updated-timestamp', float),
        ('season', str),
        ('type', str),
        ('platform-score', str),
        ('mal-score', str),
        # Multi-sync display overlay (appended last so every existing
        # hard-coded column index stays valid): a Pango style so the
        # Score cell can be italicised when it shows a reconciled/owned
        # value, and the owning provider's name for that cell's tooltip.
        ('score-style', int),
        ('score-owner', str),
        # Dedicated 'Synced Score' column text (index 25): the owner-
        # system reconciled score for a shared entry, '' otherwise.
        ('synced-score', str),
        # 'Airing Status' column text (index 26). The show's release
        # status is already at index 16 as a utils.Status; this is its
        # display label, kept as its own cell so the column renders text
        # directly while index 16 keeps sorting rows in release order.
        ('status-text', str),
    )

    def __init__(self, decimals=0, factor=1, colors=dict()):
        super().__init__(*self.__class__.__columns__())
        self.colors = colors
        self.decimals = decimals
        # Display-only multiplier (see utils.score_display_factor) so the
        # Score column matches what the sidebar shows, e.g. Kitsu's raw
        # 0-5/.25 scale displayed as 0-10/.5.
        self.factor = factor
        # {show_id: {my_field: reconciled value, ...}} -- the multi-sync
        # display overlay, applied when rows are (re)built. Empty = the
        # list shows each account's own values, unchanged.
        self.overlay = {}
        self.set_sort_column_id(1, Gtk.SortType.ASCENDING)

    def set_overlay(self, overlay):
        """Install the multi-sync display overlay. Takes effect on the
        next populate/append; call apply_overlay_to_rows(shows) to refresh
        existing rows in place (keeping the selection)."""
        self.overlay = overlay or {}

    @staticmethod
    def format_date(date):
        if date:
            try:
                return date.strftime('%Y-%m-%d')
            except ValueError:
                return '?'
        else:
            return '-'

    @classmethod
    def __columns__(cls):
        return (k for i, k in cls.__cols)

    @classmethod
    def column(cls, key):
        # next()'s default avoids StopIteration for an unknown key -- the
        # previous `except ValueError` here was dead code, since a
        # not-found next() raises StopIteration, not ValueError; it never
        # actually caught anything, and callers relying on this returning
        # None for a bad key were silently saved only by their own
        # broad-except handling further up the call chain.
        found = next((i for i in cls.__cols if i[0] == key), None)
        return cls.__cols.index(found) if found is not None else None

    def _get_color(self, show, eps, my_progress=None):
        # my_progress override lets the 'new episode' highlight key off
        # the reconciled progress the overlay is displaying, not the
        # account's own raw value.
        if my_progress is None:
            my_progress = show['my_progress']
        if show.get('queued'):
            return self.colors['is_queued']
        elif eps and max(eps) > my_progress:
            return self.colors['new_episode']
        elif show['status'] == utils.Status.AIRING:
            return self.colors['is_airing']
        elif show['status'] == utils.Status.NOTYET:
            return self.colors['not_aired']
        else:
            return None

    def append(self, show, altname=None, eps=None):
        cells = overlay_cells(show, self.overlay.get(show['id']),
                              self.decimals, self.factor)

        title_str = show['title']
        if altname:
            title_str += " [%s]" % altname

        aired_eps = utils.estimate_aired_episodes(show)

        if eps:
            available_eps = eps.keys()
        else:
            available_eps = []

        start_date = self.format_date(show['start_date'])
        end_date = self.format_date(show['end_date'])
        my_start_date = self.format_date(cells['my_start_date'])
        my_finish_date = self.format_date(cells['my_finish_date'])
        my_last_update_dt = show.get('my_last_update')
        my_last_update = utils.format_local_time(my_last_update_dt)
        my_last_update_timestamp = my_last_update_dt.timestamp() if my_last_update_dt is not None else 0

        row = [show['id'],
               title_str,
               cells['my_progress'],
               cells['my_score'],
               cells['episodes_str'],
               cells['score_str'],
               show['total'],
               aired_eps,
               available_eps,
               self._get_color(show, available_eps, cells['my_progress']),
               cells['percent'],
               start_date,
               end_date,
               my_start_date,
               my_finish_date,
               show['my_status'],
               show['status'],
               my_last_update,
               my_last_update_timestamp,
               utils.get_season_label(show),
               str(show['type']),
               show.get('platform_score') or '-',
               show.get('mal_score') or '-',
               Pango.Style.ITALIC if cells['score_italic']
               else Pango.Style.NORMAL,
               cells['score_owner'],
               cells['synced_score_str'],
               status_label(show['status']),
               ]
        super().append(row)

    def update_or_append(self, show):
        for row in self:
            if int(row[0]) == show['id']:
                self.update(show, row)
                return
        self.append(show)

    def update(self, show, row=None):
        if not row:
            for row in self:
                if int(row[0]) == show['id']:
                    break
        if row and int(row[0]) == show['id']:
            cells = overlay_cells(show, self.overlay.get(show['id']),
                                  self.decimals, self.factor)
            row[2] = cells['my_progress']
            row[4] = cells['episodes_str']
            row[3] = cells['my_score']
            row[5] = cells['score_str']
            row[9] = self._get_color(show, row[8], cells['my_progress'])
            row[10] = cells['percent']
            row[15] = show['my_status']

            my_last_update = show['my_last_update']

            row[17] = utils.format_local_time(my_last_update)
            row[18] = show['my_last_update'].timestamp() if show['my_last_update'] is not None else 0
            row[19] = utils.get_season_label(show)
            row[20] = str(show['type'])
            row[21] = show.get('platform_score') or '-'
            row[22] = show.get('mal_score') or '-'
            row[23] = (Pango.Style.ITALIC if cells['score_italic']
                       else Pango.Style.NORMAL)
            row[24] = cells['score_owner']
            row[25] = cells['synced_score_str']
            # A show that finished airing between two syncs would
            # otherwise keep its stale 'Airing' label (and its stale
            # sort key) until the whole list is repopulated.
            row[16] = show['status']
            row[26] = status_label(show['status'])
        return

        # print("Warning: Show ID not found in ShowView (%d)" % show['id'])

    def apply_overlay_to_rows(self, shows):
        """Re-apply the current overlay to the existing rows in place --
        no clear/repopulate, so the selection survives. Used right after
        an owner-score edit rebuilds the overlay.

        Recomputes every overlay-affected cell through overlay_cells(),
        so a row whose overlay entry DISAPPEARED (back in sync, overlay
        cleared on failure, mapping removed) is restored to the
        account's own values instead of keeping a stale italic
        owner-score. `shows` is the engine's current show list (the raw
        values to restore from)."""
        by_id = {show['id']: show for show in shows}
        for row in self:
            show = by_id.get(int(row[0]))
            if show is None:
                continue
            cells = overlay_cells(show, self.overlay.get(show['id']),
                                  self.decimals, self.factor)
            row[2] = cells['my_progress']
            row[3] = cells['my_score']
            row[4] = cells['episodes_str']
            row[5] = cells['score_str']
            row[9] = self._get_color(show, row[8], cells['my_progress'])
            row[10] = cells['percent']
            row[13] = self.format_date(cells['my_start_date'])
            row[14] = self.format_date(cells['my_finish_date'])
            row[23] = (Pango.Style.ITALIC if cells['score_italic']
                       else Pango.Style.NORMAL)
            row[24] = cells['score_owner']
            row[25] = cells['synced_score_str']

    def update_title(self, show, altname=None):
        for row in self:
            if int(row[0]) == show['id']:
                if altname:
                    title_str = "%s [%s]" % (show['title'], altname)
                else:
                    title_str = show['title']

                row[1] = title_str
                return

    def remove(self, show=None, show_id=None):
        for row in self:
            if int(row[0]) == (show['id'] if show is not None else show_id):
                Gtk.ListStore.remove(self, row.iter)
                return

    def playing(self, show, is_playing):
        # Change the color if the show is currently playing
        for row in self:
            if int(row[0]) == show['id']:
                if is_playing:
                    row[9] = self.colors['is_playing']
                else:
                    row[9] = self._get_color(show, row[8])
                return


class ShowListFilter(Gtk.TreeModelFilter):
    def __init__(self, status=None, *args, **kwargs):
        super().__init__(
            *args,
            **kwargs
        )
        self.set_visible_func(self.status_filter)
        self._status = status
        self._search_query = ''

    def set_search_query(self, query):
        self._search_query = (query or '').strip().lower()
        self.refilter()

    def status_filter(self, model, iterator, data):
        if self._status is not None and model[iterator][15] != self._status:
            return False
        if self._search_query:
            title = (model[iterator][1] or '').lower()
            if self._search_query not in title:
                return False
        return True

    def get_value(self, obj, key='id'):
        # ValueError: obj was a stale/invalid TreePath (get_iter). TypeError:
        # key didn't match a known column, so column() returned None and
        # get_value() got a non-int index. Both are routine (e.g. a row
        # removed between a signal firing and this being called), so return
        # None instead of crashing; anything else should propagate.
        try:
            if type(obj) is Gtk.TreePath:
                obj = self.get_iter(obj)
            if isinstance(key, (str,)):
                key = self.props.child_model.column(key)
            return super().get_value(obj, key)
        except (ValueError, TypeError):
            return None


class ShowTreeView(Gtk.TreeView):
    __gsignals__ = {'column-toggled': (GObject.SignalFlags.RUN_LAST,
                                       GObject.TYPE_PYOBJECT, (GObject.TYPE_STRING, GObject.TYPE_BOOLEAN))}

    def __init__(self, colors, visible_columns, progress_style=1):
        Gtk.TreeView.__init__(self)

        self.colors = colors
        self.visible_columns = visible_columns
        self.progress_style = progress_style

        # GTK's own interactive search (which pops up on any typed
        # character and just jumps to a matching row) is superseded by
        # the real list-filtering search bar (MainView.reveal_search),
        # and would otherwise steal the '/' keypress that opens it.
        self.set_enable_search(False)
        self.set_property('has-tooltip', True)
        self.connect('query-tooltip', self.show_tooltip)

        # Lets a row be dragged onto a status tab (see MainView) to change
        # that show's status, instead of only via the status dropdown.
        self.enable_model_drag_source(
            Gdk.ModifierType.BUTTON1_MASK, DRAG_TARGETS, Gdk.DragAction.MOVE)
        self.connect('drag-data-get', self._on_drag_data_get)

        self.cols = dict()
        self.available_columns = (
            ('Title', 1),
            ('Progress', 2),
            ('Score', 3),
            ('Percent', 10),
            ('Start', 11),
            ('End', 12),
            ('My start', 13),
            ('My end', 14),
            ('Last updated', 17),
            ('Season', 19),
            ('Type', 20),
            ('Platform Score', 21),
            ('MAL Score', 22),
            ('Synced Score', 25),
            # Sorted on the underlying utils.Status at 16, not on the
            # label text at 26, so clicking the header groups the list
            # in release order instead of alphabetically. Named 'Airing
            # Status' rather than plain 'Status' because the list
            # already has a per-tab status meaning -- where the user put
            # the show, not where the show is in its run.
            ('Airing Status', 16),
        )

        for (name, sort) in self.available_columns:
            self.cols[name] = Gtk.TreeViewColumn(name)

            # This is a hack to allow for right-clickable header
            label = Gtk.Label(name)
            label.show()
            self.cols[name].set_widget(label)

            if name == "Last updated":
                self.cols[name].set_sort_column_id(18)
                label.set_tooltip_text("Date and time of the last synced update")
            else:
                self.cols[name].set_sort_column_id(sort)

            self.append_column(self.cols[name])

            w = self.cols[name].get_widget()
            while not isinstance(w, Gtk.Button):
                w = w.get_parent()

            w.connect('button-press-event', self._header_button_press)

            if name not in self.visible_columns:
                self.cols[name].set_visible(False)

        # renderer_id = Gtk.CellRendererText()
        # self.cols['ID'].pack_start(renderer_id, False, True, 0)
        # self.cols['ID'].set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        # self.cols['ID'].set_expand(False)
        # self.cols['ID'].add_attribute(renderer_id, 'text', 0)

        renderer_title = Gtk.CellRendererText()
        self.cols['Title'].pack_start(renderer_title, False)
        self.cols['Title'].set_resizable(True)
        self.cols['Title'].set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.cols['Title'].set_expand(True)
        self.cols['Title'].add_attribute(renderer_title, 'text', 1)
        # Using foreground-gdk does not work, possibly due to the timing of it being set
        self.cols['Title'].add_attribute(renderer_title, 'foreground', 9)
        renderer_title.set_property('ellipsize', Pango.EllipsizeMode.END)

        renderer_progress = Gtk.CellRendererText()
        self.cols['Progress'].pack_start(renderer_progress, False)
        self.cols['Progress'].add_attribute(renderer_progress, 'text', 4)
        self.cols['Progress'].set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        self.cols['Progress'].set_expand(False)

        if self.progress_style == 0:
            renderer_percent = Gtk.CellRendererProgress()
            self.cols['Percent'].pack_start(renderer_percent, False)
            self.cols['Percent'].add_attribute(renderer_percent, 'value', 10)
        else:
            renderer_percent = ProgressCellRenderer(self.colors)
            self.cols['Percent'].pack_start(renderer_percent, False)
            self.cols['Percent'].add_attribute(renderer_percent, 'value', 2)
            self.cols['Percent'].add_attribute(renderer_percent, 'total', 6)
            self.cols['Percent'].add_attribute(renderer_percent, 'subvalue', 7)
            self.cols['Percent'].add_attribute(renderer_percent, 'eps', 8)
        renderer_percent.set_fixed_size(100, -1)

        renderer_score = Gtk.CellRendererText()
        self.cols['Score'].pack_start(renderer_score, False)
        self.cols['Score'].add_attribute(renderer_score, 'text', 5)
        # Italicise the Score when it shows a reconciled/owned value
        # (multi-sync), mirroring the Qt list's italic cue. Col 23 holds
        # the Pango style per row (ITALIC when overlaid, else NORMAL).
        self.cols['Score'].add_attribute(renderer_score, 'style', 23)
        renderer = Gtk.CellRendererText()
        self.cols['Start'].pack_start(renderer, False)
        self.cols['Start'].add_attribute(renderer, 'text', 11)
        renderer = Gtk.CellRendererText()
        self.cols['End'].pack_start(renderer, False)
        self.cols['End'].add_attribute(renderer, 'text', 12)
        renderer = Gtk.CellRendererText()
        self.cols['My start'].pack_start(renderer, False)
        self.cols['My start'].add_attribute(renderer, 'text', 13)
        renderer = Gtk.CellRendererText()
        self.cols['My end'].pack_start(renderer, False)
        self.cols['My end'].add_attribute(renderer, 'text', 14)
        renderer = Gtk.CellRendererText()
        self.cols['Last updated'].pack_start(renderer, False)
        self.cols['Last updated'].add_attribute(renderer, 'text', 17)
        renderer = Gtk.CellRendererText()
        self.cols['Season'].pack_start(renderer, False)
        self.cols['Season'].add_attribute(renderer, 'text', 19)
        renderer = Gtk.CellRendererText()
        self.cols['Type'].pack_start(renderer, False)
        self.cols['Type'].add_attribute(renderer, 'text', 20)
        renderer = Gtk.CellRendererText()
        self.cols['Platform Score'].pack_start(renderer, False)
        self.cols['Platform Score'].add_attribute(renderer, 'text', 21)
        renderer = Gtk.CellRendererText()
        self.cols['MAL Score'].pack_start(renderer, False)
        self.cols['MAL Score'].add_attribute(renderer, 'text', 22)
        # Synced Score: the reconciled score in the owner's own system
        # (col 25). Italicised via the same per-row Pango style as the
        # Score cell (col 23), since it too is a reconciled/owned value.
        renderer_synced = Gtk.CellRendererText()
        self.cols['Synced Score'].pack_start(renderer_synced, False)
        self.cols['Synced Score'].add_attribute(renderer_synced, 'text', 25)
        self.cols['Synced Score'].add_attribute(renderer_synced, 'style', 23)

        # Airing Status: the show's release state (col 26), sorted on the
        # utils.Status behind it (col 16, set above).
        renderer_status = Gtk.CellRendererText()
        self.cols['Airing Status'].pack_start(renderer_status, False)
        self.cols['Airing Status'].add_attribute(renderer_status, 'text', 26)
        self.cols['Airing Status'].set_sizing(
            Gtk.TreeViewColumnSizing.AUTOSIZE)
        self.cols['Airing Status'].set_expand(False)

    def _on_drag_data_get(self, widget, drag_context, data, info, time):
        model, treeiter = self.get_selection().get_selected()
        if treeiter is None:
            return
        showid = model.get_value(treeiter, ShowListStore.column('id'))
        if showid is not None:
            # set_text()/get_text() only work for atoms GTK recognizes as
            # text MIME types; our custom target needs the raw data API.
            data.set(data.get_target(), 8, str(showid).encode('utf-8'))

    def _header_button_press(self, button, event):
        if event.button == 3:
            menu = Gtk.Menu()
            for name, sort in self.available_columns:
                is_active = name in self.visible_columns

                item = Gtk.CheckMenuItem(name)
                item.set_active(is_active)
                item.connect('activate', self._header_menu_item,
                             name, not is_active)
                menu.append(item)
                item.show()

            menu.popup_at_pointer(event)
            return True

        return False

    @property
    def filter(self):
        return self.props.model.props.model

    def show_tooltip(self, view, x, y, kbd, tip):
        (has_path, tx, ty,
         model, path, tree_iter) = view.get_tooltip_context(x, y, kbd)
        if not has_path:
            return False

        # Not "_, col, _, _" -- gettext.install() makes "_" the global
        # translation function, and Python's function-wide local scoping
        # means shadowing it anywhere in this method would break the
        # _() calls below.
        _pos_ok, col, _cx, _cy = view.get_path_at_pos(tx, ty)

        def gv(key):
            return model.get_value(tree_iter, ShowListStore.column(key))

        if col is self.cols['Percent']:
            lines = []
            lines.append(_("Watched: %d") % gv('stat'))

            aired = gv('subvalue')
            status = gv('status')
            if aired and not status == utils.Status.NOTYET:
                lines.append(_("Aired%s: %d") % (
                    _(' (estimated)') if status == utils.Status.AIRING else '', aired))

            avail_eps = gv('avail-eps')
            if len(avail_eps) > 0:
                lines.append(_("Available: %d") % max(avail_eps))

            lines.append(_("Total: %s") % (gv('total-eps') or '?'))

            tip.set_markup('\n'.join(lines))
            renderer = next(iter(col.get_cells()))
            self.set_tooltip_cell(tip, path, col, renderer)
            return True
        elif col is self.cols['Last updated']:
            tip.set_text(gv('last-updated'))
            renderer = next(iter(col.get_cells()))
            self.set_tooltip_cell(tip, path, col, renderer)
            return True
        elif col is self.cols['Score']:
            owner = gv('score-owner')
            if not owner:
                return False
            tip.set_text(_('Score owned by %s, shown in its rating system')
                         % owner.capitalize())
            renderer = next(iter(col.get_cells()))
            self.set_tooltip_cell(tip, path, col, renderer)
            return True
        elif col is self.cols['Synced Score']:
            owner = gv('score-owner')
            tip.set_text(
                _('Synced from %s, in its rating system') % owner.capitalize()
                if owner else
                _('Platform-specific entry — not synced to another tracker'))
            renderer = next(iter(col.get_cells()))
            self.set_tooltip_cell(tip, path, col, renderer)
            return True

        return False

    def _header_menu_item(self, w, column_name, visible):
        self.emit('column-toggled', column_name, visible)

    def select(self, show):
        """Select specified row or first if not found"""
        for row in self.get_model():
            if int(row[0]) == show['id']:
                selection = self.get_selection()
                selection.select_iter(row.iter)
                return

        self.get_selection().select_path(Gtk.TreePath.new_first())


class ProgressCellRenderer(Gtk.CellRenderer):
    value = 0
    subvalue = 0
    _total = 0
    eps = []
    _subheight = 5

    __gproperties__ = {
        "value": (GObject.TYPE_INT, "Value",
                  "Progress percentage", 0, 100000, 0,
                  GObject.ParamFlags.READWRITE),

        "subvalue": (GObject.TYPE_INT, "Subvalue",
                     "Sub percentage", 0, 100000, 0,
                     GObject.ParamFlags.READWRITE),

        "total": (GObject.TYPE_INT, "Total",
                  "Total percentage", 0, 100000, 0,
                  GObject.ParamFlags.READWRITE),

        "eps": (GObject.TYPE_PYOBJECT, "Episodes",
                "Available episodes",
                GObject.ParamFlags.READWRITE),
    }

    def __init__(self, colors):
        Gtk.CellRenderer.__init__(self)
        self.colors = colors
        self.value = self.get_property("value")
        self.subvalue = self.get_property("subvalue")
        self.total = self.get_property("total")
        self.eps = self.get_property("eps")

    def do_set_property(self, pspec, value):
        setattr(self, pspec.name, value)

    @property
    def total(self):
        return self._total if self._total > 0 else len(self.eps)

    @total.setter
    def total(self, value):
        self._total = value

    def do_get_property(self, pspec):
        return getattr(self, pspec.name)

    def do_render(self, cr, widget, background_area, cell_area, flags):
        (x, y, w, h) = self.do_get_size(widget, cell_area)

        # set_source_rgb(0.9, 0.9, 0.9)
        cr.set_source_rgb(*self.__get_color(self.colors['progress_bg']))
        cr.rectangle(x, y, w, h)
        cr.fill()

        if not self.total:
            return

        if self.subvalue:
            if self.subvalue > self.total:
                mid = w
            else:
                mid = int(w / float(self.total) * self.subvalue)

            # set_source_rgb(0.7, 0.7, 0.7)
            cr.set_source_rgb(
                *self.__get_color(self.colors['progress_sub_bg']))
            cr.rectangle(x, y+h-self._subheight, mid, h-(h-self._subheight))
            cr.fill()

        if self.value:
            if self.value >= self.total:
                # set_source_rgb(0.6, 0.8, 0.7)
                cr.set_source_rgb(
                    *self.__get_color(self.colors['progress_complete']))
                cr.rectangle(x, y, w, h)
            else:
                mid = int(w / float(self.total) * self.value)
                # set_source_rgb(0.6, 0.7, 0.8)
                cr.set_source_rgb(
                    *self.__get_color(self.colors['progress_fg']))
                cr.rectangle(x, y, mid, h)
            cr.fill()

        if self.eps:
            # set_source_rgb(0.4, 0.5, 0.6)
            cr.set_source_rgb(
                *self.__get_color(self.colors['progress_sub_fg']))
            for episode in self.eps:
                if 0 < episode <= self.total:
                    start = int(w / float(self.total) * (episode - 1))
                    finish = int(w / float(self.total) * episode)
                    cr.rectangle(x+start, y+h-self._subheight,
                                 finish-start, h-(h-self._subheight))
                    cr.fill()

    def do_get_size(self, widget, cell_area):
        if cell_area is None:
            return 0, 0, 0, 0
        x = cell_area.x
        y = cell_area.y
        w = cell_area.width
        h = cell_area.height
        return x, y, w, h

    @staticmethod
    def __get_color(color_string):
        color = Gdk.color_parse(color_string)
        return color.red_float, color.green_float, color.blue_float
