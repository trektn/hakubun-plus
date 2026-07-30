"""Owner-first scoring: a `provider:<owner>` score policy makes the
owner the single source of truth -- its score converts into every other
provider's scale and overwrites them, the owner itself is never pushed
to, and the plan converges (no field re-proposes on the next cycle).

These lock in the behavior behind the "Score owner: AniList, convert and
overwrite others" workflow, and document the contrast: under the default
LOCAL policy the *intended* owner is instead overwritten by whoever the
local/primary value came from -- which is why the owner must be set
explicitly, not left on the default.
"""
from hakubun.sync.models import FieldPolicy, PolicyKind, SyncMode
from conftest import FakeLib, show, make_engine


def _ngnl_engine(store, extra_ownership=None):
    """No Game No Life rated on all three sites at 'the same' score in
    each site's own scale: AniList 85/100, MAL 9/10, Kitsu 4/5. These
    are NOT equal once normalized to canonical 0-10 (8.5 / 9.0 / 8.0),
    so every pairing genuinely diverges -- the interesting case."""
    mal = FakeLib('mal', [show('mal', 1, 'NGNL', mal_id=1, score=9)])
    anilist = FakeLib('anilist', [show('anilist', 9, 'NGNL',
                                        mal_id=1, score=85)])
    kitsu = FakeLib('kitsu', [show('kitsu', 5, 'NGNL', mal_id=1, score=4)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist,
                                 'kitsu': kitsu})
    return engine, mal, anilist, kitsu


def _score_pushes(plan, target):
    return [(c.old, c.new) for c in plan.changes
            if c.field == 'score' and c.target == target]


def _accept_first_sync(plan):
    """Tick the first-sync overwrites the planner deliberately leaves
    unticked (FieldChange.first_sync) -- the user opting in from the
    preview. Every scenario here is a FIRST sync of three lists that
    already disagree, so without this nothing that overwrites a real
    value would apply, which is exactly the intended safety."""
    accepted = [c for c in plan.changes if c.first_sync]
    for change in accepted:
        change.selected = True
    return accepted


def test_owner_converts_and_overwrites_others_never_pushing_owner(store):
    engine, mal, anilist, kitsu = _ngnl_engine(store)
    engine.primary = 'anilist'
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'anilist'))

    engine.fetch()
    plan = engine.plan()
    # The owner is the source of truth: nothing is ever pushed to it,
    # and the others receive its score converted to their own scale.
    assert _score_pushes(plan, 'anilist') == []
    assert plan.conflicts == []
    # First contact with MAL/Kitsu for this field: the overwrites are
    # planned but unticked until the user says so.
    assert _accept_first_sync(plan)
    engine.apply(plan)
    assert anilist.shows['9']['my_score'] == 85            # untouched
    assert mal.shows['1']['my_score'] == 9                 # 8.5 -> 9 (half up)
    assert kitsu.shows['5']['my_score'] == 4.5             # 8.5 -> 4.5 stars

    # Converged: a second fetch/plan proposes nothing (no re-proposing).
    engine.fetch()
    assert [c for c in engine.plan().changes if c.field == 'score'] == []


def test_owner_first_holds_regardless_of_which_account_is_primary(store):
    for primary in ('anilist', 'mal', 'kitsu', None):
        s = type(store)(':memory:')
        try:
            engine, mal, anilist, kitsu = _ngnl_engine(s)
            engine.primary = primary
            store_pol = FieldPolicy(PolicyKind.PROVIDER, 'anilist')
            s.set_ownership('score', store_pol)
            engine.fetch()
            plan = engine.plan()
            assert _score_pushes(plan, 'anilist') == [], primary
            engine.apply(plan)
            assert anilist.shows['9']['my_score'] == 85, primary
        finally:
            s.close()


def test_lossy_owner_scale_still_never_re_pushes_to_owner(store):
    """Even when the owner's own scale can't round-trip a canonical
    value (AniList in 5-star mode: 0-5 step 1), the owner is never
    re-pushed and the plan still converges.

    Note the deliberate nuance this pins: with a coarse owner, a finer
    value already consistent with it is NOT overridden -- AniList at 4
    stars reads as canonical 8.0, but MAL's 9.0 ALSO renders as 4 stars
    on AniList's scale, so the owner has nothing to correct there and
    MAL keeps 9. (The user's real case is a 100-point AniList, which is
    fine-grained enough that the owner's exact score does propagate --
    see the first test.) The invariant that always holds regardless of
    scale: the owner is a source, never a push target, and nothing
    re-proposes."""
    engine, mal, anilist, kitsu = _ngnl_engine(store)
    # Force AniList into a coarse 5-star format (raw 4 == canonical 8.0).
    anilist.shows['9']['my_score'] = 4
    anilist.media_info = lambda: {'mediatype': 'anime', 'score_max': 5,
                                  'score_step': 1, 'can_score': True,
                                  'can_status': True, 'can_update': True,
                                  'can_date': True, 'can_tag': True,
                                  'statuses_dict': {}}
    engine.primary = 'mal'          # signed into a DIFFERENT account
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'anilist'))

    # Drive to convergence: a coarse owner alongside a fine provider can
    # take an extra cycle to settle (the fine provider syncs before the
    # owner's real vote surfaces from precision-collapse), but the owner
    # is NEVER a push target on any cycle, and it converges.
    for cycle in range(5):
        engine.fetch()
        plan = engine.plan()
        assert _score_pushes(plan, 'anilist') == [], cycle
        if not [c for c in plan.changes if c.field == 'score']:
            break
        _accept_first_sync(plan)
        engine.apply(plan)
    uid = store.mapping_for('mal', '1')['uuid']
    assert anilist.shows['9']['my_score'] == 4          # owner never touched
    assert store.local_get(uid)['score'][0] == 8.0      # owner's value won


def test_default_local_policy_is_why_the_owner_got_overwritten(store):
    """Contrast/regression: with score left on the DEFAULT (local) policy
    and a non-AniList primary, AniList -- the site the user *thinks* owns
    the score -- is the one that gets TARGETED for overwrite. This is the
    footgun the explicit owner setting exists to avoid; if a future
    change makes 'local' stop pushing to the intended owner, revisit the
    docs/UI that tell users to set ownership.

    On a FIRST sync that overwrite is planned but not armed: there is no
    merge base with AniList, so nothing actually 'changed' -- local is
    just whichever list was read first. The change is flagged
    first_sync and left unticked, so an unattended Sync cannot fire it;
    it only lands once the user ticks it in the preview."""
    engine, mal, anilist, kitsu = _ngnl_engine(store)
    engine.primary = 'mal'          # local value comes from MAL (9.0)
    # (no set_ownership: score stays on DEFAULT_OWNERSHIP -> LOCAL)
    engine.fetch()
    plan = engine.plan()
    assert _score_pushes(plan, 'anilist') == [(8.5, 9.0)]   # owner targeted
    clobber = [c for c in plan.changes
               if c.field == 'score' and c.target == 'anilist'][0]
    assert clobber.first_sync and not clobber.selected

    # Applying as-is leaves AniList alone -- the whole point.
    engine.apply(plan)
    assert anilist.shows['9']['my_score'] == 85

    # Ticking it is what actually clobbers the owner.
    _accept_first_sync(plan)
    engine.apply(plan)
    assert anilist.shows['9']['my_score'] == 90             # MAL's 9 won
