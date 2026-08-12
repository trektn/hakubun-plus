"""Qt SyncWindow Mirror tab (no display needed).

Mirror is a deliberate, potentially large operation, so the properties
worth pinning in the UI are the safety ones: nothing is applied without
an explicit confirmation, additions and removals are approved
independently, and the tracker-facing views never name Hakubun as a
tracker.

The LAYOUT itself -- one card per title, what each row says -- is
toolkit-agnostic and tested once in tests/sync/test_mirror_cards.py.
What is tested here is the wiring: that the cards reach the widgets,
that ticking a row reaches the operation behind it, and that the
membership gestures still land on the right object.
"""

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6 import QtCore
from PyQt6.QtWidgets import QApplication

from hakubun.sync.mirror import (AddOperation, MembershipIssue, MirrorPlan,
                                 RemoveOperation)
from hakubun.sync.models import FieldPolicy, SyncOperation
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
    plan = MirrorPlan(ownership={'score':
                                 FieldPolicy.parse('provider:anilist')})
    plan.desired['u1'] = {'score': (5.0, 'anilist', 'Anilist owns score')}
    plan.observed['u1'] = {'anilist': {'score': 5.0},
                           'mal': {'score': 2.0}}
    plan.membership.append(MembershipIssue(
        uuid='u1', title='GHOST', present=['anilist', 'mal'],
        missing=['kitsu'], addable=['kitsu'],
        values={'score': 5.0}))
    plan.adds.append(AddOperation(
        'u1', 'kitsu', title='GHOST', values={'score': 5.0},
        provenance=('anilist', 'mal'), selected=False))
    plan.removes.append(RemoveOperation('u2', 'kitsu', title='Doomed'))
    plan.updates.append(SyncOperation('u1', 'score', 2.0, 5.0,
                                      target='mal', source='anilist',
                                      title='GHOST',
                                      reason='Anilist owns score'))
    plan.local.append(SyncOperation('u1', 'score', 2.0, 5.0,
                                    target='local', source='anilist',
                                    title='GHOST'))
    return plan


def _walk(item):
    """Every text under one tree item, including the item itself."""
    out = [item.text(0)]
    for i in range(item.childCount()):
        out += _walk(item.child(i))
    return out


def _texts(tree):
    out = []
    for i in range(tree.topLevelItemCount()):
        out += _walk(tree.topLevelItem(i))
    return out


def _items(item):
    out = [item]
    for i in range(item.childCount()):
        out += _items(item.child(i))
    return out


def _all_items(tree):
    out = []
    for i in range(tree.topLevelItemCount()):
        out += _items(tree.topLevelItem(i))
    return out


def _card_named(tree, title):
    return next(tree.topLevelItem(i)
                for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).text(0).startswith(title))


def test_mirror_tab_exists_after_sync(window):
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles[:2] == ['Sync', 'Mirror']


def test_the_preview_is_one_card_per_title(window):
    """Not five tabs of operations: the unit on screen is the thing the
    user is deciding about."""
    window.r_mirror_planned(_plan(), None)
    tree = window.mirror_tree
    titles = [tree.topLevelItem(i).text(0)
              for i in range(tree.topLevelItemCount())]
    assert any(t.startswith('GHOST') for t in titles)
    assert any(t.startswith('Doomed') for t in titles)


def test_a_card_carries_the_ownership_row_and_every_tracker(window):
    window.r_mirror_planned(_plan(), None)
    texts = _walk(_card_named(window.mirror_tree, 'GHOST'))
    assert any('Ownership says' in t for t in texts)
    assert any(t.strip().startswith('Anilist  ✓') for t in texts)
    assert any(t.strip().startswith('Kitsu  ✗') for t in texts)


def test_the_filter_narrows_without_hiding_the_total(window):
    """The old tabs survive as a filter -- what they were actually good
    for -- and the count says what is being hidden."""
    window.r_mirror_planned(_plan(), None)
    total = window.mirror_tree.topLevelItemCount()
    index = [i for i in range(window.mirror_filter.count())
             if window.mirror_filter.itemData(i) == 'remove'][0]
    window.mirror_filter.setCurrentIndex(index)
    assert window.mirror_tree.topLevelItemCount() < total
    assert 'of %d' % total in window.mirror_filter_count.text()


def test_tracker_rows_never_name_hakubun(window):
    """Local convergence is disclosed, but never as a TRACKER."""
    window.r_mirror_planned(_plan(), None)
    card = _card_named(window.mirror_tree, 'GHOST')
    tracker_rows = [card.child(i).text(0)
                    for i in range(card.childCount())
                    if '✓' in card.child(i).text(0)
                    or '✗' in card.child(i).text(0)]
    assert tracker_rows
    for text in tracker_rows:
        assert 'Hakubun' not in text


def test_local_convergence_is_disclosed_and_untickable(window):
    """Mirror overwrites a pending local edit -- it does not read the
    bases that would tell it one was made. That must be visible and
    refusable, not silent."""
    window.r_mirror_planned(_plan(), None)
    assert any('Hakubun' in t for t in _texts(window.mirror_tree))
    item = next(i for i in _all_items(window.mirror_tree)
                if getattr(i.data(0, QtCore.Qt.ItemDataRole.UserRole),
                           'target', None) == 'local')
    op = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
    item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
    assert op.selected is False


def test_creating_an_entry_is_a_single_tickable_row(window):
    """One decision per entry. Ticking a subset of an entry's fields is
    not something the user can express, because apply() would create a
    half-formed entry from it."""
    window.r_mirror_planned(_plan(), None)
    add_items = [i for i in _all_items(window.mirror_tree)
                 if isinstance(i.data(0, QtCore.Qt.ItemDataRole.UserRole),
                               AddOperation)]
    assert len(add_items) == 1
    assert 'Create this entry on Kitsu' in add_items[0].text(0)


def test_adds_and_removes_start_unticked(window):
    window.r_mirror_planned(_plan(), None)
    for item in _all_items(window.mirror_tree):
        op = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(op, (AddOperation, RemoveOperation)):
            assert op.selected is False


def test_ticking_a_card_ticks_everything_under_it(window):
    """"Yes to this show" is one click, not one per field."""
    window.r_mirror_planned(_plan(), None)
    card = _card_named(window.mirror_tree, 'GHOST')
    card.setCheckState(0, QtCore.Qt.CheckState.Checked)
    ops = [i.data(0, QtCore.Qt.ItemDataRole.UserRole)
           for i in _items(card)]
    ops = [o for o in ops if o is not None and hasattr(o, 'selected')]
    assert ops and all(o.selected for o in ops)


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


def test_membership_right_click_still_reaches_the_membership_menu(
        window, monkeypatch):
    """_mirror_context_menu branches on a stored resolution before it
    reads the membership payload. Tracker rows carry the issue at
    UserRole+1 and nothing at UserRole, so the branch must fall
    through -- this is the gesture the whole removal workflow depends
    on."""
    import hakubun.ui.qt.syncwindow as mod

    window.r_mirror_planned(_plan(), None)
    tree = window.mirror_tree
    card = _card_named(tree, 'GHOST')
    kitsu_row = next(card.child(i) for i in range(card.childCount())
                     if card.child(i).text(0).startswith('Kitsu'))

    actions = []
    monkeypatch.setattr(mod.QMenu, 'exec', lambda self, pos=None:
                        actions.extend(a.text() for a in self.actions()))
    monkeypatch.setattr(tree, 'itemAt', lambda pos: kitsu_row)
    window._mirror_context_menu(tree, QtCore.QPoint(0, 0))

    # Kitsu lacks the entry, so the offer is to add or to leave it --
    # and 'Remove from' is correctly absent for a tracker with no entry.
    assert 'Add to Kitsu' in actions
    assert 'Leave Kitsu as it is' in actions
    assert not any(a.startswith('Remove from') for a in actions)


def test_membership_right_click_on_a_holder_offers_removal(window,
                                                           monkeypatch):
    import hakubun.ui.qt.syncwindow as mod

    window.r_mirror_planned(_plan(), None)
    tree = window.mirror_tree
    card = _card_named(tree, 'GHOST')
    mal_row = next(card.child(i) for i in range(card.childCount())
                   if card.child(i).text(0).startswith('Mal'))

    actions = []
    monkeypatch.setattr(mod.QMenu, 'exec', lambda self, pos=None:
                        actions.extend(a.text() for a in self.actions()))
    monkeypatch.setattr(tree, 'itemAt', lambda pos: mal_row)
    window._mirror_context_menu(tree, QtCore.QPoint(0, 0))

    assert 'Remove from Mal' in actions
    assert 'Add to Mal' not in actions


def test_right_clicking_a_resolved_row_offers_the_way_back(window,
                                                           monkeypatch):
    import hakubun.ui.qt.syncwindow as mod

    plan = _plan()
    plan.updates[0].reason = 'you chose this value for these trackers'
    window.r_mirror_planned(plan, None)

    tree = window.mirror_tree
    row = next(i for i in _all_items(tree)
               if getattr(i.data(0, QtCore.Qt.ItemDataRole.UserRole),
                          'target', None) == 'mal')
    actions = []
    monkeypatch.setattr(mod.QMenu, 'exec', lambda self, pos=None:
                        actions.extend(a.text() for a in self.actions()))
    monkeypatch.setattr(tree, 'itemAt', lambda pos: row)
    window._mirror_context_menu(tree, QtCore.QPoint(0, 0))

    assert actions == ['Ask me about Score again']
