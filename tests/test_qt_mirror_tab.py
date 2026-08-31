"""Qt SyncWindow Mirror tab (no display needed).

Mirror is a deliberate, potentially large operation, so the properties
worth pinning in the UI are the safety ones: nothing is applied without
an explicit confirmation, deletions are never preselected, and the
tracker-facing views never name Hakubun as a tracker.

WHAT each card says -- one per title, and the wording of every row --
is toolkit-agnostic and tested once in tests/sync/test_mirror_cards.py.
What is tested here is the wiring: that the cards reach the tiles, that
the changes come up when a tile is pointed at, that ticking reaches the
operation behind the row, and that the membership gestures still land
on the right object.
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


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    """Cover art is decoration, and a test suite has no business
    reaching a tracker's CDN for it."""
    from hakubun.ui import posters
    monkeypatch.setattr(posters.PosterCache, 'get', lambda self, url: None)


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
    plan.images['u1'] = 'https://example.invalid/ghost.jpg'
    return plan


def _tile(window, title):
    tile = window.mirror_grid.tile_for(title)
    assert tile is not None, 'no tile for %s' % title
    tile.ensure_details()
    return tile


def _texts(tile):
    return [row.text() for row in tile.rows]


def test_mirror_tab_exists_after_sync(window):
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert titles[:2] == ['Sync', 'Mirror']


def test_the_preview_is_one_tile_per_title(window):
    """Not five trees of operations: the unit on screen is the work the
    user is deciding about, shown as the thing they recognize it by."""
    window.r_mirror_planned(_plan(), None)
    titles = [t.card.title for t in window.mirror_grid.tiles]
    assert 'GHOST' in titles
    assert 'Doomed' in titles


def test_a_tile_carries_the_ownership_row_and_its_changes(window):
    window.r_mirror_planned(_plan(), None)
    tile = _tile(window, 'GHOST')
    texts = _texts(tile)
    assert any(t.startswith('Add to Kitsu') for t in texts)
    assert any(t.startswith('Update Mal') for t in texts)
    assert tile.card.desired      # 'Ownership says' heads the overlay


def test_the_cover_fades_when_the_changes_come_up(window):
    """The whole preview: the picture is what you navigate by, the
    changes are what you read, and they occupy the same space.

    The fade is the panel's own near-opaque background, not a graphics
    effect on the cover -- an effect renders the widget through an
    offscreen pixmap, which a QScrollArea's blit-scrolling leaves
    smeared behind at the old offset.
    """
    from PyQt6.QtWidgets import QGraphicsEffect
    window.r_mirror_planned(_plan(), None)
    tile = _tile(window, 'GHOST')
    assert not tile._details.isVisibleTo(tile)
    tile.set_hovered(True)
    assert tile._details.isVisibleTo(tile)
    tile.set_hovered(False)
    assert not tile._details.isVisibleTo(tile)

    for widget in tile.findChildren(QtCore.QObject):
        assert not isinstance(getattr(widget, 'graphicsEffect', None)
                              and widget.graphicsEffect(), QGraphicsEffect)


def test_a_work_with_no_cover_still_names_itself(window):
    """Not every provider ships art, and a blank tile in a grid is
    unusable -- the title stands in for the picture."""
    plan = _plan()
    plan.images.clear()
    window.r_mirror_planned(plan, None)
    tile = _tile(window, 'GHOST')
    assert tile.card.image == ''
    assert tile.cover.text() == 'GHOST'


def test_art_replaces_the_stand_in_title_when_it_arrives(window):
    """Downloads finish after the grid is drawn, so a tile has to be
    able to take its picture later."""
    from PyQt6 import QtGui
    window.r_mirror_planned(_plan(), None)
    tile = window.mirror_grid.tile_for('GHOST')
    assert tile.card.image == 'https://example.invalid/ghost.jpg'
    pixmap = QtGui.QPixmap(10, 14)
    pixmap.fill(QtGui.QColor('#123456'))
    tile.set_cover(pixmap)
    assert tile.cover.text() == ''
    assert not tile.cover.pixmap().isNull()


def test_tracker_rows_never_name_hakubun(window):
    """Local convergence is disclosed -- named as this app's own copy,
    in the same shape as a tracker row so it cannot be mistaken for
    one with its name left off."""
    window.r_mirror_planned(_plan(), None)
    rows = _texts(_tile(window, 'GHOST'))
    assert any(t.startswith('Update Hakubun') for t in rows)
    for text in rows:
        if text.startswith('Update Hakubun'):
            continue
        assert 'Hakubun' not in text


def test_local_convergence_is_disclosed_and_refusable(window):
    """Mirror overwrites a pending local edit -- it does not read the
    bases that would tell it one was made. That must be visible and
    refusable, not silent."""
    window.r_mirror_planned(_plan(), None)
    tile = _tile(window, 'GHOST')
    row = next(r for r in tile.rows
               if getattr(r.op, 'target', None) == 'local')
    row.check.setChecked(False)
    assert row.op.selected is False


def test_creating_an_entry_is_a_single_tickable_row(window):
    """One decision per entry. Ticking a subset of an entry's fields is
    not something the user can express, because apply() would create a
    half-formed entry from it."""
    window.r_mirror_planned(_plan(), None)
    adds = [r for r in _tile(window, 'GHOST').rows
            if isinstance(r.op, AddOperation)]
    assert len(adds) == 1
    assert 'Add to Kitsu' in adds[0].text()


def test_adds_and_removes_start_unticked(window):
    window.r_mirror_planned(_plan(), None)
    for tile in window.mirror_grid.tiles:
        tile.ensure_details()
        for row in tile.rows:
            if isinstance(row.op, (AddOperation, RemoveOperation)):
                assert row.op.selected is False


def test_ticking_a_tile_ticks_everything_under_it(window):
    """"Yes to this show" is one click, not one per field."""
    window.r_mirror_planned(_plan(), None)
    tile = _tile(window, 'GHOST')
    tile.check.click()
    assert tile.card.ops and all(op.selected for op in tile.card.ops)
    assert tile.check.checkState() == QtCore.Qt.CheckState.Checked


def test_a_tile_whose_changes_were_never_opened_still_ticks(window):
    """The overlay is built on first hover, so the tile's own tick has
    to act on the CARD, not on widgets that may not exist yet."""
    window.r_mirror_planned(_plan(), None)
    tile = window.mirror_grid.tile_for('GHOST')
    tile.check.click()
    assert all(op.selected for op in tile.card.ops)


def test_unticking_one_change_makes_the_tile_partial(window):
    """A half-ticked title must say so rather than claim the whole
    work is going."""
    window.r_mirror_planned(_plan(), None)
    tile = _tile(window, 'GHOST')
    tile.check.click()
    row = next(r for r in tile.rows
               if r.text().startswith('Update Mal'))
    row.check.setChecked(False)
    assert tile.check.checkState() \
        == QtCore.Qt.CheckState.PartiallyChecked


def test_a_deletion_colours_its_whole_tile(window):
    """Deletion is the only irreversible thing Mirror does, and a grid
    is scanned rather than read."""
    window.r_mirror_planned(_plan(), None)
    doomed = window.mirror_grid.tile_for('Doomed')
    assert doomed._accent() == '#ef5350'


def test_apply_is_refused_without_confirmation(window, monkeypatch):
    """The confirmation is not advisory: declining it applies
    nothing."""
    window.r_mirror_planned(_plan(), None)

    import hakubun.ui.qt.syncwindow as mod
    monkeypatch.setattr(mod.QMessageBox, 'exec', lambda self: None)
    window.s_mirror_apply()
    assert window.engine.applied == []


def test_summary_reads_in_tracker_terms(window):
    window.r_mirror_planned(_plan(), None)
    text = window.mirror_summary.text()
    assert 'entry to add' in text and 'entry to remove' in text
    assert 'Hakubun' not in text


def test_membership_right_click_still_reaches_the_membership_menu(
        window, monkeypatch):
    """The membership rows are the only way a deletion is ever
    proposed, so the right-click that reaches them is load-bearing.
    They carry no operation, so the handler must fall through its
    resolution branch to the membership one."""
    import hakubun.ui.qt.syncwindow as mod

    plan = _plan()
    plan.membership[0].decisions['kitsu'] = 'ignore'
    window.r_mirror_planned(plan, None)
    row = next(r for r in _tile(window, 'GHOST').rows
               if r.text().startswith('Kitsu —'))

    actions = []
    monkeypatch.setattr(mod.QMenu, 'exec', lambda self, pos=None:
                        actions.extend(a.text() for a in self.actions()))
    window._mirror_context_menu(row, QtCore.QPoint(0, 0))

    # Kitsu lacks the entry, so the offer is to add or to leave it --
    # and 'Remove from' is correctly absent for a tracker with no entry.
    assert 'Add to Kitsu' in actions
    assert 'Leave Kitsu as it is' in actions
    assert 'Ask me about Kitsu again' in actions
    assert not any(a.startswith('Remove from') for a in actions)


def test_membership_right_click_on_a_holder_offers_removal(window,
                                                           monkeypatch):
    import hakubun.ui.qt.syncwindow as mod

    plan = _plan()
    plan.membership[0].decisions['mal'] = 'present'
    window.r_mirror_planned(plan, None)
    row = next(r for r in _tile(window, 'GHOST').rows
               if r.text().startswith('Mal —'))

    actions = []
    monkeypatch.setattr(mod.QMenu, 'exec', lambda self, pos=None:
                        actions.extend(a.text() for a in self.actions()))
    window._mirror_context_menu(row, QtCore.QPoint(0, 0))

    assert 'Remove from Mal' in actions
    assert 'Add to Mal' not in actions


def test_right_clicking_a_resolved_row_offers_the_way_back(window,
                                                           monkeypatch):
    import hakubun.ui.qt.syncwindow as mod

    plan = _plan()
    plan.updates[0].reason = 'you chose this value for these trackers'
    window.r_mirror_planned(plan, None)
    row = next(r for r in _tile(window, 'GHOST').rows
               if getattr(r.op, 'target', None) == 'mal')

    actions = []
    monkeypatch.setattr(mod.QMenu, 'exec', lambda self, pos=None:
                        actions.extend(a.text() for a in self.actions()))
    window._mirror_context_menu(row, QtCore.QPoint(0, 0))

    assert actions == ['Ask me about Score again']
