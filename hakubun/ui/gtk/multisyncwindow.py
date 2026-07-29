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

"""GTK multi-provider Sync window (docs/multisync.md 8) -- the GTK twin
of hakubun.ui.qt.syncwindow, over the same, UI-agnostic SyncEngine.

Four tabs: the sync Preview (changes / conflicts / apply), the field-
ownership matrix ("Where should hakubun sync to?"), the identity-conflict
workflow, and an identity Inspector. Two deliberate simplifications from
the Qt window: the Preview shows one changes tree grouped by show rather
than a tab per field, and the Inspector renders text/markup rather than
an HTML table -- the workflow and the engine calls are identical.
"""

import threading
import traceback

from gi.repository import GLib, GObject, Gtk, Pango

from hakubun import utils
from hakubun.sync import adapters, present
from hakubun.sync.models import (FieldPolicy, NormalizedEntry, SyncCancelled,
                                 USER_FIELDS)
from hakubun.sync.present import FIELD_LABELS as _FIELD_LABELS
from hakubun.sync.present import MODES as _MODES
from hakubun.sync.store import SyncStore

_PULL_COLOR = '#4caf50'
_PUSH_COLOR = '#42a5f5'
_CONFLICT_COLOR = '#e5a400'
# First-sync overwrites: planned and previewed, but unticked.
_FIRST_SYNC_COLOR = _CONFLICT_COLOR

# ShowListStore-style column indices for the Preview changes tree.
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
        # The signed-in account is the working tree: its changes fold in
        # as local intent. Kept as an attribute (not just
        # engine.primary, which stays None when the account has no
        # adapter) so the caller can tell whether a cached window still
        # belongs to the loaded account.
        self.active_api = active_api
        if active_api and active_api in self.engine.adapters:
            self.engine.primary = active_api

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(8)
        self._notebook = Gtk.Notebook()
        self._notebook.append_page(self._build_preview_tab(),
                                   Gtk.Label(label='Preview'))
        self._notebook.append_page(self._build_ownership_tab(),
                                   Gtk.Label(label='Ownership'))
        self._identity_label = Gtk.Label(label='Identity')
        self._notebook.append_page(self._build_identity_tab(),
                                   self._identity_label)
        self._notebook.append_page(self._build_inspector_tab(),
                                   Gtk.Label(label='Inspector'))
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

    # -- Preview -------------------------------------------------------

    def _build_preview_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(6)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        # The signed-in tracker's icon -- an at-a-glance answer to "whose
        # state feeds 'local' here?".
        if self.engine.primary:
            lib_info = utils.available_libs.get(self.engine.primary)
            if lib_info:
                icon = Gtk.Image.new_from_file(lib_info[1])
                icon.set_tooltip_text('Signed into %s -- its changes count '
                                      'as your local edits.' % lib_info[0])
                bar.pack_start(icon, False, False, 0)
        bar.pack_start(Gtk.Label(label='Mode:'), False, False, 0)
        self.mode_combo = Gtk.ComboBoxText()
        for _mode, label in _MODES:
            self.mode_combo.append_text(label)
        self.mode_combo.set_active(0)
        self.mode_combo.connect('changed', self._update_mode_context)
        bar.pack_start(self.mode_combo, False, False, 0)

        self.fetch_button = Gtk.Button(label='Fetch & Plan')
        self.fetch_button.connect('clicked', lambda *_a: self.s_fetch())
        self.apply_button = Gtk.Button(label='Apply')
        self.apply_button.set_sensitive(False)
        self.apply_button.connect('clicked', lambda *_a: self.s_apply())
        self.cancel_button = Gtk.Button(label='Cancel')
        self.cancel_button.set_tooltip_text(
            'Stop the sync after the current push. Whatever was already '
            'pushed stays; the rest re-plans.')
        self.cancel_button.set_no_show_all(True)
        self.cancel_button.connect('clicked', lambda *_a: self.s_cancel_apply())
        self.reset_button = Gtk.Button(label='Reset database...')
        self.reset_button.set_tooltip_text(
            'Wipe the sync database (identities, mappings, history, '
            'ownership) and start clean. Your provider lists are never '
            'touched; the next Fetch re-derives everything.')
        self.reset_button.connect('clicked', lambda *_a: self.s_reset())
        for b in (self.fetch_button, self.apply_button, self.cancel_button,
                  self.reset_button):
            bar.pack_end(b, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.mode_context = Gtk.Label(xalign=0, wrap=True)
        page.pack_start(self.mode_context, False, False, 0)
        legend = Gtk.Label(xalign=0)
        legend.set_markup(
            '<span foreground="%s">up push to a site</span>  '
            '<span foreground="%s">down pull into Hakubun</span>  '
            '-- uncheck anything to skip it' % (_PUSH_COLOR, _PULL_COLOR))
        page.pack_start(legend, False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        # Left: the bulk transaction plan, grouped by show.
        self._changes_store = Gtk.TreeStore(bool, bool, str, str,
                                            GObject.TYPE_PYOBJECT)
        self._changes_view = Gtk.TreeView(model=self._changes_store)
        self._changes_view.set_headers_visible(False)
        toggle = Gtk.CellRendererToggle()
        toggle.connect('toggled', self._on_change_toggled)
        col_toggle = Gtk.TreeViewColumn('', toggle, active=_C_ACTIVE,
                                        inconsistent=_C_INCONSISTENT)
        self._changes_view.append_column(col_toggle)
        text = Gtk.CellRendererText()
        col_text = Gtk.TreeViewColumn('Change', text, text=_C_LABEL,
                                      foreground=_C_COLOR)
        self._changes_view.append_column(col_text)
        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                               Gtk.PolicyType.AUTOMATIC)
        left_scroll.add(self._changes_view)
        paned.pack1(left_scroll, True, True)

        # Right: what needs a human (conflicts).
        decisions_frame = Gtk.Frame(label='Needs your decision')
        self._decisions_scroll = Gtk.ScrolledWindow()
        self._decisions_scroll.set_policy(Gtk.PolicyType.NEVER,
                                          Gtk.PolicyType.AUTOMATIC)
        self._decisions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                      spacing=8)
        self._decisions_box.set_border_width(6)
        self._decisions_scroll.add(self._decisions_box)
        decisions_frame.add(self._decisions_scroll)
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

        self.preview_summary = Gtk.Label(xalign=0, wrap=True)
        page.pack_start(self.preview_summary, False, False, 0)
        self._update_mode_context()
        return page

    def _current_mode(self):
        return _MODES[max(self.mode_combo.get_active(), 0)][0]

    def set_mode(self, mode):
        """Select a SyncMode in the mode combo (used by the main
        window's headless Sync-button flow to apply the configured
        multisync_mode before planning)."""
        for i, (m, _label) in enumerate(_MODES):
            if m is mode:
                self.mode_combo.set_active(i)
                return

    def is_busy(self):
        """True while a fetch/plan/apply worker is running."""
        return self._thread is not None and self._thread.is_alive()

    def _update_mode_context(self, *_a):
        self.mode_context.set_markup(
            present.mode_context(self._current_mode(), self.engine.primary))

    def _fmt_value(self, field, value):
        return present.fmt_value(field, value)

    def _fmt_target_value(self, field, value, target):
        return present.fmt_target_value(self.engine.adapters, field,
                                        value, target)

    def _change_label(self, change):
        direction, text = present.change_line(self.engine.adapters, change,
                                              self.engine.primary)
        arrow, color = (('v', _PULL_COLOR) if direction == 'pull'
                        else ('^', _PUSH_COLOR))
        if change.first_sync:
            color = _FIRST_SYNC_COLOR
        return ('%s %s' % (arrow, text), color)

    def _populate_changes(self, changes):
        self._changes_store.clear()
        groups = {}
        for change in changes:
            groups.setdefault(change.uuid, [change.title, []])[1].append(change)
        for _uid, (title, group_changes) in sorted(
                groups.items(), key=lambda kv: kv[1][0].casefold()):
            # Honour the plan's own selection: a first-sync overwrite is
            # planned unticked, and ticking it must be a deliberate act.
            states = {c.selected for c in group_changes}
            parent = self._changes_store.append(
                None, [states == {True}, len(states) > 1,
                       '%s    --  %d change(s)'
                       % (title, len(group_changes)), None, None])
            for change in group_changes:
                label, color = self._change_label(change)
                self._changes_store.append(
                    parent, [change.selected, False, label, color, change])

    def _on_change_toggled(self, _renderer, path):
        it = self._changes_store.get_iter(path)
        change = self._changes_store.get_value(it, _C_CHANGE)
        new_state = not self._changes_store.get_value(it, _C_ACTIVE)
        self._changes_store.set_value(it, _C_ACTIVE, new_state)
        if change is None:
            # A show-group header: cascade to its children.
            child = self._changes_store.iter_children(it)
            while child is not None:
                self._changes_store.set_value(child, _C_ACTIVE, new_state)
                child_change = self._changes_store.get_value(child, _C_CHANGE)
                if child_change is not None:
                    child_change.selected = new_state
                child = self._changes_store.iter_next(child)
            self._changes_store.set_value(it, _C_INCONSISTENT, False)
        else:
            change.selected = new_state
            self._sync_group_state(self._changes_store.iter_parent(it))

    def _sync_group_state(self, parent):
        if parent is None:
            return
        child = self._changes_store.iter_children(parent)
        states = []
        while child is not None:
            states.append(self._changes_store.get_value(child, _C_ACTIVE))
            child = self._changes_store.iter_next(child)
        all_on, any_on = all(states), any(states)
        self._changes_store.set_value(parent, _C_ACTIVE, all_on)
        self._changes_store.set_value(parent, _C_INCONSISTENT,
                                      any_on and not all_on)

    # -- conflicts (decisions pane) -----------------------------------

    def _local_label(self):
        return present.local_label(self.engine.primary)

    def _conflict_why(self, conflict):
        return present.conflict_why(conflict, self.engine.primary)

    def _decision_card(self, conflict):
        frame = Gtk.Frame(label='%s -- %s' % (
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
                label='Nothing needs your attention right now. Conflicts '
                '(a field changed in more than one place since the last '
                'sync) show up here after Fetch & Plan.', xalign=0, wrap=True)
            self._decisions_box.pack_start(placeholder, False, False, 0)
        self._decisions_box.show_all()

    def _resolve_inline(self, conflict, source):
        self.engine.resolve_conflict(conflict, source)
        self._status('Resolved %s for %s -- replanning...' % (
            _FIELD_LABELS.get(conflict.field, conflict.field), conflict.title))
        self._run(self.engine.plan, self.r_planned, 'Planning...',
                  self._current_mode())

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
        self._status('Resolved %s for %s -- replanning...' % (
            _FIELD_LABELS.get(conflict.field, conflict.field), conflict.title))
        self._run(self.engine.plan, self.r_planned, 'Planning...',
                  self._current_mode())

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
        plan = self.engine.plan(self._current_mode(),
                                should_cancel=self._cancel.is_set)
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
        self._populate_changes(plan.changes)
        self._changes_view.expand_all()
        self._set_decisions(plan.conflicts)
        self.apply_progress.hide()
        self.apply_button.set_sensitive(bool(plan.changes))
        groups = {c.uuid for c in plan.changes}
        if plan.clean:
            self.preview_summary.set_text('Everything is in sync.')
        else:
            self.preview_summary.set_text(
                '%d show(s): %d change(s), %d conflict(s)'
                % (len(groups), len(plan.changes), len(plan.conflicts)))
        first_sync = sum(1 for c in plan.changes if c.first_sync)
        if first_sync:
            self.preview_summary.set_text(
                '%s  --  %d first-sync overwrite(s) left unticked. %s'
                % (self.preview_summary.get_text(), first_sync,
                   present.FIRST_SYNC_HELP))
        parts = ['%d change(s)' % len(plan.changes),
                 '%d conflict(s)' % len(plan.conflicts),
                 '%d identity issue(s)' % len(plan.identity)]
        if plan.errors:
            parts.append('errors: %s' % ', '.join('%s (%s)' % kv
                                                  for kv in plan.errors.items()))
        self._status('Planned: ' + ', '.join(parts))
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
        self._set_log('Applying %d change(s)...' % selected)
        self._apply_log_scroll.show()
        self.apply_progress.set_fraction(0)
        self.apply_progress.set_text('Applying...')
        self.apply_progress.show()
        self._run(self.engine.apply, self.r_applied, 'Applying changes...',
                  plan, should_cancel=self._cancel.is_set,
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
        verb = 'Cancelled after' if result.get('cancelled') else 'Applied:'
        text = '%s %d local change(s), %d push(es)' % (
            verb, result['local'], result['pushed'])
        if result['errors']:
            text += ' -- failed: %s (their changes stay planned)' % ', '.join(
                '%s (%s)' % kv for kv in result['errors'].items())
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
            'ownership choices are deleted and re-derived on the next fetch. '
            'Your provider lists are NOT touched.')
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.YES:
            return
        if self.is_busy():
            # Resetting under a running fetch/apply would drop the
            # tables out from under it (and block the main loop on the
            # store lock). Ask it to stop; the user clicks again.
            self._cancel.set()
            self._status('Stopping the running operation first -- press '
                         'Reset again once it has.')
            return
        self.store.reset()
        self._plan = None
        self._changes_store.clear()
        self._set_decisions([])
        self.apply_progress.hide()
        self._set_log('')
        self._apply_log_scroll.hide()
        self.preview_summary.set_text('')
        self.apply_button.set_sensitive(False)
        self._refresh_identity()
        self._status('Sync database reset -- run Fetch & Plan to re-derive it.')

    def _set_log(self, text):
        self.apply_log.get_buffer().set_text(text)

    def _append_log(self, text):
        buf = self.apply_log.get_buffer()
        buf.insert(buf.get_end_iter(), ('' if buf.get_char_count() == 0
                                        else '\n') + text)

    # -- Ownership -----------------------------------------------------

    def _build_ownership_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(8)
        head = Gtk.Label(xalign=0)
        head.set_markup('<b>Where should hakubun sync to?</b>')
        page.pack_start(head, False, False, 0)
        page.pack_start(Gtk.Label(
            label='Ownership decides who wins when a field changed in two '
            'places. Single-sided changes always propagate; "Individual" '
            'keeps a field per-site; "Ask" asks every time. Scores pushed to '
            'coarser sites are rounded to their scale.', xalign=0, wrap=True),
            False, False, 0)
        if self.engine.primary:
            active = Gtk.Label(xalign=0, wrap=True)
            active.set_markup('You are signed into <b>%s</b>: changes you make '
                              'in the app belong to it and count as your local '
                              'edits.' % self.engine.primary.capitalize())
            page.pack_start(active, False, False, 0)

        providers = list(self.engine.adapters)
        columns = ([('local', 'Local')]
                   + [('provider:%s' % p, p.capitalize()
                       + (' (active)' if p == self.engine.primary else ''))
                      for p in providers]
                   + [('merge', 'Merge'), ('individual', 'Individual'),
                      ('ask', 'Ask')])
        grid = Gtk.Grid(column_spacing=18, row_spacing=10)
        grid.set_border_width(6)
        for col, (_policy, label) in enumerate(columns, start=1):
            header = Gtk.Label()
            header.set_markup('<b>%s</b>' % label)
            grid.attach(header, col, 0, 1, 1)
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
        page.pack_start(grid, False, False, 0)
        return page

    def _on_ownership_toggled(self, radio, field, policy_text):
        if not radio.get_active():
            return
        policy = FieldPolicy.parse(policy_text)
        self.store.set_ownership(field, policy)
        self._status('Ownership: %s -> %s'
                     % (_FIELD_LABELS.get(field, field), policy))

    # -- Identity ------------------------------------------------------

    def _build_identity_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(8)
        page.pack_start(Gtk.Label(
            label='Unresolved identities -- entries with ambiguous matches '
            'only. Exact ID links and single exact-title matches are linked '
            'automatically and never appear here.', xalign=0, wrap=True),
            False, False, 0)

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
            self._rb_confirm, 'Search manually -- find it on another provider')
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
            self._rb_confirm, 'Keep provider-only -- do not sync elsewhere')
        self._rb_defer = Gtk.RadioButton.new_with_label_from_widget(
            self._rb_confirm, 'Create mappings later -- keep watching')
        self._rb_ignore = Gtk.RadioButton.new_with_label_from_widget(
            self._rb_confirm, 'Ignore this title -- never ask again')
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
        self._identity_label.set_text(
            'Identity (%d)' % len(self._identity_store))

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
            '<b>%s</b>%s -- %s entry %s. %s' % (
                GLib.markup_escape_text(self._display_title(
                    issue['title'], info.get('aliases'))),
                year, issue['provider'], issue['provider_id'], why))
        self._candidate_combo.remove_all()
        self._candidate_entries = candidates
        for cand in candidates:
            provs = ', '.join('on %s (%s)' % kv for kv in sorted(
                cand.get('providers', {}).items()))
            self._candidate_combo.append_text('%s%s -- %s -- %s' % (
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
            'Keep %s-only -- do not sync this entry elsewhere'
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

    # -- Inspector -----------------------------------------------------

    def _build_inspector_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_border_width(8)
        page.pack_start(Gtk.Label(
            label='Identity inspector -- punch in one entry by its provider '
            'id and see how multisync resolved it: what it maps to on your '
            'other providers, how each link was made, and its raw data.',
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

    def _render_inspection(self, r):
        esc = GLib.markup_escape_text
        p = ['<b>%s id %s</b>\n' % (r.provider.capitalize(), esc(str(r.provider_id)))]
        if not r.found:
            p.append('\n%s' % esc(r.note))
            issue = r.identity_issue
            if issue and issue.get('candidates'):
                p.append('\n\n<b>Candidates on file:</b>')
                for c in issue['candidates']:
                    provs = ', '.join('%s:%s' % kv for kv in
                                      (c.get('providers') or {}).items()) \
                        or 'no providers yet'
                    p.append('\n  - %s%s -- %s -- <i>%s</i>' % (
                        esc(c.get('title') or '?'),
                        ' (%s)' % c['year'] if c.get('year') else '',
                        esc(provs), esc(c.get('via', ''))))
            if r.atlas_hint:
                p.append('\n\n<b>anime-relations atlas says:</b> %s' % esc(
                    ', '.join('%s=%s' % kv for kv in r.atlas_hint.items())))
            return ''.join(p)

        p.append('\n<b>%s</b>%s%s' % (
            esc(r.title or '?'), ' (%s)' % r.year if r.year else '',
            ' -- pinned provider-only (%s)' % r.provider_only.capitalize()
            if r.provider_only else ''))
        if r.aliases:
            p.append('\nAlso known as: %s'
                     % esc(', '.join(a for a in r.aliases if a != r.title)))
        p.append('\nInternal id: <tt>%s</tt>' % esc(str(r.uuid)))
        p.append('\n\n<b>Mapped providers</b>')
        for m in r.mappings:
            p.append('\n  %s: %s  (%s, via %s)' % (
                m.provider.capitalize(), esc(str(m.provider_id)),
                'confirmed' if m.confirmed else 'auto', esc(m.via or '-')))
        providers = sorted({prov for row in r.fields for prov in row.per_provider})
        rows = [row for row in r.fields
                if row.per_provider or row.local not in (None, [], 0)]
        if rows and providers:
            p.append('\n\n<b>Field data</b> (local | %s)'
                     % ' | '.join(prov.capitalize() for prov in providers))
            for row in rows:
                cells = [self._fmt_value(row.field, row.local)]
                for prov in providers:
                    pv = row.per_provider.get(prov)
                    cells.append('-' if pv is None else '%s / %s' % (
                        self._fmt_value(row.field, pv['remote']),
                        self._fmt_value(row.field, pv['base'])))
                p.append('\n  %s: %s' % (
                    _FIELD_LABELS.get(row.field, row.field),
                    esc(' | '.join(cells))))
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
