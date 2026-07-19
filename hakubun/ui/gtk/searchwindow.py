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

import os
import threading

from gi.repository import Gdk, GLib, GObject, Gtk

from hakubun import utils
from hakubun.ui.gtk import gtk_dir
from hakubun.ui.gtk.showinfobox import ShowInfoBox


class SearchThread(threading.Thread):
    def __init__(self, engine, search_text, callback):
        threading.Thread.__init__(self)
        self._entries = []
        self._error = None
        self._engine = engine
        self._search_text = search_text
        self._callback = callback
        self._stop_request = threading.Event()

    def run(self):
        try:
            self._entries = self._engine.search(self._search_text)
        except utils.HakubunError as e:
            self._entries = []
            self._error = e

        if not self._stop_request.is_set():
            GLib.idle_add(self._callback, self._entries, self._error)

    def stop(self):
        self._stop_request.set()


@Gtk.Template.from_file(os.path.join(gtk_dir, 'data/searchwindow.ui'))
class SearchWindow(Gtk.Window):
    __gtype_name__ = 'SearchWindow'

    __gsignals__ = {
        'search-error': (GObject.SignalFlags.RUN_FIRST, None,
                         (str,)),
        'go-to-show': (GObject.SignalFlags.RUN_FIRST, None,
                       (int,)),
    }

    btn_add_show = Gtk.Template.Child()
    search_paned = Gtk.Template.Child()
    shows_viewport = Gtk.Template.Child()
    show_info_container = Gtk.Template.Child()
    progress_spinner = Gtk.Template.Child()
    headerbar = Gtk.Template.Child()

    def __init__(self, engine, colors, current_status, transient_for=None):
        Gtk.Window.__init__(self, transient_for=transient_for)
        self.init_template()
        self._entries = []
        self._selected_show = None
        self._showdict = None

        self._engine = engine
        self._current_status = current_status
        self._search_thread = None

        # Shows already in the user's list, so search results can be
        # highlighted instead of letting the user accidentally re-add
        # or lose track of something they're already tracking.
        self._mylist = {show['id']: show for show in self._engine.get_list()}

        self.showlist = SearchTreeView(colors)
        self.showlist.get_selection().connect("changed", self._on_selection_changed)
        self.showlist.connect("button-press-event", self._on_show_context_menu)
        self.showlist.set_size_request(250, 350)
        self.showlist.show()

        self.info = ShowInfoBox(engine, orientation=Gtk.Orientation.VERTICAL)
        self.info.set_size_request(200, 350)
        self.info.show()

        self.shows_viewport.add(self.showlist)
        self.show_info_container.pack_start(self.info, True, True, 0)
        self.search_paned.set_position(400)
        self.set_size_request(450, 350)

    @Gtk.Template.Callback()
    def _on_search_entry_search_changed(self, search_entry):
        search_text = search_entry.get_text().strip()
        self.progress_spinner.start()
        if search_text == "":
            if self._search_thread:
                self._search_thread.stop()
            self._search_finish()
        else:
            self._search(search_text)
            self.progress_spinner.start()

    def _search(self, text):
        if self._search_thread:
            self._search_thread.stop()

        self.headerbar.set_subtitle("Searching: \"%s\"" % text)
        self._search_thread = SearchThread(self._engine,
                                           text,
                                           self._search_finish_idle)
        self._search_thread.start()

    def _search_finish(self):
        self.headerbar.set_subtitle(
            "%s result%s." % ((len(self._entries), 's')
                              if len(self._entries) > 0
                              else ('No', '')
                              )
        )
        self.progress_spinner.stop()

    def _search_finish_idle(self, entries, error):
        self._entries = entries
        self._showdict = dict()
        self._search_finish()
        self.showlist.append_start()
        for show in entries:
            self._showdict[show['id']] = show
            mylist_entry = self._mylist.get(show['id'])
            in_list_label = None
            if mylist_entry:
                statuses_dict = self._engine.mediainfo['statuses_dict']
                in_list_label = statuses_dict.get(mylist_entry['my_status'], '?')
            self.showlist.append(show, in_list_label)
        self.showlist.append_finish()

        self.btn_add_show.set_sensitive(False)

        if error:
            self.emit('search-error', error)

    @Gtk.Template.Callback()
    def _on_btn_add_show_clicked(self, btn):
        show = self._get_full_selected_show()

        if show is None:
            return

        if show['id'] in self._mylist:
            self.emit('go-to-show', show['id'])
        else:
            self._add_show(show)

    def _get_full_selected_show(self):
        for item in self._entries:
            if item['id'] == self._selected_show:
                return item

        return None

    def _add_show(self, show):
        mediainfo = self._engine.mediainfo
        statuses = mediainfo['statuses']
        statuses_dict = mediainfo['statuses_dict']

        # Default status is configurable (Preferences > Behavior): either the
        # currently active tab, or the API's "Plan to Watch" status (the
        # last entry of `statuses` by convention across every backend --
        # `statuses_start` is a different thing: the status a show flips to
        # once you start watching it, e.g. "current"/"CURRENT"). Either way
        # we always confirm before adding, so a show is never silently
        # dropped into whatever tab happened to be active (e.g. Dropped).
        default_status = self._current_status
        if self._engine.get_config('add_dialog_default_status') == 'start' \
                and statuses:
            default_status = statuses[-1]

        dialog = AddStatusDialog(
            self, show['title'], statuses, statuses_dict, default_status)
        response = dialog.run()
        chosen_status = dialog.chosen_status()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return

        try:
            self._engine.add_show(show, chosen_status)
        except utils.HakubunError as e:
            self.emit('search-error', e)

    def _on_selection_changed(self, selection):
        # Get selected show ID
        (tree_model, tree_iter) = selection.get_selected()
        if not tree_iter:
            return

        self._selected_show = int(tree_model.get(tree_iter, 0)[0])
        if self._selected_show in self._showdict:
            self.info.load(self._showdict[self._selected_show])
            self.btn_add_show.set_sensitive(True)
            if self._selected_show in self._mylist:
                self.btn_add_show.set_label('Go to')
            else:
                self.btn_add_show.set_label('Add')

    def _on_show_context_menu(self, tree_view, event):
        # Right-click "Move to" for a result already in the user's list
        # -- there's no status to move a not-yet-added show away from.
        x = int(event.x)
        y = int(event.y)
        pthinfo = tree_view.get_path_at_pos(x, y)

        if not (event.type == Gdk.EventType.BUTTON_PRESS and
                event.button == Gdk.BUTTON_SECONDARY and pthinfo):
            return False

        path, col, cellx, celly = pthinfo
        showid = int(tree_view.get_model()[path][0])
        if showid not in self._mylist:
            return False

        tree_view.grab_focus()
        tree_view.set_cursor(path, col, 0)

        menu = Gtk.Menu()
        mb_move_to = Gtk.MenuItem("Move to")
        mb_move_to.set_submenu(self._build_move_to_menu(showid))
        menu.append(mb_move_to)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _build_move_to_menu(self, showid):
        mediainfo = self._engine.mediainfo
        menu_move_to = Gtk.Menu()
        for status in mediainfo['statuses']:
            mb_status = Gtk.MenuItem(mediainfo['statuses_dict'][status])
            mb_status.connect(
                "activate", self._on_move_to_activate, showid, status)
            menu_move_to.append(mb_status)
        menu_move_to.show_all()
        return menu_move_to

    def _on_move_to_activate(self, menu_item, showid, status):
        try:
            self._engine.set_status(showid, status)
        except utils.HakubunError as e:
            self.emit('search-error', str(e))
            return

        if showid in self._mylist:
            self._mylist[showid]['my_status'] = status


class SearchTreeView(Gtk.TreeView):
    # Subtle highlight for a result already present in the user's list, so
    # it's not confused with a brand new show (see also Qt's AddTableModel
    # / AddListDelegate). Always light, so cells force dark text over it
    # regardless of the app's theme.
    IN_LIST_COLOR = '#d2e6ff'
    IN_LIST_TEXT_COLOR = '#1e1e1e'

    def __init__(self, colors):
        Gtk.TreeView.__init__(self)

        self.cols = dict()
        i = 1
        for name in ('Title', 'Type', 'Season', 'Total', 'In Your List'):
            self.cols[name] = Gtk.TreeViewColumn(name)
            self.cols[name].set_sort_column_id(i)
            self.append_column(self.cols[name])
            i += 1

        # renderer_id = Gtk.CellRendererText()
        # self.cols['ID'].pack_start(renderer_id, False)
        # self.cols['ID'].add_attribute(renderer_id, 'text', 0)

        renderer_title = Gtk.CellRendererText()
        self.cols['Title'].pack_start(renderer_title, False)
        self.cols['Title'].set_resizable(True)
        self.cols['Title'].set_expand(False)
        self.cols['Title'].add_attribute(renderer_title, 'text', 1)
        self.cols['Title'].add_attribute(renderer_title, 'foreground', 6)
        self.cols['Title'].add_attribute(renderer_title, 'background', 7)

        renderer_type = Gtk.CellRendererText()
        self.cols['Type'].pack_start(renderer_type, False)
        self.cols['Type'].add_attribute(renderer_type, 'text', 2)
        self.cols['Type'].add_attribute(renderer_type, 'foreground', 8)
        self.cols['Type'].add_attribute(renderer_type, 'background', 7)

        renderer_season = Gtk.CellRendererText()
        self.cols['Season'].pack_start(renderer_season, False)
        self.cols['Season'].add_attribute(renderer_season, 'text', 3)
        self.cols['Season'].add_attribute(renderer_season, 'foreground', 8)
        self.cols['Season'].add_attribute(renderer_season, 'background', 7)

        renderer_total = Gtk.CellRendererText()
        self.cols['Total'].pack_start(renderer_total, False)
        self.cols['Total'].add_attribute(renderer_total, 'text', 4)
        self.cols['Total'].add_attribute(renderer_total, 'foreground', 8)
        self.cols['Total'].add_attribute(renderer_total, 'background', 7)

        renderer_in_list = Gtk.CellRendererText()
        self.cols['In Your List'].pack_start(renderer_in_list, False)
        self.cols['In Your List'].add_attribute(renderer_in_list, 'text', 5)
        self.cols['In Your List'].add_attribute(renderer_in_list, 'foreground', 8)
        self.cols['In Your List'].add_attribute(renderer_in_list, 'background', 7)

        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str, str)
        self.set_model(self.store)

        self.colors = colors

    def append_start(self):
        self.freeze_child_notify()
        self.store.clear()

    def append(self, show, in_list_label=None):
        if show['status'] == utils.Status.AIRING:
            title_color = self.colors['is_airing']
        elif show['status'] == utils.Status.NOTYET:
            title_color = self.colors['not_aired']
        else:
            title_color = None

        row_bg = self.IN_LIST_COLOR if in_list_label else None
        # Cells with no dedicated color (Type/Season/Total/In Your List,
        # and Title when it has no airing-status color of its own) would
        # otherwise fall back to the theme's default text color, which can
        # be unreadable against the always-light highlight background.
        other_fg = self.IN_LIST_TEXT_COLOR if in_list_label else None
        if title_color is None and in_list_label:
            title_color = self.IN_LIST_TEXT_COLOR

        row = [
            str(show['id']),
            str(show['title']),
            str(show['type']),
            utils.get_season_label(show),
            str(show['total']),
            in_list_label or '',
            title_color,
            row_bg,
            other_fg]
        self.store.append(row)

    def append_finish(self):
        self.thaw_child_notify()
        self.store.set_sort_column_id(1, Gtk.SortType.ASCENDING)


class AddStatusDialog(Gtk.Dialog):
    """
    Confirmation dialog shown before adding a show to the list.

    Lets the user choose which status to add the show under instead of
    silently inheriting whatever tab happens to be active. This prevents
    accidentally adding a show to Dropped or On Hold when auto-sync is on.
    """

    def __init__(self, parent, show_title, statuses, statuses_dict, default_status):
        Gtk.Dialog.__init__(self, "Add to list", parent, Gtk.DialogFlags.MODAL)
        self.set_default_size(340, -1)

        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        add_btn = self.add_button("_Add", Gtk.ResponseType.OK)
        add_btn.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)

        title_label = Gtk.Label(label="<b>%s</b>" % GLib.markup_escape_text(show_title))
        title_label.set_use_markup(True)
        title_label.set_line_wrap(True)
        title_label.set_xalign(0)

        status_label = Gtk.Label(label="Add as:")
        status_label.set_xalign(0)

        self._status_combo = Gtk.ComboBoxText()
        default_index = 0
        for i, status in enumerate(statuses):
            self._status_combo.append(str(status), statuses_dict[status])
            if status == default_status:
                default_index = i
        self._status_combo.set_active(default_index)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        status_row.pack_start(status_label, False, False, 0)
        status_row.pack_start(self._status_combo, True, True, 0)

        content = self.get_content_area()
        content.set_spacing(12)
        content.set_border_width(12)
        content.pack_start(title_label, False, False, 0)
        content.pack_start(status_row, False, False, 0)

        self.show_all()

    def chosen_status(self):
        """Returns the status value selected by the user."""
        return self._status_combo.get_active_id()
