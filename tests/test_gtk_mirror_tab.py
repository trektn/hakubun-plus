"""GTK MultiSyncWindow Mirror tab -- parity with the Qt twin.

Both windows delegate their wording AND their layout to
sync/present.py -- the card model is tested once, toolkit-free, in
tests/sync/test_mirror_cards.py. What is checked here is that the GTK
side wires the same structure to it: cards reach the tiles, pointing at
a tile brings up its changes, ticking a tile reaches the operations
under it, adds and removes are opt-in, and the apply path cannot run
without the confirmation. Skipped without GTK/display.
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
from hakubun.ui.gtk.multisyncwindow import MultiSyncWindow  # noqa: E402


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


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    """Cover art is decoration, and a test suite has no business
    reaching a tracker's CDN for it."""
    from hakubun.ui import posters
    monkeypatch.setattr(posters.PosterCache, 'get', lambda self, url: None)


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
    plan.images['u1'] = 'https://example.invalid/ghost.jpg'
    return plan


def _tile(win, title):
    tile = win.mirror_grid.tile_for(title)
    assert tile is not None, 'no tile for %s' % title
    tile.ensure_details()
    return tile


def _texts(tile):
    return [row.text() for row in tile.rows]


def _event(button=3):
    return type('E', (), {'button': button, 'x': 0.0, 'y': 0.0})()


def test_mirror_tab_sits_after_sync(win):
    titles = [win._notebook.get_tab_label_text(win._notebook.get_nth_page(i))
              for i in range(win._notebook.get_n_pages())]
    assert titles[:2] == ['Sync', 'Mirror']


def test_the_preview_is_one_tile_per_title(win):
    """Not five pages of operations: the unit on screen is the work the
    user is deciding about, shown as the thing they recognize it by."""
    win.r_mirror_planned(_plan(), None)
    titles = [t.card.title for t in win.mirror_grid.tiles]
    assert 'GHOST' in titles
    assert 'Doomed' in titles


def test_a_tile_carries_the_ownership_row_and_its_changes(win):
    win.r_mirror_planned(_plan(), None)
    tile = _tile(win, 'GHOST')
    texts = _texts(tile)
    assert any(t.startswith('Add to Kitsu') for t in texts)
    assert any(t.startswith('Update Mal') for t in texts)
    assert tile.card.desired      # 'Ownership says' heads the overlay


def test_the_cover_fades_when_the_changes_come_up(win):
    """The whole preview: the picture is what you navigate by, the
    changes are what you read, and they occupy the same space.

    The fade is the panel's own near-opaque background, not an opacity
    set on the cover -- that forces GTK to render the widget through an
    offscreen surface every frame, for a result the panel already
    gives.
    """
    win.r_mirror_planned(_plan(), None)
    tile = _tile(win, 'GHOST')
    assert not tile._details.get_visible()
    tile.set_hovered(True)
    assert tile._details.get_visible()
    tile.set_hovered(False)
    assert not tile._details.get_visible()
    assert tile.cover.get_opacity() == 1.0


def test_the_details_stay_up_when_the_pointer_moves_onto_them(win):
    """The flicker bug, pinned.

    A tile is an EventBox with no visible window, so its GdkWindow is
    input-only -- and an input-only window cannot contain the windows
    of the widgets inside it. The details panel is therefore a SIBLING
    in the window hierarchy, and crossing onto it reports NONLINEAR,
    not INFERIOR. Trusting the detail alone hid the panel the instant
    the pointer touched it, which re-entered the tile, which showed it
    again.
    """
    win.r_mirror_planned(_plan(), None)
    tile = _tile(win, 'GHOST')
    tile.set_hovered(True)

    crossing = type('E', (), {'detail': Gdk.NotifyType.NONLINEAR})()
    tile._on_leave(tile, crossing)
    # Deferred, and conditional on where the pointer really is -- so
    # nothing has been hidden yet by the leave itself.
    assert tile._details.get_visible()
    assert tile._hide_source

    tile._on_enter(tile, None)
    assert not tile._hide_source, 'a re-entry must cancel the hide'
    assert tile._details.get_visible()


def test_a_work_with_no_cover_still_names_itself(win):
    """Not every provider ships art -- the tile's title carries it."""
    plan = _plan()
    plan.images.clear()
    win.r_mirror_planned(plan, None)
    assert _tile(win, 'GHOST').card.image == ''


def test_tracker_rows_never_name_hakubun(win):
    """Local convergence is disclosed, but never as a TRACKER."""
    win.r_mirror_planned(_plan(), None)
    rows = _texts(_tile(win, 'GHOST'))
    assert any(r.startswith('Update Hakubun') for r in rows)
    for row in rows:
        if row.startswith('Update Hakubun'):
            continue        # this app's own copy, named as such
        assert 'Hakubun' not in row


def test_creating_an_entry_is_a_single_tickable_row(win):
    """One decision per entry -- apply() would otherwise be able to
    create a half-formed entry from a ticked subset of its fields."""
    win.r_mirror_planned(_plan(), None)
    adds = [r for r in _tile(win, 'GHOST').rows
            if isinstance(r.op, AddOperation)]
    assert len(adds) == 1
    assert 'Add to Kitsu' in adds[0].text()


def test_adds_and_removes_start_unticked(win):
    win.r_mirror_planned(_plan(), None)
    for tile in win.mirror_grid.tiles:
        tile.ensure_details()
        for row in tile.rows:
            if isinstance(row.op, (AddOperation, RemoveOperation)):
                assert row.op.selected is False
                assert row.check.get_active() is False


def test_ticking_a_tile_ticks_everything_under_it(win):
    """"Yes to this show" is one click, not one per field."""
    win.r_mirror_planned(_plan(), None)
    tile = _tile(win, 'GHOST')
    tile._on_check_clicked(None, None)
    assert tile.card.ops and all(op.selected for op in tile.card.ops)
    assert tile.check.get_active() is True
    assert tile.check.get_inconsistent() is False
    assert all(r.check.get_active() for r in tile.rows
               if r.check is not None)


def test_a_tile_whose_changes_were_never_opened_still_ticks(win):
    """The overlay is built on first hover, so the tile's own tick has
    to act on the CARD, not on widgets that may not exist yet."""
    win.r_mirror_planned(_plan(), None)
    tile = win.mirror_grid.tile_for('GHOST')
    tile._on_check_clicked(None, None)
    assert all(op.selected for op in tile.card.ops)


def test_unticking_one_change_makes_the_tile_inconsistent(win):
    """A half-ticked title must say so rather than claim the whole
    work is going."""
    win.r_mirror_planned(_plan(), None)
    tile = _tile(win, 'GHOST')
    tile._on_check_clicked(None, None)
    row = next(r for r in tile.rows if r.text().startswith('Update Mal'))
    row.check.set_active(False)
    assert row.op.selected is False
    assert tile.check.get_inconsistent() is True


def test_a_deletion_colours_its_whole_tile(win):
    """Deletion is the only irreversible thing Mirror does, and a grid
    is scanned rather than read."""
    win.r_mirror_planned(_plan(), None)
    assert win.mirror_grid.tile_for('Doomed')._accent() == '#ef5350'


def test_apply_is_refused_without_confirmation(win, monkeypatch):
    win.r_mirror_planned(_plan(), None)
    monkeypatch.setattr(Gtk.MessageDialog, 'run',
                        lambda self: Gtk.ResponseType.CANCEL)
    monkeypatch.setattr(Gtk.MessageDialog, 'destroy', lambda self: None)
    win.s_mirror_apply()
    assert win.engine.applied == []


def test_right_clicking_a_settled_row_reaches_the_membership_menu(
        win, monkeypatch):
    """One handler serves both gestures, so it has to tell an
    informational row from an operation row. This is the branch the
    whole membership workflow depends on -- and the only way back to a
    decision that, being settled, produces no operations."""
    plan = _plan()
    plan.membership[0].decisions['kitsu'] = 'ignore'
    win.r_mirror_planned(plan, None)
    row = next(r for r in _tile(win, 'GHOST').rows
               if r.text().startswith('Kitsu —'))

    labels = []
    monkeypatch.setattr(Gtk.Menu, 'popup_at_pointer',
                        lambda self, event: labels.extend(
                            c.get_label() for c in self.get_children()))

    assert win._on_mirror_row_button(row, _event()) is True
    assert 'Add to Kitsu' in labels
    assert 'Leave Kitsu as it is' in labels
    assert 'Ask me about Kitsu again' in labels
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
    labels = []
    monkeypatch.setattr(Gtk.Menu, 'popup_at_pointer',
                        lambda self, event: labels.extend(
                            c.get_label() for c in self.get_children()))

    row = next(r for r in _tile(win, 'GHOST').rows
               if getattr(r.op, 'target', None) == 'mal')
    assert win._on_mirror_row_button(row, _event()) is True
    assert labels == ['Ask me about Score again']


def test_an_ordinary_change_row_offers_no_menu(win):
    """Right-clicking a plain update has nothing to decide: the row
    must fall through rather than pop up an empty menu."""
    win.r_mirror_planned(_plan(), None)
    row = next(r for r in _tile(win, 'GHOST').rows
               if getattr(r.op, 'target', None) == 'mal')
    assert win._on_mirror_row_button(row, _event()) is False
