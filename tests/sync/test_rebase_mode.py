"""REBASE preview mode: retroactively force each field's declared owner
onto local and every other tracker, ignoring the merge base.

This is the answer to "I set MAL to own scores but nothing merged to
Kitsu/AniList": ownership only breaks ties on divergence, so under MERGE
an ownership change stays inert until a value actually moves. REBASE
re-asserts the owner as the single source of truth and pushes it out to
everyone, no matter what the three-way diff says.
"""
from hakubun.sync.models import FieldPolicy, PolicyKind, SyncMode
from conftest import FakeLib, show, make_engine


def _pushes(plan, field, target):
    return [(c.old, c.new) for c in plan.changes
            if c.field == field and c.target == target]


def _divergent_engine(store):
    """No Game No Life rated differently on each tracker: MAL 9/10,
    AniList 85/100, Kitsu 4/5 -> canonical 9.0 / 8.5 / 8.0. Fetched but
    not yet reconciled; local seeds from the first-listed provider (MAL,
    9.0)."""
    mal = FakeLib('mal', [show('mal', 1, 'NGNL', mal_id=1, score=9)])
    anilist = FakeLib('anilist', [show('anilist', 9, 'NGNL',
                                       mal_id=1, score=85)])
    kitsu = FakeLib('kitsu', [show('kitsu', 5, 'NGNL', mal_id=1, score=4)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist,
                                 'kitsu': kitsu})
    engine.primary = 'anilist'
    engine.fetch()
    uid = store.mapping_for('mal', '1')['uuid']
    assert store.local_get(uid)['score'][0] == 9.0    # seeded from MAL
    return engine, mal, anilist, kitsu, uid


def test_rebase_forces_provider_owner_onto_every_other_tracker(store):
    engine, mal, anilist, kitsu, uid = _divergent_engine(store)
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'mal'))

    plan = engine.plan(mode=SyncMode.REBASE)
    # MAL (9.0) is forced onto AniList and Kitsu; MAL itself is never a
    # push target; no conflicts are ever raised in rebase.
    assert _pushes(plan, 'score', 'anilist') == [(8.5, 9.0)]
    assert _pushes(plan, 'score', 'kitsu') == [(8.0, 9.0)]
    assert _pushes(plan, 'score', 'mal') == []
    assert plan.conflicts == []
    engine.apply(plan)
    assert anilist.shows['9']['my_score'] == 90        # 9.0 -> /100
    assert kitsu.shows['5']['my_score'] == 4.5         # 9.0 -> 4.5 stars
    assert mal.shows['1']['my_score'] == 9             # owner untouched

    # Converged: a second rebase proposes nothing.
    engine.fetch()
    assert [c for c in engine.plan(mode=SyncMode.REBASE).changes
            if c.field == 'score'] == []


def test_rebase_owner_can_pull_local_down_and_push_the_rest(store):
    """When the owner isn't the provider that happened to seed local,
    rebase overwrites LOCAL too (retroactive), then propagates."""
    engine, mal, anilist, kitsu, uid = _divergent_engine(store)
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'anilist'))

    plan = engine.plan(mode=SyncMode.REBASE)
    # AniList (8.5) becomes the truth: local 9.0 -> 8.5, Kitsu -> 4.5.
    # MAL is left alone because 8.5 and its current 9.0 both render as
    # MAL 9 (nothing would actually change on MAL).
    assert _pushes(plan, 'score', 'local') == [(9.0, 8.5)]
    assert _pushes(plan, 'score', 'kitsu') == [(8.0, 8.5)]
    assert _pushes(plan, 'score', 'anilist') == []     # owner untouched
    assert _pushes(plan, 'score', 'mal') == []         # 9.0/8.5 both -> MAL 9
    engine.apply(plan)
    assert store.local_get(uid)['score'][0] == 8.5
    assert kitsu.shows['5']['my_score'] == 4.5


def test_rebase_local_owner_pushes_local_out(store):
    engine, mal, anilist, kitsu, uid = _divergent_engine(store)
    # score stays on the default 'local' policy; local is 9.0 (from MAL).
    plan = engine.plan(mode=SyncMode.REBASE)
    assert _pushes(plan, 'score', 'anilist') == [(8.5, 9.0)]
    assert _pushes(plan, 'score', 'kitsu') == [(8.0, 9.0)]
    engine.apply(plan)
    assert anilist.shows['9']['my_score'] == 90
    assert kitsu.shows['5']['my_score'] == 4.5


def test_rebase_leaves_unowned_fields_alone(store):
    engine, mal, anilist, kitsu, uid = _divergent_engine(store)
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'mal'))
    plan = engine.plan(mode=SyncMode.REBASE)
    # tags default to Merge, notes to Individual -- no single owner, so
    # rebase touches only the owned score.
    assert {c.field for c in plan.changes} == {'score'}


def _kitsu_only_with_known_mal_id(store):
    """A show that exists only on Kitsu, which publishes a MAL id
    (56566) MAL's own account has never actually added -- exactly the
    'recognized but never pushed' bug report this feature answers.
    MAL is still a connected/fetched provider, just with an empty list,
    so identity resolution's pre-emptive mapping (from Kitsu's
    published mal_id) sits unused: a real mapping row with no remote
    snapshot behind it."""
    mal = FakeLib('mal', [])
    kitsu = FakeLib('kitsu', [show('kitsu', 47546, 'Undead Girl Murder Farce',
                                   progress=5, score=4, mal_id=56566)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    engine.fetch()
    uid = store.mapping_for('kitsu', '47546')['uuid']
    assert store.mapping_for('mal', '56566')['uuid'] == uid   # pre-linked
    assert store.remote_get('mal', '56566') == {}             # never fetched
    return engine, mal, kitsu, uid


def test_rebase_creates_missing_entry_on_a_connected_provider(store):
    engine, mal, kitsu, uid = _kitsu_only_with_known_mal_id(store)
    plan = engine.plan(mode=SyncMode.REBASE)

    creates = [c for c in plan.changes if c.creates_entry]
    assert {c.target for c in creates} == {'mal'}
    assert all(c.target != 'kitsu' for c in plan.changes)   # already exists
    # Unselected by default: adding to a real account is opt-in.
    assert all(not c.selected for c in creates)
    score_create = next(c for c in creates if c.field == 'score')
    assert score_create.old is None
    assert score_create.new == 8.0   # Kitsu 4/5 stars -> canonical 8.0
    # score defaults to the LOCAL policy: source says so, not the
    # provider that happened to seed local (Kitsu) -- "where did this
    # come from" must be answerable from the change alone.
    assert score_create.source == 'local'

    for c in creates:
        c.selected = True
    engine.apply(plan)

    assert mal.shows['56566']['my_score'] == 8
    assert mal.shows['56566']['my_progress'] == 5
    assert store.remote_get('mal', '56566')['score'][0] == 8.0
    assert store.base_get(uid, 'mal').get('score') == 8.0


def test_rebase_missing_entry_not_selected_stays_unapplied(store):
    engine, mal, kitsu, uid = _kitsu_only_with_known_mal_id(store)
    plan = engine.plan(mode=SyncMode.REBASE)
    engine.apply(plan)   # nothing ticked -- must be a no-op for MAL
    assert mal.shows == {}
    assert store.remote_get('mal', '56566') == {}


def test_rebase_add_skips_provider_without_can_add(store):
    mal = FakeLib('mal', [], extra_info={'can_add': False})
    kitsu = FakeLib('kitsu', [show('kitsu', 1, 'X', score=4, mal_id=9)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    engine.fetch()
    plan = engine.plan(mode=SyncMode.REBASE)
    assert [c for c in plan.changes if c.creates_entry] == []


def test_rebase_create_credits_the_actual_provider_owner(store):
    """A provider-owned field's create value is attributed to that
    provider, not blanket-labeled 'local' -- the UI's 'from %s' note
    (present.change_line) reads change.source directly."""
    engine, mal, kitsu, uid = _kitsu_only_with_known_mal_id(store)
    store.set_ownership('progress', FieldPolicy(PolicyKind.PROVIDER, 'kitsu'))
    plan = engine.plan(mode=SyncMode.REBASE)
    progress_create = next(c for c in plan.changes
                           if c.creates_entry and c.field == 'progress')
    assert progress_create.source == 'kitsu'


def test_rebase_create_falls_back_to_local_when_the_owner_is_missing(store):
    """A field owned by the very provider being created has no value
    to draw from there -- fall back to local instead of leaving a
    brand-new entry blank on that field."""
    engine, mal, kitsu, uid = _kitsu_only_with_known_mal_id(store)
    # MAL is the missing provider; make it the declared owner of
    # status too, on top of the default LOCAL score/progress/etc.
    store.set_ownership('status', FieldPolicy(PolicyKind.PROVIDER, 'mal'))
    plan = engine.plan(mode=SyncMode.REBASE)
    status_create = next(c for c in plan.changes
                         if c.creates_entry and c.target == 'mal'
                         and c.field == 'status')
    assert status_create.source == 'local'
    assert status_create.new == store.local_get(uid)['status'][0]
