"""End-to-end fetch -> plan -> apply -> undo flows over the real
engine/planner/adapters with fake libs."""

from hakubun.sync.models import FieldPolicy, PolicyKind

from conftest import FakeLib, make_engine, show


def own(store, field, provider):
    store.set_ownership(field, FieldPolicy(PolicyKind.PROVIDER,
                                           provider=provider))


# -- seeding & convergence --------------------------------------------

def test_first_fetch_seeds_local_and_settles(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=3, score=8)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=3, score=4, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']
    local = store.local_get(uid)
    assert local['progress'][0] == 3 and local['score'][0] == 8.0
    # Agreeing values settled bases for BOTH providers.
    assert store.base_get(uid, 'mal')['progress'] == 3
    assert store.base_get(uid, 'kitsu')['progress'] == 3
    # Fully in sync: an empty plan.
    plan = engine.plan()
    assert not plan.changes and not plan.conflicts


def test_repeated_cycles_are_stable(store):
    """fetch+plan+apply cycles reach a fixed point: no oscillating
    pushes from precision residue (MAL echoing 8 for a finer 8.4)."""
    mal = FakeLib('mal', [show('mal', 1, 'Bebop', score=8)])
    anilist = FakeLib('anilist', [show('anilist', 9, 'Bebop',
                                       score=84, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist})
    for _ in range(3):
        assert engine.fetch() == {}
        plan = engine.plan()
        assert not plan.conflicts
        engine.apply(plan)
    uid = store.mapping_for('mal', '1')['uuid']
    assert store.local_get(uid)['score'][0] == 8.4   # finest survives
    assert mal.shows['1']['my_score'] == 8           # never re-pushed
    assert engine.plan().changes == []


# -- apply mechanics --------------------------------------------------

def test_kitsu_push_uses_library_entry_id(store):
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=3)])
    engine = make_engine(store, {'kitsu': kitsu})
    own(store, 'progress', 'kitsu')
    assert engine.fetch() == {}
    uid = store.mapping_for('kitsu', 'k1')['uuid']
    engine.set_local_field(uid, 'progress', 5)
    result = engine.apply(engine.plan())
    assert result['pushed'] == 1 and result['errors'] == {}
    # The fake raises if my_id is missing/None -- reaching the show
    # proves the persisted '_my_id' was passed through.
    assert kitsu.shows['k1']['my_progress'] == 5


def test_rate_limited_push_retries_and_succeeds(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=3)])
    mal.rate_limit_first = 1
    mal.rate_limit_retry_after = 0    # retry immediately in tests
    engine = make_engine(store, {'mal': mal})
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']
    engine.set_local_field(uid, 'progress', 4)
    result = engine.apply(engine.plan())
    assert result['pushed'] == 1 and result['errors'] == {}
    assert mal.shows['1']['my_progress'] == 4


def test_provider_failure_isolates_and_replans(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=3)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=3, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']
    engine.set_local_field(uid, 'progress', 6)

    kitsu.fail_update = True
    result = engine.apply(engine.plan())
    assert 'kitsu' in result['errors']
    assert mal.shows['1']['my_progress'] == 6      # mal still landed
    assert kitsu.shows['k1']['my_progress'] == 3   # kitsu untouched

    # kitsu's base never advanced, so the same push re-plans.
    kitsu.fail_update = False
    plan = engine.plan()
    retries = [c for c in plan.changes if c.target == 'kitsu'
               and c.field == 'progress']
    assert len(retries) == 1 and retries[0].new == 6
    engine.apply(plan)
    assert kitsu.shows['k1']['my_progress'] == 6


def test_apply_cancellation_keeps_committed_work(store):
    shows_mal = [show('mal', i, 'Show %d' % i, progress=1)
                 for i in (1, 2)]
    mal = FakeLib('mal', shows_mal)
    engine = make_engine(store, {'mal': mal})
    assert engine.fetch() == {}
    for pid in ('1', '2'):
        uid = store.mapping_for('mal', pid)['uuid']
        engine.set_local_field(uid, 'progress', 9)
    calls = []

    def cancel_after_first():
        return len(calls) >= 1

    real_update = mal.update_show

    def counting_update(item):
        real_update(item)
        calls.append(item)

    mal.update_show = counting_update
    result = engine.apply(engine.plan(),
                          should_cancel=cancel_after_first)
    assert result['cancelled'] is True
    assert result['pushed'] == 1
    # The uncommitted remainder re-plans next time.
    remaining = [c for c in engine.plan().changes if c.target == 'mal']
    assert len(remaining) == 1 and remaining[0].new == 9


# -- undo -------------------------------------------------------------

def test_undo_restores_local_and_replans_compensating_push(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=3)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=3, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    own(store, 'progress', 'kitsu')
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']

    kitsu.shows['k1']['my_progress'] = 8    # owner-side edit
    assert engine.fetch() == {}
    result = engine.apply(engine.plan())    # pull 8 locally, push to mal
    assert store.local_get(uid)['progress'][0] == 8
    assert mal.shows['1']['my_progress'] == 8

    engine.undo(result['txn'])
    assert store.local_get(uid)['progress'][0] == 3
    # The pushes happened (the providers hold 8): undo rewinds local
    # and the next plan proposes the compensating pushes back to 3.
    plan = engine.plan()
    comp = {c.target: c.new for c in plan.changes
            if c.field == 'progress' and c.target != 'local'}
    assert comp == {'mal': 3, 'kitsu': 3}
    engine.apply(plan)
    assert mal.shows['1']['my_progress'] == 3
    assert kitsu.shows['k1']['my_progress'] == 3


# -- episode-structure translation ------------------------------------

def test_completion_translates_between_structures(store):
    """MAL lists the movie as 4 episodes, Kitsu as 1: completing on MAL
    pushes 1 (not 4) to Kitsu."""
    mal = FakeLib('mal', [show('mal', 1, 'Kaguya First Kiss',
                               progress=4, total=4)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Kaguya First Kiss',
                                   progress=0, total=1, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    plan = engine.plan()
    assert not plan.conflicts
    pushes = [c for c in plan.changes if c.target == 'kitsu'
              and c.field == 'progress']
    assert len(pushes) == 1 and pushes[0].new == 1
    engine.apply(plan)
    assert kitsu.shows['k1']['my_progress'] == 1
    # Bases recorded in each provider's RAW structure.
    uid = store.mapping_for('mal', '1')['uuid']
    assert store.base_get(uid, 'kitsu')['progress'] == 1
    assert store.base_get(uid, 'mal')['progress'] == 4


def test_incomparable_partials_freeze_as_structural_conflict(store):
    mal = FakeLib('mal', [show('mal', 1, 'Kaguya First Kiss',
                               progress=2, total=4)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Kaguya First Kiss',
                                   progress=0, total=1, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    plan = engine.plan()
    structural = [c for c in plan.conflicts if c.structural]
    assert structural and structural[0].field == 'progress'
    # The frozen field produced no push either way.
    assert [c for c in plan.changes if c.field == 'progress'] == []


# -- creating entries -------------------------------------------------

def test_create_offer_applies_when_ticked(store):
    mal = FakeLib('mal', [])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=5, score=4, mal_id=77)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    plan = engine.plan()
    creates = [c for c in plan.changes if c.creates_entry]
    assert creates and all(not c.selected for c in creates)
    assert all(c.target == 'mal' for c in creates)
    for c in creates:
        c.selected = True   # the user opts in
    result = engine.apply(plan)
    assert result['errors'] == {}
    assert mal.shows['77']['my_progress'] == 5


def test_create_offer_respects_can_add(store):
    mal = FakeLib('mal', [], extra_info={'can_add': False})
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Frieren',
                                   progress=5, mal_id=77)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    assert [c for c in engine.plan().changes if c.creates_entry] == []
