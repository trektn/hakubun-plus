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


def test_mal_rounding_alone_is_never_a_conflict(store):
    """local 9.5, MAL 10 (MAL's own rounding of 9.5), no other
    provider present: MAL offers no independent information -- fully
    silent, no conflict, no change, no push."""
    eng, libs = _setup(store, show('mal', 1, 'Frieren', score=0))
    store.set_ownership('score', FieldPolicy(PolicyKind.ASK))
    uid = _uid(store)
    eng.edit_local(uid, 'score', 9.5)
    libs['mal'].shows['1']['my_score'] = 10   # MAL: only integers
    assert eng.fetch() == {}
    plan = eng.plan()
    assert [c for c in plan.conflicts if c.field == 'score'] == []
    assert [c for c in plan.changes if c.field == 'score'] == []


def test_mal_rounding_collapses_out_of_a_real_anilist_conflict(store):
    """Screenshot case 1: local 9.5 / AniList 9.9 / MAL 10. MAL's '10'
    is just what AniList's 9.9 (or local's 9.5) rounds to on MAL's
    scale -- it must not appear as a third option. Only AniList's
    genuinely different, more precise number is a real conflict."""
    eng, libs = _setup(store,
                       show('mal', 1, '3-gatsu no Lion', score=0),
                       show('anilist', 9, '3-gatsu no Lion', mal_id=1,
                            score=0))
    store.set_ownership('score', FieldPolicy(PolicyKind.ASK))
    uid = _uid(store)
    eng.edit_local(uid, 'score', 9.5)
    libs['anilist'].shows['9']['my_score'] = 99   # canonical 9.9
    libs['mal'].shows['1']['my_score'] = 10
    assert eng.fetch() == {}
    plan = eng.plan()
    conflicts = [c for c in plan.conflicts if c.field == 'score']
    assert len(conflicts) == 1
    assert conflicts[0].values == {'local': 9.5, 'anilist': 9.9}
    assert 'mal' not in conflicts[0].values


def test_duplicate_provider_scores_collapse_to_one_option(store):
    """Screenshot case 2: local 2.5 / AniList 3 / MAL 3. AniList and
    MAL exactly agree with each other -- MAL's vote is a duplicate of
    AniList's, not independent corroboration worth a separate button."""
    eng, libs = _setup(store,
                       show('mal', 1, '3D Kanojo', score=0),
                       show('anilist', 9, '3D Kanojo', mal_id=1, score=0))
    store.set_ownership('score', FieldPolicy(PolicyKind.ASK))
    uid = _uid(store)
    eng.edit_local(uid, 'score', 2.5)
    libs['anilist'].shows['9']['my_score'] = 30   # canonical 3.0
    libs['mal'].shows['1']['my_score'] = 3
    assert eng.fetch() == {}
    plan = eng.plan()
    conflicts = [c for c in plan.conflicts if c.field == 'score']
    assert len(conflicts) == 1
    assert conflicts[0].values == {'local': 2.5, 'anilist': 3.0}


def test_genuine_mal_only_edit_still_surfaces(store):
    """MAL disagreeing with EVERYONE (not explained by local or any
    other provider) is a real edit and must still be asked about."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', score=0),
                       show('anilist', 9, 'Bebop', mal_id=1, score=0))
    store.set_ownership('score', FieldPolicy(PolicyKind.ASK))
    uid = _uid(store)
    eng.edit_local(uid, 'score', 8.0)
    libs['anilist'].shows['9']['my_score'] = 80   # agrees with local: 8.0
    libs['mal'].shows['1']['my_score'] = 5        # real, unexplained edit
    assert eng.fetch() == {}
    plan = eng.plan()
    conflicts = [c for c in plan.conflicts if c.field == 'score']
    assert len(conflicts) == 1
    assert conflicts[0].values == {'local': 8.0, 'mal': 5.0}


def test_push_includes_title_for_the_libs_own_logging(store):
    """Field report: 'cannot sync. Apply failed 'title'' -- a bare
    KeyError('title') str()s to exactly that. Every real lib's
    update_show()/delete_show() unconditionally reads item['title']
    for a log line (not the actual API payload, which is id/my_* only)
    -- the adapter's minimal patch dict must always supply one."""
    eng, libs = _setup(store, show('mal', 1, 'Cowboy Bebop', progress=3))
    uid = _uid(store)
    eng.edit_local(uid, 'progress', 8)
    result = eng.apply(eng.plan())
    assert result['errors'] == {}
    assert libs['mal'].updates[-1]['title'] == 'Cowboy Bebop'
    assert libs['mal'].shows['1']['my_progress'] == 8


def test_date_pushes_are_real_date_objects(store):
    """Field report: \"apply failed 'str' object has no attribute
    'year'\" -- canonical dates are ISO strings, but the libs expect
    datetime.date (libanilist reads .year/.month/.day directly).
    Pushing a date must convert; the whole apply crashed otherwise."""
    import datetime
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    uid = _uid(store)
    eng.edit_local(uid, 'start_date', '2026-07-14')
    result = eng.apply(eng.plan())
    assert result['errors'] == {}
    pushed = libs['mal'].updates[-1]['my_start_date']
    assert isinstance(pushed, datetime.date)
    assert pushed == datetime.date(2026, 7, 14)
    # Roundtrip converges: the provider now reports the date object,
    # which normalizes back to the same canonical string -- quiet plan.
    assert eng.fetch() == {}
    assert [c for c in eng.plan().changes if c.field == 'start_date'] == []


def test_kitsu_pushes_carry_the_library_entry_id(store):
    """Both Kitsu backends address updates by item['my_id'] (the
    library-entry id from the fetch), not the media id -- read
    unconditionally, so a push without it dies. It must survive the
    whole trip: fetch -> remote-state '_my_id' -> apply -> push."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', progress=3),
                       show('anilist', 9, 'Bebop', mal_id=1, progress=3))
    # Add a kitsu side by exact-title auto-link.
    kitsu = FakeLib('kitsu', [show('kitsu', 77, 'Bebop', progress=3)])
    from hakubun.sync.adapters import ProviderAdapter
    eng.adapters['kitsu'] = ProviderAdapter('kitsu', kitsu)
    assert eng.fetch() == {}
    assert store.mapping_for('kitsu', '77') is not None

    uid = _uid(store)
    eng.edit_local(uid, 'progress', 8)
    result = eng.apply(eng.plan())
    assert result['errors'] == {}, result['errors']
    assert kitsu.updates, 'kitsu never received the push'
    assert kitsu.updates[-1]['my_id'] == 'entry-77'
    assert kitsu.shows['77']['my_progress'] == 8


def test_unexpected_lib_crash_degrades_to_provider_error(store):
    """Boundary hardening: three field crashes in a row came from libs
    reading keys/types the adapter didn't supply. Whatever the NEXT
    one is, it must isolate to that provider (error in the report,
    changes re-planned), never kill the entire apply."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', progress=3),
                       show('anilist', 9, 'Bebop', mal_id=1, progress=3))
    uid = _uid(store)
    eng.edit_local(uid, 'progress', 8)

    def explode(item):
        raise RuntimeError('surprise from deep inside a lib')
    libs['mal'].update_show = explode

    result = eng.apply(eng.plan())
    assert 'mal' in result['errors']
    assert 'RuntimeError' in result['errors']['mal']
    # The other provider proceeded normally.
    assert libs['anilist'].shows['9']['my_progress'] == 8
    # And MAL's change is still pending, not lost.
    plan2 = eng.plan()
    assert [c for c in plan2.changes
            if c.field == 'progress' and c.target == 'mal']


def test_apply_reports_progress_per_step(store):
    """apply(progress=cb) calls back once per unit of real work (the
    local commit, then each provider/show push batch) so a UI can show
    an honest progress bar."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', progress=3),
                       show('anilist', 9, 'Bebop', mal_id=1, progress=3))
    uid = _uid(store)
    eng.edit_local(uid, 'progress', 8)
    ticks = []
    result = eng.apply(eng.plan(), progress=lambda d, t, m: ticks.append((d, t, m)))
    assert result['errors'] == {}
    # local edit -> pull step is 0 here (primary=None, local-owned), so
    # steps are the two pushes (mal + anilist).
    totals = {t for _, t, _ in ticks}
    assert totals == {2}
    assert ticks[-1][0] == 2               # ended at total
    assert any('Pushing to' in m for _, _, m in ticks)
    assert any('Pushed to' in m for _, _, m in ticks)


def test_apply_progress_reports_a_failed_provider(store):
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    uid = _uid(store)
    eng.edit_local(uid, 'progress', 8)
    libs['mal'].fail_update = True
    ticks = []
    result = eng.apply(eng.plan(), progress=lambda d, t, m: ticks.append((d, t, m)))
    assert 'mal' in result['errors']
    assert any(m.startswith('FAILED Mal') for _, _, m in ticks)


def test_rate_limited_push_retries_then_succeeds(store, monkeypatch):
    """A 429 no longer fails the provider: the push waits and retries,
    honoring a Retry-After, and succeeds -- the whole point being that
    a big first sync survives AniList's rate limit."""
    import hakubun.sync.adapters as adapters_mod
    # Make waits instant so the test isn't slow.
    monkeypatch.setattr(adapters_mod.ProviderAdapter, '_sleep',
                        lambda self, seconds, cancel: None)
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    libs['mal'].rate_limit_first = 3       # 3 x 429, then success
    libs['mal'].rate_limit_retry_after = 2
    uid = _uid(store)
    eng.edit_local(uid, 'progress', 8)
    waits = []
    result = eng.apply(eng.plan(),
                       progress=lambda d, t, m: waits.append(m))
    assert result['errors'] == {}
    assert result['cancelled'] is False
    assert libs['mal'].shows['1']['my_progress'] == 8   # eventually pushed
    assert any('Rate limited by Mal' in m and 'waiting 2s' in m
               for m in waits)


def test_rate_limit_gives_up_after_max_retries(store, monkeypatch):
    import hakubun.sync.adapters as adapters_mod
    monkeypatch.setattr(adapters_mod.ProviderAdapter, '_sleep',
                        lambda self, seconds, cancel: None)
    eng, libs = _setup(store, show('mal', 1, 'Bebop', progress=3))
    libs['mal'].rate_limit_first = 999     # never recovers
    uid = _uid(store)
    eng.edit_local(uid, 'progress', 8)
    result = eng.apply(eng.plan())
    assert 'mal' in result['errors']
    assert '429' in result['errors']['mal']
    # The change stays plannable for a later retry.
    assert [c for c in eng.plan().changes if c.target == 'mal']


def test_cancel_stops_a_run_and_keeps_the_done_part(store):
    """should_cancel() true stops between push batches; already-pushed
    changes stay committed, the rest re-plan."""
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', progress=3),
                       show('anilist', 9, 'Bebop', mal_id=1, progress=3))
    # Two shows so there are multiple push batches to stop between.
    kitsu = FakeLib('kitsu', [show('kitsu', 5, 'Cowboy Bebop 2', progress=1)])
    from hakubun.sync.adapters import ProviderAdapter
    eng.adapters['kitsu'] = ProviderAdapter('kitsu', kitsu)
    eng.fetch()
    for e in store.entities():
        eng.edit_local(e['uuid'], 'progress', 7)

    # Cancel after the very first push batch.
    calls = {'n': 0}
    def should_cancel():
        calls['n'] += 1
        return calls['n'] > 1
    result = eng.apply(eng.plan(), should_cancel=should_cancel)
    assert result['cancelled'] is True
    assert result['pushed'] >= 1
    assert result['pushed'] < 3           # stopped before finishing all
    # A replan still has the un-pushed changes.
    assert eng.plan().changes


def test_push_pacing_spaces_out_requests(monkeypatch):
    """Consecutive pushes to one provider are spaced by the provider's
    interval (the proactive half of not tripping the rate limit)."""
    import hakubun.sync.adapters as adapters_mod
    from hakubun.sync.adapters import ProviderAdapter
    slept = []
    monkeypatch.setattr(adapters_mod.ProviderAdapter, '_sleep',
                        lambda self, seconds, cancel: slept.append(seconds))
    monkeypatch.setattr(adapters_mod, '_PUSH_INTERVAL', {'mal': 0.5})
    lib = FakeLib('mal', [show('mal', 1, 'A'), show('mal', 2, 'B')])
    adapter = ProviderAdapter('mal', lib)
    adapter.push('1', {'progress': 5}, title='A')   # first: no wait
    assert slept == [] or slept[0] <= 0
    adapter.push('2', {'progress': 5}, title='B')   # second: paced
    assert any(s > 0 for s in slept)


def test_rewatch_count_is_a_synced_field(store):
    """Rewatch count (MAL num_times_rewatched / AniList repeat / Kitsu
    reconsumeCount) is tracked like progress: normalized in, owned, and
    pushed out."""
    from hakubun.sync.models import USER_FIELDS, DEFAULT_OWNERSHIP
    assert 'rewatches' in USER_FIELDS
    assert 'rewatches' in DEFAULT_OWNERSHIP
    eng, libs = _setup(store,
                       show('mal', 1, 'Bebop', progress=26),
                       show('anilist', 9, 'Bebop', mal_id=1, progress=26))
    uid = _uid(store)
    # Rewatched it twice; sync pushes that everywhere.
    eng.edit_local(uid, 'rewatches', 2)
    result = eng.apply(eng.plan())
    assert result['errors'] == {}
    assert libs['mal'].updates[-1]['my_rewatched_times'] == 2
    assert libs['anilist'].updates[-1]['my_rewatched_times'] == 2
    # Converges.
    assert eng.fetch() == {}
    assert [c for c in eng.plan().changes if c.field == 'rewatches'] == []


def test_rewatch_count_normalizes_from_provider(store):
    from hakubun.sync import normalize
    from conftest import MEDIAINFO
    s = show('anilist', 9, 'Bebop', my_rewatched_times=3)
    entry = normalize.normalize_show('anilist', s, MEDIAINFO['anilist'])
    assert entry.user['rewatches'] == 3


def test_blank_never_conflicts_with_zero_or_empty(store):
    """A provider returning None/blank for a field is the same state as
    another returning 0 (or '' / []), never a conflict or a change --
    Kitsu's blank reconsumeCount vs MAL/AniList's 0 rewatches was the
    reported case, but the guarantee is field-wide."""
    from hakubun.sync.diff import eq, emptyish
    for blank in (None, 0, 0.0, False, '', []):
        assert emptyish(blank), blank
    assert eq(None, 0) and eq(0, None) and eq(None, '') and eq([], None)
    assert not eq(None, 5) and not eq('', 'x') and not eq(['a'], None)

    # Full plan: kitsu returns blank rewatches, MAL/AniList return 0.
    mal = FakeLib('mal', [show('mal', 1, 'Bebop', mal_id=1)])
    anilist = FakeLib('anilist', [show('anilist', 9, 'Bebop', mal_id=1)])
    kitsu_show = show('kitsu', 1, 'Bebop', mal_id=1)
    kitsu_show['my_rewatched_times'] = None          # blank
    kitsu = FakeLib('kitsu', [kitsu_show])
    from hakubun.sync.adapters import ProviderAdapter
    from hakubun.sync.engine import SyncEngine
    eng = SyncEngine(store, {n: ProviderAdapter(n, l) for n, l in
                             {'mal': mal, 'anilist': anilist,
                              'kitsu': kitsu}.items()})
    eng.primary = 'mal'
    assert eng.fetch() == {}
    plan = eng.plan()
    assert [c for c in plan.changes if c.field == 'rewatches'] == []
    assert [c for c in plan.conflicts if c.field == 'rewatches'] == []
