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

"""Multi-provider Sync window (docs/multisync.md §8).

Three sections: the sync Preview (changes / conflicts / apply), the
field-ownership matrix ("Where should hakubun sync to?" -- lives here,
not in Settings), and the identity-conflict workflow.
"""

import threading
import traceback

from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QMenu, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QRadioButton,
                             QScrollArea, QSplitter, QTabWidget, QTextBrowser,
                             QToolButton, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from hakubun import messenger, utils
from hakubun.sync import adapters, normalize
from hakubun.sync.adapters import adapter_from_account
from hakubun.sync.engine import SyncEngine
from hakubun.sync.models import (FieldChange, FieldConflict, FieldPolicy,
                                 NormalizedEntry, PolicyKind, SyncMode,
                                 USER_FIELDS)
from hakubun.sync.store import SyncStore

_FIELD_LABELS = {
    'score': 'Score', 'progress': 'Watched Episodes',
    'rewatches': 'Rewatches', 'status': 'Status',
    'notes': 'Notes', 'start_date': 'Start Date',
    'finish_date': 'Finish Date', 'tags': 'Tags', 'favorite': 'Favorites',
}


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
            # rather than only detecting it after the fact.
            self.store = SyncStore(utils.to_data_path(
                'multisync-%s.db' % media_type))
            self.engine, self._adapter_errors = \
                self._build_engine(accountman, media_type)
        # The signed-in account is the app's editing surface (the
        # working tree): its changes fold in as local intent.
        if active_api and active_api in self.engine.adapters:
            self.engine.primary = active_api

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_preview_tab(), 'Preview')
        self.tabs.addTab(self._build_ownership_tab(), 'Ownership')
        self.tabs.addTab(self._build_identity_tab(), 'Identity')
        self.tabs.addTab(self._build_inspector_tab(), 'Inspector')
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

    def _build_engine(self, accountman, media_type):
        by_provider, errors = {}, []
        msg = messenger.Messenger(None, 'Sync')
        for num, account in accountman.get_accounts():
            api = account['api']
            if api in by_provider:
                errors.append('%s: only one account per provider is '
                              'supported for now' % api)
                continue
            try:
                # Force this media type on every account (a Kitsu
                # account last used for manga still contributes its
                # ANIME list to an anime sync). Providers that can't do
                # this media type at all (e.g. VNDB is VN-only) raise
                # here and are reported, not silently dropped.
                adapter = adapter_from_account(account, msg,
                                               media_type=media_type)
            except Exception as e:
                errors.append('%s: %s' % (api, e))
                continue
            by_provider[api] = adapter
        from hakubun.sync.relations import RelationsAtlas
        atlas = RelationsAtlas.from_file() if media_type == 'anime' else None
        return SyncEngine(self.store, by_provider, relations=atlas), errors

    # -- Preview -------------------------------------------------------

    def _build_preview_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
        # The signed-in tracker's icon, same imagery the main window
        # uses (utils.available_libs) -- an at-a-glance answer to
        # "whose state feeds 'local' here?".
        if self.engine.primary:
            lib_info = utils.available_libs.get(self.engine.primary)
            if lib_info:
                icon_label = QLabel()
                icon_label.setPixmap(QtGui.QPixmap(lib_info[1]).scaled(
                    24, 24, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation))
                icon_label.setToolTip(
                    'Signed into %s -- its changes count as your local '
                    'edits.' % lib_info[0])
                bar.addWidget(icon_label)
        bar.addWidget(QLabel('Mode:'))
        self.mode_combo = QComboBox()
        for mode, label in ((SyncMode.MERGE, 'Merge (reconcile all)'),
                            (SyncMode.MIRROR, 'Mirror (local pushes out)'),
                            (SyncMode.PULL, 'Pull (providers update local)')):
            self.mode_combo.addItem(label, mode)
        bar.addWidget(self.mode_combo)
        bar.addStretch()
        self.fetch_button = QPushButton('Fetch && Plan')
        self.fetch_button.clicked.connect(self.s_fetch)
        self.apply_button = QPushButton('Apply')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.s_apply)
        self.cancel_button = QPushButton('Cancel')
        self.cancel_button.setToolTip(
            'Stop the sync after the current push. Whatever was already '
            'pushed stays; the rest re-plans.')
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.s_cancel_apply)
        self.reset_button = QPushButton('Reset database...')
        self.reset_button.setToolTip(
            'Wipe the sync database (identities, mappings, history, '
            'ownership) and start clean. Your provider lists are never '
            'touched; the next Fetch re-derives everything.')
        self.reset_button.clicked.connect(self.s_reset)
        bar.addWidget(self.fetch_button)
        bar.addWidget(self.apply_button)
        bar.addWidget(self.cancel_button)
        bar.addWidget(self.reset_button)
        layout.addLayout(bar)

        # What this mode will actually do, naming the signed-in account
        # (mirror without that context is a footgun).
        self.mode_context = QLabel()
        self.mode_context.setWordWrap(True)
        self.mode_combo.currentIndexChanged.connect(
            self._update_mode_context)
        self._update_mode_context()
        layout.addWidget(self.mode_context)

        legend = QLabel(
            '<span style="color:#42a5f5">⬆ push to a site</span> · '
            '<span style="color:#4caf50">⬇ pull into Hakubun</span> — '
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
        self._change_items = {}   # id(FieldChange) -> [item in each tab
                                  # showing it] -- keeps checkbox state
                                  # in sync across tabs (see
                                  # _on_change_item_toggled): the SAME
                                  # change appears in 'All' and in
                                  # exactly one category tab.
        splitter.addWidget(self.preview_tabs)

        self.decisions_box = QGroupBox('Needs your decision')
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

        self.preview_summary = QLabel()
        layout.addWidget(self.preview_summary)
        return page

    def _update_mode_context(self):
        mode = self.mode_combo.currentData()
        primary = self.engine.primary
        signed = (' You are signed into <b>%s</b>; changes made in the '
                  'app count as local.' % primary.capitalize()
                  if primary else '')
        if mode is SyncMode.MIRROR:
            text = ('<b>Mirror:</b> pushes local state%s over every '
                    'provider — remote-only changes will be '
                    'overwritten.' % (
                        ' (as fed by <b>%s</b>, your signed-in account)'
                        % primary.capitalize() if primary else ''))
        elif mode is SyncMode.PULL:
            text = ('<b>Pull:</b> providers update local state; '
                    'nothing is pushed.%s' % signed)
        else:
            text = ('<b>Merge:</b> reconciles every provider into '
                    'local state, then pushes the result.%s' % signed)
        self.mode_context.setText(text)

    # -- preview rendering --------------------------------------------

    _PULL_COLOR = QtGui.QColor('#4caf50')
    _PUSH_COLOR = QtGui.QColor('#42a5f5')
    _CONFLICT_COLOR = QtGui.QColor('#ff9800')

    @classmethod
    def _fmt_value(cls, field, value):
        if value is None or value == []:
            return '—'
        if isinstance(value, list):
            return ', '.join(map(str, value))
        if isinstance(value, bool):
            return 'Yes' if value else 'No'
        if field == 'score' and isinstance(value, float):
            return ('%g' % value)
        if field == 'status' and isinstance(value, str):
            return value.replace('_', ' ').title()
        return str(value)

    def _fmt_target_value(self, field, value, target):
        """What the destination will actually end up holding. A push's
        canonical value is unprojected -- MAL can never actually hold
        6.5, so showing that as 'what gets pushed' is a lie about what
        will happen, even though the field is a genuine, correctly-
        reasoned change. Local (pulls, kept values) stays canonical,
        since that's exactly what local state holds."""
        if target != 'local' and field == 'score' \
                and target in self.engine.adapters:
            info = self.engine.adapters[target].mediainfo
            smax = info.get('score_max', 10)
            value = normalize.provider_score(
                value, smax, info.get('score_step', 1))
            if target == 'kitsu' and smax:
                # UI-only re-expression, not a precision change: Kitsu's
                # API stores ratings at quarter-star granularity
                # (ratingTwenty, 0-20) on its 0-5 native scale, but
                # kitsu.app's own list view only ever shows half-star
                # increments -- projecting straight through the raw
                # native scale for display would show a number ("4.25")
                # nobody sees on Kitsu itself. Every quarter-star value
                # doubles to an exact half-integer on a 0-10 scale
                # (i.e. canonical units), which is what Kitsu's site
                # actually displays -- so show that instead. The
                # quantization above (what will actually be pushed) is
                # unchanged; only how it's presented differs.
                value = value * (10.0 / smax)
        return self._fmt_value(field, value)

    def _change_item(self, change):
        label = _FIELD_LABELS.get(change.field, change.field)
        if change.target == 'local':
            values = '%s → %s' % (self._fmt_value(change.field, change.old),
                                  self._fmt_value(change.field, change.new))
            text = '⬇ Pull from %s — %s: %s' % (
                change.source.capitalize(), label, values)
            color = self._PULL_COLOR
        else:
            values = '%s → %s' % (
                self._fmt_target_value(change.field, change.old,
                                       change.target),
                self._fmt_target_value(change.field, change.new,
                                       change.target))
            text = '⬆ Push to %s — %s: %s' % (
                change.target.capitalize(), label, values)
            color = self._PUSH_COLOR
        item = QTreeWidgetItem([text])
        item.setForeground(0, color)
        item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, QtCore.Qt.CheckState.Checked)
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, change)
        return item

    def _populate_preview_tree(self, tree, changes):
        """Fill one category tab's tree: one box per show, its changes
        as directional child rows -- same shape 'All' and every
        per-field tab share, just over a different subset of changes.
        Registers each item into self._change_items so a checkbox
        toggle can be mirrored onto this same change's item in every
        other tab that also shows it."""
        tree.clear()
        groups = {}
        for change in changes:
            groups.setdefault(change.uuid,
                              [change.title, []])[1].append(change)
        bold = QtGui.QFont()
        bold.setBold(True)
        for uid, (show_title, group_changes) in sorted(
                groups.items(), key=lambda kv: kv[1][0].casefold()):
            group = QTreeWidgetItem(['%s    —  %d change(s)'
                                     % (show_title, len(group_changes))])
            group.setFont(0, bold)
            group.setFlags(group.flags()
                           | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                           | QtCore.Qt.ItemFlag.ItemIsAutoTristate)
            group.setCheckState(0, QtCore.Qt.CheckState.Checked)
            for change in group_changes:
                item = self._change_item(change)
                group.addChild(item)
                self._change_items.setdefault(id(change), []).append(item)
            tree.addTopLevelItem(group)
        tree.expandAll()

    def _make_preview_tree(self):
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.itemChanged.connect(self._on_change_item_toggled)
        return tree

    def _on_change_item_toggled(self, item, column):
        if column != 0:
            return
        change = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not isinstance(change, FieldChange):
            return   # a show-group header; Qt's own tristate handles it
        change.selected = (item.checkState(0)
                          == QtCore.Qt.CheckState.Checked)
        for twin in self._change_items.get(id(change), []):
            if twin is not item and twin.checkState(0) != item.checkState(0):
                twin.setCheckState(0, item.checkState(0))

    def _local_label(self):
        """'local' is really just whatever the signed-in account fed
        it (see the primary fold in SyncEngine._plan_entity -- local's
        value always equals the primary's own value when a primary is
        set). Naming it 'Local' as if it were some third, separate
        thing is needless jargon; call it by the platform it actually
        is. Falls back to 'Local' only when no account is signed in."""
        return self.engine.primary.capitalize() if self.engine.primary \
            else 'Local'

    def _conflict_why(self, conflict):
        """Explain why this needs a human, not just what differs."""
        label = _FIELD_LABELS.get(conflict.field, conflict.field)
        local_label = self._local_label()
        others = sorted(s for s in conflict.values if s != 'local')
        sides = ' and '.join(
            ['%s (%s)' % (local_label, self._fmt_value(
                conflict.field, conflict.values['local']))]
            + ['%s (%s)' % (s.capitalize(),
                            self._fmt_value(conflict.field,
                                            conflict.values[s]))
               for s in others])
        why = ('%s changed in more than one place since the last sync '
               '— %s — so syncing either way would overwrite someone. '
               % (label, sides))
        kind = conflict.policy.kind
        if kind is PolicyKind.ASK:
            why += ("The '%s' policy is set to Ask, which leaves every "
                    'such tie to you.' % label)
        elif kind is PolicyKind.MERGE:
            why += ('Both sides changed at effectively the same time, '
                    "so 'newest wins' cannot break the tie.")
        else:
            why += ("The providers disagree with each other and the "
                    "'%s' policy does not name a winner among them."
                    % conflict.policy)
        if conflict.note:
            why += ' Note: %s.' % conflict.note
        return why

    def _decision_card(self, conflict):
        box = QGroupBox('%s — %s' % (
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
                text = ('Use %s: %s' % (self._local_label(), shown)
                        if self.engine.primary else
                        'Keep yours: %s' % shown)
            else:
                text = 'Use %s: %s' % (source.capitalize(), shown)
            button = QPushButton(text)
            button.clicked.connect(
                lambda _checked=False, c=conflict, s=source:
                self._resolve_inline(c, s))
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
                'Nothing needs your attention right now. Conflicts '
                '(a field changed in more than one place since the '
                'last sync) will show up here after Fetch & Plan.')
            placeholder.setWordWrap(True)
            self.decisions_layout.insertWidget(
                self.decisions_layout.count() - 1, placeholder)
        self.decisions_box.setTitle(
            'Needs your decision (%d)' % len(conflicts))

    def _resolve_inline(self, conflict, source):
        self.engine.resolve_conflict(conflict, source)
        self._status('Resolved %s for %s — replanning...' % (
            _FIELD_LABELS.get(conflict.field, conflict.field),
            conflict.title))
        self._replan()

    def _replan(self):
        # Plan only -- resolution changed local state, no need to hit
        # the network again.
        self._run(self.engine.plan, self.r_planned, 'Planning...',
                  self.mode_combo.currentData())

    def _iter_change_items(self):
        # 'All' (always tab 0) holds every change exactly once; reading
        # from it (rather than every tab) avoids processing the same
        # change twice when it also appears in a category tab.
        tree = self._all_changes_tree
        if tree is None:
            return   # no plan rendered yet -- nothing to iterate
        for i in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(i)
            for j in range(group.childCount()):
                child = group.child(j)
                data = child.data(0, QtCore.Qt.ItemDataRole.UserRole)
                if isinstance(data, FieldChange):
                    yield child, data

    def s_fetch(self):
        self._run(self._fetch_and_plan, self.r_planned,
                  'Fetching provider lists...')

    def _fetch_and_plan(self):
        errors = self.engine.fetch()
        plan = self.engine.plan(self.mode_combo.currentData())
        plan.errors.update(errors)
        return plan

    def r_planned(self, plan, error):
        if error is not None:
            self._status('Sync failed: %s' % error)
            return
        self._plan = plan
        # Category tabs: 'All' always present, plus one per field that
        # actually has a change this plan (Score, Watched Episodes,
        # ...) -- a category with nothing to show just doesn't appear.
        # Rebuilt fresh every plan since which categories are relevant
        # changes plan to plan.
        current_tab_label = self.preview_tabs.tabText(
            self.preview_tabs.currentIndex()) \
            if self.preview_tabs.count() else None
        self.preview_tabs.clear()
        self._change_items = {}

        self._all_changes_tree = self._make_preview_tree()
        self._populate_preview_tree(self._all_changes_tree, plan.changes)
        self.preview_tabs.addTab(self._all_changes_tree,
                                 'All (%d)' % len(plan.changes))

        by_field = {}
        for change in plan.changes:
            by_field.setdefault(change.field, []).append(change)
        restore_index = 0
        for field in USER_FIELDS:
            field_changes = by_field.get(field)
            if not field_changes:
                continue
            tree = self._make_preview_tree()
            self._populate_preview_tree(tree, field_changes)
            label = '%s (%d)' % (_FIELD_LABELS.get(field, field),
                                 len(field_changes))
            self.preview_tabs.addTab(tree, label)
            if current_tab_label and label.split(' (')[0] == \
                    (current_tab_label or '').split(' (')[0]:
                restore_index = self.preview_tabs.count() - 1
        # Stay on the same category (by name) across replans when it
        # still exists, rather than always snapping back to 'All'.
        if current_tab_label and current_tab_label.startswith('All'):
            restore_index = 0
        self.preview_tabs.setCurrentIndex(restore_index)

        groups = {change.uuid for change in plan.changes}
        self._set_decisions(plan.conflicts)
        # A fresh plan supersedes the last apply's progress bar; its
        # log stays readable until the next apply clears it.
        self.apply_progress.setVisible(False)
        self.apply_button.setEnabled(bool(plan.changes))
        if plan.clean:
            self.preview_summary.setText('Everything is in sync.')
        else:
            self.preview_summary.setText(
                '%d show(s): %d change(s), %d conflict(s)'
                % (len(groups), len(plan.changes), len(plan.conflicts)))
        parts = ['%d change(s)' % len(plan.changes),
                 '%d conflict(s)' % len(plan.conflicts),
                 '%d identity issue(s)' % len(plan.identity)]
        if plan.errors:
            parts.append('errors: %s' % ', '.join(
                '%s (%s)' % kv for kv in plan.errors.items()))
        self._status('Planned: ' + ', '.join(parts))
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
            'Applying %d change(s)...' % selected)
        self.apply_log.setVisible(True)
        self.apply_progress.setRange(0, 0)   # busy until first report
        self.apply_progress.setVisible(True)
        self._run(self.engine.apply, self.r_applied,
                  'Applying changes...', plan,
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
        verb = 'Cancelled after' if result.get('cancelled') else 'Applied:'
        text = '%s %d local change(s), %d push(es)' % (
            verb, result['local'], result['pushed'])
        if result['errors']:
            text += ' -- failed: %s' % ', '.join(
                '%s (%s)' % kv for kv in result['errors'].items())
            text += ' (their changes stay planned)'
        self.apply_log.appendPlainText(text)
        self._status(text)
        self.s_fetch()

    def s_reset(self):
        answer = QMessageBox.question(
            self, 'Reset sync database',
            'Wipe the sync database? Identities, mappings, sync '
            'history, resolved decisions and ownership choices are '
            'deleted and re-derived on the next fetch. Your provider '
            'lists are NOT touched.')
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self._task is not None and self._task.isRunning():
            self._task.wait()
        self.store.reset()
        self._plan = None
        self.preview_tabs.clear()
        self._change_items = {}
        self._set_decisions([])
        self.apply_progress.setVisible(False)
        self.apply_log.clear()
        self.apply_log.setVisible(False)
        self.preview_summary.clear()
        self.apply_button.setEnabled(False)
        self._refresh_identity()
        self._status('Sync database reset -- run Fetch & Plan to '
                     're-derive it.')

    # -- Ownership -----------------------------------------------------

    def _build_ownership_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel('<b>Where should hakubun sync to?</b>'))
        layout.addWidget(QLabel(
            'Ownership decides who wins when a field changed in two '
            'places.\nSingle-sided changes always propagate; '
            '"Individual" keeps a field per-site; "Ask" asks every '
            'time.\nScores pushed to coarser sites are rounded to '
            'their scale.'))
        if self.engine.primary:
            layout.addWidget(QLabel(
                'You are signed into <b>%s</b>: changes you make in the '
                'app (and tracker updates) belong to it and count as '
                'your local edits.' % self.engine.primary.capitalize()))
        providers = list(self.engine.adapters)
        columns = ([('local', 'Local')]
                   + [('provider:%s' % p,
                       p.capitalize() + (' (active)'
                                         if p == self.engine.primary
                                         else ''))
                      for p in providers]
                   + [('merge', 'Merge'), ('individual', 'Individual'),
                      ('ask', 'Ask')])
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
            group = QButtonGroup(page)
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
        box = QGroupBox()
        box.setLayout(grid)
        layout.addWidget(box)
        layout.addStretch()
        return page

    def _ownership_changed(self, checked):
        if not checked:
            return
        radio = self.sender()
        field = radio.property('field')
        policy = FieldPolicy.parse(radio.property('policy'))
        self.store.set_ownership(field, policy)
        self._status("Ownership: %s -> %s"
                     % (_FIELD_LABELS.get(field, field), policy))

    # -- Identity ------------------------------------------------------

    def _build_identity_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel(
            '<b>Unresolved identities</b> -- entries with ambiguous '
            'matches only. Exact ID links and single exact-title '
            'matches are linked automatically and never appear here. '
            'Right-click a row to inspect it or open it on its site.'))
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
        self.rb_search = QRadioButton('Search manually -- '
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
            'Keep provider-only -- do not sync this entry elsewhere')
        self.rb_defer = QRadioButton(
            'Create provider mappings later -- keep watching for new '
            'matches')
        self.rb_ignore = QRadioButton('Ignore this title -- never ask again')
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
        """Native-script titles get a latin alias alongside, so a user
        whose AniList title language is Native can still tell what a
        row refers to."""
        import re as _re
        title = title or '?'
        if not _re.search('[A-Za-z]', title):
            for alias in aliases or []:
                if alias and _re.search('[A-Za-z]', alias):
                    return '%s  /  %s' % (title, alias)
        return title

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
        self.tabs.setTabText(2, 'Identity (%d)'
                             % self.identity_tree.topLevelItemCount())

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
            '<b>%s</b>%s -- %s entry %s.<br>%s' % (
                self._display_title(issue['title'],
                                    info.get('aliases')),
                year, issue['provider'], issue['provider_id'], why))
        self.candidate_combo.clear()
        for cand in candidates:
            providers = ', '.join('on %s (%s)' % kv
                                  for kv in sorted(
                                      cand.get('providers', {}).items()))
            label = '%s%s -- %s -- %s' % (
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
            'Keep %s-only -- do not sync this entry elsewhere'
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
            if self.tabs.tabText(i).startswith('Inspector'):
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

    # -- Inspector -------------------------------------------------------

    def _build_inspector_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(
            '<b>Identity inspector</b> — punch in one entry by its '
            'provider id and see exactly how multisync resolved it: '
            'what it maps to on your other providers, how each link '
            'was made (a published id, the anime-relations atlas, a '
            'title match, or your own confirmation), and the raw data '
            'recorded for it.')
        intro.setWordWrap(True)
        layout.addWidget(intro)

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
        layout.addLayout(bar)

        self.inspect_output = QTextBrowser()
        self.inspect_output.setOpenExternalLinks(False)
        self.inspect_output.setHtml(
            '<p style="color:gray">Nothing looked up yet.</p>')
        layout.addWidget(self.inspect_output, 1)
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

    def _render_inspection(self, r):
        p = ['<h3>%s id %s</h3>' % (r.provider.capitalize(),
                                    r.provider_id)]
        if not r.found:
            p.append('<p>%s</p>' % r.note)
            issue = r.identity_issue
            if issue and issue.get('candidates'):
                p.append('<p><b>Candidates on file:</b></p><ul>')
                for c in issue['candidates']:
                    providers = ', '.join(
                        '%s:%s' % kv
                        for kv in (c.get('providers') or {}).items()) \
                        or 'no providers yet'
                    p.append('<li>%s%s — %s — <i>%s</i></li>' % (
                        c.get('title') or '?',
                        ' (%s)' % c['year'] if c.get('year') else '',
                        providers, c.get('via', '')))
                p.append('</ul>')
            if r.atlas_hint:
                p.append('<p><b>anime-relations atlas independently '
                         'says:</b> %s</p>' % ', '.join(
                             '%s=%s' % kv for kv in
                             r.atlas_hint.items()))
            return ''.join(p)

        p.append('<p><b>%s</b>%s%s</p>' % (
            r.title or '?', ' (%s)' % r.year if r.year else '',
            ' — pinned provider-only (%s), excluded from cross-'
            'provider sync' % r.provider_only.capitalize()
            if r.provider_only else ''))
        if r.aliases:
            p.append('<p>Also known as: %s</p>'
                     % ', '.join(a for a in r.aliases if a != r.title))
        p.append('<p>Internal id: <code>%s</code></p>' % r.uuid)

        p.append('<h4>Mapped providers</h4>'
                 '<table border="1" cellpadding="4" cellspacing="0">'
                 '<tr><th>Provider</th><th>ID</th><th>Confirmed</th>'
                 '<th>Linked via</th></tr>')
        mapped_ids = {}
        for m in r.mappings:
            mapped_ids[m.provider] = m.provider_id
            p.append('<tr><td>%s</td><td>%s</td><td>%s</td>'
                     '<td>%s</td></tr>' % (
                         m.provider.capitalize(), m.provider_id,
                         'Yes' if m.confirmed else 'Auto',
                         m.via or '—'))
        p.append('</table>')

        if r.atlas_hint:
            mismatches = [
                '%s: mapped %s, atlas says %s'
                % (prov.capitalize(), mapped_ids[prov], hint_id)
                for prov, hint_id in r.atlas_hint.items()
                if prov in mapped_ids and mapped_ids[prov] != hint_id]
            verdict = ('<span style="color:#ff9800">differs from the '
                      'mapping above: %s</span>' % '; '.join(mismatches)
                      if mismatches else
                      'consistent with the mapping above')
            p.append('<p><b>anime-relations atlas independently says:'
                     '</b> %s — %s</p>' % (
                         ', '.join('%s=%s' % kv
                                   for kv in r.atlas_hint.items()),
                         verdict))

        providers = sorted({prov for row in r.fields
                            for prov in row.per_provider})
        rows = [row for row in r.fields
               if row.per_provider or row.local not in (None, [], 0)]
        if rows and providers:
            p.append('<h4>Field data</h4>'
                     '<table border="1" cellpadding="4" cellspacing="0">'
                     '<tr><th>Field</th><th>Local</th>' + ''.join(
                         '<th>%s (remote / last-synced)</th>'
                         % prov.capitalize() for prov in providers)
                     + '</tr>')
            for row in rows:
                cells = ['<td>%s</td>' % _FIELD_LABELS.get(
                             row.field, row.field),
                        '<td>%s</td>' % self._fmt_value(row.field,
                                                        row.local)]
                for prov in providers:
                    pv = row.per_provider.get(prov)
                    cells.append(
                        '<td>—</td>' if pv is None else
                        '<td>%s / %s</td>' % (
                            self._fmt_value(row.field, pv['remote']),
                            self._fmt_value(row.field, pv['base'])))
                p.append('<tr>%s</tr>' % ''.join(cells))
            p.append('</table>')
        return ''.join(p)

    # -- plumbing ------------------------------------------------------

    def _run(self, fn, callback, busy_text, *args,
            forward_progress=False, **kwargs):
        if self._task is not None and self._task.isRunning():
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
