"""GTK MultiSyncWindow Mirror tab -- parity with the Qt twin.

Both windows delegate their wording AND their layout to
sync/present.py -- the card model is tested once, toolkit-free, in
tests/sync/test_mirror_cards.py. What is checked here is that the GTK
side wires the same structure to it: cards reach the store, ticking a
card reaches the operations under it, adds and removes are opt-in, and
the apply path cannot run without the confirmation.
Skipped without GTK/display.
"""

import pytest

gi = pytest.importorskip('gi')
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk  # noqa: E402,F401

if Gdk.Display.get_default() is None:
    pytest.skip('no display available for GTK window tests',
                allow_module_level=True)

from hakubun.sync.mirror import (AddOperation,  # noqa: E402
                                 MembershipIssue, MirrorPlan,
                                 RemoveOperation)
from hakubun.sync.models import FieldPolicy, SyncOperation  # noqa: E402
from hakubun.sync.store import SyncStore  # noqa: E402
from hakubun.ui.gtk.multisyncwindow import (  # noqa: E402
    MultiSyncWindow, _C_ACTIVE, _C_CHANGE, _C_LABEL)


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
def win():
    window = MultiSyncWindow(engine=_FakeEngine())
    yield window
    window.destroy()


def _plan():
    plan = MirrorPlan(ownership={'score':
                                 FieldPolicy.parse('provider:anilist')})
    plan.desired['u1'] = {'score': (5.0, 'anilist', 'Anilist owns score')}
    plan.observed['u1'] = {'anilist': {'score': 5.0},
                           'mal': {'score': 2.0}}
    plan.membership.append(MembershipIssue(
        uuid='u1', title='GHOST', present=['anilist', 'mal'],
        missing=['kitsu'], addable=['kitsu'], values={'score': 5.0}))
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


def _rows(store):
    out = []

    def walk(it):
        while it is not None:
            out.append(store.get_value(it, _C_LABEL))
            walk(store.iter_children(it))
            it = store.iter_next(it)

    walk(store.get_iter_first())
    return out


def test_mirror_tab_sits_after_sync(win):
    titles = [win._notebook.get_tab_label_text(win._notebook.get_nth_page(i))
              for i in range(win._notebook.get_n_pages())]
    assert titles[:2] == ['Sync', 'Mirror']


def test_the_preview_is_one_card_per_title(win):
    """Not five pages of operations: the unit on screen is the thing
    the user is deciding about."""
    win.r_mirror_planned(_plan(), None)
    store = win._mirror_store
    titles = []
    it = store.get_iter_first()
    while it is not None:
        titles.append(store.get_value(it, _C_LABEL))
        it = store.iter_next(it)
    assert any(t.startswith('GHOST') for t in titles)
    assert any(t.startswith('Doomed') for t in titles)


def test_a_card_carries_the_ownership_row_and_every_tracker(win):
    win.r_mirror_planned(_plan(), None)
    rows = _rows(win._mirror_store)
    assert any('Ownership says' in r for r in rows)
    assert any(r.startswith('Anilist  ✓') for r in rows)
    assert any(r.startswith('Kitsu  ✗') for r in rows)


def test_the_filter_narrows_without_hiding_the_total(win):
    """The old pages survive as a filter -- what they were actually
    good for -- and the count says what is being hidden."""
    win.r_mirror_planned(_plan(), None)
    total = win._mirror_store.iter_n_children(None)
    win.mirror_filter.set_active_id('remove')
    assert win._mirror_store.iter_n_children(None) < total
    assert 'of %d' % total in win.mirror_filter_count.get_text()


def test_tracker_rows_never_name_hakubun(win):
    """Local convergence is disclosed, but never as a TRACKER."""
    win.r_mirror_planned(_plan(), None)
    for row in _rows(win._mirror_store):
        if '✓' in row or '✗' in row:
            assert 'Hakubun' not in row


def test_local_convergence_is_disclosed(win):
    """Mirror overwrites a pending local edit; that must be visible."""
    win.r_mirror_planned(_plan(), None)
    assert any('Hakubun' in r for r in _rows(win._mirror_store))


def test_creating_an_entry_is_a_single_tickable_row(win):
    """One decision per entry -- apply() would otherwise be able to
    create a half-formed entry from a ticked subset of its fields."""
    win.r_mirror_planned(_plan(), None)
    store = win._mirror_store
    found = []

    def walk(it):
        while it is not None:
            op = store.get_value(it, _C_CHANGE)
            if isinstance(op, AddOperation):
                found.append(store.get_value(it, _C_LABEL))
            walk(store.iter_children(it))
            it = store.iter_next(it)

    walk(store.get_iter_first())
    assert len(found) == 1
    assert 'Create this entry on Kitsu' in found[0]


def test_adds_and_removes_start_unticked(win):
    win.r_mirror_planned(_plan(), None)
    store = win._mirror_store

    def check(it):
        while it is not None:
            op = store.get_value(it, _C_CHANGE)
            if isinstance(op, (AddOperation, RemoveOperation)):
                assert store.get_value(it, _C_ACTIVE) is False
                assert op.selected is False
            check(store.iter_children(it))
            it = store.iter_next(it)

    check(store.get_iter_first())


def test_ticking_a_card_ticks_everything_under_it(win):
    """"Yes to this show" is one click, not one per field. The cascade
    has to be RECURSIVE: a card's operations sit under its tracker
    rows, so a one-level cascade ticked the tracker rows and left the
    operations alone."""
    win.r_mirror_planned(_plan(), None)
    store = win._mirror_store
    it = next(i for i in _iter_top(store)
              if store.get_value(i, _C_LABEL).startswith('GHOST'))
    win._on_change_toggled(None, store.get_path(it), store)

    ops = []

    def walk(node):
        while node is not None:
            op = store.get_value(node, _C_CHANGE)
            if op is not None and hasattr(op, 'selected'):
                ops.append(op)
            walk(store.iter_children(node))
            node = store.iter_next(node)

    walk(store.iter_children(it))
    assert ops and all(o.selected for o in ops)


def test_apply_is_refused_without_confirmation(win, monkeypatch):
    win.r_mirror_planned(_plan(), None)
    monkeypatch.setattr(Gtk.MessageDialog, 'run',
                        lambda self: Gtk.ResponseType.CANCEL)
    monkeypatch.setattr(Gtk.MessageDialog, 'destroy', lambda self: None)
    win.s_mirror_apply()
    assert win.engine.applied == []


def test_gates_are_passed_through_independently(win, monkeypatch):
    monkeypatch.setattr(Gtk.MessageDialog, 'run',
                        lambda self: Gtk.ResponseType.OK)
    monkeypatch.setattr(Gtk.MessageDialog, 'destroy', lambda self: None)
    for adds, removes in ((False, False), (True, False), (False, True),
                          (True, True)):
        win.r_mirror_planned(_plan(), None)
        win.mirror_allow_adds.set_active(adds)
        win.mirror_allow_removes.set_active(removes)
        win.s_mirror_apply()
        if win._thread is not None:
            win._thread.join()
    assert win.engine.applied == [(False, False), (True, False),
                                  (False, True), (True, True)]


def test_right_clicking_a_tracker_row_reaches_the_membership_menu(
        win, monkeypatch):
    """One tree now serves both gestures, so the handler has to tell a
    tracker row from an operation row. This is the branch the whole
    removal workflow depends on."""
    win.r_mirror_planned(_plan(), None)
    store = win._mirror_store
    path = _path_of(store, lambda label: label.startswith('Kitsu  ✗'),
                    under='GHOST')

    labels = []
    monkeypatch.setattr(Gtk.Menu, 'popup_at_pointer',
                        lambda self, event: labels.extend(
                            c.get_label() for c in self.get_children()))
    view = win._mirror_view
    monkeypatch.setattr(view, 'get_path_at_pos', lambda x, y: (path,))
    event = type('E', (), {'button': 3, 'x': 0.0, 'y': 0.0})()

    assert win._on_mirror_row_button(view, event) is True
    assert 'Add to Kitsu' in labels
    assert 'Leave Kitsu as it is' in labels
    assert not any(l.startswith('Remove from') for l in labels)


def test_right_clicking_a_resolved_row_offers_the_way_back(win,
                                                           monkeypatch):
    """A row produced by a stored decision must reach the handler --
    and must not be mistaken for a tracker row."""
    plan = _plan()
    plan.updates[0].reason = 'you chose this value for these trackers'
    win.r_mirror_planned(plan, None)

    seen = []
    monkeypatch.setattr(win, '_clear_mirror_resolution',
                        lambda op: seen.append(op))
    monkeypatch.setattr(Gtk.Menu, 'popup_at_pointer',
                        lambda self, event: None)

    store = win._mirror_store
    path = _path_of(store, lambda label: label.startswith('Score:'))
    view = win._mirror_view
    monkeypatch.setattr(view, 'get_path_at_pos', lambda x, y: (path,))
    event = type('E', (), {'button': 3, 'x': 0.0, 'y': 0.0})()

    assert win._on_mirror_row_button(view, event) is True


def _iter_top(store):
    it = store.get_iter_first()
    while it is not None:
        yield it
        it = store.iter_next(it)


def _path_of(store, match, under=None):
    """Path of the first row whose label satisfies `match`, optionally
    only within the card whose title starts with `under` -- several
    cards carry a row for the same tracker."""
    found = []

    def walk(it):
        while it is not None:
            if not found and match(store.get_value(it, _C_LABEL)):
                found.append(store.get_path(it))
            walk(store.iter_children(it))
            it = store.iter_next(it)

    if under is not None:
        top = next(i for i in _iter_top(store)
                   if store.get_value(i, _C_LABEL).startswith(under))
        walk(store.iter_children(top))
    else:
        walk(store.get_iter_first())
    assert found, 'no row matched'
    return found[0]
