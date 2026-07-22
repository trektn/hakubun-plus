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

from PyQt6 import QtCore
from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPushButton, QRadioButton,
                             QTabWidget, QTreeWidget, QTreeWidgetItem,
                             QVBoxLayout, QWidget)

from hakubun import messenger, utils
from hakubun.sync.adapters import adapter_from_account
from hakubun.sync.engine import SyncEngine
from hakubun.sync.models import (FieldPolicy, NormalizedEntry, PolicyKind,
                                 SyncMode, USER_FIELDS)
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
    def __init__(self, parent, accountman, engine=None):
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
        return SyncEngine(self.store, adapters), errors

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
        bar.addWidget(self.fetch_button)
        bar.addWidget(self.apply_button)
        layout.addLayout(bar)

        layout.addWidget(QLabel('<b>Changes</b> (uncheck to skip):'))
        self.changes_tree = QTreeWidget()
        self.changes_tree.setHeaderLabels(['Show', 'Change'])
        self.changes_tree.setRootIsDecorated(False)
        layout.addWidget(self.changes_tree, 3)

        layout.addWidget(QLabel('<b>Conflicts</b> (resolve before they '
                                'sync):'))
        self.conflicts_tree = QTreeWidget()
        self.conflicts_tree.setHeaderLabels(['Show', 'Conflict'])
        self.conflicts_tree.setRootIsDecorated(False)
        layout.addWidget(self.conflicts_tree, 2)
        resolve_bar = QHBoxLayout()
        resolve_bar.addStretch()
        self.resolve_button = QPushButton('Resolve...')
        self.resolve_button.clicked.connect(self.s_resolve)
        resolve_bar.addWidget(self.resolve_button)
        layout.addLayout(resolve_bar)
        return page

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
        self.changes_tree.clear()
        for change in plan.changes:
            item = QTreeWidgetItem([change.title, change.describe()])
            item.setFlags(item.flags()
                          | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, QtCore.Qt.CheckState.Checked)
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, change)
            self.changes_tree.addTopLevelItem(item)
        self.conflicts_tree.clear()
        for conflict in plan.conflicts:
            item = QTreeWidgetItem([conflict.title, conflict.describe()])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, conflict)
            self.conflicts_tree.addTopLevelItem(item)
        for tree in (self.changes_tree, self.conflicts_tree):
            tree.resizeColumnToContents(0)
        self.apply_button.setEnabled(bool(plan.changes))
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
        for i in range(self.changes_tree.topLevelItemCount()):
            item = self.changes_tree.topLevelItem(i)
            change = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
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

    def s_resolve(self):
        item = self.conflicts_tree.currentItem()
        if item is None:
            return
        conflict = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        options = list(conflict.values.items())
        labels = ['%s: %s' % (source, value) for source, value in options]
        box = QMessageBox(self)
        box.setWindowTitle('Resolve conflict')
        box.setText('%s -- %s\nKeep which value?'
                    % (conflict.title, conflict.field))
        buttons = [box.addButton(label, QMessageBox.ButtonRole.AcceptRole)
                   for label in labels]
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        for (source, _), button in zip(options, buttons):
            if button is clicked:
                self.engine.resolve_conflict(conflict, source)
                self._status('Resolved %s (%s wins); replan to sync it.'
                             % (conflict.field, source))
                self.s_fetch()
                return

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
        providers = list(self.engine.adapters)
        columns = ([('local', 'Local')]
                   + [('provider:%s' % p, p.capitalize()) for p in providers]
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
        layout.addWidget(QLabel('<b>Unresolved identities</b> -- entries '
                                'that could not be linked safely:'))
        self.identity_tree = QTreeWidget()
        self.identity_tree.setHeaderLabels(['Provider', 'Title', 'Status'])
        self.identity_tree.setRootIsDecorated(False)
        self.identity_tree.currentItemChanged.connect(
            self._identity_selected)
        layout.addWidget(self.identity_tree, 2)

        self.identity_box = QGroupBox('Resolution')
        box_layout = QVBoxLayout(self.identity_box)
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

    def _refresh_identity(self):
        self.identity_tree.clear()
        for issue in self.store.identity_open():
            item = QTreeWidgetItem([issue['provider'], issue['title'] or '?',
                                    issue['status']])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, issue)
            self.identity_tree.addTopLevelItem(item)
        self.identity_tree.resizeColumnToContents(0)
        self.tabs.setTabText(2, 'Identity (%d)'
                             % self.identity_tree.topLevelItemCount())

    def _identity_selected(self, item, _previous=None):
        self.identity_box.setEnabled(item is not None)
        if item is None:
            return
        issue = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        self.candidate_combo.clear()
        for cand in issue['candidates']:
            label = '%s (%s) via %s' % (
                cand.get('title'), ', '.join(
                    '%s:%s' % kv for kv in cand.get('providers',
                                                    {}).items()),
                cand.get('via'))
            self.candidate_combo.addItem(label, cand['uuid'])
        self.rb_confirm.setEnabled(bool(issue['candidates']))
        if issue['candidates']:
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
