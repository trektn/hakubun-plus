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
                             QScrollArea, QTabWidget, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget)

from hakubun import messenger, utils
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
        self.preview_tree = QTreeWidget()
        self.preview_tree.setHeaderHidden(True)
        layout.addWidget(self.preview_tree, 3)

        # Decisions live in their own box: the plan above is the bulk
        # transaction; this explains WHY each item needs a human and
        # takes the answer inline.
        self.decisions_box = QGroupBox('Needs your decision')
        decisions_outer = QVBoxLayout(self.decisions_box)
        self.decisions_scroll = QScrollArea()
        self.decisions_scroll.setWidgetResizable(True)
        holder = QWidget()
        self.decisions_layout = QVBoxLayout(holder)
        self.decisions_layout.addStretch()
        self.decisions_scroll.setWidget(holder)
        decisions_outer.addWidget(self.decisions_scroll)
        self.decisions_box.setVisible(False)
        layout.addWidget(self.decisions_box, 2)

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

    def _change_item(self, change):
        label = _FIELD_LABELS.get(change.field, change.field)
        values = '%s → %s' % (self._fmt_value(change.field, change.old),
                              self._fmt_value(change.field, change.new))
        if change.target == 'local':
            text = '⬇ Pull from %s — %s: %s' % (
                change.source.capitalize(), label, values)
            color = self._PULL_COLOR
        else:
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
        self._clear_decisions()
        for conflict in sorted(plan.conflicts,
                               key=lambda c: c.title.casefold()):
            self.decisions_layout.insertWidget(
                self.decisions_layout.count() - 1,
                self._decision_card(conflict))
        self.decisions_box.setVisible(bool(plan.conflicts))
        self.decisions_box.setTitle(
            'Needs your decision (%d)' % len(plan.conflicts))
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
        self._clear_decisions()
        self.decisions_box.setVisible(False)
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
        for col, (_, label) in enumerate(columns, start=1):
            grid.addWidget(QLabel('<b>%s</b>' % label), 0, col)
        self._ownership_groups = {}
        ownership = self.store.ownership()
        for row, field in enumerate(USER_FIELDS, start=1):
            grid.addWidget(QLabel(_FIELD_LABELS.get(field, field)), row, 0)
            group = QButtonGroup(page)
            current = ownership[field].serialize()
            for col, (policy_text, _) in enumerate(columns, start=1):
                radio = QRadioButton()
                radio.setProperty('policy', policy_text)
                radio.setProperty('field', field)
                if policy_text == current:
                    radio.setChecked(True)
                radio.toggled.connect(self._ownership_changed)
                group.addButton(radio)
                grid.addWidget(radio, row, col,
                               QtCore.Qt.AlignmentFlag.AlignHCenter)
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
