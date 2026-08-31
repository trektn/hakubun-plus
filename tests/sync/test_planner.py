"""Planner behavior: field policies -> explicit SyncOperations.

These tests drive the planner through the real engine (fetch against
fake libs, then plan) so the values it sees are exactly what production
sees: normalized entries, recorded bases, provider capabilities.
"""

from hakubun.sync.models import FieldPolicy, PolicyKind

from conftest import FakeLib, make_engine, show


def own(store, field, provider):
    store.set_ownership(field, FieldPolicy(PolicyKind.PROVIDER,
                                           provider=provider))


def ops(plan, field=None, target=None):
    return [c for c in plan.changes
            if (field is None or c.field == field)
            and (target is None or c.target == target)
            and not c.creates_entry]


# -- provider-owned fields --------------------------------------------

def test_owner_external_change_propagates_without_conflict(store):
    """The doc's central example: Kitsu owns progress; a change made on
    Kitsu's website flows to local and every other provider, with no
    manual conflict."""
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=10)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=10, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    own(store, 'progress', 'kitsu')
    assert engine.fetch() == {}
    assert not engine.plan().conflicts

    kitsu.shows['k1']['my_progress'] = 12    # external edit on the owner
    assert engine.fetch() == {}
    plan = engine.plan()
    assert not plan.conflicts
    uid = store.mapping_for('mal', '1')['uuid']
    pulls = ops(plan, 'progress', 'local')
    assert len(pulls) == 1 and pulls[0].new == 12 \
        and pulls[0].source == 'kitsu'
    assert 'Kitsu owns progress' in pulls[0].reason
    pushes = ops(plan, 'progress', 'mal')
    assert len(pushes) == 1 and pushes[0].new == 12
    engine.apply(plan)
    assert mal.shows['1']['my_progress'] == 12
    assert store.local_get(uid)['progress'][0] == 12


def test_local_edit_routes_to_the_owner(store):
    """A local edit to an owned field is sent to the owning provider
    (and everyone else), not overwritten by the owner's stale value."""
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=10)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=10, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    own(store, 'progress', 'kitsu')
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']

    engine.set_local_field(uid, 'progress', 15)
    plan = engine.plan()
    assert not plan.conflicts
    assert ops(plan, 'progress', 'local') == []   # local already holds it
    to_owner = ops(plan, 'progress', 'kitsu')
    assert len(to_owner) == 1 and to_owner[0].new == 15 \
        and to_owner[0].source == 'local'
    assert 'local edit' in to_owner[0].reason
    engine.apply(plan)
    assert kitsu.shows['k1']['my_progress'] == 15
    assert mal.shows['1']['my_progress'] == 15
    # Converged: the next plan proposes nothing.
    assert engine.fetch() == {}
    assert ops(engine.plan(), 'progress') == []


def test_owner_beats_simultaneous_local_edit(store):
    """When the owner itself moved, its authority wins even against a
    concurrent local edit -- ownership means authoritative source, not
    conflict priority."""
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=10)])
    engine = make_engine(store, {'kitsu': kitsu})
    own(store, 'progress', 'kitsu')
    assert engine.fetch() == {}
    uid = store.mapping_for('kitsu', 'k1')['uuid']

    engine.set_local_field(uid, 'progress', 15)
    kitsu.shows['k1']['my_progress'] = 12
    assert engine.fetch() == {}
    plan = engine.plan()
    pulls = ops(plan, 'progress', 'local')
    assert len(pulls) == 1 and pulls[0].new == 12


def test_unmapped_owner_asserts_nothing(store):
    """If the owner doesn't list the entry there is no authoritative
    value: the planner emits nothing rather than inventing an authority
    (and never treats the missing field as an empty value)."""
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=10)])
    engine = make_engine(store, {'mal': mal})
    own(store, 'progress', 'kitsu')    # kitsu not even connected
    assert engine.fetch() == {}
    assert ops(engine.plan(), 'progress') == []


# -- individual fields ------------------------------------------------

def test_individual_field_never_syncs(store):
    """Different scores everywhere under an individual policy: no ops,
    no conflicts, no comparisons."""
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', score=7)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   score=4, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    store.set_ownership('score', FieldPolicy(PolicyKind.INDIVIDUAL))
    assert engine.fetch() == {}
    plan = engine.plan()
    assert ops(plan, 'score') == []
    assert [c for c in plan.conflicts if c.field == 'score'] == []


# -- reconciliation fields --------------------------------------------

def test_manual_first_sync_disagreement_conflicts(store):
    """Two sides genuinely disagree on a manual field with no shared
    history: a FieldConflict with the strategy's reason; resolving it
    produces clean pushes and the conflict never re-raises."""
    mal = FakeLib('mal', [show('mal', 1, 'Bebop', score=7)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Bebop',
                                   score=4.5, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    plan = engine.plan()
    conflicts = [c for c in plan.conflicts if c.field == 'score']
    assert len(conflicts) == 1
    assert 'Manual reconciliation' in conflicts[0].reason

    engine.resolve_conflict(conflicts[0], 'kitsu')   # user picks 9.0
    plan = engine.plan()
    assert [c for c in plan.conflicts if c.field == 'score'] == []
    pushes = ops(plan, 'score', 'mal')
    assert len(pushes) == 1 and pushes[0].new == 9.0
    engine.apply(plan)
    assert mal.shows['1']['my_score'] == 9
    assert engine.fetch() == {}
    assert [c for c in engine.plan().conflicts if c.field == 'score'] == []


def test_manual_single_sided_remote_change_propagates(store):
    """After a settled sync, one provider's website edit flows through
    a manual field with no conflict -- base history attributes it."""
    mal = FakeLib('mal', [show('mal', 1, 'Bebop', score=7)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Bebop',
                                   score=3.5, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}    # kitsu 3.5/5 == 7.0 == mal 7: settled
    assert not engine.plan().conflicts

    kitsu.shows['k1']['my_score'] = 4.5    # website edit -> 9.0
    assert engine.fetch() == {}
    plan = engine.plan()
    assert not plan.conflicts
    pulls = ops(plan, 'score', 'local')
    assert len(pulls) == 1 and pulls[0].new == 9.0
    assert 'only Kitsu changed' in pulls[0].reason


def test_precision_redundant_score_is_not_a_conflict(store):
    """MAL's integer 8 alongside a finer 8.4 elsewhere is rounding, not
    disagreement: no conflict, and MAL is never 'corrected'."""
    mal = FakeLib('mal', [show('mal', 1, 'Bebop', score=8)])
    anilist = FakeLib('anilist', [show('anilist', 9, 'Bebop',
                                       score=84, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist})
    assert engine.fetch() == {}
    plan = engine.plan()
    assert [c for c in plan.conflicts if c.field == 'score'] == []
    assert ops(plan, 'score', 'mal') == []


def test_union_default_for_tags(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren')])
    anilist = FakeLib('anilist', [show('anilist', 9, 'Frieren',
                                       my_tags='fantasy', mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist})
    assert engine.fetch() == {}
    plan = engine.plan()
    assert not plan.conflicts
    engine.apply(plan)
    uid = store.mapping_for('mal', '1')['uuid']
    assert store.local_get(uid)['tags'][0] == ['fantasy']


def test_progress_default_takes_furthest_on_first_sync(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=4)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=7, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    plan = engine.plan()
    assert not [c for c in plan.conflicts if c.field == 'progress']
    engine.apply(plan)
    uid = store.mapping_for('mal', '1')['uuid']
    assert store.local_get(uid)['progress'][0] == 7
    assert mal.shows['1']['my_progress'] == 7


# -- reasons -----------------------------------------------------------

def test_every_operation_carries_a_reason(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=4)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=7, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    own(store, 'status', 'kitsu')
    assert engine.fetch() == {}
    plan = engine.plan()
    assert plan.changes
    assert all(c.reason for c in plan.changes)
    assert all(c.reason for c in plan.conflicts) or not plan.conflicts
