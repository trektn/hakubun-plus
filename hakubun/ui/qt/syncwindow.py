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

from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QRadioButton,
                             QScrollArea, QSplitter, QTabWidget, QTextBrowser,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from hakubun import messenger, utils
from hakubun.sync import normalize
from hakubun.sync.adapters import adapter_from_account
from hakubun.sync.engine import SyncEngine
from hakubun.sync.models import (FieldConflict, FieldPolicy,
                                 NormalizedEntry, PolicyKind, SyncMode,
                                 USER_FIELDS)
from hakubun.sync.store import SyncStore

_FIELD_LABELS = {
    'score': 'Score', 'progress': 'Watched Episodes', 'status': 'Status',
    'notes': 'Notes', 'start_date': 'Start Date',
    'finish_date': 'Finish Date', 'tags': 'Tags', 'favorite': 'Favorites',
}


class _Task(QtCore.QThread):
    """Runs one engine call off the GUI thread."""
    done = QtCore.pyqtSignal(object, object)   # result, error

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
    def __init__(self, parent, accountman, engine=None, active_api=None):
        super().__init__(parent)
        self.setWindowTitle('Multi-provider Sync')
        self.resize(760, 560)
        self._task = None
        self._plan = None

        if engine is not None:
            # Injection seam for tests: a prebuilt SyncEngine (fake
            # adapters, in-memory store).
            self.store = engine.store
            self.engine, self._adapter_errors = engine, []
        else:
            self.store = SyncStore(utils.to_data_path('multisync.db'))
            self.engine, self._adapter_errors = \
                self._build_engine(accountman)
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
            self._status('No provider accounts configured.')
        self._refresh_identity()

    def _build_engine(self, accountman):
        adapters, errors = {}, []
        msg = messenger.Messenger(None, 'Sync')
        for num, account in accountman.get_accounts():
            api = account['api']
            if api in adapters:
                errors.append('%s: only one account per provider is '
                              'supported for now' % api)
                continue
            try:
                adapters[api] = adapter_from_account(account, msg)
            except Exception as e:
                errors.append('%s: %s' % (api, e))
        from hakubun.sync.relations import RelationsAtlas
        return SyncEngine(self.store, adapters,
                          relations=RelationsAtlas.from_file()), errors

    # -- Preview -------------------------------------------------------

    def _build_preview_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        bar = QHBoxLayout()
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
        self.reset_button = QPushButton('Reset database...')
        self.reset_button.setToolTip(
            'Wipe the sync database (identities, mappings, history, '
            'ownership) and start clean. Your provider lists are never '
            'touched; the next Fetch re-derives everything.')
        self.reset_button.clicked.connect(self.s_reset)
        bar.addWidget(self.fetch_button)
        bar.addWidget(self.apply_button)
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

        self.preview_tree = QTreeWidget()
        self.preview_tree.setHeaderHidden(True)
        splitter.addWidget(self.preview_tree)

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
            value = normalize.provider_score(
                value, info.get('score_max', 10), info.get('score_step', 1))
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

    def _conflict_why(self, conflict):
        """Explain why this needs a human, not just what differs."""
        label = _FIELD_LABELS.get(conflict.field, conflict.field)
        others = sorted(s for s in conflict.values if s != 'local')
        sides = ' and '.join(
            ['your side (%s)' % self._fmt_value(conflict.field,
                                                conflict.values['local'])]
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
            text = ('Keep yours: %s' % shown if source == 'local'
                    else 'Use %s: %s' % (source.capitalize(), shown))
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
        for i in range(self.preview_tree.topLevelItemCount()):
            group = self.preview_tree.topLevelItem(i)
            for j in range(group.childCount()):
                child = group.child(j)
                data = child.data(0, QtCore.Qt.ItemDataRole.UserRole)
                if data is not None and not isinstance(data,
                                                       FieldConflict):
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
        self.preview_tree.clear()
        # The transaction plan: one box per show, its pulls/pushes as
        # directional rows. Decisions get their own panel below.
        groups = {}
        for change in plan.changes:
            groups.setdefault(change.uuid,
                              [change.title, []])[1].append(change)
        bold = QtGui.QFont()
        bold.setBold(True)
        for uid, (show_title, changes) in sorted(
                groups.items(), key=lambda kv: kv[1][0].casefold()):
            group = QTreeWidgetItem(['%s    —  %d change(s)'
                                     % (show_title, len(changes))])
            group.setFont(0, bold)
            group.setFlags(group.flags()
                           | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                           | QtCore.Qt.ItemFlag.ItemIsAutoTristate)
            group.setCheckState(0, QtCore.Qt.CheckState.Checked)
            for change in changes:
                group.addChild(self._change_item(change))
            self.preview_tree.addTopLevelItem(group)
        self.preview_tree.expandAll()
        self._set_decisions(plan.conflicts)
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
        self._run(self.engine.apply, self.r_applied,
                  'Applying changes...', plan)

    def r_applied(self, result, error):
        if error is not None:
            self._status('Apply failed: %s' % error)
            return
        text = 'Applied: %d local change(s), %d push(es)' % (
            result['local'], result['pushed'])
        if result['errors']:
            text += ' -- failed: %s' % ', '.join(
                '%s (%s)' % kv for kv in result['errors'].items())
            text += ' (their changes stay planned)'
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
        self.preview_tree.clear()
        self._set_decisions([])
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
            'matches are linked automatically and never appear here.'))
        self.identity_tree = QTreeWidget()
        self.identity_tree.setHeaderLabels(
            ['Provider', 'Title', 'Also known as', 'Status'])
        self.identity_tree.setRootIsDecorated(False)
        self.identity_tree.currentItemChanged.connect(
            self._identity_selected)
        layout.addWidget(self.identity_tree, 2)

        self.identity_box = QGroupBox('Resolution')
        box_layout = QVBoxLayout(self.identity_box)
        self.identity_info = QLabel()
        self.identity_info.setWordWrap(True)
        box_layout.addWidget(self.identity_info)
        self.rb_confirm = QRadioButton('Use a matched entry')
        self.candidate_combo = QComboBox()
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
        self.search_results = QComboBox()
        self.rb_provider_only = QRadioButton(
            'Keep provider-only -- do not sync this entry elsewhere')
        self.rb_defer = QRadioButton(
            'Create provider mappings later -- keep watching for new '
            'matches')
        self.rb_ignore = QRadioButton('Ignore this title -- never ask again')
        for w in (self.rb_confirm, self.candidate_combo, self.rb_search):
            box_layout.addWidget(w)
        box_layout.addLayout(search_bar)
        box_layout.addWidget(self.search_results)
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
            item = QTreeWidgetItem([issue['provider'], display,
                                    ' / '.join(others[:2]),
                                    issue['status']])
            tip = '\n'.join(filter(None, [
                '%s id %s' % (issue['provider'], issue['provider_id']),
                'Year: %s' % info['year'] if info.get('year') else None,
                'All titles: %s' % ', '.join(
                    [issue['title'] or ''] + others) if others else None]))
            for col in range(4):
                item.setToolTip(col, tip)
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, issue)
            self.identity_tree.addTopLevelItem(item)
        for col in range(3):
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
            self.candidate_combo.addItem(label, cand['uuid'])
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
        self._status('Found %d result(s).' % (len(results or [])))

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
                uid = self.candidate_combo.currentData()
                if uid is None:
                    return
                identity.resolve_conflict(issue['id'], 'confirm',
                                          target_uuid=uid)
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

    def _run(self, fn, callback, busy_text, *args, **kwargs):
        if self._task is not None and self._task.isRunning():
            self._status('Another sync operation is still running.')
            return
        self._status(busy_text)
        self.fetch_button.setEnabled(False)
        task = _Task(fn, *args, **kwargs)

        def done(result, error):
            # The signal is emitted from inside run(), so the thread may
            # not have fully stopped yet -- wait for it, or a chained
            # _run() from the callback (e.g. the auto-replan after
            # apply) would see isRunning() and silently drop.
            task.wait()
            self.fetch_button.setEnabled(True)
            callback(result, error)
        task.done.connect(done)
        self._task = task
        task.start()

    def _status(self, text):
        self.status_label.setText(text)

    def closeEvent(self, event):
        if self._task is not None and self._task.isRunning():
            self._task.wait()
        self.store.close()
        super().closeEvent(event)
