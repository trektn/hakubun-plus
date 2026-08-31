"""GTK MultiSyncWindow information architecture: new entries separated
from ordinary changes, headline summary, per-field rule combos and the
Advanced matrix staying in agreement. Skipped without GTK/display."""

import pytest

gi = pytest.importorskip('gi')
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk  # noqa: E402,F401

if Gdk.Display.get_default() is None:
    pytest.skip('no display available for GTK window tests',
                allow_module_level=True)

from hakubun.sync.models import (FieldPolicy, SyncOperation,  # noqa: E402
                                 SyncPlan)
from hakubun.sync.engine import SyncEngine  # noqa: E402
from hakubun.sync.store import SyncStore  # noqa: E402
from hakubun.ui.gtk.multisyncwindow import MultiSyncWindow  # noqa: E402


class _FakeEngine:
    def __init__(self):
        self.store = SyncStore(':memory:')
        self.adapters = {}
        self.primary = None


@pytest.fixture
def win():
    window = MultiSyncWindow(engine=_FakeEngine())
    yield window
    window.destroy()


def _ops(n, creates=False):
    return [
        SyncOperation(uuid='%s%d' % ('n' if creates else 'u', i),
                      field='progress', old=None if creates else 1, new=5,
                      target='mal', source='kitsu', title='Show %d' % i,
                      selected=not creates, creates_entry=creates)
        for i in range(n)
    ]


def _store_changes(store):
    found = []
    it = store.get_iter_first()
    while it is not None:
        child = store.iter_children(it)
        while child is not None:
            found.append(store.get_value(child, 4))
            child = store.iter_next(child)
        it = store.iter_next(it)
    return found


def test_new_entries_are_separated_from_changes(win):
    win.r_planned(SyncPlan(changes=_ops(2) + _ops(2, creates=True)), None)
    ordinary = _store_changes(win._changes_store)
    creates = _store_changes(win._creates_store)
    assert len(ordinary) == 2
    assert not any(c.creates_entry for c in ordinary)
    assert len(creates) == 2
    assert all(c.creates_entry and not c.selected for c in creates)
    assert win._changes_page_label.get_text() == 'Changes (2)'
    assert win._creates_page_label.get_text() == 'New entries (2)'
    assert '2 change(s)' in win.summary_label.get_text()
    assert '2 new entries' in win.summary_label.get_text()


def test_ticking_a_new_entry_is_an_explicit_opt_in(win):
    win.r_planned(SyncPlan(changes=_ops(1, creates=True)), None)
    store = win._creates_store
    parent = store.get_iter_first()
    child = store.iter_children(parent)
    change = store.get_value(child, 4)
    assert change.selected is False
    win._on_change_toggled(None, store.get_path(child), store)
    assert change.selected is True


def test_config_combo_and_matrix_stay_in_agreement(win):
    combo = win._policy_combos['progress']
    combo.set_active_id('reconcile:manual')
    assert win.store.ownership()['progress'].serialize() \
        == 'reconcile:manual'
    assert win._matrix_radios[('progress', 'reconcile:manual')] \
        .get_active()
    # And the other way round: the Advanced matrix updates the combo.
    radio = win._matrix_radios[('progress', 'reconcile:progress')]
    radio.set_active(True)
    assert win.store.ownership()['progress'].serialize() \
        == 'reconcile:progress'
    assert combo.get_active_id() == 'reconcile:progress'


def test_nonfunctional_tags_and_favorites_are_not_policy_controls(win):
    assert 'tags' not in win._policy_combos
    assert 'favorite' not in win._policy_combos
    assert not any(field in ('tags', 'favorite')
                   for field, _policy in win._matrix_radios)


def test_exotic_matrix_policy_still_shown_in_simple_view(win):
    # Something the simple list doesn't offer for progress.
    win.store.set_ownership('progress',
                            FieldPolicy.parse('reconcile:union'))
    win._refresh_policy_widgets()
    assert win._policy_combos['progress'].get_active_id() \
        == 'reconcile:union'


def test_missing_tracker_link_is_visible_and_ignorable():
    store = SyncStore(':memory:')
    uid = store.create_entity('GHOST', media_type='anime')
    store.add_mapping(uid, 'anilist', '9')
    store.remote_set_all('anilist', '9', {'score': 5.0})
    engine = SyncEngine(store, {'anilist': object(), 'mal': object()})
    window = MultiSyncWindow(engine=engine)
    try:
        assert len(window._identity_store) == 1
        it = window._identity_store.get_iter_first()
        issue = window._identity_store.get_value(it, 5)
        assert issue['provider'] == 'mal'
        window._identity_view.get_selection().select_iter(it)
        assert window._search_provider.get_active_text() == 'mal'
        assert not window._search_provider.get_sensitive()
        assert window._rb_search.get_active()

        issue = window._identity_store.get_value(it, 5)
        # A gap has no MAL id to inspect, so right-click uses the existing
        # AniList mapping and runs the Inspector instead of only filling it.
        assert window._identity_inspect_target(issue) == ('anilist', '9')
        window._inspect_from_identity(
            *window._identity_inspect_target(issue))
        assert window._notebook.get_current_page() == 4
        assert window._inspect_provider.get_active_text() == 'anilist'
        assert window._inspect_id.get_text() == '9'
        assert 'Anilist id 9' in window._inspect_output.get_text()

        engine.set_membership(uid, 'mal', 'ignore')
        window._refresh_identity()
        assert len(window._identity_store) == 0
    finally:
        window.destroy()
        store.close()


def test_missing_tracker_link_accepts_an_exact_id():
    store = SyncStore(':memory:')
    uid = store.create_entity('GHOST', media_type='anime')
    store.add_mapping(uid, 'anilist', '9')
    store.remote_set_all('anilist', '9', {'score': 5.0})
    engine = SyncEngine(store, {'anilist': object(), 'mal': object()})
    window = MultiSyncWindow(engine=engine)
    try:
        it = window._identity_store.get_iter_first()
        window._identity_view.get_selection().select_iter(it)
        assert window._exact_id_provider.get_active_id() == 'mal'
        assert not window._exact_id_provider.get_sensitive()
        window._rb_exact_id.set_active(True)
        window._exact_id_value.set_text('77')
        window.s_identity_resolve()
        assert store.mapping_for('mal', '77')['uuid'] == uid
        assert len(window._identity_store) == 0
    finally:
        window.destroy()
        store.close()
