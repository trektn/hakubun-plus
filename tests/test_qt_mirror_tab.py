"""Qt SyncWindow Mirror tab (no display needed).

Mirror is a deliberate, potentially large operation, so the properties
worth pinning in the UI are the safety ones: nothing is applied without
an explicit confirmation, additions and removals are approved
independently, and the tracker-facing views never name Hakubun as a
tracker.
"""

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6 import QtCore
from PyQt6.QtWidgets import QApplication

from hakubun.sync.mirror import MembershipIssue, MirrorPlan, RemoveOperation
from hakubun.sync.models import SyncOperation
from hakubun.sync.store import SyncStore
from hakubun.ui.qt.syncwindow import SyncWindow


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeEngine:
    def __init__(self):
        self.store = SyncStore(':memory:')
        self.adapters = {}
        self.primary = None
        self.applied = []

    def apply_mirror(self, plan, allow_adds=False, allow_removes=False,
                     **kwargs):
        self.applied.append((allow_adds, allow_removes))
        return {'local': 0, 'pushed': 0, 'removed': 0, 'skipped': 0,
                'errors': {}, 'cancelled': False}


@pytest.fixture
def window(qapp):
    win = SyncWindow(None, None, engine=_FakeEngine(), active_api=None,
                     media_type='anime')
    yield win
    win.close()


def _plan():
    plan = MirrorPlan()
    plan.membership.append(MembershipIssue(
        uuid='u1', title='GHOST', present=['anilist', 'mal'],
        missing=['kitsu'], addable=['kitsu'],
        values={'score': 5.0}))
    plan.adds.append(SyncOperation('u1', 'score', None, 5.0,
                                   target='kitsu', source='anilist',
                                   title='GHOST', selected=False,
                                   creates_entry=True,
                                   provenance=('anilist', 'mal')))
    plan.removes.append(RemoveOperation('u2', 'kitsu', title='Doomed'))
    plan.updates.append(SyncOperation('u1', 'score', 2.0, 5.0,
                                      target='mal', source='anilist',
                                      title='GHOST',
                                      reason='Anilist owns score'))
    plan.local.append(SyncOperation('u1', 'score', 2.0, 5.0,
                                    target='local', source='anilist',
                                    title='GHOST'))
    return plan


def test_mirror_tab_exists_after_sync(window):
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles[:2] == ['Sync', 'Mirror']


def test_categories_are_counted_separately(window):
    window.r_mirror_planned(_plan(), None)
    titles = [window.mirror_tabs.tabText(i)
              for i in range(window.mirror_tabs.count())]
    assert titles == ['Tracker membership (1)', 'Entries to add (1)',
                      'Entries to remove (1)', 'Fields to update (1)']


def test_membership_view_shows_the_tracker_matrix(window):
    window.r_mirror_planned(_plan(), None)
    tree = window._mirror_trees['membership']
    group = tree.topLevelItem(0)
    rows = [group.child(i).text(0) for i in range(group.childCount())]
    assert any(r.startswith('Anilist  ✓') for r in rows)
    assert any(r.startswith('Kitsu  ✗') for r in rows)
    # The discrepancy is stated between trackers, never as a transfer
    # from the app.
    assert not any('Hakubun' in r for r in rows)


def test_local_convergence_is_never_shown_as_a_tracker_row(window):
    """MirrorPlan.local is applied but not displayed: it is
    reconciliation state, not one of the trackers being mirrored."""
    window.r_mirror_planned(_plan(), None)
    for tree in window._mirror_trees.values():
        for i in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(i)
            texts = [group.text(0)] + [group.child(j).text(0)
                                       for j in range(group.childCount())]
            assert not any('Hakubun' in t for t in texts)
            assert not any('local' in t for t in texts)


def test_adds_and_removes_start_unticked(window):
    window.r_mirror_planned(_plan(), None)
    for key in ('add', 'remove'):
        tree = window._mirror_trees[key]
        group = tree.topLevelItem(0)
        for i in range(group.childCount()):
            op = group.child(i).data(
                0, QtCore.Qt.ItemDataRole.UserRole)
            assert op.selected is False


def test_apply_is_refused_without_confirmation(window, monkeypatch):
    """The confirmation is not advisory: declining it applies
    nothing."""
    window.r_mirror_planned(_plan(), None)

    import hakubun.ui.qt.syncwindow as mod
    monkeypatch.setattr(mod.QMessageBox, 'exec', lambda self: None)
    window.s_mirror_apply()
    assert window.engine.applied == []


def test_gates_are_passed_through_independently(window, monkeypatch):
    import hakubun.ui.qt.syncwindow as mod

    def accept(box):
        # Click the Apply button the dialog just built.
        box.clickedButton = lambda: box.buttons()[0]

    monkeypatch.setattr(mod.QMessageBox, 'exec', accept)

    for adds, removes in ((False, False), (True, False), (False, True),
                          (True, True)):
        window.r_mirror_planned(_plan(), None)
        window.mirror_allow_adds.setChecked(adds)
        window.mirror_allow_removes.setChecked(removes)
        window.s_mirror_apply()
        if window._task is not None:
            window._task.wait()
    assert window.engine.applied == [(False, False), (True, False),
                                     (False, True), (True, True)]


def test_summary_reads_in_tracker_terms(window):
    window.r_mirror_planned(_plan(), None)
    text = window.mirror_summary.text()
    assert 'entry to add' in text and 'entry to remove' in text
    assert 'Hakubun' not in text
