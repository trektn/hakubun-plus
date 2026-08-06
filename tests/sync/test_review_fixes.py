"""Regressions for the multisync review pass.

Each test here pins a behaviour that was demonstrably wrong before, so
the fix cannot be undone silently. See the commit message for the full
list; the short version is: nothing overwrites another tracker without
either a shared history or an explicit tick.
"""
import sqlite3
import threading

import pytest

from hakubun.sync import present
from hakubun.sync.identity import IdentityResolver
from hakubun.sync.models import (FieldPolicy, NormalizedEntry, PolicyKind,
                                 SyncMode)
from hakubun.sync.store import SyncStore
from conftest import FakeLib, show, make_engine


# -- first sync never silently overwrites -----------------------------

def _two_trackers(store, primary='mal'):
    """The same show, genuinely different state on each site, never
    synced before: MAL 3 eps / 7, AniList 20 eps / 95 (canonical 9.5)."""
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', mal_id=1,
                               progress=3, score=7, total=28)])
    anilist = FakeLib('anilist', [show('anilist', 11, 'Frieren', mal_id=1,
                                       progress=20, score=95, total=28)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist})
    engine.primary = primary
    engine.fetch()
    return engine, mal, anilist


def test_first_sync_overwrites_are_planned_but_not_armed(store):
    """THE bug: on a brand-new database the default `local` policy made
    whichever list was ingested first win every field, and the plan
    reported ZERO conflicts -- so a single headless Sync click rolled
    the other tracker's real progress and score backwards.

    The changes are still planned and previewed (the user may well want
    them), but they are flagged first_sync and left unticked, so apply()
    is a no-op until someone opts in."""
    engine, mal, anilist = _two_trackers(store)
    plan = engine.plan(SyncMode.MERGE)

    clobbers = [c for c in plan.changes if c.target == 'anilist']
    assert {c.field for c in clobbers} == {'score', 'progress'}
    assert all(c.first_sync and not c.selected for c in clobbers)

    engine.apply(plan)
    assert anilist.shows['11']['my_progress'] == 20     # untouched
    assert anilist.shows['11']['my_score'] == 95        # untouched


def test_first_sync_overwrite_applies_once_ticked(store):
    """The safety is a speed bump, not a wall: ticking the box in the
    preview applies the change exactly as before."""
    engine, mal, anilist = _two_trackers(store)
    plan = engine.plan(SyncMode.MERGE)
    for change in plan.changes:
        change.selected = True
    engine.apply(plan)
    assert anilist.shows['11']['my_progress'] == 3
    assert anilist.shows['11']['my_score'] == 70


def test_filling_a_blank_is_not_an_overwrite(store):
    """Only a real value being replaced by another real value is the
    coin flip we guard. Writing into an empty field loses nobody's data
    and must keep applying automatically, or a first sync would stop
    doing the one thing it is unambiguously for."""
    anilist = FakeLib('anilist', [show('anilist', 11, 'Frieren', mal_id=1,
                                       progress=12, score=90)])
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', mal_id=1,
                               progress=0, score=0)])
    # AniList first, so local seeds from it and MAL is the empty side.
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    engine.primary = 'anilist'
    engine.fetch()
    plan = engine.plan(SyncMode.MERGE)
    assert [c.target for c in plan.changes] == ['mal', 'mal']
    assert not any(c.first_sync for c in plan.changes)
    engine.apply(plan)
    assert mal.shows['1']['my_progress'] == 12
    assert mal.shows['1']['my_score'] == 9


def test_second_sync_overwrites_are_armed_again(store):
    """Once a merge base exists, a divergence is a real change by a real
    person and the normal policy machinery owns it -- first_sync must
    NOT sticky-disable the field forever."""
    engine, mal, anilist = _two_trackers(store)
    plan = engine.plan(SyncMode.MERGE)
    for change in plan.changes:
        change.selected = True
    engine.apply(plan)

    # A genuine later edit on the signed-in account.
    mal.shows['1']['my_score'] = 10
    engine.fetch()
    plan = engine.plan(SyncMode.MERGE)
    pushes = [c for c in plan.changes
              if c.field == 'score' and c.target == 'anilist']
    assert pushes and all(c.selected and not c.first_sync for c in pushes)


# -- rebase respects the working tree ----------------------------------

def test_rebase_local_owner_does_not_revert_the_signed_in_account(store):
    """Rebase used to read the pre-fold database value, so 'rebase to
    local' pushed a STALE number back over the account you are signed
    into -- silently reverting the rating you had just set in the app."""
    engine, mal, anilist = _two_trackers(store)
    plan = engine.plan(SyncMode.MERGE)
    for change in plan.changes:
        change.selected = True
    engine.apply(plan)                       # establishes a merge base

    mal.shows['1']['my_score'] = 10          # the user rates it in-app
    engine.fetch()

    store.set_ownership('score', FieldPolicy(PolicyKind.LOCAL))
    plan = engine.plan(SyncMode.REBASE)
    # MAL is the working tree: its 10 propagates, and MAL is never told
    # to go back to the old value.
    assert [(c.old, c.new) for c in plan.changes
            if c.field == 'score' and c.target == 'mal'] == []
    assert [c.new for c in plan.changes
            if c.field == 'score' and c.target == 'anilist'] == [10.0]


def test_rebase_ignores_a_fold_with_no_shared_history(store):
    """...but only a fold backed by a real merge base counts. With no
    shared history the primary's value is just what that site happens to
    hold, so 'rebase to local' must not quietly become 'rebase to the
    signed-in account' on a first sync."""
    engine, mal, anilist = _two_trackers(store, primary='anilist')
    store.set_ownership('score', FieldPolicy(PolicyKind.LOCAL))
    plan = engine.plan(SyncMode.REBASE)
    # local seeded from MAL (7.0) and stays the rebase target.
    assert [c.new for c in plan.changes
            if c.field == 'score' and c.target == 'anilist'] == [7.0]


# -- identity conflicts get closed -------------------------------------

def test_creating_an_entity_closes_the_question_it_answers(store):
    """An entry that once had ambiguous candidates, then stopped having
    any, was resolved into its own entity while its identity row stayed
    'open' FOREVER -- step 1 short-circuits on every later fetch, so
    nothing ever revisited it. The Identity tab then listed an entry
    that was already linked, and 'resolving' it again repointed or
    duplicated the mapping."""
    kitsu = FakeLib('kitsu', [show('kitsu', 1, 'Frieren')])
    mal = FakeLib('mal', [show('mal', 100, 'Frieren Season 2')])
    engine = make_engine(store, {'kitsu': kitsu, 'mal': mal})
    engine.fetch()
    open_rows = store.identity_open()
    assert [(r['provider'], r['provider_id']) for r in open_rows] \
        == [('mal', '100')]

    # The candidate gains its own MAL entry, so there is nothing left to
    # ask about and mal/100 becomes its own entity.
    ent = [e for e in store.entities() if e['title'] == 'Frieren'][0]
    store.add_mapping(ent['uuid'], 'mal', 999, confirmed=True)
    engine.fetch()

    assert store.mapping_for('mal', '100') is not None
    assert store.identity_open() == []
    assert store.identity_get('mal', '100')['status'] == 'resolved'


def test_type_mismatch_warning_survives_being_re_homed(store):
    """The one row that must NOT be auto-closed: a quarantined
    type-mismatched mapping is a 'your source data is wrong' warning,
    and re-homing the entry does not make it untrue."""
    uid = store.create_entity('Foo', media_type='anime')
    store.add_mapping(uid, 'kitsu', '9', confirmed=True)
    resolver = IdentityResolver(store)
    resolver.resolve_entry(NormalizedEntry(provider='kitsu', provider_id='9',
                                           title='Foo', media_type='manga'))
    row = store.identity_get('kitsu', '9')
    assert row['status'] == 'open'
    assert any('TYPE MISMATCH' in (c.get('via') or '')
               for c in row['candidates'])


# -- store robustness --------------------------------------------------

def test_failed_begin_releases_the_store_lock(store):
    """__enter__ acquired the lock BEFORE `BEGIN IMMEDIATE`, and when
    that raised (two connections share the file by design, so
    'database is locked' is reachable) __exit__ never ran -- the lock
    was held for the life of the process and every other thread's read
    blocked forever."""
    class Busy:
        """sqlite3.Connection is not monkeypatchable; stand in for it."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args):
            if sql.startswith('BEGIN'):
                raise sqlite3.OperationalError('database is locked')
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real = store._conn
    store._conn = Busy(real)
    try:
        with pytest.raises(sqlite3.OperationalError):
            with store.transaction():
                pass                      # never reached
    finally:
        store._conn = real

    assert store._txn_depth == 0
    released = threading.Event()
    thread = threading.Thread(target=lambda: (store.entities(),
                                              released.set()))
    thread.start()
    thread.join(timeout=5)
    assert released.is_set(), 'store lock was leaked by a failed BEGIN'


def test_entity_cache_follows_another_connection(tmp_path):
    """The list overlay keeps one long-lived store while the sync window
    writes through its own; the entities snapshot only invalidated on
    ITS OWN writes, so once built it was frozen for the whole process --
    the overlay then read stale/absent entity totals and rendered
    impossible progress like '4 / 1'."""
    path = str(tmp_path / 'multisync-anime.db')
    reader, writer = SyncStore(path), SyncStore(path)
    try:
        assert reader.entities_with_aliases() == []
        writer.create_entity('Frieren', media_type='anime', total=28)
        rows = reader.entities_with_aliases()
        assert [e['title'] for e, _aliases in rows] == ['Frieren']
        assert rows[0][0]['total'] == 28
    finally:
        reader.close()
        writer.close()


# -- shared presentation -----------------------------------------------

def test_both_front_ends_describe_a_plan_identically(store):
    """The Qt and GTK sync windows had their own copies of every
    formatting/explanation helper, and the copies had already drifted.
    They now delegate to hakubun.sync.present, so one wording change
    reaches both."""
    engine, mal, anilist = _two_trackers(store)
    plan = engine.plan(SyncMode.MERGE)
    change = [c for c in plan.changes
              if c.field == 'score' and c.target == 'anilist'][0]

    direction, text = present.change_line(engine.adapters, change, 'mal')
    assert direction == 'push'
    assert text.startswith('Push to Anilist, Score:')
    assert present.FIRST_SYNC_NOTE in text      # says why it is unticked
    assert present.local_label('mal') == 'Mal'
    assert present.local_label(None) == 'Local'
    # Settings' dropdown must never be able to select the destructive
    # retroactive mode.
    assert SyncMode.REBASE not in present.SETTINGS_MODES.values()
