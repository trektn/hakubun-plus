"""Sync pipeline flows: conflicts, policies, failure, rollback, modes."""

import pytest

from hakubun.sync.models import (FieldPolicy, PolicyKind, SyncMode)

from conftest import FakeLib, make_engine, show


def _setup(store, mal_show=None, anilist_show=None):
    libs = {}
    if mal_show is not None:
        libs['mal'] = FakeLib('mal', [mal_show])
    if anilist_show is not None:
        libs['anilist'] = FakeLib('anilist', [anilist_show])
    eng = make_engine(store, libs)
    assert eng.fetch() == {}
    return eng, libs


def _uid(store):
    ents = store.entities()
    assert len(ents) == 1
    return ents[0]['uuid']


def test_conflicting_ratings_owner_wins_and_rounds(store):
    """Score diverges on both sides; owner (AniList) wins; the value
    pushed to MAL is rounded to its integer scale."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', score=7),           # 7.0
                       show('anilist', 9, 'Bebop', mal_id=1, score=70))
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER,
                                             'anilist'))
    # Diverge: local edited to 9.0; AniList edited to 8.4 (84/100).
    store.local_set(_uid(store), 'score', 9.0)
    libs['anilist'].shows['9']['my_score'] = 84
    assert eng.fetch() == {}
    plan = eng.plan()
    assert not plan.conflicts
    pulls = [c for c in plan.changes if c.target == 'local'
             and c.field == 'score']
    assert len(pulls) == 1 and pulls[0].new == 8.4
    assert pulls[0].source == 'anilist'
    result = eng.apply(plan)
    assert result['errors'] == {}
    # MAL received the rounded projection of 8.4 -> 8.
    mal_scores = [u['my_score'] for u in libs['mal'].updates
                  if 'my_score' in u]
    assert mal_scores == [8]
    # Converged: the next plan is quiet (8.4 projects to MAL's 8).
    assert eng.fetch() == {}
    plan2 = eng.plan()
    assert [c for c in plan2.changes if c.field == 'score'] == []


def test_conflicting_notes_individual_policy_never_syncs(store):
    """Notes policy 'individual': differing notes are left alone --
    no changes, no conflicts (default policy)."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', my_notes='mal notes'),
                       show('anilist', 9, 'Bebop', mal_id=1,
                            my_notes='anilist notes'))
    store.local_set(_uid(store), 'notes', 'my own local notes')
    assert eng.fetch() == {}
    plan = eng.plan()
    assert [c for c in plan.changes if c.field == 'notes'] == []
    assert [c for c in plan.conflicts if c.field == 'notes'] == []


def test_unknown_episode_count_never_conflicts(store):
    """A provider with total=None (airing, incomplete metadata) fills
    in from the provider that knows; nothing conflicts."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Airing Show', total=26),
                       show('anilist', 9, 'Airing Show', mal_id=1,
                            total=None))
    ent = store.entities()[0]
    assert ent['total'] == 26           # known value filled the unknown
    plan = eng.plan()
    assert plan.conflicts == []


def test_simultaneous_edits_ask_policy_conflicts_then_resolves(store):
    """Same field changed locally and remotely with 'ask' (reconciled
    by user): a conflict surfaces; resolution pushes the choice."""
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    store.set_ownership('progress', FieldPolicy(PolicyKind.ASK))
    uid = _uid(store)
    store.local_set(uid, 'progress', 5)          # local edit
    libs['mal'].shows['1']['my_progress'] = 7    # simultaneous remote edit
    assert eng.fetch() == {}
    plan = eng.plan()
    conflicts = [c for c in plan.conflicts if c.field == 'progress']
    assert len(conflicts) == 1
    assert conflicts[0].values == {'local': 5, 'mal': 7}
    # No change is applied while the conflict stands.
    assert [c for c in plan.changes if c.field == 'progress'] == []

    eng.resolve_conflict(conflicts[0], 'local')  # user keeps 5
    plan2 = eng.plan()
    assert not [c for c in plan2.conflicts if c.field == 'progress']
    pushes = [c for c in plan2.changes
              if c.field == 'progress' and c.target == 'mal']
    assert len(pushes) == 1 and pushes[0].new == 5
    eng.apply(plan2)
    assert libs['mal'].shows['1']['my_progress'] == 5


def test_provider_api_failure_is_isolated(store):
    """One provider failing mid-apply doesn't lose the local commit or
    block the other provider; its changes re-plan afterwards."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', progress=3),
                       show('anilist', 9, 'Bebop', mal_id=1, progress=3))
    uid = _uid(store)
    store.local_set(uid, 'progress', 8)
    libs['mal'].fail_update = True
    plan = eng.plan()
    targets = {c.target for c in plan.changes if c.field == 'progress'}
    assert targets == {'mal', 'anilist'}
    result = eng.apply(plan)
    assert 'mal' in result['errors']
    assert libs['anilist'].shows['9']['my_progress'] == 8
    assert libs['mal'].shows['1']['my_progress'] == 3   # unchanged
    # Failed pushes re-plan; succeeded ones don't.
    plan2 = eng.plan()
    targets2 = {c.target for c in plan2.changes if c.field == 'progress'}
    assert targets2 == {'mal'}
    libs['mal'].fail_update = False
    result2 = eng.apply(plan2)
    assert result2['errors'] == {}
    assert libs['mal'].shows['1']['my_progress'] == 8
    assert eng.plan().changes == []


def test_fetch_failure_is_isolated_too(store):
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop'),
                       show('anilist', 9, 'Bebop', mal_id=1))
    libs['mal'].fail_fetch = True
    errors = eng.fetch()
    assert set(errors) == {'mal'}


def test_rollback_of_local_edit_plans_compensating_push(store):
    """Undo of an edit whose value was already pushed restores local
    state and re-plans a compensating push of the restored value."""
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    uid = _uid(store)
    edit_txn = eng.edit_local(uid, 'progress', 6)
    result = eng.apply(eng.plan())
    assert result['errors'] == {}
    assert libs['mal'].shows['1']['my_progress'] == 6
    undo_txn = eng.undo(edit_txn)
    assert undo_txn
    assert store.local_get(uid)['progress'][0] == 3     # restored
    events = store.events_of_txn(undo_txn)
    assert all(e['op'] == 'undo' for e in events)
    plan2 = eng.plan()
    pushes = [c for c in plan2.changes
              if c.field == 'progress' and c.target == 'mal']
    assert len(pushes) == 1 and pushes[0].new == 3      # compensating
    eng.apply(plan2)
    assert libs['mal'].shows['1']['my_progress'] == 3
    # Double-undo is refused.
    with pytest.raises(ValueError):
        eng.undo(edit_txn)


def test_rollback_of_applied_pull(store):
    """Undo of an apply that pulled a remote change restores the local
    value and plans the push that re-asserts it."""
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    uid = _uid(store)
    libs['mal'].shows['1']['my_progress'] = 9           # website edit
    assert eng.fetch() == {}
    result = eng.apply(eng.plan())                      # pulls 9
    assert store.local_get(uid)['progress'][0] == 9
    eng.undo(result['txn'])
    assert store.local_get(uid)['progress'][0] == 3
    plan = eng.plan()
    pushes = [c for c in plan.changes
              if c.field == 'progress' and c.target == 'mal']
    assert len(pushes) == 1 and pushes[0].new == 3


def test_mirror_mode_pushes_local_over_remote_changes(store):
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    libs['mal'].shows['1']['my_progress'] = 9       # remote edit
    assert eng.fetch() == {}
    plan = eng.plan(SyncMode.MIRROR)
    assert plan.conflicts == []
    pushes = [c for c in plan.changes if c.field == 'progress']
    assert len(pushes) == 1
    assert pushes[0].target == 'mal' and pushes[0].new == 3
    eng.apply(plan)
    assert libs['mal'].shows['1']['my_progress'] == 3


def test_pull_mode_updates_local_and_never_pushes(store):
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    uid = _uid(store)
    store.local_set(uid, 'progress', 5)             # local edit
    libs['mal'].shows['1']['my_progress'] = 9       # remote edit too
    assert eng.fetch() == {}
    plan = eng.plan(SyncMode.PULL)
    assert plan.conflicts == []
    changes = [c for c in plan.changes if c.field == 'progress']
    assert len(changes) == 1 and changes[0].target == 'local'
    assert changes[0].new == 9
    eng.apply(plan)
    assert store.local_get(uid)['progress'][0] == 9
    assert libs['mal'].updates == []                # nothing pushed


def test_remote_only_edit_pulls_cleanly_under_local_policy(store):
    """A pure website edit (local untouched) must pull, not be
    overwritten, even though the policy is 'local' -- ownership decides
    divergence, not single-sided changes."""
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    uid = _uid(store)
    libs['mal'].shows['1']['my_progress'] = 4
    assert eng.fetch() == {}
    plan = eng.plan()
    changes = [c for c in plan.changes if c.field == 'progress']
    assert len(changes) == 1 and changes[0].target == 'local'
    assert changes[0].new == 4
    eng.apply(plan)
    assert store.local_get(uid)['progress'][0] == 4


def test_event_log_records_every_modification(store):
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    uid = _uid(store)
    eng.edit_local(uid, 'progress', 6)
    eng.apply(eng.plan())
    all_events = store.events_query(uid=uid, field='progress')
    ops = {e['op'] for e in all_events}
    assert {'set', 'push'} <= ops
    sets = [e for e in all_events
            if e['op'] == 'set' and e['source'] == 'local']
    assert sets[0]['old_value'] == 3 and sets[0]['new_value'] == 6
    pushes = [e for e in all_events if e['op'] == 'push']
    assert pushes[0]['source'] == 'mal'
    history = eng.history.watch_history(uid)
    assert history and history[0]['to'] == 6


def test_primary_provider_edit_is_local_intent(store):
    """The signed-in account is the working tree: its changes fold into
    local without policy friction -- even under 'ask' they are not a
    conflict against the reconciled DB's stale value."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', progress=3),
                       show('anilist', 9, 'Bebop', mal_id=1, progress=3))
    store.set_ownership('progress', FieldPolicy(PolicyKind.ASK))
    eng.primary = 'mal'
    libs['mal'].shows['1']['my_progress'] = 7    # edit made in the app
    assert eng.fetch() == {}
    plan = eng.plan()
    assert plan.conflicts == []
    locals_ = [c for c in plan.changes
               if c.field == 'progress' and c.target == 'local']
    assert len(locals_) == 1 and locals_[0].new == 7
    assert locals_[0].source == 'mal'
    pushes = [c for c in plan.changes
              if c.field == 'progress' and c.target == 'anilist']
    assert len(pushes) == 1 and pushes[0].new == 7
    eng.apply(plan)
    assert libs['anilist'].shows['9']['my_progress'] == 7


def test_owner_still_beats_primary_intent(store):
    """Field ownership arbitrates between the working tree's intent and
    the other providers: Score -> AniList wins over a Kitsu/MAL-side
    edit, and MAL receives the rounded projection."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', score=7),
                       show('anilist', 9, 'Bebop', mal_id=1, score=70))
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER,
                                             'anilist'))
    eng.primary = 'mal'
    libs['mal'].shows['1']['my_score'] = 9        # app-side edit
    libs['anilist'].shows['9']['my_score'] = 84   # owner's own edit
    assert eng.fetch() == {}
    plan = eng.plan()
    assert plan.conflicts == []
    locals_ = [c for c in plan.changes
               if c.field == 'score' and c.target == 'local']
    assert len(locals_) == 1 and locals_[0].new == 8.4
    assert locals_[0].source == 'anilist'
    eng.apply(plan)
    assert libs['mal'].shows['1']['my_score'] == 8


def test_primary_vs_other_divergence_still_asks(store):
    """'Ask' still guards genuine cross-provider divergence: the
    conflict is between the app-side intent and the other provider,
    and the intent itself is still recorded into local state."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', progress=3),
                       show('anilist', 9, 'Bebop', mal_id=1, progress=3))
    store.set_ownership('progress', FieldPolicy(PolicyKind.ASK))
    eng.primary = 'mal'
    libs['mal'].shows['1']['my_progress'] = 7      # app edit
    libs['anilist'].shows['9']['my_progress'] = 9  # website edit elsewhere
    assert eng.fetch() == {}
    plan = eng.plan()
    conflicts = [c for c in plan.conflicts if c.field == 'progress']
    assert len(conflicts) == 1
    assert conflicts[0].values == {'local': 7, 'anilist': 9}
    locals_ = [c for c in plan.changes
               if c.field == 'progress' and c.target == 'local']
    assert len(locals_) == 1 and locals_[0].new == 7
    assert not [c for c in plan.changes
                if c.field == 'progress' and c.target != 'local']


def test_progress_completion_across_structures_is_equivalent(store):
    """Kaguya First Kiss: a 1-episode movie on MAL/Kitsu, a 4-episode
    listing on AniList. 1/1 and 4/4 are the same fact -- no changes, no
    conflicts, and definitely no 'update AniList to 1'."""
    eng, libs = _setup(store,
                       show('mal', 1, 'First Kiss', progress=1, total=1),
                       show('anilist', 9, 'First Kiss', mal_id=1,
                            progress=4, total=4))
    assert eng.fetch() == {}
    plan = eng.plan()
    assert [c for c in plan.changes if c.field == 'progress'] == []
    assert [c for c in plan.conflicts if c.field == 'progress'] == []


def test_progress_completion_converts_on_push(store):
    """Completing the 4-episode listing on the active account pushes 1
    (the movie's own total) to the movie entry -- never a raw 4."""
    eng, libs = _setup(store,
                       show('mal', 1, 'First Kiss', progress=0, total=1),
                       show('anilist', 9, 'First Kiss', mal_id=1,
                            progress=4, total=4))
    eng.primary = 'anilist'
    assert eng.fetch() == {}
    plan = eng.plan()
    assert plan.conflicts == []
    pushes = [c for c in plan.changes
              if c.field == 'progress' and c.target == 'mal']
    assert len(pushes) == 1 and pushes[0].new == 1     # converted!
    # And the AniList side is left alone (it already reads complete).
    assert not [c for c in plan.changes
                if c.field == 'progress' and c.target == 'anilist']
    eng.apply(plan)
    assert libs['mal'].shows['1']['my_progress'] == 1
    # Converged: replan is quiet.
    assert eng.fetch() == {}
    assert [c for c in eng.plan().changes if c.field == 'progress'] == []


def test_partial_progress_across_structures_conflicts_with_note(store):
    """Episode 2 of a 4-episode listing has no meaningful projection
    onto a 1-episode movie: surfaced once, honestly, never guessed."""
    eng, libs = _setup(store,
                       show('mal', 1, 'First Kiss', progress=0, total=1),
                       show('anilist', 9, 'First Kiss', mal_id=1,
                            progress=2, total=4))
    eng.primary = 'anilist'
    assert eng.fetch() == {}
    plan = eng.plan()
    conflicts = [c for c in plan.conflicts if c.field == 'progress']
    assert len(conflicts) == 1
    assert 'episode structures differ' in conflicts[0].note
    assert not [c for c in plan.changes if c.field == 'progress'
                and c.target != 'local']
