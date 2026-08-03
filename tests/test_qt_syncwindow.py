"""Qt SyncWindow preview-tab incremental replan (no display needed).

Skipped where PyQt6 isn't installed. A replan (engine.plan(), fired after
every conflict resolution -- see SyncWindow._replan) used to tear down
and rebuild every preview tab from scratch, discarding every tick the
user had made anywhere in the window even when only one field on one
show actually changed. r_planned's category tabs are now diffed by
content instead: a category whose change set is unchanged (matched by
_reconcile_changes) keeps its widgets, items and tick state untouched.
"""

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6 import QtCore
from PyQt6.QtWidgets import QApplication

from hakubun.sync.models import FieldChange, SyncMode, SyncPlan
from hakubun.sync.store import SyncStore
from hakubun.ui.qt.syncwindow import SyncWindow


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


class _FakeEngine:
    def __init__(self):
        self.store = SyncStore(':memory:')
        self.adapters = {}
        self.primary = None


def _score_changes(n, bump_uuid=None):
    return [
        FieldChange(uuid='u%d' % i, field='score', old=1,
                   new=(3 if bump_uuid == 'u%d' % i else 2),
                   target='mal', source='local', title='Show %d' % i)
        for i in range(n)
    ]


def _progress_changes(n):
    return [
        FieldChange(uuid='p%d' % i, field='progress', old=1, new=2,
                   target='mal', source='local', title='Prog %d' % i)
        for i in range(n)
    ]


def _make_window(qapp):
    return SyncWindow(None, None, engine=_FakeEngine(), active_api=None,
                      media_type='anime')


def test_identical_replan_leaves_widgets_and_ticks_untouched(qapp):
    win = _make_window(qapp)
    score = _score_changes(5)
    progress = _progress_changes(3)
    win.r_planned(SyncPlan(mode=SyncMode.MERGE, changes=score + progress),
                  None)

    score_tree = win._category_trees['score']
    item, change = next(iter(win._iter_tree_changes(score_tree)))
    item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
    item_ids_before = {id(it) for it, _c in win._iter_tree_changes(score_tree)}

    # A replan producing content-identical but freshly-allocated objects
    # (exactly what SyncEngine.plan() does every time).
    win.r_planned(SyncPlan(mode=SyncMode.MERGE, changes=(
        _score_changes(5) + _progress_changes(3))), None)

    assert win._category_trees['score'] is score_tree
    item_ids_after = {id(it) for it, _c in win._iter_tree_changes(score_tree)}
    assert item_ids_before == item_ids_after
    assert change.selected is False


def test_changed_category_rebuilds_only_itself(qapp):
    win = _make_window(qapp)
    win.r_planned(SyncPlan(mode=SyncMode.MERGE, changes=(
        _score_changes(5) + _progress_changes(3))), None)
    score_items_1 = {id(it) for it, _c
                     in win._iter_tree_changes(win._category_trees['score'])}
    progress_items_1 = {id(it) for it, _c in win._iter_tree_changes(
        win._category_trees['progress'])}

    win.r_planned(SyncPlan(mode=SyncMode.MERGE, changes=(
        _score_changes(5, bump_uuid='u0') + _progress_changes(3))), None)

    score_items_2 = {id(it) for it, _c
                     in win._iter_tree_changes(win._category_trees['score'])}
    progress_items_2 = {id(it) for it, _c in win._iter_tree_changes(
        win._category_trees['progress'])}
    assert score_items_2 != score_items_1        # rebuilt: value changed
    assert progress_items_2 == progress_items_1   # untouched: unrelated field


def test_change_items_registration_stays_consistent(qapp):
    win = _make_window(qapp)
    win.r_planned(SyncPlan(mode=SyncMode.MERGE, changes=(
        _score_changes(5) + _progress_changes(3))), None)
    win.r_planned(SyncPlan(mode=SyncMode.MERGE, changes=(
        _score_changes(5, bump_uuid='u2') + _progress_changes(3))), None)

    all_tree = win._category_trees['__all__']
    total = sum(1 for _ in win._iter_tree_changes(all_tree))
    assert total == 8 == len(win._change_items)

    # twin mirroring (All <-> category tab) still holds post-incremental-update
    all_item, all_change = next(
        (it, c) for it, c in win._iter_tree_changes(all_tree) if c.uuid == 'u2')
    score_item, score_change = next(
        (it, c) for it, c
        in win._iter_tree_changes(win._category_trees['score'])
        if c.uuid == 'u2')
    assert all_change is score_change
    all_item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
    assert score_item.checkState(0) == QtCore.Qt.CheckState.Unchecked


def test_category_appearing_or_vanishing_falls_back_to_full_rebuild(qapp):
    win = _make_window(qapp)
    score = _score_changes(5)
    win.r_planned(SyncPlan(mode=SyncMode.MERGE, changes=score), None)
    assert win.preview_tabs.count() == 2   # All, Score

    win.r_planned(SyncPlan(mode=SyncMode.MERGE, changes=(
        score + _progress_changes(2))), None)
    assert win.preview_tabs.count() == 3   # All, Score, Watched Episodes
    assert win._category_order == ['__all__', 'score', 'progress']
