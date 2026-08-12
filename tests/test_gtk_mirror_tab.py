"""GTK MultiSyncWindow Mirror tab -- parity with the Qt twin.

Both windows delegate their wording to sync/present.py, so what is
worth checking here is that the GTK side wires the same structure to
it: the four categories, the tracker presence matrix, opt-in adds and
removes, and an apply path that cannot run without the confirmation.
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

from hakubun.sync.mirror import (MembershipIssue, MirrorPlan,  # noqa: E402
                                 RemoveOperation)
from hakubun.sync.models import SyncOperation  # noqa: E402
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
    plan = MirrorPlan()
    plan.membership.append(MembershipIssue(
        uuid='u1', title='GHOST', present=['anilist', 'mal'],
        missing=['kitsu'], addable=['kitsu'], values={'score': 5.0}))
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


def test_categories_are_counted_separately(win):
    win.r_mirror_planned(_plan(), None)
    assert win._mirror_page_labels['membership'].get_text() \
        == 'Tracker membership (1)'
    assert win._mirror_page_labels['add'].get_text() == 'Entries to add (1)'
    assert win._mirror_page_labels['remove'].get_text() \
        == 'Entries to remove (1)'
    assert win._mirror_page_labels['update'].get_text() \
        == 'Fields to update (1)'


def test_membership_view_shows_the_tracker_matrix(win):
    win.r_mirror_planned(_plan(), None)
    rows = _rows(win._mirror_stores['membership'])
    assert any(r.startswith('Anilist  ✓') for r in rows)
    assert any(r.startswith('Kitsu  ✗') for r in rows)


def test_no_tracker_view_ever_names_hakubun(win):
    """Local convergence is disclosed, but never as a TRACKER: it gets
    its own category and appears in none of the tracker-facing ones."""
    win.r_mirror_planned(_plan(), None)
    for key in ('membership', 'add', 'remove', 'update'):
        for row in _rows(win._mirror_stores[key]):
            assert 'Hakubun' not in row
            assert 'local' not in row


def test_local_convergence_is_disclosed_in_its_own_category(win):
    """Mirror overwrites a pending local edit; that must be visible."""
    win.r_mirror_planned(_plan(), None)
    rows = _rows(win._mirror_stores['local'])
    assert any('Hakubun' in r for r in rows)
    assert win._mirror_page_labels['local'].get_text() \
        == "Hakubun's copy (1)"


def test_adds_and_removes_start_unticked(win):
    win.r_mirror_planned(_plan(), None)
    for key in ('add', 'remove'):
        store = win._mirror_stores[key]

        def check(it):
            while it is not None:
                op = store.get_value(it, _C_CHANGE)
                if op is not None:
                    assert store.get_value(it, _C_ACTIVE) is False
                    assert op.selected is False
                check(store.iter_children(it))
                it = store.iter_next(it)

        check(store.get_iter_first())


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


def test_each_mirror_category_has_its_own_view_and_store(win):
    """_make_changes_tree sets self._last_changes_view as a side effect
    and is called once per category (plus twice by the Sync tab), and
    the context-menu handler looks its model up by the `key` it was
    connected with. If two categories shared a view, or a handler were
    bound to the wrong key, it would read _C_CHANGE out of a different
    model, get None, and the menu would silently never appear."""
    assert set(win._mirror_views) == {'add', 'remove', 'update', 'local'}
    ids = {id(v) for v in win._mirror_views.values()}
    assert len(ids) == 4
    assert id(win._membership_view) not in ids
    for key, view in win._mirror_views.items():
        assert view.get_model() is win._mirror_stores[key]


def test_right_clicking_a_resolved_row_offers_the_way_back(win,
                                                           monkeypatch):
    """The gesture the resolution affordance depends on, wired
    end-to-end: a row produced by a stored decision must reach the
    handler and find its own model."""
    plan = _plan()
    plan.updates[0].reason = 'you chose this value for these trackers'
    win.r_mirror_planned(plan, None)

    seen = []
    monkeypatch.setattr(win, '_clear_mirror_resolution',
                        lambda op: seen.append(op))
    monkeypatch.setattr(Gtk.Menu, 'popup_at_pointer',
                        lambda self, event: None)

    view = win._mirror_views['update']
    store = win._mirror_stores['update']
    # The child row (the op itself), not the group header.
    path = store.get_path(store.iter_children(store.get_iter_first()))
    monkeypatch.setattr(view, 'get_path_at_pos', lambda x, y: (path,))

    event = type('E', (), {'button': 3, 'x': 0.0, 'y': 0.0})()
    assert win._on_mirror_op_button(view, event, 'update') is True
