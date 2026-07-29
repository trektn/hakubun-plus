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
