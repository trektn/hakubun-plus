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

"""Multi-provider Sync window (docs/multisync.md §12).

Organized around the user's questions, not the engine's architecture:

* Sync -- what will change (Changes / New entries side by side with
  the Decisions pane), how much needs the user, and the Sync button.
* Mirror -- make the TRACKERS agree according to Ownership: tracker
  membership, entries to add, entries to remove, fields to update.
  A deliberate, potentially large operation, kept separate from
  ordinary incremental Sync and gated behind an explicit confirmation
  with independent add/remove approval.
* Configuration -- one friendly rule picker per field ("Keep furthest
  progress", "Ask me when they differ", ...).
* Identity -- titles that need matching across providers.
* Advanced -- the full policy matrix, the identity Inspector and the
  destructive Reset, deliberately out of the main workflow.

'Hakubun' is the app's own reconciled state (the engine's 'local');
it is always labeled as the app, never as a provider.
"""

import threading
import traceback

from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog,
                             QGridLayout,
                             QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QRadioButton,
                             QScrollArea, QSplitter, QTabWidget, QTextBrowser,
                             QToolButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from hakubun import utils
from hakubun.sync import adapters, present
from hakubun.sync.models import (FieldPolicy, NormalizedEntry,
                                 SyncCancelled, SyncOperation, USER_FIELDS)
from hakubun.sync.inspect import atlas_label as _atlas_label
from hakubun.sync.present import FIELD_LABELS as _FIELD_LABELS
from hakubun.sync.store import SyncStore


class _Task(QtCore.QThread):
    """Runs one engine call off the GUI thread."""
    done = QtCore.pyqtSignal(object, object)   # result, error
    progressed = QtCore.pyqtSignal(int, int, str)   # done, total, message

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._call = (fn, args, kwargs)

    def run(self):
        fn, args, kwargs = self._call
        try:
            self.done.emit(fn(*args, **kwargs), None)
        except Exception as e:
            self.done.emit(None, e)


class SyncWindow(QDialog):
    def __init__(self, parent, accountman, engine=None, active_api=None,
                media_type='anime'):
        super().__init__(parent)
        self.media_type = media_type
        self.setWindowTitle('Multi-provider Sync (%s)'
                           % media_type.capitalize())
        self.resize(760, 560)
        self._task = None
        self._plan = None
        self._closed = False
        self._cancel = threading.Event()
        # Guards the two policy views (Configuration combos, Advanced
        # matrix) against feedback loops while one refreshes the other.
        self._policy_updating = False

        if engine is not None:
            # Injection seam for tests: a prebuilt SyncEngine (fake
            # adapters, in-memory store).
            self.store = engine.store
            self.engine, self._adapter_errors = engine, []
        else:
            # A separate database PER media type, never one shared
            # file: MAL/AniList/Kitsu each use independent numeric id
            # spaces for anime vs manga (their id 1 for anime and id 1
            # for manga are unrelated works), so a single mappings
            # table keyed only on (provider, provider_id) would
            # collide between them the moment an account switches
            # media type -- exactly the breakage this separates away,
            # rather than only detecting it after the fact. The path
            # scheme lives in uibridge.store_path, shared with the
            # list overlay -- one definition, or they could drift onto
            # different files.
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

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_sync_tab(), 'Sync')
        self.tabs.addTab(self._build_mirror_tab(), 'Mirror')
        self.tabs.addTab(self._build_config_tab(), 'Configuration')
        self._identity_tab = self._build_identity_tab()
        self.tabs.addTab(self._identity_tab, 'Identity')
        self.tabs.addTab(self._build_advanced_tab(), 'Advanced')
        layout.addWidget(self.tabs)
        self.status_label = QLabel()
        layout.addWidget(self.status_label)

        if self._adapter_errors:
            self._status('Some accounts could not be loaded: %s'
                         % '; '.join(self._adapter_errors))
        elif not self.engine.adapters:
            self._status('No %s accounts configured (check Settings if '
                        'you have accounts set up for the other media '
                        'type).' % media_type)
        self._refresh_identity()

    # -- Sync ----------------------------------------------------------

    def _build_sync_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        # The signed-in tracker's icon, same imagery the main window
        # uses (utils.available_libs). Signed-in is shown as a fact
        # about accounts, never as what 'Hakubun' means.
        if self.engine.primary:
            lib_info = utils.available_libs.get(self.engine.primary)
            if lib_info:
                icon_label = QLabel()
                icon_label.setPixmap(QtGui.QPixmap(lib_info[1]).scaled(
                    24, 24, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation))
                icon_label.setToolTip(
                    'Signed into %s. Hakubun keeps its own state; '
                    'the signed-in site gets no special say in the '
                    'sync.' % lib_info[0])
                bar.addWidget(icon_label)
        # The headline: how much will change, how much needs a human,
        # what would create entries -- the first thing to read.
        self.summary_label = QLabel('Fetch changes to see what would '
                                    'sync.')
        bar.addWidget(self.summary_label)
        bar.addStretch()
        self.fetch_button = QPushButton('Fetch changes')
        self.fetch_button.setToolTip(
            'Download each site\'s list and work out what would sync. '
            'Nothing is written anywhere until you press Sync.')
        self.fetch_button.clicked.connect(self.s_fetch)
        self.apply_button = QPushButton('Sync selected')
        self.apply_button.setToolTip(
            'Carry out every ticked change: update Hakubun and push '
            'to the sites.')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.s_apply)
        self.cancel_button = QPushButton('Cancel')
        self.cancel_button.setToolTip(
            'Stop the sync after the current push. Whatever was already '
            'pushed stays; the rest is offered again next time.')
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.s_cancel_apply)
        bar.addWidget(self.fetch_button)
        bar.addWidget(self.apply_button)
        bar.addWidget(self.cancel_button)
        layout.addLayout(bar)

        # How the plan is decided (field rules), so the list below
        # never reads as magic.
        plan_context = QLabel(present.plan_context())
        plan_context.setWordWrap(True)
        layout.addWidget(plan_context)

        legend = QLabel(
            '<span style="color:#42a5f5">⬆ updates a site</span> · '
            '<span style="color:#4caf50">⬇ updates Hakubun</span> · '
            'uncheck anything to skip it')
        layout.addWidget(legend)

        # Left: the bulk transaction plan. Right: what needs a human --
        # a separate pane (not a panel squeezed underneath) so a long
        # plan and a handful of decisions don't fight for the same
        # vertical space. Splitter so either side can be resized.
        splitter = QSplitter(QtCore.Qt.Orientation.Horizontal)

        # One tree per category (All, plus one per field that actually
        # has a change this plan -- Score/Watched Episodes/etc appear
        # only when relevant, see r_planned) so a long mixed plan can
        # be narrowed down instead of scrolled through as one list.
        self.preview_tabs = QTabWidget()
        self._all_changes_tree = None   # built by the first r_planned
        self._new_entries_tree = None   # the '__new__' category's tree
        self._change_items = {}   # id(SyncOperation) -> [item in each tab
                                  # showing it] -- keeps checkbox state
                                  # in sync across tabs (see
                                  # _on_change_item_toggled): the SAME
                                  # change appears in 'All' and in
                                  # exactly one category tab.
        # Incremental-replan bookkeeping (see _update_category_tabs):
        # which tab tree holds which category, what set of change
        # objects it last showed (by identity), and the category order
        # -- None means "never rendered", forcing a full rebuild once.
        self._category_trees = {}
        self._category_ids = {}
        self._category_order = None
        # (uuid, field, target) -> the SyncOperation last shown for
        # that slot, so a replan that reproduces an identical change can
        # reuse the SAME object and keep its `.selected` -- otherwise
        # every replan (e.g. after resolving one conflict) silently
        # discarded every other tick in the window.
        self._prev_change_index = {}
        splitter.addWidget(self.preview_tabs)

        self.decisions_box = QGroupBox('Decisions')
        decisions_outer = QVBoxLayout(self.decisions_box)
        self.decisions_scroll = QScrollArea()
        self.decisions_scroll.setWidgetResizable(True)
        holder = QWidget()
        self.decisions_layout = QVBoxLayout(holder)
        self.decisions_layout.addStretch()
        self.decisions_scroll.setWidget(holder)
        decisions_outer.addWidget(self.decisions_scroll)
        splitter.addWidget(self.decisions_box)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([620, 380])
        layout.addWidget(splitter, 1)
        self._set_decisions([])   # placeholder until the first plan

        # Revealed during Apply: a real progress bar (one tick per
        # network push batch, since that's what actually takes time)
        # over a running log of what was pushed where.
        self.apply_progress = QProgressBar()
        self.apply_progress.setVisible(False)
        self.apply_log = QPlainTextEdit()
        self.apply_log.setReadOnly(True)
        self.apply_log.setMaximumHeight(110)
        self.apply_log.setVisible(False)
        layout.addWidget(self.apply_progress)
        layout.addWidget(self.apply_log)
        return page

    # -- preview rendering --------------------------------------------

    _PULL_COLOR = QtGui.QColor('#4caf50')
    _PUSH_COLOR = QtGui.QColor('#42a5f5')
    _CONFLICT_COLOR = QtGui.QColor('#ff9800')
    # New-entry creates: planned, previewed, but unticked -- same
    # "needs a human" hue as a conflict, because that is what it is.
    _CREATE_COLOR = QtGui.QColor('#ff9800')

    def _fmt_value(self, field, value):
        return present.fmt_value(field, value)

    def _fmt_target_value(self, field, value, target):
        return present.fmt_target_value(self.engine.adapters, field,
                                        value, target)

    def _change_item(self, change):
        direction, text = present.change_line(self.engine.adapters, change,
                                              self.engine.primary)
        arrow, color = (('⬇', self._PULL_COLOR) if direction == 'pull'
                        else ('⬆', self._PUSH_COLOR))
        item = QTreeWidgetItem(['%s %s' % (arrow, text)])
        item.setForeground(0, self._CREATE_COLOR
                           if change.creates_entry else color)
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        # Honour the plan's own selection: a new-entry create is
        # planned unticked, and ticking it must be a deliberate act.
        item.setCheckState(0, QtCore.Qt.CheckState.Checked if change.selected
                           else QtCore.Qt.CheckState.Unchecked)
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, change)
        return item

    def _populate_preview_tree(self, tree, changes):
        """Fill one category tab's tree: one box per show, its changes
        as directional child rows -- same shape 'All' and every
        per-field tab share, just over a different subset of changes.
        Item registration into self._change_items (so a checkbox toggle
        can be mirrored onto this same change's item in every other tab
        that also shows it) happens in one pass over all tabs after
        r_planned decides which trees to rebuild, not here -- rebuilding
        it per-tree here would double-register tabs left untouched by
        the incremental path in _update_category_tabs."""
        tree.clear()
        groups = {}
        for change in changes:
            groups.setdefault(change.uuid,
                              [change.title, []])[1].append(change)
        bold = QtGui.QFont()
        bold.setBold(True)
        for uid, (show_title, group_changes) in sorted(
                groups.items(), key=lambda kv: kv[1][0].casefold()):
            group = QTreeWidgetItem(['%s (%d change(s))'
                                     % (show_title, len(group_changes))])
            group.setFont(0, bold)
            group.setFlags(group.flags()
                           | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                           | QtCore.Qt.ItemFlag.ItemIsAutoTristate)
            for change in group_changes:
                item = self._change_item(change)
                group.addChild(item)
            # Set AFTER the children so Qt's autotristate derives the
            # header from them (a group of unticked create rows must
            # not show as fully checked).
            states = {c.selected for c in group_changes}
            group.setCheckState(0, QtCore.Qt.CheckState.Checked
                                if states == {True} else
                                QtCore.Qt.CheckState.Unchecked
                                if states == {False} else
                                QtCore.Qt.CheckState.PartiallyChecked)
            tree.addTopLevelItem(group)
        tree.expandAll()

    def _make_preview_tree(self):
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.itemChanged.connect(self._on_change_item_toggled)
        tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(
            lambda pos, t=tree: self._change_context_menu(t, pos))
        return tree

    def _change_context_menu(self, tree, pos):
        """Right-click on a new-entry row: durably decline the creation
        (engine.decline_create), as opposed to leaving it unticked --
        which only skips it for this plan."""
        item = tree.itemAt(pos)
        if item is None:
            return
        change = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not isinstance(change, SyncOperation) \
                or not change.creates_entry:
            return
        menu = QMenu(self)
        action = menu.addAction(
            'Never create this on %s' % present.label(change.target))
        action.triggered.connect(
            lambda _checked=False, c=change: self._decline_create(c))
        menu.exec(tree.viewport().mapToGlobal(pos))

    def _decline_create(self, change):
        self.engine.decline_create(change.uuid, change.target)
        self._status('%s will not be offered for creation on %s '
                     'again.' % (change.title,
                                 present.label(change.target)))
        self._replan()

    def _on_change_item_toggled(self, item, column):
        if column != 0:
            return
        change = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not isinstance(change, SyncOperation):
            return   # a show-group header; Qt's own tristate handles it
        change.selected = (item.checkState(0)
                          == QtCore.Qt.CheckState.Checked)
        for twin in self._change_items.get(id(change), []):
            if twin is not item and twin.checkState(0) != item.checkState(0):
                twin.setCheckState(0, item.checkState(0))

    def _local_label(self):
        return present.local_label(self.engine.primary)

    def _conflict_why(self, conflict):
        return present.conflict_why(conflict, self.engine.primary)

    def _decision_card(self, conflict):
        box = QGroupBox('%s: %s' % (
            conflict.title,
            _FIELD_LABELS.get(conflict.field, conflict.field)))
        card = QVBoxLayout(box)
        why = QLabel(self._conflict_why(conflict))
        why.setWordWrap(True)
        card.addWidget(why)
        row = QHBoxLayout()
        for source, value in sorted(conflict.values.items()):
            shown = self._fmt_value(conflict.field, value)
            if source == 'local':
                text = present.conflict_choice_label(conflict, source,
                                                     self.engine.primary)
            elif conflict.structural:
                # The provider's number is in ITS OWN episode
                # structure -- adopting it raw would record a
                # different amount as watched, so it is information
                # here, never a button (engine.resolve_conflict
                # refuses it too).
                info = QLabel('%s is at %s (its own structure)'
                              % (source.capitalize(), shown))
                row.addWidget(info)
                continue
            else:
                text = present.conflict_choice_label(conflict, source,
                                                     self.engine.primary)
            button = QPushButton(text)
            button.clicked.connect(
                lambda _checked=False, c=conflict, s=source:
                self._resolve_inline(c, s))
            row.addWidget(button)
        if conflict.structural:
            button = QPushButton('Set episodes…')
            button.clicked.connect(
                lambda _checked=False, c=conflict:
                self._resolve_structural(c))
            row.addWidget(button)
        row.addStretch()
        card.addLayout(row)
        return box

    def _clear_decisions(self):
        while self.decisions_layout.count() > 1:
            item = self.decisions_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _set_decisions(self, conflicts):
        """Fill the right-hand panel; always visible (a placeholder
        when empty) so the split layout doesn't jump around."""
        self._clear_decisions()
        if conflicts:
            for conflict in sorted(conflicts,
                                   key=lambda c: c.title.casefold()):
                self.decisions_layout.insertWidget(
                    self.decisions_layout.count() - 1,
                    self._decision_card(conflict))
        else:
            placeholder = QLabel(
                'Nothing needs your decision right now. When a value '
                'changed in more than one place and Hakubun cannot '
                'choose on its own, the choice appears here after '
                'Fetch changes.')
            placeholder.setWordWrap(True)
            self.decisions_layout.insertWidget(
                self.decisions_layout.count() - 1, placeholder)
        self.decisions_box.setTitle('Decisions (%d)' % len(conflicts))

    def _resolve_inline(self, conflict, source):
        self.engine.resolve_conflict(conflict, source)
        self._status('Resolved %s for %s, replanning...' % (
            _FIELD_LABELS.get(conflict.field, conflict.field),
            conflict.title))
        self._replan()

    def _resolve_structural(self, conflict):
        """Resolve a differing-episode-structures conflict with an
        explicit episode count in the LOCAL structure."""
        current = conflict.values.get('local') or 0
        value, ok = QInputDialog.getInt(
            self, 'Set episodes',
            '%s\nEpisodes watched, in the local structure:'
            % conflict.title, int(current), 0, 100000)
        if not ok:
            return
        self.engine.resolve_conflict(conflict, 'value', value=value)
        self._status('Resolved %s for %s, replanning...' % (
            _FIELD_LABELS.get(conflict.field, conflict.field),
            conflict.title))
        self._replan()

    def _replan(self):
        # Plan only -- resolution changed local state, no need to hit
        # the network again.
        self._run(self.engine.plan, self.r_planned, 'Planning...')

    @staticmethod
    def _iter_tree_changes(tree):
        for i in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(i)
            for j in range(group.childCount()):
                child = group.child(j)
                data = child.data(0, QtCore.Qt.ItemDataRole.UserRole)
                if isinstance(data, SyncOperation):
                    yield child, data

    def _iter_change_items(self):
        # 'Changes' holds every ordinary change exactly once and 'New
        # entries' every create exactly once; together they cover the
        # whole plan without double-counting the per-field tabs.
        for tree in (self._all_changes_tree, self._new_entries_tree):
            if tree is not None:
                yield from self._iter_tree_changes(tree)

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

    @staticmethod
    def _change_content_equal(a, b):
        """Whether two SyncOperations describe the same proposed edit --
        everything except `.selected`, which is the user's own
        tick/untick and must never be what decides whether a replan
        treats a change as 'the same one' or a new proposal."""
        return (a.old == b.old and a.new == b.new and a.source == b.source
                and a.title == b.title and a.remote_raw == b.remote_raw
                and a.reason == b.reason
                and a.creates_entry == b.creates_entry)

    def _reconcile_changes(self, changes):
        """Swap a freshly-planned SyncOperation for the previous render's
        object when it describes the exact same edit (matched on
        uuid+field+target, then content), so the user's tick survives
        and -- just as importantly -- _update_category_tabs below can
        tell an unchanged category from a changed one by comparing
        object identity instead of walking every field of every
        change."""
        prev = self._prev_change_index
        reconciled = []
        for change in changes:
            old = prev.get((change.uuid, change.field, change.target))
            if old is not None and self._change_content_equal(old, change):
                reconciled.append(old)
            else:
                reconciled.append(change)
        return reconciled

    def _rebuild_category_tabs(self, categories):
        """Full teardown and rebuild of every preview tab. Used only
        when the SET of categories changes (a field's change list goes
        from empty to non-empty or back) -- the incremental path in
        _update_category_tabs can't express that as a same-shape
        update. Preserves the current tab selection by name, same as
        before this was split out."""
        current_tab_label = self.preview_tabs.tabText(
            self.preview_tabs.currentIndex()) \
            if self.preview_tabs.count() else None
        self.preview_tabs.clear()
        self._category_trees = {}
        self._category_ids = {}
        restore_index = 0
        for key, changes, label in categories:
            tree = self._make_preview_tree()
            self._populate_preview_tree(tree, changes)
            if key == '__new__':
                # New entries get their own tab, never mixed into the
                # ordinary change list: "create this title on another
                # site" is a different kind of act than a field update,
                # and it stays opt-in. The tab carries its own help.
                holder = QWidget()
                holder_layout = QVBoxLayout(holder)
                help_label = QLabel(present.CREATES_ENTRY_HELP)
                help_label.setWordWrap(True)
                holder_layout.addWidget(help_label)
                holder_layout.addWidget(tree, 1)
                self.preview_tabs.addTab(holder, label)
            else:
                self.preview_tabs.addTab(tree, label)
            self._category_trees[key] = tree
            self._category_ids[key] = frozenset(id(c) for c in changes)
            if current_tab_label and label.split(' (')[0] == \
                    (current_tab_label or '').split(' (')[0]:
                restore_index = self.preview_tabs.count() - 1
        # Stay on the same category (by name) across replans when it
        # still exists, rather than always snapping back to 'All'.
        if current_tab_label and current_tab_label.startswith('All'):
            restore_index = 0
        self.preview_tabs.setCurrentIndex(restore_index)
        self._category_order = [key for key, _changes, _label in categories]

    def _update_category_tabs(self, categories):
        """Same categories as last render, in the same order: rebuild
        only the tab trees whose change SET actually differs from last
        time (by object identity -- safe because _reconcile_changes
        already folded identical changes back onto their previous
        objects). Everything else -- widgets, scroll position, tick
        state, expand/collapse state, the current tab -- is left
        exactly as the user had it, since nothing about it changed.
        This is what makes resolving one conflict (which almost always
        touches a single field on a single show, see _resolve_inline)
        cheap instead of tearing down every tree in the window."""
        for index, (key, changes, label) in enumerate(categories):
            ids = frozenset(id(c) for c in changes)
            if self._category_ids.get(key) == ids:
                continue
            tree = self._category_trees[key]
            self._populate_preview_tree(tree, changes)
            self.preview_tabs.setTabText(index, label)
            self._category_ids[key] = ids

    def r_planned(self, plan, error):
        if isinstance(error, SyncCancelled):
            self._status('Sync cancelled.')
            return
        if error is not None:
            self._status('Sync failed: %s' % error)
            return
        # A replanned change identical to what was already shown reuses
        # that SAME SyncOperation object, so its `.selected` (the user's
        # own tick/untick) survives -- see _reconcile_changes. Without
        # this, resolving one conflict (which always replans, see
        # _replan) silently reset every other tick in the window back
        # to the plan's own default on every single click.
        plan.changes = self._reconcile_changes(plan.changes)
        self._prev_change_index = {(c.uuid, c.field, c.target): c
                                   for c in plan.changes}
        self._plan = plan

        # Category tabs: 'Changes' (every ordinary change) always
        # present, plus one per field that actually has a change this
        # plan (Score, Watched Episodes, ...), plus -- separately --
        # 'New entries' for creates: adding a title to a site is a
        # different kind of act than updating a field, so it never
        # hides inside the ordinary list.
        ordinary = [c for c in plan.changes if not c.creates_entry]
        creates = [c for c in plan.changes if c.creates_entry]
        by_field = {}
        for change in ordinary:
            by_field.setdefault(change.field, []).append(change)
        categories = [('__all__', ordinary, 'Changes (%d)'
                       % len(ordinary))]
        for field in USER_FIELDS:
            field_changes = by_field.get(field)
            if not field_changes:
                continue
            categories.append((field, field_changes, '%s (%d)' % (
                _FIELD_LABELS.get(field, field), len(field_changes))))
        if creates:
            categories.append(('__new__', creates,
                               'New entries (%d)' % len(creates)))

        # Which categories exist changes only when a field's change set
        # goes from empty to non-empty or back -- comparatively rare
        # (mostly full syncs where whole categories appear/disappear).
        # The common replan -- resolving one conflict on one show --
        # leaves the same categories present, just one of them changed,
        # so it takes the incremental path and rebuilds only that one
        # tab instead of tearing down every tree in the window.
        desired_keys = [key for key, _changes, _label in categories]
        if desired_keys != self._category_order:
            self._rebuild_category_tabs(categories)
        else:
            self._update_category_tabs(categories)
        self._all_changes_tree = self._category_trees['__all__']
        self._new_entries_tree = self._category_trees.get('__new__')

        # Re-derive from the CURRENT widget state (rebuilt tabs plus
        # tabs the incremental path above left untouched) rather than
        # accumulating registrations per-tree: an id(SyncOperation) can
        # be reused by the GC once an old, no-longer-referenced change
        # object is dropped, so any stale entry left over from a
        # rebuilt-away tree would risk mirroring a checkbox toggle onto
        # a deleted Qt item -- or worse, a different, unrelated one.
        self._change_items = {}
        for tree in self._category_trees.values():
            for item, change in self._iter_tree_changes(tree):
                self._change_items.setdefault(id(change), []).append(item)

        self._set_decisions(plan.conflicts)
        # A fresh plan supersedes the last apply's progress bar; its
        # log stays readable until the next apply clears it.
        self.apply_progress.setVisible(False)
        self.apply_button.setEnabled(bool(plan.changes))
        self.summary_label.setText('<b>%s</b>' % present.plan_summary(plan))
        self._status(present.plan_status(plan))
        self._refresh_identity()

    def s_apply(self):
        if self._plan is None:
            return
        for item, change in self._iter_change_items():
            change.selected = (item.checkState(0)
                               == QtCore.Qt.CheckState.Checked)
        plan, self._plan = self._plan, None
        self.apply_button.setEnabled(False)
        self._cancel.clear()
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        selected = sum(1 for c in plan.changes if c.selected)
        self.apply_log.clear()
        self.apply_log.appendPlainText(
            'Syncing %d selected change(s)...' % selected)
        self.apply_log.setVisible(True)
        self.apply_progress.setRange(0, 0)   # busy until first report
        self.apply_progress.setVisible(True)
        self._run(self.engine.apply, self.r_applied,
                  'Syncing selected changes...', plan,
                  should_cancel=self._cancel.is_set, forward_progress=True)

    def s_cancel_apply(self):
        self._cancel.set()
        self.cancel_button.setEnabled(False)
        self.apply_log.appendPlainText('Cancelling after the current '
                                       'push...')

    def _on_apply_progress(self, done, total, message):
        if total:
            self.apply_progress.setRange(0, total)
            self.apply_progress.setValue(done)
        self.apply_log.appendPlainText(message)

    def r_applied(self, result, error):
        self.cancel_button.setVisible(False)
        if error is not None:
            self.apply_progress.setVisible(False)
            self.apply_log.appendPlainText('Apply failed: %s' % error)
            self._status('Apply failed: %s' % error)
            return
        self.apply_progress.setRange(0, 1)
        self.apply_progress.setValue(1)
        verb = 'Cancelled after' if result.get('cancelled') else 'Synced:'
        text = '%s %d Hakubun update(s), %d site update(s)' % (
            verb, result['local'], result['pushed'])
        if result['errors']:
            text += '. Failed: %s' % ', '.join(
                '%s (%s)' % kv for kv in result['errors'].items())
            text += ' (their changes will be offered again)'
        self.apply_log.appendPlainText(text)
        self._status(text)
        self.s_fetch()

    def s_reset(self):
        answer = QMessageBox.question(
            self, 'Reset sync database',
            'Wipe the sync database? Identities, mappings, sync '
            'history, resolved decisions and per-field sync rules are '
            'deleted and re-derived on the next fetch. Your provider '
            'lists are NOT touched.')
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.is_busy():
            # Never block the GUI thread waiting on a running fetch --
            # that can be minutes of provider downloads with a frozen,
            # unrepaintable window. Ask the worker to stop and let the
            # user click again once it has.
            self._cancel.set()
            self._status('Stopping the running operation first. Press '
                         'Reset again once it has.')
            return
        self.store.reset()
        self._plan = None
        self.preview_tabs.clear()
        self._all_changes_tree = None
        self._new_entries_tree = None
        self._change_items = {}
        # Tabs were just torn down out from under _category_trees --
        # reset the incremental-replan bookkeeping too, or the next
        # r_planned would think an unchanged category can be left alone
        # and try to reuse a tree Qt has already deleted.
        self._category_trees = {}
        self._category_ids = {}
        self._category_order = None
        self._prev_change_index = {}
        self._set_decisions([])
        # The Mirror tab's preview describes the database that was just
        # wiped -- membership decisions included. Clear it too, or its
        # rows would offer to act on entities that no longer exist.
        self._mirror_plan = None
        self.mirror_tree.clear()
        self._set_mirror_decisions([])
        self.mirror_apply_button.setEnabled(False)
        self.mirror_summary.setText('Preview a mirror to see what '
                                    'would change.')
        self.apply_progress.setVisible(False)
        self.apply_log.clear()
        self.apply_log.setVisible(False)
        self.summary_label.setText('Fetch changes to see what would '
                                   'sync.')
        self.apply_button.setEnabled(False)
        self._refresh_identity()
        self._refresh_policy_widgets()
        self._status('Sync database reset. Run Fetch changes to '
                     're-derive it.')

    # -- Mirror ---------------------------------------------------------
    #
    # Sync is incremental; Mirror converges the TRACKERS onto what
    # Ownership says (sync/mirror.py). Its own tab, its own preview and
    # its own apply path -- deliberately not overloaded onto the Sync
    # preview, because it is a different, potentially much larger
    # operation and the two must not be confusable.
    #
    # Hakubun never appears here as a tracker side. Local state still
    # converges (MirrorPlan.local) but is not shown: it is
    # reconciliation state, not one of the things being mirrored.

    def _build_mirror_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        self.mirror_summary = QLabel('Preview a mirror to see what '
                                     'would change.')
        bar.addWidget(self.mirror_summary)
        bar.addStretch()
        self.mirror_preview_button = QPushButton('Preview mirror')
        self.mirror_preview_button.setToolTip(
            'Work out what each tracker should contain according to '
            'Ownership. Nothing is written anywhere until you apply.')
        self.mirror_preview_button.clicked.connect(self.s_mirror_preview)
        self.mirror_apply_button = QPushButton('Apply mirror…')
        self.mirror_apply_button.setToolTip(
            'Review the totals, then carry out the ticked changes.')
        self.mirror_apply_button.setEnabled(False)
        self.mirror_apply_button.clicked.connect(self.s_mirror_apply)
        self.mirror_cancel_button = QPushButton('Cancel')
        self.mirror_cancel_button.setVisible(False)
        self.mirror_cancel_button.clicked.connect(self.s_cancel_apply)
        bar.addWidget(self.mirror_preview_button)
        bar.addWidget(self.mirror_apply_button)
        bar.addWidget(self.mirror_cancel_button)
        layout.addLayout(bar)

        help_label = QLabel(present.MIRROR_TAB_HELP.replace('\n\n', '<br>'))
        help_label.setWordWrap(True)
        layout.addWidget(help_label)

        # No "allow adding" / "allow removing" checkboxes, and no
        # category filter. Both asked a question the preview and the
        # confirmation already answer: you can see every change, and
        # nothing is written until you confirm. A plan you must filter
        # to understand is one you cannot confidently apply.

        splitter = QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.mirror_tree = QTreeWidget()
        self.mirror_tree.setHeaderHidden(True)
        self.mirror_tree.itemChanged.connect(self._on_mirror_item_toggled)
        self.mirror_tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.mirror_tree.customContextMenuRequested.connect(
            lambda pos: self._mirror_context_menu(self.mirror_tree, pos))
        splitter.addWidget(self.mirror_tree)

        self.mirror_decisions_box = QGroupBox('Decisions')
        outer = QVBoxLayout(self.mirror_decisions_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        self.mirror_decisions_layout = QVBoxLayout(holder)
        self.mirror_decisions_layout.addStretch()
        scroll.setWidget(holder)
        outer.addWidget(scroll)
        splitter.addWidget(self.mirror_decisions_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([620, 380])
        layout.addWidget(splitter, 1)

        self._mirror_plan = None
        self._set_mirror_decisions([])
        return page

    # -- mirror preview rendering --------------------------------------

    def _mirror_op_item(self, op, text, color):
        item = QTreeWidgetItem([text])
        item.setForeground(0, color)
        item.setFlags(item.flags()
                      | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, QtCore.Qt.CheckState.Checked if op.selected
                           else QtCore.Qt.CheckState.Unchecked)
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, op)
        return item

    def _on_mirror_item_toggled(self, item, column):
        if column != 0:
            return
        card = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 3)
        if card is not None:
            # A card's own tick answers the whole title at once. Qt's
            # auto-tristate only reaches CHECKABLE descendants, and a
            # card's operations sit under its tracker rows, which are
            # not checkable -- so propagating by hand is what makes
            # "yes to this show" a single click instead of one per
            # field.
            state = item.checkState(0)
            if state == QtCore.Qt.CheckState.PartiallyChecked:
                return
            selected = state == QtCore.Qt.CheckState.Checked
            for op in card.ops:
                op.selected = selected
            self.mirror_tree.blockSignals(True)
            try:
                self._sync_mirror_checks(item)
            finally:
                self.mirror_tree.blockSignals(False)
            return
        op = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if op is None or not hasattr(op, 'selected'):
            return
        op.selected = (item.checkState(0)
                       == QtCore.Qt.CheckState.Checked)
        # Keep the card above in step, so a card that is half-ticked
        # says so rather than claiming the whole title is going.
        parent = item.parent()
        while parent is not None:
            owner = parent.data(0, QtCore.Qt.ItemDataRole.UserRole + 3)
            if owner is not None:
                self.mirror_tree.blockSignals(True)
                try:
                    parent.setCheckState(0,
                                         self._card_check_state(owner))
                finally:
                    self.mirror_tree.blockSignals(False)
                return
            parent = parent.parent()

    @staticmethod
    def _card_check_state(card):
        states = {o.selected for o in card.ops}
        if states == {True}:
            return QtCore.Qt.CheckState.Checked
        if states == {False} or not states:
            return QtCore.Qt.CheckState.Unchecked
        return QtCore.Qt.CheckState.PartiallyChecked

    def _sync_mirror_checks(self, item):
        """Redraw every operation tick under `item` from the operations
        themselves, so the widgets agree with the plan."""
        for i in range(item.childCount()):
            child = item.child(i)
            op = child.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if op is not None and hasattr(op, 'selected'):
                child.setCheckState(
                    0, QtCore.Qt.CheckState.Checked if op.selected
                    else QtCore.Qt.CheckState.Unchecked)
            self._sync_mirror_checks(child)

    def _render_mirror_cards(self):
        """Draw the plan as one group per title, its changes as flat
        rows -- the same shape the Sync tab uses.

        A card's own tick drives every change under it, so "yes to this
        show" is one click rather than one per field.
        """
        tree = self.mirror_tree
        plan = self._mirror_plan
        tree.blockSignals(True)
        try:
            tree.clear()
            if plan is None:
                return
            cards = present.mirror_cards(plan, self.engine.adapters)
            bold = QtGui.QFont()
            bold.setBold(True)
            for card in cards:
                top = QTreeWidgetItem(
                    ['%s   —   %s'
                     % (card.title, present.mirror_card_headline(card))])
                top.setFont(0, bold)
                top.setData(0, QtCore.Qt.ItemDataRole.UserRole + 3, card)
                top.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1,
                            card.issue)
                if card.ops:
                    # Checkable, but NOT auto-tristate: Qt derives an
                    # auto-tristate item's state from its checkable
                    # direct children, and a card's rows include
                    # informational ones that are not checkable. Left
                    # on, Qt overrode every click back to unchecked.
                    top.setFlags(top.flags()
                                 | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                self._build_mirror_card(top, card)
                if card.ops:
                    top.setCheckState(0, self._card_check_state(card))
                tree.addTopLevelItem(top)
                top.setExpanded(len(cards) <= 25)
        finally:
            tree.blockSignals(False)

    def _build_mirror_card(self, top, card):
        # What ownership says this work SHOULD be. Under a single
        # master list this would just be that list's entry; ownership
        # assembles it per field from each field's own authority, so it
        # names the owner alongside every value.
        if card.desired:
            header = QTreeWidgetItem(
                ['Ownership says:  '
                 + '   ·   '.join('%s %s (%s)' % (name, value, owner)
                                  if owner else '%s %s' % (name, value)
                                  for name, value, owner, _why
                                  in card.desired)])
            header.setForeground(0, self._PULL_COLOR)
            top.addChild(header)

        for op, text in card.rows:
            if op is None:
                # Informational: a settled membership decision, or a
                # tracker identity has not matched. Not actionable
                # here, so not tickable -- but shown, because the way
                # back to that decision is this card's context menu.
                item = QTreeWidgetItem([text])
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1,
                             card.issue)
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 2,
                             text.split(' — ')[0].strip())
                top.addChild(item)
                continue
            if hasattr(op, 'values'):
                color = self._CREATE_COLOR
            elif not hasattr(op, 'field'):
                color = self._CONFLICT_COLOR
            elif op.target == 'local':
                color = self._PULL_COLOR
            else:
                color = self._PUSH_COLOR
            top.addChild(self._mirror_op_item(op, text, color))

        for conflict in card.conflicts:
            note = QTreeWidgetItem(
                ['%s needs your decision — see Decisions'
                 % present.field_label(conflict.field)])
            note.setForeground(0, self._CONFLICT_COLOR)
            top.addChild(note)

    def _mirror_context_menu(self, tree, pos):
        """Right-click a tracker row in the membership view: record what
        should be true of this entry on that tracker.

        These are the three membership decisions (sync/membership.py),
        and they persist -- the next mirror does not rediscover a
        discrepancy the user has already settled. 'Remove from' is the
        ONLY way a deletion is ever proposed."""
        item = tree.itemAt(pos)
        if item is None:
            return
        op = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if op is not None and 'you chose this' in getattr(op, 'reason', ''):
            # This row exists because of a stored Mirror decision --
            # the field is settled and so no longer appears as a
            # conflict card. Without this there is no way back to it.
            menu = QMenu(self)
            act = menu.addAction('Ask me about %s again'
                                 % present.field_label(op.field))
            act.triggered.connect(
                lambda _c=False: self._clear_mirror_resolution(op))
            menu.exec(tree.viewport().mapToGlobal(pos))
            return
        issue = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1)
        provider_label = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 2)
        if issue is None or not provider_label:
            return
        provider = provider_label.lower()
        menu = QMenu(self)
        has_entry = provider in issue.present
        if not has_entry:
            act = menu.addAction(present.mirror_add_label(issue, provider))
            act.triggered.connect(
                lambda _c=False: self._set_membership(issue, provider,
                                                      'present'))
        else:
            act = menu.addAction(present.mirror_remove_label(provider))
            act.triggered.connect(
                lambda _c=False: self._confirm_membership_removal(
                    issue, provider))
        act = menu.addAction(present.mirror_ignore_label(provider))
        act.triggered.connect(
            lambda _c=False: self._set_membership(issue, provider,
                                                  'ignore'))
        if provider in issue.decisions:
            act = menu.addAction('Ask me about %s again' % provider_label)
            act.triggered.connect(
                lambda _c=False: self._set_membership(issue, provider,
                                                      None))
        menu.exec(tree.viewport().mapToGlobal(pos))

    def _clear_mirror_resolution(self, op):
        self.engine.clear_mirror_resolution(op.uuid, op.field)
        self._status('%s: %s will be asked about again.'
                     % (op.title, present.field_label(op.field)))
        self.s_mirror_preview()

    def _confirm_membership_removal(self, issue, provider):
        """Marking a tracker 'absent' is what authorizes a deletion, so
        it is confirmed at the moment it is recorded as well as at the
        moment it is applied."""
        answer = QMessageBox.question(
            self, 'Remove from %s' % present.label(provider),
            'Mark "%s" as not belonging on %s?\n\nMirror will then '
            'offer to DELETE that entry from your %s account. Nothing '
            'is deleted until you tick it, allow removals, and apply.'
            % (issue.title, present.label(provider),
               present.label(provider)))
        if answer != QMessageBox.StandardButton.Yes:
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
        while self.mirror_decisions_layout.count() > 1:
            item = self.mirror_decisions_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if conflicts:
            for conflict in sorted(conflicts,
                                   key=lambda c: c.title.casefold()):
                self.mirror_decisions_layout.insertWidget(
                    self.mirror_decisions_layout.count() - 1,
                    self._mirror_decision_card(conflict))
        else:
            placeholder = QLabel(
                'Nothing needs your decision. When two trackers hold '
                'different values for a field whose rule cannot settle '
                'it on its own, the choice appears here.')
            placeholder.setWordWrap(True)
            self.mirror_decisions_layout.insertWidget(
                self.mirror_decisions_layout.count() - 1, placeholder)
        self.mirror_decisions_box.setTitle('Decisions (%d)'
                                           % len(conflicts))

    def _mirror_decision_card(self, conflict):
        """A mirror decision is between TRACKERS -- the card lists only
        tracker sides, and resolving one adopts that tracker's value as
        the entry's value everywhere."""
        box = QGroupBox('%s: %s' % (
            conflict.title,
            _FIELD_LABELS.get(conflict.field, conflict.field)))
        card = QVBoxLayout(box)
        why = QLabel(present.mirror_conflict_why(conflict))
        why.setWordWrap(True)
        card.addWidget(why)
        if conflict.structural:
            note = QLabel(present.MIRROR_STRUCTURAL_NOTE)
            note.setWordWrap(True)
            card.addWidget(note)
        row = QHBoxLayout()
        for source in sorted(conflict.values):
            if source == 'local':
                continue
            shown = self._fmt_value(conflict.field,
                                    conflict.values[source])
            if conflict.structural:
                # Each tracker's number is in its OWN episode
                # structure, so there is no single value to adopt
                # across them and no honest conversion. Information
                # only -- the per-entry fix lives in Sync, which has
                # the local structure to resolve against.
                row.addWidget(QLabel('%s is at %s (its own structure)'
                                     % (present.label(source), shown)))
                continue
            button = QPushButton('Use %s: %s' % (present.label(source),
                                                 shown))
            button.clicked.connect(
                lambda _c=False, cf=conflict, s=source:
                self._resolve_mirror(cf, s))
            row.addWidget(button)
        row.addStretch()
        card.addLayout(row)
        return box

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
        self.mirror_apply_button.setEnabled(not plan.clean)
        self.mirror_summary.setText('<b>%s</b>'
                                    % present.mirror_plan_summary(plan))
        self._status(present.mirror_plan_summary(plan))

    def s_mirror_apply(self):
        """Never applies silently: the totals are shown first, per
        tracker, and the two bulk gates must be ticked for the
        corresponding category to run at all."""
        if self._mirror_plan is None:
            return
        plan = self._mirror_plan
        # Both categories are approved by the one confirmation below.
        # The separate "allow adding" / "allow removing" checkboxes
        # asked the same question the preview and this dialog already
        # answer. The engine still defaults them off (there is a
        # headless path too); the UI passes what the user confirmed.
        allow_adds = allow_removes = True

        text = present.mirror_confirmation(plan)

        box = QMessageBox(self)
        box.setWindowTitle('Apply mirror')
        box.setText(text)
        apply_button = box.addButton('Apply',
                                     QMessageBox.ButtonRole.AcceptRole)
        box.addButton('Cancel', QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not apply_button:
            self._status('Mirror cancelled.')
            return

        self._mirror_plan = None
        self.mirror_apply_button.setEnabled(False)
        self._cancel.clear()
        self.mirror_cancel_button.setVisible(True)
        self.mirror_cancel_button.setEnabled(True)
        self.apply_log.clear()
        self.apply_log.appendPlainText('Mirroring...')
        self.apply_log.setVisible(True)
        self.apply_progress.setRange(0, 0)
        self.apply_progress.setVisible(True)
        self._run(self.engine.apply_mirror, self.r_mirror_applied,
                  'Applying the mirror...', plan,
                  allow_adds=allow_adds, allow_removes=allow_removes,
                  should_cancel=self._cancel.is_set,
                  forward_progress=True)

    def r_mirror_applied(self, result, error):
        self.mirror_cancel_button.setVisible(False)
        if error is not None:
            self.apply_progress.setVisible(False)
            self.apply_log.appendPlainText('Mirror failed: %s' % error)
            self._status('Mirror failed: %s' % error)
            return
        self.apply_progress.setRange(0, 1)
        self.apply_progress.setValue(1)
        text = present.mirror_result_status(result)
        self.apply_log.appendPlainText(text)
        self._status(text)
        # Re-fetch, then re-preview MIRROR: the user is looking at this
        # tab, and leaving a stale plan on screen (with _mirror_plan
        # already cleared, so the button does nothing) reads as the
        # apply having failed.
        self._run(self._fetch_and_mirror, self.r_refreshed_after_mirror,
                  'Refreshing after the mirror...')

    def _fetch_and_mirror(self):
        """One fetch feeding BOTH previews. The Sync tab must be
        rebuilt too: left alone it keeps a stale plan and an enabled
        Sync button describing changes the mirror already made."""
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
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel('<b>How should each field sync?</b>'))
        intro = QLabel(
            'Pick one rule per field: follow one site, keep the best '
            'value, ask you when sites disagree, or don\'t sync it at '
            'all. Scores pushed to sites with a coarser scale are '
            'rounded to fit. The full policy matrix lives in the '
            'Advanced tab; both views control the same settings.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        providers = list(self.engine.adapters)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        self._policy_combos = {}
        ownership = self.store.ownership()
        for row, field in enumerate(USER_FIELDS):
            grid.addWidget(QLabel(_FIELD_LABELS.get(field, field)),
                           row, 0)
            combo = QComboBox()
            current = ownership[field].serialize()
            for key, choice_label in present.policy_choices(
                    field, providers, current):
                combo.addItem(choice_label, key)
            combo.setCurrentIndex(combo.findData(current))
            combo.currentIndexChanged.connect(
                lambda _idx, f=field: self._on_policy_combo(f))
            grid.addWidget(combo, row, 1)
            self._policy_combos[field] = combo

        # ENTRIES: the same question one row down, about whole entries
        # rather than a field. Field ownership says where a score comes
        # from; it cannot say whether Kitsu should hold the entry at
        # all, which is what made every membership difference a
        # per-entry question.
        row = len(USER_FIELDS)
        grid.addWidget(QLabel(present.ENTRY_OWNER_LABEL), row, 0)
        self.entry_owner_combo = QComboBox()
        master = self.store.master()
        for key, choice_label in present.entry_owner_choices(providers,
                                                             master):
            self.entry_owner_combo.addItem(choice_label, key)
        self.entry_owner_combo.setCurrentIndex(
            max(0, self.entry_owner_combo.findData(master or '')))
        self.entry_owner_combo.currentIndexChanged.connect(
            lambda _idx: self._on_entry_owner())
        grid.addWidget(self.entry_owner_combo, row, 1)
        help_label = QLabel(present.ENTRY_OWNER_HELP.split('\n\n')[0])
        help_label.setWordWrap(True)
        help_label.setToolTip(present.ENTRY_OWNER_HELP)
        grid.addWidget(help_label, row + 1, 0, 1, 3)

        grid.setColumnStretch(2, 1)
        box = QGroupBox()
        box.setLayout(grid)
        layout.addWidget(box)
        layout.addStretch()
        return page

    def _on_entry_owner(self):
        if self._policy_updating:
            return
        provider = self.entry_owner_combo.currentData() or None
        self.store.set_master(provider)
        self._status('Entries now follow: %s'
                     % (present.label(provider) if provider
                        else 'no one (nothing removed automatically)'))

    def _on_policy_combo(self, field):
        if self._policy_updating:
            return
        serialized = self._policy_combos[field].currentData()
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
            providers = list(self.engine.adapters)
            ownership = self.store.ownership()
            for field, combo in getattr(self, '_policy_combos',
                                        {}).items():
                current = ownership[field].serialize()
                if combo.findData(current) < 0:
                    # Set through the matrix to something the simple
                    # list doesn't offer for this field -- show the
                    # truth rather than misreport it.
                    combo.addItem(present.policy_label(
                        FieldPolicy.parse(current)), current)
                combo.setCurrentIndex(combo.findData(current))
            for field, group in getattr(self, '_ownership_groups',
                                        {}).items():
                current = ownership[field].serialize()
                for radio in group.buttons():
                    if radio.property('policy') == current:
                        radio.setChecked(True)
        finally:
            self._policy_updating = False

    # -- Advanced: the full policy matrix ------------------------------

    def _build_matrix_box(self):
        box = QGroupBox('Full policy matrix')
        layout = QVBoxLayout(box)
        blurb = QLabel(
            'Every combination, including ones the simple view does '
            'not offer. A <b>provider</b> column makes that tracker '
            'authoritative: its value wins everywhere, always. The '
            '<b>rule</b> columns: Manual asks you when sides genuinely '
            'disagree, Union combines sets, Highest/Lowest pick an '
            'extreme, Progress favours the furthest episode. '
            '"Individual" keeps a field per-site and never syncs it.')
        blurb.setWordWrap(True)
        layout.addWidget(blurb)
        providers = list(self.engine.adapters)
        columns = ([('provider:%s' % p, p.capitalize())
                    for p in providers]
                   + [('reconcile:manual', 'Manual'),
                      ('reconcile:union', 'Union'),
                      ('reconcile:max', 'Highest'),
                      ('reconcile:min', 'Lowest'),
                      ('reconcile:progress', 'Progress'),
                      ('individual', 'Individual')])
        grid = QGridLayout()
        # Generous spacing: the previous tight grid packed radios close
        # enough together to be hard to click without misclicking a
        # neighboring row/column, and headers weren't centered over
        # their column's radios (headers default to left-aligned text,
        # radios were centered -- the two never lined up).
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(18)
        for col, (_, label) in enumerate(columns, start=1):
            header = QLabel('<b>%s</b>' % label)
            header.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
            grid.addWidget(header, 0, col)
            grid.setColumnMinimumWidth(col, 96)
        self._ownership_groups = {}
        ownership = self.store.ownership()
        for row, field in enumerate(USER_FIELDS, start=1):
            field_label = QLabel(_FIELD_LABELS.get(field, field))
            field_label.setMinimumHeight(30)
            grid.addWidget(field_label, row, 0,
                          QtCore.Qt.AlignmentFlag.AlignVCenter)
            group = QButtonGroup(box)
            current = ownership[field].serialize()
            for col, (policy_text, _) in enumerate(columns, start=1):
                radio = QRadioButton()
                radio.setMinimumSize(28, 28)
                radio.setProperty('policy', policy_text)
                radio.setProperty('field', field)
                if policy_text == current:
                    radio.setChecked(True)
                radio.toggled.connect(self._ownership_changed)
                group.addButton(radio)
                grid.addWidget(radio, row, col,
                               QtCore.Qt.AlignmentFlag.AlignCenter)
            self._ownership_groups[field] = group
        layout.addLayout(grid)
        return box

    def _ownership_changed(self, checked):
        if not checked or self._policy_updating:
            return
        radio = self.sender()
        self._set_policy(radio.property('field'),
                         radio.property('policy'))

    # -- Identity ------------------------------------------------------

    def _build_identity_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.identity_intro = QLabel()
        self.identity_intro.setWordWrap(True)
        layout.addWidget(self.identity_intro)
        self.identity_tree = QTreeWidget()
        self.identity_tree.setHeaderLabels(
            ['Provider', 'Type', 'Title', 'Also known as', 'Status'])
        self.identity_tree.setRootIsDecorated(False)
        self.identity_tree.currentItemChanged.connect(
            self._identity_selected)
        self.identity_tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.identity_tree.customContextMenuRequested.connect(
            self._identity_context_menu)
        layout.addWidget(self.identity_tree, 2)

        self.identity_box = QGroupBox('Resolution')
        box_layout = QVBoxLayout(self.identity_box)
        self.identity_info = QLabel()
        self.identity_info.setWordWrap(True)
        box_layout.addWidget(self.identity_info)
        self.rb_confirm = QRadioButton('Use a matched entry')
        self.candidate_combo = QComboBox()
        self.candidate_combo.currentIndexChanged.connect(
            self._update_candidate_open_button)
        self.candidate_open_button = QToolButton()
        self.candidate_open_button.setText('Open page')
        self.candidate_open_button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.candidate_open_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.candidate_open_button.setMenu(QMenu(self.candidate_open_button))
        self.candidate_open_button.setEnabled(False)
        self.rb_search = QRadioButton('Search manually: '
                                      'find the entry on another provider')
        search_bar = QHBoxLayout()
        self.search_provider = QComboBox()
        self.search_text = QLineEdit()
        self.search_button = QPushButton('Search')
        self.search_button.clicked.connect(self.s_identity_search)
        search_bar.addWidget(self.search_provider)
        search_bar.addWidget(self.search_text)
        search_bar.addWidget(self.search_button)
        results_row = QHBoxLayout()
        self.search_results = QComboBox()
        self.search_results.currentIndexChanged.connect(
            self._update_search_open_button)
        self.search_open_button = QPushButton('Open page')
        self.search_open_button.setEnabled(False)
        self.search_open_button.clicked.connect(self.s_open_search_result)
        results_row.addWidget(self.search_results, 1)
        results_row.addWidget(self.search_open_button)
        self.rb_provider_only = QRadioButton(
            'Keep provider-only: do not sync this entry elsewhere')
        self.rb_defer = QRadioButton(
            'Create provider mappings later: keep watching for new '
            'matches')
        self.rb_ignore = QRadioButton('Ignore this title: never ask again')
        for w in (self.rb_confirm, self.candidate_combo,
                 self.candidate_open_button, self.rb_search):
            box_layout.addWidget(w)
        box_layout.addLayout(search_bar)
        box_layout.addLayout(results_row)
        for w in (self.rb_provider_only, self.rb_defer, self.rb_ignore):
            box_layout.addWidget(w)
        resolve_bar = QHBoxLayout()
        resolve_bar.addStretch()
        self.identity_resolve_button = QPushButton('Resolve')
        self.identity_resolve_button.clicked.connect(self.s_identity_resolve)
        resolve_bar.addWidget(self.identity_resolve_button)
        box_layout.addLayout(resolve_bar)
        self.identity_box.setEnabled(False)
        layout.addWidget(self.identity_box, 3)
        return page

    @staticmethod
    def _display_title(title, aliases):
        return present.display_title(title, aliases)

    def _refresh_identity(self):
        self.identity_tree.clear()
        for issue in self.store.identity_open():
            info = issue.get('entry') or {}
            aliases = info.get('aliases') or []
            display = self._display_title(issue['title'], aliases)
            others = [a for a in aliases if a and a != issue['title']]
            media_type = info.get('media_type')
            item = QTreeWidgetItem([
                issue['provider'],
                media_type.capitalize() if media_type else '?',
                display, ' / '.join(others[:2]), issue['status']])
            # A row whose only candidate is a type mismatch is a data
            # problem, not an ordinary ambiguity -- make it visually
            # impossible to miss in the list itself, not just the text.
            if any('TYPE MISMATCH' in (c.get('via') or '')
                  for c in issue['candidates']):
                item.setForeground(0, self._CONFLICT_COLOR)
                item.setForeground(1, self._CONFLICT_COLOR)
            tip = '\n'.join(filter(None, [
                '%s id %s' % (issue['provider'], issue['provider_id']),
                'Year: %s' % info['year'] if info.get('year') else None,
                'All titles: %s' % ', '.join(
                    [issue['title'] or ''] + others) if others else None]))
            for col in range(5):
                item.setToolTip(col, tip)
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, issue)
            self.identity_tree.addTopLevelItem(item)
        for col in range(4):
            self.identity_tree.resizeColumnToContents(col)
        count = self.identity_tree.topLevelItemCount()
        self.identity_intro.setText(
            ('<b>%d title(s) need matching:</b> Hakubun could not '
             'decide on its own which entry on your other sites each '
             'of these is. ' % count if count else
             '<b>No titles need matching right now.</b> ')
            + 'Certain matches (exact ID links, single exact-title '
            'matches) are linked automatically and never appear here. '
            'Right-click a row to inspect it or open it on its site.')
        # Found by index, never hardcoded: this said `2`, which was
        # Identity's slot until the Mirror tab was inserted at 1 and
        # pushed everything down. After that it stamped "Identity (N)"
        # over the CONFIGURATION tab, leaving two tabs called Identity
        # and no way to tell which was which.
        self.tabs.setTabText(self.tabs.indexOf(self._identity_tab),
                             'Identity (%d)' % count)

    def _identity_selected(self, item, _previous=None):
        self.identity_box.setEnabled(item is not None)
        if item is None:
            return
        issue = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        info = issue.get('entry') or {}
        candidates = issue['candidates']
        year = ' (%s)' % info['year'] if info.get('year') else ''
        why = ('%d possible existing match(es) were found by title, '
               'none certain enough to link automatically.'
               % len(candidates) if candidates else
               'No existing entry matched and no cross-provider ID '
               'was published.')
        self.identity_info.setText(
            '<b>%s</b>%s: %s entry %s.<br>%s' % (
                self._display_title(issue['title'],
                                    info.get('aliases')),
                year, issue['provider'], issue['provider_id'], why))
        self.candidate_combo.clear()
        for cand in candidates:
            providers = ', '.join('on %s (%s)' % kv
                                  for kv in sorted(
                                      cand.get('providers', {}).items()))
            label = '%s%s: %s (%s)' % (
                self._display_title(cand.get('title'),
                                    cand.get('aliases')),
                ' (%s)' % cand['year'] if cand.get('year') else '',
                providers or 'no providers yet',
                cand.get('via', ''))
            self.candidate_combo.addItem(label, cand)   # full dict: the
            # Open-page button needs providers/media_type, not just uuid.
        self.rb_confirm.setEnabled(bool(candidates))
        if candidates:
            self.rb_confirm.setChecked(True)
        else:
            self.rb_defer.setChecked(True)
        self.rb_provider_only.setText(
            'Keep %s-only: do not sync this entry elsewhere'
            % issue['provider'])
        self.search_provider.clear()
        for name in self.engine.adapters:
            if name != issue['provider']:
                self.search_provider.addItem(name)
        self.search_text.setText(issue['title'] or '')
        self.search_results.clear()
        self._update_candidate_open_button()
        self._update_search_open_button()

    def s_identity_search(self):
        provider = self.search_provider.currentText()
        if not provider:
            return
        adapter = self.engine.adapters[provider]
        self._run(adapter.search, self.r_identity_search,
                  'Searching %s...' % provider,
                  self.search_text.text().strip())

    def r_identity_search(self, results, error):
        if error is not None:
            self._status('Search failed: %s' % error)
            return
        self.search_results.clear()
        for entry in results or []:
            self.search_results.addItem(
                '%s (%s %s)' % (entry.title, entry.provider,
                                entry.provider_id), entry)
        self.rb_search.setChecked(True)
        self._update_search_open_button()
        self._status('Found %d result(s).' % (len(results or [])))

    # -- Inspect / Open-page (Identity + resolution box) ---------------

    def _open_provider_page(self, provider, media_type, provider_id):
        url = adapters.web_url(provider, media_type, provider_id)
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
        else:
            self._status('No web page known for %s.' % provider)

    def _update_candidate_open_button(self):
        menu = self.candidate_open_button.menu()
        menu.clear()
        cand = self.candidate_combo.currentData()
        providers = (cand or {}).get('providers') or {}
        media_type = (cand or {}).get('media_type')
        for provider, pid in sorted(providers.items()):
            action = menu.addAction('Open on %s' % provider.capitalize())
            action.triggered.connect(
                lambda checked=False, p=provider, mt=media_type, i=pid:
                self._open_provider_page(p, mt, i))
        self.candidate_open_button.setEnabled(bool(providers))

    def _update_search_open_button(self):
        self.search_open_button.setEnabled(
            self.search_results.currentData() is not None)

    def s_open_search_result(self):
        entry = self.search_results.currentData()
        if entry is None:
            return
        self._open_provider_page(entry.provider, entry.media_type,
                                 entry.provider_id)

    def _identity_context_menu(self, pos):
        item = self.identity_tree.itemAt(pos)
        if item is None:
            return
        issue = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        inspect_action = menu.addAction('Inspect')
        inspect_action.triggered.connect(
            lambda: self._inspect_from_identity(issue['provider'],
                                                issue['provider_id']))
        media_type = (issue.get('entry') or {}).get('media_type')
        url = adapters.web_url(issue['provider'], media_type,
                               issue['provider_id'])
        open_action = menu.addAction('Open on %s'
                                     % issue['provider'].capitalize())
        open_action.setEnabled(url is not None)
        if url:
            open_action.triggered.connect(
                lambda checked=False, u=url:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(u)))
        menu.exec(self.identity_tree.viewport().mapToGlobal(pos))

    def _inspect_from_identity(self, provider, provider_id):
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).startswith('Advanced'):
                self.tabs.setCurrentIndex(i)
                break
        idx = self.inspect_provider.findData(provider)
        if idx >= 0:
            self.inspect_provider.setCurrentIndex(idx)
        self.inspect_id.setText(str(provider_id))
        self.s_inspect()

    def s_identity_resolve(self):
        item = self.identity_tree.currentItem()
        if item is None:
            return
        issue = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        identity = self.engine.identity
        entry = NormalizedEntry(provider=issue['provider'],
                                provider_id=issue['provider_id'],
                                title=issue['title'] or '')
        try:
            if self.rb_confirm.isChecked():
                cand = self.candidate_combo.currentData()
                if cand is None:
                    return
                identity.resolve_conflict(issue['id'], 'confirm',
                                          target_uuid=cand['uuid'])
            elif self.rb_search.isChecked():
                found = self.search_results.currentData()
                if found is None:
                    self._status('Pick a search result first.')
                    return
                mapping = self.store.mapping_for(found.provider,
                                                 found.provider_id)
                if mapping:
                    uid = mapping['uuid']
                else:
                    uid = self.store.create_entity(
                        found.title, media_type=found.media_type,
                        year=found.year, total=found.total)
                    self.store.add_mapping(uid, found.provider,
                                           found.provider_id,
                                           confirmed=True)
                identity.resolve_conflict(issue['id'], 'confirm',
                                          target_uuid=uid)
            elif self.rb_provider_only.isChecked():
                identity.resolve_conflict(issue['id'], 'provider_only',
                                          entry=entry)
            elif self.rb_defer.isChecked():
                identity.resolve_conflict(issue['id'], 'defer')
            elif self.rb_ignore.isChecked():
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
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self._build_matrix_box())

        inspector_box = QGroupBox('Identity inspector')
        inspector_layout = QVBoxLayout(inspector_box)
        intro = QLabel(
            'Enter one entry by its provider ID to see exactly how '
            'multisync resolved it: what it maps to on your other '
            'providers, how each link was made (a published ID, the '
            'anime-relations atlas, a title match, or your own '
            'confirmation), and the raw data recorded for it.')
        intro.setWordWrap(True)
        inspector_layout.addWidget(intro)

        bar = QHBoxLayout()
        bar.addWidget(QLabel('Provider:'))
        self.inspect_provider = QComboBox()
        for name in self.engine.adapters:
            self.inspect_provider.addItem(name.capitalize(), name)
        bar.addWidget(self.inspect_provider)
        bar.addWidget(QLabel('ID:'))
        self.inspect_id = QLineEdit()
        self.inspect_id.setPlaceholderText('e.g. 52991')
        self.inspect_id.returnPressed.connect(self.s_inspect)
        bar.addWidget(self.inspect_id)
        inspect_button = QPushButton('Look up')
        inspect_button.clicked.connect(self.s_inspect)
        bar.addWidget(inspect_button)
        self.inspect_open_button = QPushButton('Open page')
        self.inspect_open_button.setEnabled(False)
        self.inspect_open_button.clicked.connect(self.s_inspect_open)
        bar.addWidget(self.inspect_open_button)
        bar.addStretch()
        inspector_layout.addLayout(bar)

        self.inspect_output = QTextBrowser()
        self.inspect_output.setOpenExternalLinks(False)
        self.inspect_output.setHtml(
            '<p style="color:gray">Nothing looked up yet.</p>')
        inspector_layout.addWidget(self.inspect_output, 1)
        layout.addWidget(inspector_box, 1)

        # Destructive, so it lives here rather than next to the Sync
        # button -- same semantics and confirmation as before.
        reset_row = QHBoxLayout()
        self.reset_button = QPushButton('Reset sync database...')
        self.reset_button.setToolTip(
            'Wipe the sync database (identities, mappings, history, '
            'sync rules) and start clean. Your provider lists are '
            'never touched; the next Fetch changes re-derives '
            'everything.')
        self.reset_button.clicked.connect(self.s_reset)
        reset_row.addWidget(self.reset_button)
        reset_row.addStretch()
        layout.addLayout(reset_row)
        return page

    def s_inspect(self):
        provider = self.inspect_provider.currentData()
        provider_id = self.inspect_id.text().strip()
        if not provider or not provider_id:
            return
        from hakubun.sync.inspect import inspect_entry
        result = inspect_entry(self.store, provider, provider_id,
                               atlas=self.engine.identity.atlas)
        self.inspect_output.setHtml(self._render_inspection(result))
        # Openable even when unresolved -- media_type may be unknown
        # (a truly never-seen id), in which case web_url's own anime
        # default applies; still lets the user go look at the raw page.
        url = adapters.web_url(provider, result.media_type, provider_id)
        self.inspect_open_button.setEnabled(url is not None)
        self.inspect_open_button.setText(
            'Open on %s' % provider.capitalize())
        self._inspect_url = url

    def s_inspect_open(self):
        if getattr(self, '_inspect_url', None):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._inspect_url))

    @staticmethod
    def _mono(text):
        """Wrap an id/technical value in monospace, escaping nothing
        beyond what's needed for ids/enums (never raw user titles)."""
        return '<code>%s</code>' % text

    def _render_inspection(self, r):
        p = ['<h3>%s id %s</h3>' % (r.provider.capitalize(),
                                    self._mono(r.provider_id))]
        if not r.found:
            p.append('<p>%s</p>' % r.note)
            issue = r.identity_issue
            if issue and issue.get('candidates'):
                p.append('<p><b>Candidates on file:</b></p><ul>')
                for c in issue['candidates']:
                    providers = ', '.join(
                        self._mono('%s:%s' % kv)
                        for kv in (c.get('providers') or {}).items()) \
                        or 'no providers yet'
                    p.append('<li>%s%s: %s (<i>%s</i>)</li>' % (
                        c.get('title') or '?',
                        ' (%s)' % c['year'] if c.get('year') else '',
                        providers, c.get('via', '')))
                p.append('</ul>')
            if r.atlas_hint:
                p.append('<p><b>%s independently says:</b> %s</p>' % (
                    _atlas_label(r), self._mono(', '.join(
                        '%s=%s' % kv for kv in r.atlas_hint.items()))))
            return ''.join(p)

        p.append('<p><b>%s</b>%s%s</p>' % (
            r.title or '?', ' (%s)' % r.year if r.year else '',
            ', pinned provider-only (%s), excluded from cross-provider '
            'sync' % r.provider_only.capitalize()
            if r.provider_only else ''))
        if r.aliases:
            p.append('<p>Also known as: %s</p>'
                     % ', '.join(a for a in r.aliases if a != r.title))
        p.append('<p>Internal id: %s</p>' % self._mono(r.uuid))

        p.append('<h4>Mapped providers</h4>'
                 '<table border="1" cellpadding="4" cellspacing="0">'
                 '<tr><th>Provider</th><th>ID</th><th>Confirmed</th>'
                 '<th>Linked via</th></tr>')
        mapped_ids = {}
        for m in r.mappings:
            mapped_ids[m.provider] = m.provider_id
            p.append('<tr><td>%s</td><td>%s</td><td>%s</td>'
                     '<td>%s</td></tr>' % (
                         m.provider.capitalize(),
                         self._mono(m.provider_id),
                         'Yes' if m.confirmed else 'Auto',
                         m.via or '-'))
        p.append('</table>')

        if r.atlas_hint:
            mismatches = [
                '%s: mapped %s, atlas says %s'
                % (prov.capitalize(), self._mono(mapped_ids[prov]),
                   self._mono(hint_id))
                for prov, hint_id in r.atlas_hint.items()
                if prov in mapped_ids and mapped_ids[prov] != hint_id]
            verdict = ('<span style="color:#ff9800">differs from the '
                      'mapping above (%s)</span>' % '; '.join(mismatches)
                      if mismatches else
                      'consistent with the mapping above')
            p.append('<p><b>%s independently says:</b> %s, %s</p>' % (
                _atlas_label(r),
                self._mono(', '.join(
                    '%s=%s' % kv for kv in r.atlas_hint.items())),
                verdict))

        providers = sorted({prov for row in r.fields
                            for prov in row.per_provider})
        rows = [row for row in r.fields
               if row.per_provider or row.local not in (None, [], 0)]
        if rows and providers:
            # The synchronization layer, separate from the identity
            # explanation above: which policy governs each field is
            # WHY sync does what it does with these numbers.
            p.append('<h4>Field data</h4>'
                     '<table border="1" cellpadding="4" cellspacing="0">'
                     '<tr><th>Field</th><th>Policy</th><th>Local</th>'
                     + ''.join(
                         '<th>%s (remote / last-synced)</th>'
                         % prov.capitalize() for prov in providers)
                     + '</tr>')
            for row in rows:
                cells = ['<td>%s</td>' % _FIELD_LABELS.get(
                             row.field, row.field),
                        '<td>%s</td>' % (present.policy_label(row.policy)
                                         if row.policy else '-'),
                        '<td>%s</td>' % self._mono(self._fmt_value(
                            row.field, row.local))]
                for prov in providers:
                    pv = row.per_provider.get(prov)
                    cells.append(
                        '<td>-</td>' if pv is None else
                        '<td>%s</td>' % self._mono(
                            '%s / %s' % (
                                self._fmt_value(row.field, pv['remote']),
                                self._fmt_value(row.field, pv['base']))))
                p.append('<tr>%s</tr>' % ''.join(cells))
            p.append('</table>')
        return ''.join(p)

    # -- plumbing ------------------------------------------------------

    def is_busy(self):
        """True while a fetch/plan/apply task is running -- callers
        that would _run() something should check this first rather
        than have the request dropped with only this (possibly
        hidden) window's status label updated."""
        return self._task is not None and self._task.isRunning()

    def _run(self, fn, callback, busy_text, *args,
            forward_progress=False, **kwargs):
        if self.is_busy():
            self._status('Another sync operation is still running.')
            return
        self._status(busy_text)
        self.fetch_button.setEnabled(False)
        task = _Task(fn, *args, **kwargs)
        if forward_progress:
            # fn accepts progress(done, total, msg); the callable runs
            # in the worker thread, and emitting a signal is the
            # thread-safe way to marshal it onto the GUI thread.
            task._call[2]['progress'] = task.progressed.emit
            task.progressed.connect(self._on_apply_progress)

        def done(result, error):
            # `done` is a QUEUED delivery (the signal is emitted from
            # the worker thread). If the window closed in the meantime,
            # its store is already closed -- running the callback would
            # touch a closed database inside a Qt slot, which aborts the
            # whole process. Bail.
            if self._closed:
                return
            # The signal is emitted from inside run(), so the thread may
            # not have fully stopped yet -- wait for it, or a chained
            # _run() from the callback (e.g. the auto-replan after
            # apply) would see isRunning() and silently drop.
            task.wait()
            self.fetch_button.setEnabled(True)
            try:
                callback(result, error)
            except Exception as e:
                # A callback bug (or an unforeseen store/state issue)
                # must not abort the app via PyQt's unhandled-slot
                # excepthook.
                traceback.print_exc()
                self._status('Internal error: %s' % e)
        task.done.connect(done)
        self._task = task
        task.start()

    def _status(self, text):
        self.status_label.setText(text)

    def closeEvent(self, event):
        if not self._closed:
            self._closed = True
            # Interrupt any in-flight apply so a rate-limit wait (up to
            # a minute) doesn't block the close on the _task.wait()
            # below.
            self._cancel.set()
            if self._task is not None:
                # Drop any queued done/progress deliveries before
                # closing the store, so nothing runs against it after.
                for signal in (self._task.done, self._task.progressed):
                    try:
                        signal.disconnect()
                    except (TypeError, RuntimeError):
                        pass
                if self._task.isRunning():
                    self._task.wait()
            self.store.close()
        super().closeEvent(event)
