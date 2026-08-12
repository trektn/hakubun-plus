"""Conflict semantics and the resolution lifecycle.

Two invariants: a missing/empty provider value must never manufacture a
conflict out of an otherwise unanimous value (missing data is not a
competing opinion), and resolving a conflict must actually produce the
follow-up SyncOperations -- resolve -> replan -> apply -> provider
state, for every kind of choice."""

from hakubun.sync import strategies
from hakubun.sync.strategies import (Conflict, NoChange, ReconcileContext,
                                     Resolved)

from conftest import FakeLib, make_engine, show

# Dates canonicalize to ISO strings (see normalize).
D = '2026-08-10'
D2 = '2026-08-11'


def ctx(changed=None):
    return ReconcileContext(field='finish_date', changed=changed or {})


manual = strategies.get_strategy('manual')


# -- strategy level: missing values abstain ---------------------------

def test_unanimous_value_with_one_missing_side_resolves():
    result = manual.reconcile('finish_date', {
        'local': D, 'anilist': D, 'kitsu': D, 'mal': None}, ctx())
    assert isinstance(result, Resolved) and result.value == D


def test_unanimous_value_with_several_missing_sides_resolves():
    result = manual.reconcile('finish_date', {
        'local': D, 'anilist': D, 'kitsu': None, 'mal': None}, ctx())
    assert isinstance(result, Resolved) and result.value == D


def test_local_plus_one_provider_agree_other_missing():
    result = manual.reconcile('finish_date', {
        'local': D, 'anilist': D, 'mal': None}, ctx())
    assert isinstance(result, Resolved) and result.value == D


def test_known_values_disagreeing_is_still_a_conflict():
    result = manual.reconcile('finish_date', {
        'local': D, 'anilist': D2, 'mal': None}, ctx())
    assert isinstance(result, Conflict)


def test_all_sides_missing_is_not_a_conflict():
    result = manual.reconcile('finish_date', {
        'local': None, 'anilist': None, 'mal': None}, ctx())
    assert isinstance(result, NoChange)


def test_deliberate_clear_votes_and_propagates():
    # MAL provably moved TO empty since the last sync: that is a real
    # opinion (a clear), and being the only change it propagates.
    result = manual.reconcile('finish_date', {
        'local': D, 'anilist': D, 'mal': None},
        ctx({'mal': True, 'anilist': False, 'local': False}))
    assert isinstance(result, Resolved) and result.value is None


def test_clear_against_another_change_is_a_conflict():
    result = manual.reconcile('finish_date', {
        'local': D, 'anilist': D2, 'mal': None},
        ctx({'mal': True, 'anilist': True, 'local': False}))
    assert isinstance(result, Conflict)


# -- engine level: the whole lifecycle --------------------------------

def test_missing_provider_date_fills_in_without_conflict(store):
    """Three sides hold the same finish date, MAL holds none: no
    conflict -- the unanimous value is adopted and pushed to MAL."""
    mal = FakeLib('mal', [show('mal', 1, 'GHOST')])
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', mal_id=1,
                                       my_finish_date=D)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', mal_id=1,
                                   my_finish_date=D)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    plan = engine.plan()
    assert plan.conflicts == []
    fills = [c for c in plan.changes if c.target == 'mal'
             and c.field == 'finish_date']
    assert len(fills) == 1 and fills[0].new == D
    engine.apply(plan)
    assert mal.shows['1']['my_finish_date'] is not None
    # Stable: nothing replans, nothing re-conflicts.
    plan = engine.plan()
    assert plan.changes == [] and plan.conflicts == []


def _score_conflicted(store):
    """anilist 9.0 vs kitsu 7.0 on a manual-reconciled score, no shared
    history: a genuine conflict. Local is seeded from anilist (first
    fetched), so the precision collapse folds anilist onto it."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', score=3.5)])
    engine = make_engine(store, {'anilist': anilist, 'kitsu': kitsu})
    assert engine.fetch() == {}
    plan = engine.plan()
    conflicts = [c for c in plan.conflicts if c.field == 'score']
    assert len(conflicts) == 1
    return engine, anilist, kitsu, conflicts[0]


def _apply_replan(engine):
    plan = engine.plan()
    assert plan.conflicts == []
    engine.apply(plan)
    return plan


def test_use_provider_resolution_pushes_the_choice_everywhere(store):
    engine, anilist, kitsu, conflict = _score_conflicted(store)
    engine.resolve_conflict(conflict, 'kitsu')     # adopt 7.0
    plan = _apply_replan(engine)
    pushes = {c.target: c.new for c in plan.changes
              if c.field == 'score' and c.target != 'local'}
    # anilist was collapsed out of the conflict's options (redundant
    # with local's echo of it) -- the resolution must still reach it.
    assert pushes.get('anilist') == 7.0
    assert anilist.shows['9']['my_score'] == 70
    assert kitsu.shows['k1']['my_score'] == 3.5    # untouched
    assert engine.plan().changes == []             # settled


def test_keep_local_resolution_pushes_local_everywhere(store):
    engine, anilist, kitsu, conflict = _score_conflicted(store)
    engine.resolve_conflict(conflict, 'local')     # keep 9.0
    plan = _apply_replan(engine)
    pushes = {c.target: c.new for c in plan.changes
              if c.field == 'score' and c.target != 'local'}
    assert pushes.get('kitsu') == 9.0
    assert kitsu.shows['k1']['my_score'] == 4.5    # 9.0 on kitsu's scale
    assert anilist.shows['9']['my_score'] == 90    # already agreed
    assert engine.plan().changes == []


def test_explicit_value_resolution_pushes_it_everywhere(store):
    engine, anilist, kitsu, conflict = _score_conflicted(store)
    engine.resolve_conflict(conflict, 'value', value=8.0)
    plan = _apply_replan(engine)
    pushes = {c.target: c.new for c in plan.changes
              if c.field == 'score' and c.target != 'local'}
    assert pushes == {'anilist': 8.0, 'kitsu': 8.0}
    assert anilist.shows['9']['my_score'] == 80
    assert kitsu.shows['k1']['my_score'] == 4.0
    assert engine.plan().changes == []


def test_resolving_to_a_missing_value_is_an_explicit_clear(store):
    """Choosing an empty value in a conflict clears the field
    everywhere -- deliberately, once, and without re-conflicting."""
    engine, anilist, kitsu, conflict = _score_conflicted(store)
    engine.resolve_conflict(conflict, 'value', value=None)
    plan = engine.plan()
    assert plan.conflicts == []
    pushes = {c.target: c.new for c in plan.changes
              if c.field == 'score' and c.target != 'local'}
    assert pushes == {'anilist': None, 'kitsu': None}


def test_resolution_does_not_reraise_after_a_later_provider_edit(store):
    """After a resolution fully applies, a NEW single-sided provider
    edit propagates normally instead of fighting the stale
    resolution."""
    engine, anilist, kitsu, conflict = _score_conflicted(store)
    engine.resolve_conflict(conflict, 'local')
    _apply_replan(engine)
    kitsu.shows['k1']['my_score'] = 2.0            # website edit: 4.0
    assert engine.fetch() == {}
    plan = engine.plan()
    assert plan.conflicts == []
    pulled = [c for c in plan.changes if c.target == 'local'
              and c.field == 'score']
    assert len(pulled) == 1 and pulled[0].new == 4.0
