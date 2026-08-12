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

"""GTK multi-provider Sync window (docs/multisync.md §12) -- the GTK twin
of hakubun.ui.qt.syncwindow, over the same, UI-agnostic SyncEngine.

Same information architecture as the Qt window: Sync (Changes and New
entries side by side with the Decisions pane), Mirror (make the
trackers agree according to Ownership -- membership, adds, removes,
field updates, behind an explicit bulk confirmation), Configuration
(one friendly rule picker per field), Identity (titles needing
matching) and Advanced (full policy matrix, inspector, reset).
'Hakubun' is the app's
own reconciled state (the engine's 'local'); it is always labeled as
the app, never as a provider. Two deliberate simplifications from the
Qt window: no per-field change category tabs (one Changes tree grouped
by show), and the Inspector renders text/markup rather than an HTML
table -- the workflow and the engine calls are identical.
"""

import threading
import traceback

from gi.repository import GLib, GObject, Gtk, Pango

from hakubun import utils
from hakubun.sync import adapters, present
from hakubun.sync.models import (FieldPolicy, NormalizedEntry, SyncCancelled,
                                 USER_FIELDS)
from hakubun.sync.inspect import atlas_label
from hakubun.sync.present import FIELD_LABELS as _FIELD_LABELS
from hakubun.sync.store import SyncStore

_PULL_COLOR = '#4caf50'
_PUSH_COLOR = '#42a5f5'
_CONFLICT_COLOR = '#e5a400'
# New-entry creates: planned and previewed, but unticked.
_CREATE_COLOR = _CONFLICT_COLOR

# ShowListStore-style column indices for the change trees.
_C_ACTIVE, _C_INCONSISTENT, _C_LABEL, _C_COLOR, _C_CHANGE = range(5)


class MultiSyncWindow(Gtk.Window):
    def __init__(self, accountman=None, active_api=None, media_type='anime',
                 engine=None, transient_for=None):
        Gtk.Window.__init__(self, title='Multi-provider Sync (%s)'
                            % media_type.capitalize(),
                            transient_for=transient_for)
        self.set_default_size(820, 600)
        self.media_type = media_type
        self._plan = None
        self._closed = False
        self._store_closed = False
        self._thread = None
        self._cancel = threading.Event()
        self._inspect_url = None
        # Guards the two policy views (Configuration combos, Advanced
        # matrix) against feedback loops while one refreshes the other.
        self._policy_updating = False

        if engine is not None:
            # Injection seam for tests: a prebuilt SyncEngine.
            self.store = engine.store
            self.engine, self._adapter_errors = engine, []
        else:
            # Path scheme shared with the list overlay via
            # uibridge.store_path -- one definition, or they could
            # drift onto different files.
            from hakubun.sync import uibridge
            self.store = SyncStore(uibridge.store_path(media_type))
            self.engine, self._adapter_errors = present.build_engine(
                self.store, accountman, media_type)
        # The signed-in account, kept for display (icons/labels) and
        # so the caller can tell whether a cached window still belongs
        # to the loaded account (engine.primary stays None when the
        # account has no adapter). It carries no sync authority --
        # field policies do.
        self.active_api = active_api
        if active_api and active_api in self.engine.adapters:
            self.engine.primary = active_api

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(8)
        self._notebook = Gtk.Notebook()
        self._notebook.append_page(self._build_sync_tab(),
                                   Gtk.Label(label='Sync'))
        self._notebook.append_page(self._build_mirror_tab(),
                                   Gtk.Label(label='Mirror'))
        self._notebook.append_page(self._build_config_tab(),
                                   Gtk.Label(label='Configuration'))
        self._identity_label = Gtk.Label(label='Identity')
        self._notebook.append_page(self._build_identity_tab(),
                                   self._identity_label)
        self._notebook.append_page(self._build_advanced_tab(),
                                   Gtk.Label(label='Advanced'))
        outer.pack_start(self._notebook, True, True, 0)
        self._status_label = Gtk.Label(xalign=0)
        outer.pack_start(self._status_label, False, False, 0)
        self.add(outer)
        self.connect('destroy', self._on_destroy)
        # Show the CONTENT, not the toplevel: the headless Sync-button
        # flow constructs this window without surfacing it (it is only
        # present()ed when something needs the user). no_show_all
        # widgets (Cancel, progress bar) stay hidden either way.
        outer.show_all()

        if self._adapter_errors:
            self._status('Some accounts could not be loaded: %s'
                         % '; '.join(self._adapter_errors))
        elif not self.engine.adapters:
            self._status('No %s accounts configured (check Settings if you '
                         'have accounts for the other media type).'
                         % media_type)
        self._refresh_identity()

    # -- Sync ----------------------------------------------------------

    def _build_sync_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(6)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        # The signed-in tracker's icon. Signed-in is shown as a fact
        # about accounts, never as what 'Hakubun' means.
        if self.engine.primary:
            lib_info = utils.available_libs.get(self.engine.primary)
            if lib_info:
                icon = Gtk.Image.new_from_file(lib_info[1])
                icon.set_tooltip_text(
                    'Signed into %s. Hakubun keeps its own state; the '
                    'signed-in site gets no special say in the sync.'
                    % lib_info[0])
                bar.pack_start(icon, False, False, 0)
        # The headline: how much will change, how much needs a human,
        # what would create entries -- the first thing to read.
        self.summary_label = Gtk.Label(xalign=0)
        self.summary_label.set_text('Fetch changes to see what would '
                                    'sync.')
        bar.pack_start(self.summary_label, False, False, 0)

        self.fetch_button = Gtk.Button(label='Fetch changes')
        self.fetch_button.set_tooltip_text(
            'Download each site\'s list and work out what would sync. '
            'Nothing is written anywhere until you press Sync.')
        self.fetch_button.connect('clicked', lambda *_a: self.s_fetch())
        self.apply_button = Gtk.Button(label='Sync selected')
        self.apply_button.set_tooltip_text(
            'Carry out every ticked change: update Hakubun and push to '
            'the sites.')
        self.apply_button.set_sensitive(False)
        self.apply_button.connect('clicked', lambda *_a: self.s_apply())
        self.cancel_button = Gtk.Button(label='Cancel')
        self.cancel_button.set_tooltip_text(
            'Stop the sync after the current push. Whatever was already '
            'pushed stays; the rest is offered again next time.')
        self.cancel_button.set_no_show_all(True)
        self.cancel_button.connect('clicked', lambda *_a: self.s_cancel_apply())
        # pack_end lays widgets out from the box's end inward, so the
        # first one packed ends up at the outer edge: pack in reverse of
        # the intended left-to-right (workflow) order -- Fetch changes,
        # Sync selected, Cancel.
        for b in (self.cancel_button, self.apply_button,
                  self.fetch_button):
            bar.pack_end(b, False, False, 0)
        page.pack_start(bar, False, False, 0)

        # How the plan is decided (field rules), so the list below
        # never reads as magic.
        plan_context = Gtk.Label(xalign=0, wrap=True)
        plan_context.set_markup(present.plan_context())
        page.pack_start(plan_context, False, False, 0)
        legend = Gtk.Label(xalign=0)
        legend.set_markup(
            '<span foreground="%s">↑ updates a site</span>  ·  '
            '<span foreground="%s">↓ updates Hakubun</span>  ·  '
            'uncheck anything to skip it' % (_PUSH_COLOR, _PULL_COLOR))
        page.pack_start(legend, False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        # Left: an inner notebook -- ordinary Changes, and (separately,
        # because "create this title on another site" is a different
        # kind of act than a field update) New entries. Both grouped by
        # show, both with opt-out/opt-in checkboxes.
        inner = Gtk.Notebook()
        self._changes_store, changes_widget = self._make_changes_tree()
        self._changes_view = self._last_changes_view
        self._changes_page_label = Gtk.Label(label='Changes')
        inner.append_page(changes_widget, self._changes_page_label)

        creates_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                              spacing=4)
        creates_help = Gtk.Label(xalign=0, wrap=True,
                                 label=present.CREATES_ENTRY_HELP)
        creates_box.pack_start(creates_help, False, False, 0)
        self._creates_store, creates_widget = self._make_changes_tree()
        self._creates_view = self._last_changes_view
        self._creates_view.connect('button-press-event',
                                   self._on_creates_button)
        creates_box.pack_start(creates_widget, True, True, 0)
        self._creates_page_label = Gtk.Label(label='New entries')
        inner.append_page(creates_box, self._creates_page_label)
        paned.pack1(inner, True, True)

        # Right: what needs a human (conflicts).
        decisions_frame = Gtk.Frame(label='Decisions')
        self._decisions_scroll = Gtk.ScrolledWindow()
        self._decisions_scroll.set_policy(Gtk.PolicyType.NEVER,
                                          Gtk.PolicyType.AUTOMATIC)
        self._decisions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                      spacing=8)
        self._decisions_box.set_border_width(6)
        self._decisions_scroll.add(self._decisions_box)
        decisions_frame.add(self._decisions_scroll)
        self._decisions_frame = decisions_frame
        paned.pack2(decisions_frame, True, True)
        paned.set_position(500)
        page.pack_start(paned, True, True, 0)
        self._set_decisions([])

        # Revealed during Apply.
        self.apply_progress = Gtk.ProgressBar()
        self.apply_progress.set_no_show_all(True)
        self.apply_progress.set_show_text(True)
        page.pack_start(self.apply_progress, False, False, 0)
        self.apply_log = Gtk.TextView()
        self.apply_log.set_editable(False)
        self.apply_log.set_cursor_visible(False)
        self.apply_log.set_monospace(True)
        self._apply_log_scroll = Gtk.ScrolledWindow()
        self._apply_log_scroll.set_no_show_all(True)
        self._apply_log_scroll.set_min_content_height(90)
        self._apply_log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                          Gtk.PolicyType.AUTOMATIC)
        self._apply_log_scroll.add(self.apply_log)
        page.pack_start(self._apply_log_scroll, False, False, 0)

        return page

    def is_busy(self):
        """True while a fetch/plan/apply worker is running."""
        return self._thread is not None and self._thread.is_alive()

    def _fmt_value(self, field, value):
        return present.fmt_value(field, value)

    def _fmt_target_value(self, field, value, target):
        return present.fmt_target_value(self.engine.adapters, field,
                                        value, target)

    def _change_label(self, change):
        direction, text = present.change_line(self.engine.adapters, change,
                                              self.engine.primary)
        # Plain Arrows-block glyphs, not the emoji-set ⬆/⬇ Qt uses --
        # those can fall back to a color-emoji font under Pango, which
        # ignores the foreground colour and breaks the push/pull coding.
        arrow, color = (('↓', _PULL_COLOR) if direction == 'pull'
                        else ('↑', _PUSH_COLOR))
        if change.creates_entry:
            color = _CREATE_COLOR
        return ('%s %s' % (arrow, text), color)

    def _make_changes_tree(self):
        """(TreeStore, scrolled widget) for one show-grouped, checkbox
        change list -- built twice, once for Changes and once for New
        entries, both over the same column scheme and toggle logic.
        The TreeView itself lands in self._last_changes_view."""
        store = Gtk.TreeStore(bool, bool, str, str, GObject.TYPE_PYOBJECT)
        view = Gtk.TreeView(model=store)
        view.set_headers_visible(False)
        toggle = Gtk.CellRendererToggle()
        toggle.connect('toggled', self._on_change_toggled, store)
        view.append_column(Gtk.TreeViewColumn(
            '', toggle, active=_C_ACTIVE, inconsistent=_C_INCONSISTENT))
        text = Gtk.CellRendererText()
        view.append_column(Gtk.TreeViewColumn(
            'Change', text, text=_C_LABEL, foreground=_C_COLOR))
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                          Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)
        self._last_changes_view = view
        return store, scroll

    def _populate_changes(self, store, changes):
        store.clear()
        groups = {}
        for change in changes:
            groups.setdefault(change.uuid, [change.title, []])[1].append(change)
        for _uid, (title, group_changes) in sorted(
                groups.items(), key=lambda kv: kv[1][0].casefold()):
            # Honour the plan's own selection: a new-entry create is
            # planned unticked, and ticking it must be a deliberate act.
            states = {c.selected for c in group_changes}
            parent = store.append(
                None, [states == {True}, len(states) > 1,
                       '%s (%d change(s))'
                       % (title, len(group_changes)), None, None])
            for change in group_changes:
                label, color = self._change_label(change)
                store.append(
                    parent, [change.selected, False, label, color, change])

    def _on_change_toggled(self, _renderer, path, store):
        it = store.get_iter(path)
        change = store.get_value(it, _C_CHANGE)
        new_state = not store.get_value(it, _C_ACTIVE)
        store.set_value(it, _C_ACTIVE, new_state)
        if change is None:
            # A group header: cascade to everything beneath it.
            # RECURSIVE, because Mirror's cards are three levels deep
            # (title -> tracker -> operation) -- a one-level cascade
            # ticked the tracker rows and left the operations they
            # contain untouched, so "yes to this show" silently did
            # nothing. The change trees are two levels deep and so are
            # unaffected.
            self._cascade_selection(store, it, new_state)
            store.set_value(it, _C_INCONSISTENT, False)
        else:
            change.selected = new_state
            parent = store.iter_parent(it)
            while parent is not None:
                self._sync_group_state(store, parent)
                parent = store.iter_parent(parent)

    def _on_creates_button(self, view, event):
        """Right-click on a new-entry row: durably decline the creation
        (engine.decline_create), as opposed to leaving it unticked --
        which only skips it for this plan."""
        if event.button != 3:
            return False
        pathinfo = view.get_path_at_pos(int(event.x), int(event.y))
        if pathinfo is None:
            return False
        it = self._creates_store.get_iter(pathinfo[0])
        change = self._creates_store.get_value(it, _C_CHANGE)
        if change is None or not change.creates_entry:
            return False
        menu = Gtk.Menu()
        item = Gtk.MenuItem(label='Never create this on %s'
                            % present.label(change.target))
        item.connect('activate',
                     lambda *_a, c=change: self._decline_create(c))
        menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _decline_create(self, change):
        self.engine.decline_create(change.uuid, change.target)
        self._status('%s will not be offered for creation on %s again.'
                     % (change.title, present.label(change.target)))
        self._run(self.engine.plan, self.r_planned, 'Planning...')

    @staticmethod
    def _cascade_selection(store, parent, new_state):
        child = store.iter_children(parent)
        while child is not None:
            store.set_value(child, _C_ACTIVE, new_state)
            store.set_value(child, _C_INCONSISTENT, False)
            change = store.get_value(child, _C_CHANGE)
            if change is not None and hasattr(change, 'selected'):
                change.selected = new_state
            MultiSyncWindow._cascade_selection(store, child, new_state)
            child = store.iter_next(child)

    @staticmethod
    def _sync_group_state(store, parent):
        if parent is None:
            return
        child = store.iter_children(parent)
        states = []
        while child is not None:
            states.append(store.get_value(child, _C_ACTIVE))
            child = store.iter_next(child)
        all_on, any_on = all(states), any(states)
        store.set_value(parent, _C_ACTIVE, all_on)
        store.set_value(parent, _C_INCONSISTENT,
                        any_on and not all_on)

    # -- conflicts (decisions pane) -----------------------------------

    def _local_label(self):
        return present.local_label(self.engine.primary)

    def _conflict_why(self, conflict):
        return present.conflict_why(conflict, self.engine.primary)

    def _decision_card(self, conflict):
        frame = Gtk.Frame(label='%s: %s' % (
            conflict.title, _FIELD_LABELS.get(conflict.field, conflict.field)))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(6)
        why = Gtk.Label(label=self._conflict_why(conflict), xalign=0, wrap=True)
        box.pack_start(why, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for source, value in sorted(conflict.values.items()):
            shown = self._fmt_value(conflict.field, value)
            if source == 'local':
                text = present.conflict_choice_label(conflict, source,
                                                     self.engine.primary)
            elif conflict.structural:
                # The provider's number is in ITS OWN episode structure
                # -- adopting it raw would record a different amount as
                # watched, so it is information here, never a button
                # (engine.resolve_conflict refuses it too).
                info = Gtk.Label(label='%s is at %s (its own structure)'
                                 % (source.capitalize(), shown))
                row.pack_start(info, False, False, 0)
                continue
            else:
                text = present.conflict_choice_label(conflict, source,
                                                     self.engine.primary)
            button = Gtk.Button(label=text)
            button.connect('clicked', lambda _b, c=conflict, s=source:
                           self._resolve_inline(c, s))
            row.pack_start(button, False, False, 0)
        if conflict.structural:
            button = Gtk.Button(label='Set episodes…')
            button.connect('clicked', lambda _b, c=conflict:
                           self._resolve_structural(c))
            row.pack_start(button, False, False, 0)
        box.pack_start(row, False, False, 0)
        frame.add(box)
        return frame

    def _set_decisions(self, conflicts):
        for child in self._decisions_box.get_children():
            self._decisions_box.remove(child)
        if conflicts:
            for conflict in sorted(conflicts, key=lambda c: c.title.casefold()):
                self._decisions_box.pack_start(self._decision_card(conflict),
                                               False, False, 0)
        else:
            placeholder = Gtk.Label(
                label='Nothing needs your decision right now. When a '
                'value changed in more than one place and Hakubun '
                'cannot choose on its own, the choice appears here '
                'after Fetch changes.', xalign=0, wrap=True)
            self._decisions_box.pack_start(placeholder, False, False, 0)
        self._decisions_frame.set_label('Decisions (%d)' % len(conflicts))
        self._decisions_box.show_all()

    def _resolve_inline(self, conflict, source):
        self.engine.resolve_conflict(conflict, source)
        self._status('Resolved %s for %s, replanning...' % (
            _FIELD_LABELS.get(conflict.field, conflict.field), conflict.title))
        self._run(self.engine.plan, self.r_planned, 'Planning...')

    def _resolve_structural(self, conflict):
        """Resolve a differing-episode-structures conflict with an
        explicit episode count in the LOCAL structure."""
        dialog = Gtk.Dialog(title='Set episodes', transient_for=self,
                            modal=True)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OK, Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_border_width(8)
        content.set_spacing(6)
        content.pack_start(Gtk.Label(
            label='%s\nEpisodes watched, in the local structure:'
            % conflict.title, xalign=0, wrap=True), False, False, 0)
        spin = Gtk.SpinButton.new_with_range(0, 100000, 1)
        spin.set_value(int(conflict.values.get('local') or 0))
        content.pack_start(spin, False, False, 0)
        content.show_all()
        response = dialog.run()
        value = spin.get_value_as_int()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.engine.resolve_conflict(conflict, 'value', value=value)
        self._status('Resolved %s for %s, replanning...' % (
            _FIELD_LABELS.get(conflict.field, conflict.field), conflict.title))
        self._run(self.engine.plan, self.r_planned, 'Planning...')

    # -- fetch / plan / apply -----------------------------------------

    def s_fetch(self):
        self._cancel.clear()
        self._run(self._fetch_and_plan, self.r_planned,
                  'Fetching provider lists...')

    def _fetch_and_plan(self):
        # Cancellable (window close sets _cancel): the engine checks
        # between providers/entries, so a close never stays parked
        # behind three full list downloads.
        errors = self.engine.fetch(should_cancel=self._cancel.is_set)
        plan = self.engine.plan(should_cancel=self._cancel.is_set)
        plan.errors.update(errors)
        return plan

    def r_planned(self, plan, error):
        if isinstance(error, SyncCancelled):
            self._status('Sync cancelled.')
            return
        if error is not None:
            self._status('Sync failed: %s' % error)
            return
        self._plan = plan
        ordinary = [c for c in plan.changes if not c.creates_entry]
        creates = [c for c in plan.changes if c.creates_entry]
        self._populate_changes(self._changes_store, ordinary)
        self._changes_view.expand_all()
        self._populate_changes(self._creates_store, creates)
        self._creates_view.expand_all()
        self._changes_page_label.set_text('Changes (%d)' % len(ordinary))
        self._creates_page_label.set_text('New entries (%d)'
                                          % len(creates))
        self._set_decisions(plan.conflicts)
        self.apply_progress.hide()
        self.apply_button.set_sensitive(bool(plan.changes))
        self.summary_label.set_markup(
            '<b>%s</b>' % GLib.markup_escape_text(
                present.plan_summary(plan)))
        self._status(present.plan_status(plan))
        self._refresh_identity()

    def s_apply(self):
        if self._plan is None:
            return
        plan, self._plan = self._plan, None
        self.apply_button.set_sensitive(False)
        self._cancel.clear()
        self.cancel_button.show()
        self.cancel_button.set_sensitive(True)
        selected = sum(1 for c in plan.changes if c.selected)
        self._set_log('Syncing %d selected change(s)...' % selected)
        self._apply_log_scroll.show()
        self.apply_progress.set_fraction(0)
        self.apply_progress.set_text('Syncing...')
        self.apply_progress.show()
        self._run(self.engine.apply, self.r_applied,
                  'Syncing selected changes...', plan,
                  should_cancel=self._cancel.is_set,
                  forward_progress=True)

    def s_cancel_apply(self):
        self._cancel.set()
        self.cancel_button.set_sensitive(False)
        self._append_log('Cancelling after the current push...')

    def _on_apply_progress(self, done, total, message):
        if total:
            self.apply_progress.set_fraction(min(done / total, 1.0))
            self.apply_progress.set_text('%d / %d' % (done, total))
        self._append_log(message)
        return False

    def r_applied(self, result, error):
        self.cancel_button.hide()
        if error is not None:
            self.apply_progress.hide()
            self._append_log('Apply failed: %s' % error)
            self._status('Apply failed: %s' % error)
            return
        self.apply_progress.set_fraction(1.0)
        verb = 'Cancelled after' if result.get('cancelled') else 'Synced:'
        text = '%s %d Hakubun update(s), %d site update(s)' % (
            verb, result['local'], result['pushed'])
        if result['errors']:
            text += ('. Failed: %s (their changes will be offered '
                     'again)' % ', '.join(
                         '%s (%s)' % kv
                         for kv in result['errors'].items()))
        self._append_log(text)
        self._status(text)
        self.s_fetch()

    def s_reset(self):
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text='Reset sync database?')
        dialog.format_secondary_text(
            'Identities, mappings, sync history, resolved decisions and '
            'per-field sync rules are deleted and re-derived on the next '
            'fetch. Your provider lists are NOT touched.')
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.YES:
            return
        if self.is_busy():
            # Resetting under a running fetch/apply would drop the
            # tables out from under it (and block the main loop on the
            # store lock). Ask it to stop; the user clicks again.
            self._cancel.set()
            self._status('Stopping the running operation first. Press '
                         'Reset again once it has.')
            return
        self.store.reset()
        self._plan = None
        self._changes_store.clear()
        self._creates_store.clear()
        self._changes_page_label.set_text('Changes')
        self._creates_page_label.set_text('New entries')
        self._set_decisions([])
        # The Mirror tab's preview describes the database that was just
        # wiped -- membership decisions included. Clear it too, or its
        # rows would offer to act on entities that no longer exist.
        self._mirror_plan = None
        self._render_mirror_cards()
        self._set_mirror_decisions([])
        self.mirror_apply_button.set_sensitive(False)
        self.mirror_summary.set_text('Preview a mirror to see what '
                                     'would change.')
        self.apply_progress.hide()
        self._set_log('')
        self._apply_log_scroll.hide()
        self.summary_label.set_text('Fetch changes to see what would '
                                    'sync.')
        self.apply_button.set_sensitive(False)
        self._refresh_identity()
        self._refresh_policy_widgets()
        self._status('Sync database reset. Run Fetch changes to '
                     're-derive it.')

    def _set_log(self, text):
        self.apply_log.get_buffer().set_text(text)

    def _append_log(self, text):
        buf = self.apply_log.get_buffer()
        buf.insert(buf.get_end_iter(), ('' if buf.get_char_count() == 0
                                        else '\n') + text)

    # -- Mirror ---------------------------------------------------------
    #
    # GTK twin of the Qt Mirror tab; same information architecture, same
    # wording (both delegate to sync/present.py). Sync is incremental,
    # Mirror converges the TRACKERS onto what Ownership says
    # (sync/mirror.py) -- its own tab, preview and apply path, because
    # it is a different and potentially much larger operation.
    #
    # Hakubun never appears here as a tracker side. Local state still
    # converges (MirrorPlan.local) but is not shown: it is
    # reconciliation state, not one of the things being mirrored.

    def _build_mirror_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(6)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.mirror_summary = Gtk.Label(xalign=0)
        self.mirror_summary.set_text('Preview a mirror to see what '
                                     'would change.')
        bar.pack_start(self.mirror_summary, False, False, 0)
        self.mirror_preview_button = Gtk.Button(label='Preview mirror')
        self.mirror_preview_button.set_tooltip_text(
            'Work out what each tracker should contain according to '
            'Ownership. Nothing is written anywhere until you apply.')
        self.mirror_preview_button.connect(
            'clicked', lambda *_a: self.s_mirror_preview())
        self.mirror_apply_button = Gtk.Button(label='Apply mirror…')
        self.mirror_apply_button.set_tooltip_text(
            'Review the totals, then carry out the ticked changes.')
        self.mirror_apply_button.set_sensitive(False)
        self.mirror_apply_button.connect(
            'clicked', lambda *_a: self.s_mirror_apply())
        self.mirror_cancel_button = Gtk.Button(label='Cancel')
        self.mirror_cancel_button.set_no_show_all(True)
        self.mirror_cancel_button.connect(
            'clicked', lambda *_a: self.s_cancel_apply())
        for b in (self.mirror_cancel_button, self.mirror_apply_button,
                  self.mirror_preview_button):
            bar.pack_end(b, False, False, 0)
        page.pack_start(bar, False, False, 0)

        help_label = Gtk.Label(xalign=0, wrap=True)
        help_label.set_markup(present.MIRROR_TAB_HELP)
        page.pack_start(help_label, False, False, 0)

        # The bulk gates. Unticked by default and enforced in the
        # engine (SyncEngine.apply_mirror), not here: these two boxes
        # are how the user expresses the choice, not what makes it
        # safe. Additions and removals are approved independently --
        # "yes to adds, no to deletes" is the common answer.
        gates = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.mirror_allow_adds = Gtk.CheckButton(
            label='Allow adding entries')
        self.mirror_allow_adds.set_tooltip_text(
            'Permit Mirror to create new entries on trackers that are '
            'missing them. Each entry must still be ticked.')
        self.mirror_allow_removes = Gtk.CheckButton(
            label='Allow removing entries')
        self.mirror_allow_removes.set_tooltip_text(
            'Permit Mirror to DELETE entries from trackers you marked '
            'as not needing them. This cannot be undone from Hakubun.')
        gates.pack_start(self.mirror_allow_adds, False, False, 0)
        gates.pack_start(self.mirror_allow_removes, False, False, 0)
        page.pack_start(gates, False, False, 0)

        # ONE view, organized by title -- see the Qt twin. The old
        # per-kind pages survive as a filter, which is what they were
        # actually good for: narrowing a large plan.
        filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                             spacing=6)
        filter_row.pack_start(Gtk.Label(label='Show:'), False, False, 0)
        self.mirror_filter = Gtk.ComboBoxText()
        for key, text in present.CARD_CATEGORIES:
            self.mirror_filter.append(key, text)
        self.mirror_filter.set_active(0)
        self.mirror_filter.connect(
            'changed', lambda *_a: self._render_mirror_cards())
        filter_row.pack_start(self.mirror_filter, False, False, 0)
        self.mirror_filter_count = Gtk.Label(xalign=0)
        filter_row.pack_start(self.mirror_filter_count, False, False, 0)
        page.pack_start(filter_row, False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._mirror_store, mirror_widget = self._make_changes_tree()
        self._mirror_view = self._last_changes_view
        self._mirror_view.connect('button-press-event',
                                  self._on_mirror_row_button)
        paned.pack1(mirror_widget, True, True)

        frame = Gtk.Frame(label='Decisions')
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._mirror_decisions_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._mirror_decisions_box.set_border_width(6)
        scroll.add(self._mirror_decisions_box)
        frame.add(scroll)
        self._mirror_decisions_frame = frame
        paned.pack2(frame, True, True)
        paned.set_position(520)
        page.pack_start(paned, True, True, 0)

        self._mirror_plan = None
        self._set_mirror_decisions([])
        return page

    def _on_mirror_row_button(self, view, event):
        """Right-click inside a Mirror card.

        One handler, because there is now one tree: a TRACKER row
        carries (issue, label) and offers the three membership
        decisions (sync/membership.py), while an OPERATION row produced
        by a stored decision offers the way back to it -- that field is
        settled and so no longer appears as a conflict card.

        Membership decisions persist: the next mirror does not
        rediscover a discrepancy the user has already settled, and
        'Remove from' is the ONLY way a deletion is ever proposed.
        """
        if event.button != 3:
            return False
        pathinfo = view.get_path_at_pos(int(event.x), int(event.y))
        if pathinfo is None:
            return False
        store = self._mirror_store
        payload = store.get_value(store.get_iter(pathinfo[0]), _C_CHANGE)
        if payload is not None and not isinstance(payload, tuple):
            if 'you chose this' not in getattr(payload, 'reason', ''):
                return False
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label='Ask me about %s again'
                                % present.field_label(payload.field))
            item.connect('activate', lambda *_a:
                         self._clear_mirror_resolution(payload))
            menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        if not isinstance(payload, tuple):
            return False
        issue, provider_label = payload
        if issue is None:
            return False
        provider = provider_label.lower()
        menu = Gtk.Menu()
        if provider in issue.present:
            item = Gtk.MenuItem(
                label=present.mirror_remove_label(provider))
            item.connect('activate', lambda *_a:
                         self._confirm_membership_removal(issue, provider))
        else:
            item = Gtk.MenuItem(
                label=present.mirror_add_label(issue, provider))
            item.connect('activate', lambda *_a:
                         self._set_membership(issue, provider, 'present'))
        menu.append(item)
        item = Gtk.MenuItem(label=present.mirror_ignore_label(provider))
        item.connect('activate', lambda *_a:
                     self._set_membership(issue, provider, 'ignore'))
        menu.append(item)
        if provider in issue.decisions:
            item = Gtk.MenuItem(label='Ask me about %s again'
                                % provider_label)
            item.connect('activate', lambda *_a:
                         self._set_membership(issue, provider, None))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _clear_mirror_resolution(self, op):
        self.engine.clear_mirror_resolution(op.uuid, op.field)
        self._status('%s: %s will be asked about again.'
                     % (op.title, present.field_label(op.field)))
        self.s_mirror_preview()

    def _confirm_membership_removal(self, issue, provider):
        """Marking a tracker 'absent' is what authorizes a deletion, so
        it is confirmed at the moment it is recorded as well as at the
        moment it is applied."""
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text='Mark "%s" as not belonging on %s?'
                 % (issue.title, present.label(provider)))
        dialog.format_secondary_text(
            'Mirror will then offer to DELETE that entry from your %s '
            'account. Nothing is deleted until you tick it, allow '
            'removals, and apply.' % present.label(provider))
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.YES:
            return
        self._set_membership(issue, provider, 'absent')

    def _set_membership(self, issue, provider, want):
        if want is None:
            self.engine.clear_membership(issue.uuid, provider)
            self._status('%s: %s will be asked about again.'
                         % (issue.title, present.label(provider)))
        else:
            self.engine.set_membership(issue.uuid, provider, want)
            self._status('%s: recorded "%s" for %s.'
                         % (issue.title, want, present.label(provider)))
        self.s_mirror_preview()

    def _set_mirror_decisions(self, conflicts):
        for child in self._mirror_decisions_box.get_children():
            self._mirror_decisions_box.remove(child)
        if conflicts:
            for conflict in sorted(conflicts,
                                   key=lambda c: c.title.casefold()):
                self._mirror_decisions_box.pack_start(
                    self._mirror_decision_card(conflict), False, False, 0)
        else:
            placeholder = Gtk.Label(
                label='Nothing needs your decision. When two trackers '
                'hold different values for a field whose rule cannot '
                'settle it on its own, the choice appears here.',
                xalign=0, wrap=True)
            self._mirror_decisions_box.pack_start(placeholder, False,
                                                  False, 0)
        self._mirror_decisions_frame.set_label('Decisions (%d)'
                                               % len(conflicts))
        self._mirror_decisions_box.show_all()

    def _mirror_decision_card(self, conflict):
        """A mirror decision is between TRACKERS -- the card lists only
        tracker sides."""
        frame = Gtk.Frame(label='%s: %s' % (
            conflict.title,
            _FIELD_LABELS.get(conflict.field, conflict.field)))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_border_width(6)
        box.pack_start(Gtk.Label(label=present.mirror_conflict_why(conflict),
                                 xalign=0, wrap=True), False, False, 0)
        if conflict.structural:
            box.pack_start(Gtk.Label(label=present.MIRROR_STRUCTURAL_NOTE,
                                     xalign=0, wrap=True), False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for source in sorted(conflict.values):
            if source == 'local':
                continue
            shown = self._fmt_value(conflict.field,
                                    conflict.values[source])
            if conflict.structural:
                # Each tracker's number is in its OWN episode
                # structure: information only, never a button. See the
                # Qt twin.
                row.pack_start(
                    Gtk.Label(label='%s is at %s (its own structure)'
                              % (present.label(source), shown)),
                    False, False, 0)
                continue
            button = Gtk.Button(label='Use %s: %s'
                                % (present.label(source), shown))
            button.connect('clicked', lambda _b, c=conflict, s=source:
                           self._resolve_mirror(c, s))
            row.pack_start(button, False, False, 0)
        box.pack_start(row, False, False, 0)
        frame.add(box)
        return frame

    def _resolve_mirror(self, conflict, source):
        # The MIRROR resolution path, not Sync's: Sync records a
        # decision by writing local state, which Mirror never reads --
        # the conflict would come straight back on the next preview.
        self.engine.resolve_mirror_conflict(conflict, source)
        self._status('Resolved %s for %s, re-previewing...' % (
            _FIELD_LABELS.get(conflict.field, conflict.field),
            conflict.title))
        self.s_mirror_preview()

    def s_mirror_preview(self):
        self._cancel.clear()
        self._run(self.engine.mirror_plan, self.r_mirror_planned,
                  'Working out what each tracker should contain...',
                  should_cancel=self._cancel.is_set)

    def r_mirror_planned(self, plan, error):
        if isinstance(error, SyncCancelled):
            self._status('Mirror cancelled.')
            return
        if error is not None:
            self._status('Mirror preview failed: %s' % error)
            return
        self._mirror_plan = plan
        self._render_mirror_cards()
        self._set_mirror_decisions(plan.conflicts)
        self.mirror_apply_button.set_sensitive(not plan.clean)
        self.mirror_summary.set_markup(
            '<b>%s</b>' % GLib.markup_escape_text(
                present.mirror_plan_summary(plan)))
        self._status(present.mirror_plan_summary(plan))

    def _render_mirror_cards(self):
        """Draw the plan as one card per title -- see the Qt twin's
        _render_mirror_cards for why the plan is projected this way."""
        store = self._mirror_store
        store.clear()
        plan = self._mirror_plan
        if plan is None:
            self.mirror_filter_count.set_text('')
            return
        category = self.mirror_filter.get_active_id() or 'all'
        cards = present.mirror_cards(plan, self.engine.adapters, category)
        total = len(present.mirror_cards(plan, self.engine.adapters))
        self.mirror_filter_count.set_text(
            '%d of %d entr%s' % (len(cards), total,
                                 'y' if len(cards) == 1 else 'ies'))
        for card in cards:
            states = {o.selected for o in card.ops}
            parent = store.append(None, [
                states == {True}, len(states) > 1,
                '%s   —   %s' % (card.title,
                                 present.mirror_card_headline(card)),
                None, None])
            self._build_mirror_card(store, parent, card)

    def _build_mirror_card(self, store, parent, card):
        if card.desired:
            store.append(parent, [
                False, False,
                'Ownership says:  '
                + '   ·   '.join('%s %s (%s)' % (name, value, owner)
                                 if owner else '%s %s' % (name, value)
                                 for name, value, owner, _why
                                 in card.desired),
                _PULL_COLOR, None])

        for row in card.trackers:
            bits = ['%s  %s' % (row.label, '✓' if row.present else '✗')]
            if row.owns:
                bits.append('owns %s' % ', '.join(row.owns))
            if row.values:
                bits.append(' · '.join('%s %s' % (n, v)
                                       for n, v in row.values))
            if row.note:
                bits.append(row.note)
            # Carries (issue, tracker label) so the membership menu can
            # act on the row the user actually right-clicked.
            tracker_it = store.append(parent, [
                False, False, '   —   '.join(bits),
                _PULL_COLOR if row.present else _CREATE_COLOR,
                (card.issue, row.label) if card.issue is not None
                else None])

            for name, old, new, why in row.changes:
                op = next((o for o in card.ops
                           if getattr(o, 'target', None) == row.provider
                           and present.field_label(getattr(o, 'field', ''))
                           == name), None)
                if op is None:
                    continue
                text = '%s: %s → %s' % (name, old, new)
                if why:
                    text += '  — %s' % why
                store.append(tracker_it, [op.selected, False, text,
                                          _PUSH_COLOR, op])

            if row.action == 'add':
                op = next((o for o in card.ops
                           if getattr(o, 'provider', None) == row.provider
                           and hasattr(o, 'values')), None)
                if op is not None:
                    # ONE row for the whole entry -- see the Qt twin.
                    store.append(tracker_it, [
                        op.selected, False,
                        'Create this entry on %s with %s'
                        % (row.label,
                           ' · '.join('%s %s' % (n, v)
                                      for n, v in row.add_values)),
                        _CREATE_COLOR, op])
            elif row.action == 'remove':
                op = next((o for o in card.ops
                           if getattr(o, 'provider', None) == row.provider
                           and not hasattr(o, 'values')), None)
                if op is not None:
                    store.append(tracker_it, [
                        op.selected, False,
                        'DELETE this entry from %s' % row.label,
                        _CONFLICT_COLOR, op])

        for conflict in card.conflicts:
            store.append(parent, [
                False, False,
                '%s needs your decision — see Decisions'
                % present.field_label(conflict.field),
                _CONFLICT_COLOR, None])

        if card.local:
            group = store.append(parent, [
                False, False, "%s's own copy" % present.local_label(),
                None, None])
            for op in card.local:
                store.append(group, [op.selected, False,
                                     present.mirror_local_line(op),
                                     _PULL_COLOR, op])

    def s_mirror_apply(self):
        """Never applies silently: the totals are shown first, per
        tracker, and the two bulk gates must be ticked for the
        corresponding category to run at all."""
        if self._mirror_plan is None:
            return
        plan = self._mirror_plan
        allow_adds = self.mirror_allow_adds.get_active()
        allow_removes = self.mirror_allow_removes.get_active()

        notes = []
        if plan.selected_adds() and not allow_adds:
            notes.append('Additions are ticked but "Allow adding '
                         'entries" is off — they will be skipped.')
        if plan.selected_removes() and not allow_removes:
            notes.append('Removals are ticked but "Allow removing '
                         'entries" is off — they will be skipped.')

        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text='Apply mirror?')
        dialog.format_secondary_text(
            present.mirror_confirmation(plan)
            + ('\n\n' + '\n'.join(notes) if notes else ''))
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.OK:
            self._status('Mirror cancelled.')
            return

        self._mirror_plan = None
        self.mirror_apply_button.set_sensitive(False)
        self._cancel.clear()
        self.mirror_cancel_button.show()
        self.mirror_cancel_button.set_sensitive(True)
        self._set_log('Mirroring...')
        self._apply_log_scroll.show()
        self.apply_progress.set_fraction(0)
        self.apply_progress.set_text('Mirroring...')
        self.apply_progress.show()
        self._run(self.engine.apply_mirror, self.r_mirror_applied,
                  'Applying the mirror...', plan,
                  allow_adds=allow_adds, allow_removes=allow_removes,
                  should_cancel=self._cancel.is_set,
                  forward_progress=True)

    def r_mirror_applied(self, result, error):
        self.mirror_cancel_button.hide()
        if error is not None:
            self.apply_progress.hide()
            self._append_log('Mirror failed: %s' % error)
            self._status('Mirror failed: %s' % error)
            return
        self.apply_progress.set_fraction(1.0)
        text = present.mirror_result_status(result)
        self._append_log(text)
        self._status(text)
        # Re-fetch, then re-preview MIRROR -- see the Qt twin.
        self._run(self._fetch_and_mirror, self.r_refreshed_after_mirror,
                  'Refreshing after the mirror...')

    def _fetch_and_mirror(self):
        """One fetch feeding BOTH previews -- see the Qt twin: left
        alone, the Sync tab keeps a stale plan and an enabled Sync
        button describing changes the mirror already made."""
        errors = self.engine.fetch(should_cancel=self._cancel.is_set)
        mirror = self.engine.mirror_plan(should_cancel=self._cancel.is_set)
        sync = self.engine.plan(should_cancel=self._cancel.is_set)
        mirror.errors.update(errors)
        sync.errors.update(errors)
        return mirror, sync

    def r_refreshed_after_mirror(self, plans, error):
        if error is not None or plans is None:
            self.r_mirror_planned(None, error)
            return
        mirror, sync = plans
        self.r_mirror_planned(mirror, None)
        self.r_planned(sync, None)

    # -- Configuration (simple per-field rules) ------------------------

    def _build_config_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(8)
        head = Gtk.Label(xalign=0)
        head.set_markup('<b>How should each field sync?</b>')
        page.pack_start(head, False, False, 0)
        page.pack_start(Gtk.Label(
            label='Pick one rule per field: follow one site, keep the '
            'best value, ask you when sites disagree, or don\'t sync it '
            'at all. Scores pushed to sites with a coarser scale are '
            'rounded to fit. The full policy matrix lives in the '
            'Advanced tab; both views control the same settings.',
            xalign=0, wrap=True), False, False, 0)

        providers = list(self.engine.adapters)
        grid = Gtk.Grid(column_spacing=18, row_spacing=8)
        grid.set_border_width(6)
        self._policy_combos = {}
        ownership = self.store.ownership()
        for row, field in enumerate(USER_FIELDS):
            grid.attach(Gtk.Label(label=_FIELD_LABELS.get(field, field),
                                  xalign=0), 0, row, 1, 1)
            combo = Gtk.ComboBoxText()
            current = ownership[field].serialize()
            for key, choice_label in present.policy_choices(
                    field, providers, current):
                combo.append(key, choice_label)
            combo.set_active_id(current)
            combo.connect('changed', self._on_policy_combo, field)
            grid.attach(combo, 1, row, 1, 1)
            self._policy_combos[field] = combo
        page.pack_start(grid, False, False, 0)
        return page

    def _on_policy_combo(self, combo, field):
        if self._policy_updating:
            return
        serialized = combo.get_active_id()
        if serialized:
            self._set_policy(field, serialized)

    def _set_policy(self, field, serialized):
        policy = FieldPolicy.parse(serialized)
        self.store.set_ownership(field, policy)
        self._status('%s now syncs as: %s'
                     % (_FIELD_LABELS.get(field, field),
                        present.policy_label(policy)))
        self._refresh_policy_widgets()

    def _refresh_policy_widgets(self):
        """Reflect the store's current per-field policies into BOTH the
        simple Configuration combos and the Advanced matrix -- either
        one can change them, and the other must never show stale
        state."""
        self._policy_updating = True
        try:
            ownership = self.store.ownership()
            for field, combo in getattr(self, '_policy_combos',
                                        {}).items():
                current = ownership[field].serialize()
                if not combo.set_active_id(current):
                    # Set through the matrix to something the simple
                    # list doesn't offer for this field -- show the
                    # truth rather than misreport it.
                    combo.append(current, present.policy_label(
                        FieldPolicy.parse(current)))
                    combo.set_active_id(current)
            for (field, policy_text), radio in getattr(
                    self, '_matrix_radios', {}).items():
                if policy_text == ownership[field].serialize():
                    radio.set_active(True)
        finally:
            self._policy_updating = False

    # -- Advanced: the full policy matrix ------------------------------

    def _build_matrix_widget(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head = Gtk.Label(xalign=0)
        head.set_markup('<b>Full policy matrix</b>')
        box.pack_start(head, False, False, 0)
        box.pack_start(Gtk.Label(
            label='Every combination, including ones the simple view '
            'does not offer. A provider column makes that tracker '
            'authoritative: its value wins everywhere, always. The rule '
            'columns: Manual asks you when sides genuinely disagree, '
            'Union combines sets, Highest/Lowest pick an extreme, '
            'Progress favours the furthest episode. "Individual" keeps '
            'a field per-site and never syncs it.',
            xalign=0, wrap=True), False, False, 0)

        providers = list(self.engine.adapters)
        columns = ([('provider:%s' % p, p.capitalize())
                    for p in providers]
                   + [('reconcile:manual', 'Manual'),
                      ('reconcile:union', 'Union'),
                      ('reconcile:max', 'Highest'),
                      ('reconcile:min', 'Lowest'),
                      ('reconcile:progress', 'Progress'),
                      ('individual', 'Individual')])
        grid = Gtk.Grid(column_spacing=18, row_spacing=10)
        grid.set_border_width(6)
        for col, (_policy, label) in enumerate(columns, start=1):
            header = Gtk.Label()
            header.set_markup('<b>%s</b>' % label)
            grid.attach(header, col, 0, 1, 1)
        self._matrix_radios = {}
        ownership = self.store.ownership()
        for row, field in enumerate(USER_FIELDS, start=1):
            grid.attach(Gtk.Label(label=_FIELD_LABELS.get(field, field),
                                  xalign=0), 0, row, 1, 1)
            current = ownership[field].serialize()
            group_leader = None
            for col, (policy_text, _label) in enumerate(columns, start=1):
                radio = Gtk.RadioButton.new_from_widget(group_leader)
                if group_leader is None:
                    group_leader = radio
                radio.set_halign(Gtk.Align.CENTER)
                if policy_text == current:
                    radio.set_active(True)
                radio.connect('toggled', self._on_ownership_toggled,
                              field, policy_text)
                grid.attach(radio, col, row, 1, 1)
                self._matrix_radios[(field, policy_text)] = radio
        box.pack_start(grid, False, False, 0)
        return box

    def _on_ownership_toggled(self, radio, field, policy_text):
        if not radio.get_active() or self._policy_updating:
            return
        self._set_policy(field, policy_text)

    # -- Identity ------------------------------------------------------

    def _build_identity_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(8)
        self._identity_intro = Gtk.Label(xalign=0, wrap=True)
        page.pack_start(self._identity_intro, False, False, 0)

        # Columns: provider, type, title, aka, status, py-object issue.
        self._identity_store = Gtk.ListStore(str, str, str, str, str,
                                             GObject.TYPE_PYOBJECT)
        self._identity_view = Gtk.TreeView(model=self._identity_store)
        for i, title in enumerate(('Provider', 'Type', 'Title',
                                   'Also known as', 'Status')):
            self._identity_view.append_column(
                Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i))
        self._identity_view.get_selection().connect(
            'changed', self._on_identity_selected)
        id_scroll = Gtk.ScrolledWindow()
        id_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        id_scroll.add(self._identity_view)
        page.pack_start(id_scroll, True, True, 0)

        self._identity_box = Gtk.Frame(label='Resolution')
        rbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        rbox.set_border_width(6)
        self._identity_info = Gtk.Label(xalign=0, wrap=True)
        rbox.pack_start(self._identity_info, False, False, 0)

        self._rb_confirm = Gtk.RadioButton.new_with_label_from_widget(
            None, 'Use a matched entry')
        self._candidate_combo = Gtk.ComboBoxText()
        self._rb_search = Gtk.RadioButton.new_with_label_from_widget(
            self._rb_confirm, 'Search manually: find it on another provider')
        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._search_provider = Gtk.ComboBoxText()
        self._search_text = Gtk.Entry()
        self._search_button = Gtk.Button(label='Search')
        self._search_button.connect('clicked', lambda *_a: self.s_identity_search())
        search_row.pack_start(self._search_provider, False, False, 0)
        search_row.pack_start(self._search_text, True, True, 0)
        search_row.pack_start(self._search_button, False, False, 0)
        self._search_results = Gtk.ComboBoxText()
        self._search_result_entries = []
        self._rb_provider_only = Gtk.RadioButton.new_with_label_from_widget(
            self._rb_confirm, 'Keep provider-only: do not sync elsewhere')
        self._rb_defer = Gtk.RadioButton.new_with_label_from_widget(
            self._rb_confirm, 'Create mappings later: keep watching')
        self._rb_ignore = Gtk.RadioButton.new_with_label_from_widget(
            self._rb_confirm, 'Ignore this title: never ask again')
        for w in (self._rb_confirm, self._candidate_combo, self._rb_search):
            rbox.pack_start(w, False, False, 0)
        rbox.pack_start(search_row, False, False, 0)
        rbox.pack_start(self._search_results, False, False, 0)
        for w in (self._rb_provider_only, self._rb_defer, self._rb_ignore):
            rbox.pack_start(w, False, False, 0)
        resolve_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._identity_resolve_button = Gtk.Button(label='Resolve')
        self._identity_resolve_button.connect(
            'clicked', lambda *_a: self.s_identity_resolve())
        resolve_row.pack_end(self._identity_resolve_button, False, False, 0)
        rbox.pack_start(resolve_row, False, False, 0)
        self._identity_box.add(rbox)
        self._identity_box.set_sensitive(False)
        page.pack_start(self._identity_box, False, False, 0)
        return page

    @staticmethod
    def _display_title(title, aliases):
        return present.display_title(title, aliases)

    def _refresh_identity(self):
        if not hasattr(self, '_identity_store'):
            return
        self._identity_store.clear()
        for issue in self.store.identity_open():
            info = issue.get('entry') or {}
            aliases = info.get('aliases') or []
            others = [a for a in aliases if a and a != issue['title']]
            media_type = info.get('media_type')
            self._identity_store.append([
                issue['provider'],
                media_type.capitalize() if media_type else '?',
                self._display_title(issue['title'], aliases),
                ' / '.join(others[:2]), issue['status'], issue])
        count = len(self._identity_store)
        self._identity_intro.set_markup(
            ('<b>%d title(s) need matching:</b> Hakubun could not '
             'decide on its own which entry on your other sites each '
             'of these is. ' % count if count else
             '<b>No titles need matching right now.</b> ')
            + 'Certain matches (exact ID links, single exact-title '
            'matches) are linked automatically and never appear here.')
        self._identity_label.set_text('Identity (%d)' % count)

    def _selected_identity(self):
        model, it = self._identity_view.get_selection().get_selected()
        return model.get_value(it, 5) if it is not None else None

    def _on_identity_selected(self, _selection):
        issue = self._selected_identity()
        self._identity_box.set_sensitive(issue is not None)
        if issue is None:
            return
        info = issue.get('entry') or {}
        candidates = issue['candidates']
        year = ' (%s)' % info['year'] if info.get('year') else ''
        why = ('%d possible match(es) found by title, none certain enough to '
               'link automatically.' % len(candidates) if candidates else
               'No existing entry matched and no cross-provider ID was '
               'published.')
        self._identity_info.set_markup(
            '<b>%s</b>%s: %s entry %s. %s' % (
                GLib.markup_escape_text(self._display_title(
                    issue['title'], info.get('aliases'))),
                year, issue['provider'], issue['provider_id'], why))
        self._candidate_combo.remove_all()
        self._candidate_entries = candidates
        for cand in candidates:
            provs = ', '.join('on %s (%s)' % kv for kv in sorted(
                cand.get('providers', {}).items()))
            self._candidate_combo.append_text('%s%s: %s (%s)' % (
                self._display_title(cand.get('title'), cand.get('aliases')),
                ' (%s)' % cand['year'] if cand.get('year') else '',
                provs or 'no providers yet', cand.get('via', '')))
        self._rb_confirm.set_sensitive(bool(candidates))
        if candidates:
            self._candidate_combo.set_active(0)
            self._rb_confirm.set_active(True)
        else:
            self._rb_defer.set_active(True)
        self._rb_provider_only.set_label(
            'Keep %s-only: do not sync this entry elsewhere'
            % issue['provider'])
        self._search_provider.remove_all()
        for name in self.engine.adapters:
            if name != issue['provider']:
                self._search_provider.append_text(name)
        if self._search_provider.get_model().iter_n_children(None):
            self._search_provider.set_active(0)
        self._search_text.set_text(issue['title'] or '')
        self._search_results.remove_all()
        self._search_result_entries = []

    def s_identity_search(self):
        provider = self._search_provider.get_active_text()
        if not provider:
            return
        adapter = self.engine.adapters[provider]
        self._run(adapter.search, self.r_identity_search,
                  'Searching %s...' % provider, self._search_text.get_text().strip())

    def r_identity_search(self, results, error):
        if error is not None:
            self._status('Search failed: %s' % error)
            return
        self._search_results.remove_all()
        self._search_result_entries = list(results or [])
        for entry in self._search_result_entries:
            self._search_results.append_text(
                '%s (%s %s)' % (entry.title, entry.provider, entry.provider_id))
        if self._search_result_entries:
            self._search_results.set_active(0)
        self._rb_search.set_active(True)
        self._status('Found %d result(s).' % len(self._search_result_entries))

    def s_identity_resolve(self):
        issue = self._selected_identity()
        if issue is None:
            return
        identity = self.engine.identity
        entry = NormalizedEntry(provider=issue['provider'],
                                provider_id=issue['provider_id'],
                                title=issue['title'] or '')
        try:
            if self._rb_confirm.get_active():
                idx = self._candidate_combo.get_active()
                if idx < 0:
                    return
                identity.resolve_conflict(issue['id'], 'confirm',
                                          target_uuid=self._candidate_entries[idx]['uuid'])
            elif self._rb_search.get_active():
                idx = self._search_results.get_active()
                if idx < 0:
                    self._status('Pick a search result first.')
                    return
                found = self._search_result_entries[idx]
                mapping = self.store.mapping_for(found.provider, found.provider_id)
                if mapping:
                    uid = mapping['uuid']
                else:
                    uid = self.store.create_entity(
                        found.title, media_type=found.media_type,
                        year=found.year, total=found.total)
                    self.store.add_mapping(uid, found.provider,
                                           found.provider_id, confirmed=True)
                identity.resolve_conflict(issue['id'], 'confirm', target_uuid=uid)
            elif self._rb_provider_only.get_active():
                identity.resolve_conflict(issue['id'], 'provider_only', entry=entry)
            elif self._rb_defer.get_active():
                identity.resolve_conflict(issue['id'], 'defer')
            elif self._rb_ignore.get_active():
                identity.resolve_conflict(issue['id'], 'ignore')
        except ValueError as e:
            self._status('Could not resolve: %s' % e)
            return
        self._refresh_identity()
        self._status('Identity updated; fetch again to sync the entry.')

    # -- Advanced (matrix, inspector, reset) ---------------------------

    def _build_advanced_tab(self):
        """Technical and destructive tools, deliberately out of the
        main workflow: the full policy matrix, the identity inspector,
        and the database reset."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(8)
        page.pack_start(self._build_matrix_widget(), False, False, 0)

        head = Gtk.Label(xalign=0)
        head.set_markup('<b>Identity inspector</b>')
        page.pack_start(head, False, False, 0)
        page.pack_start(Gtk.Label(
            label='Enter one entry by its provider ID and see how '
            'multisync resolved it: what it maps to on your other '
            'providers, how each link was made, and its raw data.',
            xalign=0, wrap=True), False, False, 0)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.pack_start(Gtk.Label(label='Provider:'), False, False, 0)
        self._inspect_provider = Gtk.ComboBoxText()
        for name in self.engine.adapters:
            self._inspect_provider.append_text(name)
        if self.engine.adapters:
            self._inspect_provider.set_active(0)
        bar.pack_start(self._inspect_provider, False, False, 0)
        bar.pack_start(Gtk.Label(label='ID:'), False, False, 0)
        self._inspect_id = Gtk.Entry()
        self._inspect_id.set_placeholder_text('e.g. 52991')
        self._inspect_id.connect('activate', lambda *_a: self.s_inspect())
        bar.pack_start(self._inspect_id, True, True, 0)
        look_up = Gtk.Button(label='Look up')
        look_up.connect('clicked', lambda *_a: self.s_inspect())
        bar.pack_start(look_up, False, False, 0)
        self._inspect_open = Gtk.Button(label='Open page')
        self._inspect_open.set_sensitive(False)
        self._inspect_open.connect('clicked', lambda *_a: self.s_inspect_open())
        bar.pack_start(self._inspect_open, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self._inspect_output = Gtk.Label(xalign=0, yalign=0, wrap=True,
                                         selectable=True)
        self._inspect_output.set_markup(
            '<span foreground="gray">Nothing looked up yet.</span>')
        out_scroll = Gtk.ScrolledWindow()
        out_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        out_scroll.add(self._inspect_output)
        page.pack_start(out_scroll, True, True, 0)

        # Destructive, so it lives here rather than next to the Sync
        # button -- same semantics and confirmation as before.
        reset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.reset_button = Gtk.Button(label='Reset sync database...')
        self.reset_button.set_tooltip_text(
            'Wipe the sync database (identities, mappings, history, '
            'sync rules) and start clean. Your provider lists are '
            'never touched; the next Fetch changes re-derives '
            'everything.')
        self.reset_button.connect('clicked', lambda *_a: self.s_reset())
        reset_row.pack_start(self.reset_button, False, False, 0)
        page.pack_start(reset_row, False, False, 0)
        return page

    def s_inspect(self):
        provider = self._inspect_provider.get_active_text()
        provider_id = self._inspect_id.get_text().strip()
        if not provider or not provider_id:
            return
        from hakubun.sync.inspect import inspect_entry
        result = inspect_entry(self.store, provider, provider_id,
                               atlas=self.engine.identity.atlas)
        self._inspect_output.set_markup(self._render_inspection(result))
        url = adapters.web_url(provider, result.media_type, provider_id)
        self._inspect_open.set_sensitive(url is not None)
        self._inspect_url = url

    def s_inspect_open(self):
        if self._inspect_url:
            Gtk.show_uri_on_window(self, self._inspect_url,
                                   Gtk.get_current_event_time())

    @staticmethod
    def _mono(text):
        """Wrap an id/technical value in monospace (Pango <tt>)."""
        return '<tt>%s</tt>' % text

    def _render_inspection(self, r):
        esc = GLib.markup_escape_text
        p = ['<b>%s id %s</b>\n' % (r.provider.capitalize(),
                                    self._mono(esc(str(r.provider_id))))]
        if not r.found:
            p.append('\n%s' % esc(r.note))
            issue = r.identity_issue
            if issue and issue.get('candidates'):
                p.append('\n\n<b>Candidates on file:</b>')
                for c in issue['candidates']:
                    provs = ', '.join(self._mono(esc('%s:%s' % kv)) for kv in
                                      (c.get('providers') or {}).items()) \
                        or 'no providers yet'
                    p.append('\n  - %s%s: %s (<i>%s</i>)' % (
                        esc(c.get('title') or '?'),
                        ' (%s)' % c['year'] if c.get('year') else '',
                        provs, esc(c.get('via', ''))))
            if r.atlas_hint:
                p.append('\n\n<b>%s says:</b> %s'
                         % (esc(atlas_label(r)),
                            self._mono(esc(', '.join(
                                '%s=%s' % kv
                                for kv in r.atlas_hint.items())))))
            return ''.join(p)

        p.append('\n<b>%s</b>%s%s' % (
            esc(r.title or '?'), ' (%s)' % r.year if r.year else '',
            ', pinned provider-only (%s)' % r.provider_only.capitalize()
            if r.provider_only else ''))
        if r.aliases:
            p.append('\nAlso known as: %s'
                     % esc(', '.join(a for a in r.aliases if a != r.title)))
        p.append('\nInternal id: %s' % self._mono(esc(str(r.uuid))))
        p.append('\n\n<b>Mapped providers</b>')
        for m in r.mappings:
            p.append('\n  %s: %s  (%s, via %s)' % (
                m.provider.capitalize(), self._mono(esc(str(m.provider_id))),
                'confirmed' if m.confirmed else 'auto', esc(m.via or '-')))
        providers = sorted({prov for row in r.fields for prov in row.per_provider})
        rows = [row for row in r.fields
                if row.per_provider or row.local not in (None, [], 0)]
        if rows and providers:
            p.append('\n\n<b>Field data</b> (policy; local | %s)'
                     % ' | '.join(prov.capitalize() for prov in providers))
            for row in rows:
                cells = [self._fmt_value(row.field, row.local)]
                for prov in providers:
                    pv = row.per_provider.get(prov)
                    cells.append('-' if pv is None else '%s / %s' % (
                        self._fmt_value(row.field, pv['remote']),
                        self._fmt_value(row.field, pv['base'])))
                p.append('\n  %s (<i>%s</i>): %s' % (
                    _FIELD_LABELS.get(row.field, row.field),
                    esc(present.policy_label(row.policy))
                    if row.policy else '-',
                    self._mono(esc(' | '.join(cells)))))
        return ''.join(p)

    # -- plumbing ------------------------------------------------------

    def _run(self, fn, callback, busy_text, *args, forward_progress=False,
             **kwargs):
        if self._thread is not None and self._thread.is_alive():
            self._status('Another sync operation is still running.')
            return
        self._status(busy_text)
        self.fetch_button.set_sensitive(False)
        if forward_progress:
            kwargs['progress'] = lambda done, total, msg: GLib.idle_add(
                self._on_apply_progress, done, total, msg)

        def worker():
            try:
                result, error = fn(*args, **kwargs), None
            except Exception as e:  # incl. bugs -- surfaced, never swallowed
                traceback.print_exc()
                result, error = None, e
            GLib.idle_add(self._done, callback, result, error)

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def _done(self, callback, result, error):
        if self._closed:
            # This idle fires AFTER the worker's engine call returned
            # (queueing it is the worker's last statement), so nothing
            # touches the store anymore -- close it unconditionally.
            # Gating on _thread.is_alive() here would race: the main
            # loop can run this callback while the worker is still
            # unwinding, and with no later retry the connection (and
            # its WAL files) would leak for the life of the process.
            self._close_store()
            return False
        self.fetch_button.set_sensitive(True)
        try:
            callback(result, error)
        except Exception as e:
            traceback.print_exc()
            self._status('Internal error: %s' % e)
        return False

    def _status(self, text):
        self._status_label.set_text(text)

    def _close_store(self):
        if not self._store_closed:
            self._store_closed = True
            self.store.close()

    def _maybe_close_store(self):
        # Close the store exactly once, only when no worker is still using
        # it -- closing it under a running fetch/apply would crash. (The
        # running worker's own _done closes it instead; see _done.)
        if self._store_closed:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._close_store()

    def _on_destroy(self, *_a):
        if self._closed:
            return
        self._closed = True
        # Interrupt any in-flight apply so a rate-limit wait doesn't keep
        # the worker (and the store) alive for long.
        self._cancel.set()
        self._maybe_close_store()
