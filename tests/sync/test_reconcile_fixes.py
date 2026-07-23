"""Tests for the reconciliation-robustness round:

* remote-tracking timestamps mean 'first seen changed', so newest-wins
  arbitration compares real change times instead of fetch times;
* fetch failure isolation covers entry PROCESSING, not just the network
  call, with per-provider rollback;
* entries deleted on a provider's website drop out of the remote
  snapshot (and their merge bases), instead of being diffed forever;
* bulk store getters (the N+1 fix for overlay/planner) return exactly
  what the single getters do;
* structural (differing-episode-structures) conflicts refuse raw
  adoption of a provider-side value;
* an identity conflict whose mapping was quarantined reopens even if
  its row was previously marked resolved.
"""

import pytest

from hakubun.sync.identity import IdentityResolver
from hakubun.sync.models import (FieldConflict, FieldPolicy,
                                 NormalizedEntry, PolicyKind)

from conftest import FakeLib, make_engine, show


# -- remote_set_all timestamp semantics -------------------------------

def test_unchanged_remote_value_keeps_first_seen_ts(store):
    store.remote_set_all('mal', '1', {'score': 7.0}, ts=100)
    store.remote_set_all('mal', '1', {'score': 7.0}, ts=200)
    assert store.remote_get('mal', '1')['score'] == (7.0, 100)


def test_changed_remote_value_advances_ts(store):
    store.remote_set_all('mal', '1', {'score': 7.0}, ts=100)
    store.remote_set_all('mal', '1', {'score': 8.0}, ts=300)
    assert store.remote_get('mal', '1')['score'] == (8.0, 300)


# -- bulk getters -----------------------------------------------------

def test_bulk_getters_match_single_getters(store):
    u1 = store.create_entity('One')
    u2 = store.create_entity('Two')
    store.add_mapping(u1, 'mal', '1')
    store.add_mapping(u1, 'kitsu', 'a')
    store.add_mapping(u2, 'mal', '2')
    store.local_set(u1, 'score', 8.5, ts=10)
    store.local_set(u1, 'progress', 3, ts=11)
    store.local_set(u2, 'status', 'watching', ts=12)
    store.remote_set_all('mal', '1', {'score': 8.0, 'progress': 3}, ts=20)
    store.remote_set_all('mal', '2', {'status': 'watching'}, ts=21)
    store.base_set(u1, 'mal', 'score', 8.0)
    store.base_set(u2, 'mal', 'status', 'watching')

    assert store.local_get_many([u1, u2]) == {
        u1: store.local_get(u1), u2: store.local_get(u2)}
    assert store.remote_get_many('mal', ['1', '2']) == {
        '1': store.remote_get('mal', '1'),
        '2': store.remote_get('mal', '2')}
    assert store.base_get_many([u1, u2]) == {
        (u1, 'mal'): store.base_get(u1, 'mal'),
        (u2, 'mal'): store.base_get(u2, 'mal')}
    many = store.mappings_many([u1, u2])
    assert many[u1] == store.mappings_of(u1)
    assert many[u2] == store.mappings_of(u2)


# -- fetch: processing failure isolation ------------------------------

def test_processing_failure_rolls_back_provider_and_isolates(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=3)])
    anilist = FakeLib('anilist', [show('anilist', 11, 'Frieren',
                                       progress=3)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist})

    real = engine.identity.resolve_entry

    def boom(entry):
        if entry.provider == 'anilist':
            raise RuntimeError('identity bug')
        return real(entry)

    engine.identity.resolve_entry = boom
    errors = engine.fetch()

    assert 'anilist' in errors and 'identity bug' in errors['anilist']
    assert 'mal' not in errors
    # mal's data landed; anilist's partial writes were rolled back.
    assert store.remote_get('mal', '1')
    assert store.remote_get('anilist', '11') == {}


# -- fetch: deletions on the provider ---------------------------------

def test_deleted_remote_entry_drops_snapshot_and_base(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=3),
                          show('mal', 2, 'Mushishi', progress=5)])
    engine = make_engine(store, {'mal': mal})
    assert engine.fetch() == {}
    uid2 = store.mapping_for('mal', '2')['uuid']
    assert store.remote_get('mal', '2')
    assert store.base_get(uid2, 'mal')

    del mal.shows['2']          # deleted on the website
    assert engine.fetch() == {}

    assert store.remote_get('mal', '2') == {}
    assert store.base_get(uid2, 'mal') == {}
    # Local state is untouched: a remote delete never propagates.
    assert store.local_get(uid2)['progress'][0] == 5
    # And the planner no longer proposes anything for that entry.
    plan = engine.plan()
    assert not [c for c in plan.changes
                if c.uuid == uid2 and c.target == 'mal']


def test_empty_fetch_never_wipes_the_snapshot(store):
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=3)])
    engine = make_engine(store, {'mal': mal})
    assert engine.fetch() == {}

    mal.shows = {}              # API hiccup or a genuinely emptied list
    assert engine.fetch() == {}

    # Indistinguishable from a silent API failure: keep the snapshot.
    assert store.remote_get('mal', '1')


# -- structural conflicts refuse raw adoption -------------------------

def test_structural_conflict_rejects_provider_choice(store):
    uid = store.create_entity('Kaguya First Kiss', total=4)
    store.local_set(uid, 'progress', 2)
    conflict = FieldConflict(
        uid, 'progress', {'local': 2, 'kitsu': 1}, base=None,
        policy=FieldPolicy(PolicyKind.LOCAL), structural=True)
    engine = make_engine(store, {})

    with pytest.raises(ValueError):
        engine.resolve_conflict(conflict, 'kitsu')
    # 'local' and an explicit value stay allowed.
    engine.resolve_conflict(conflict, 'local')
    engine.resolve_conflict(conflict, 'value', value=3)
    assert store.local_get(uid)['progress'][0] == 3


# -- fields a provider cannot represent are absent, not empty ---------

def test_normalize_omits_unrepresentable_fields():
    from hakubun.sync import normalize
    from conftest import MEDIAINFO

    # MAL has no tags feature (no can_tag): the field must be ABSENT,
    # not fabricated as []. No lib reports notes/favorites at all.
    entry = normalize.normalize_show(
        'mal', show('mal', 1, 'Frieren'), MEDIAINFO['mal'])
    assert 'tags' not in entry.user
    assert 'notes' not in entry.user
    assert 'favorite' not in entry.user
    # AniList (can_tag) keeps tags, parsed from the my_tags string.
    entry = normalize.normalize_show(
        'anilist', show('anilist', 1, 'Frieren', my_tags='b, a'),
        MEDIAINFO['anilist'])
    assert entry.user['tags'] == ['a', 'b']


def test_unsent_push_never_advances_base_or_remote(store):
    """A push the adapter could not deliver (capability gap) must not
    be recorded as delivered: doing so poisons the merge base with a
    value the remote never held, and the provider's real value later
    reads as a fresh remote edit that pulls local's value away."""
    from hakubun.sync.models import FieldChange, SyncPlan, SyncMode

    mal = FakeLib('mal', [show('mal', 1, 'Frieren')])
    engine = make_engine(store, {'mal': mal})
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']

    plan = SyncPlan(SyncMode.MERGE)
    plan.changes.append(FieldChange(uid, 'tags', None, ['a'],
                                    target='mal', source='local',
                                    title='Frieren'))
    result = engine.apply(plan)

    assert result['pushed'] == 0 and result['errors'] == {}
    assert 'tags' not in store.base_get(uid, 'mal')
    assert 'tags' not in store.remote_get('mal', '1')


def test_fetch_prunes_fabricated_remote_fields(store):
    """Rows earlier revisions fabricated (tags=[] for a tagless
    provider) are dropped by the next fetch."""
    mal = FakeLib('mal', [show('mal', 1, 'Frieren')])
    engine = make_engine(store, {'mal': mal})
    store.remote_set_all('mal', '1', {'tags': [], 'favorite': False})
    assert engine.fetch() == {}
    remote = store.remote_get('mal', '1')
    assert 'tags' not in remote and 'favorite' not in remote
    assert 'progress' in remote          # real fields intact


def test_local_tags_survive_tagless_providers(store):
    """End-to-end: tags live on AniList, MAL cannot store them; local
    tags must persist across repeated fetch+plan cycles instead of
    being erased by MAL's inability to hold them."""
    mal = FakeLib('mal', [show('mal', 1, 'Frieren', progress=3)])
    anilist = FakeLib('anilist', [show('anilist', 11, 'Frieren',
                                       progress=3, my_tags='fantasy',
                                       mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist})

    for _ in range(3):
        assert engine.fetch() == {}
        plan = engine.plan()
        assert not plan.conflicts
        engine.apply(plan)

    uid = store.mapping_for('mal', '1')['uuid']
    assert store.local_get(uid)['tags'][0] == ['fantasy']


# -- overlay: provider-structure display ------------------------------

def test_overlay_converts_progress_and_skips_precision_noise(store):
    from hakubun.sync.overlay import build_overlay
    from conftest import MEDIAINFO

    mal = FakeLib('mal', [show('mal', 1, 'Kaguya First Kiss',
                               progress=4, total=4, score=8,
                               status='completed')])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Kaguya First Kiss',
                                   progress=1, total=1, mal_id=1,
                                   status='completed')])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']

    # Signed into Kitsu (the 1-episode movie): the reconciled local
    # 4/4 must render as Kitsu's own 1, never an impossible '4 / 1'.
    overlay = build_overlay(store, 'kitsu', MEDIAINFO['kitsu'],
                            provider_mediainfo={'kitsu':
                                                MEDIAINFO['kitsu']})
    assert overlay.get('k1', {}).get('my_progress') in (None, 1)

    # Precision noise: canonical 8.4 displays as MAL's own 8 -- no
    # override cue for a difference the account cannot even show.
    store.local_set(uid, 'score', 8.4, source='local')
    overlay = build_overlay(store, 'mal', MEDIAINFO['mal'],
                            provider_mediainfo={'mal': MEDIAINFO['mal']})
    assert 'my_score' not in overlay.get('1', {})


# -- identity issues for entries deleted on the provider --------------

def test_unlisted_entry_closes_its_identity_conflict(store):
    from hakubun.sync.models import NormalizedEntry as NE

    mal = FakeLib('mal', [show('mal', 1, 'Frieren'),
                          show('mal', 2, 'Frieren S2')])
    engine = make_engine(store, {'mal': mal})
    assert engine.fetch() == {}
    # Manufacture an open conflict for entry 2, as if it were
    # ambiguous, then delete the entry on the website.
    row_uid = store.mapping_for('mal', '2')['uuid']
    store.remove_mapping('mal', '2')
    store.identity_upsert('mal', '2', 'Frieren S2',
                          [{'uuid': row_uid, 'via': 'title (similar)'}])
    assert store.identity_open()

    del mal.shows['2']
    assert engine.fetch() == {}
    assert store.identity_open() == []
    assert store.identity_get('mal', '2')['status'] == 'unlisted'


# -- cancellable fetch/plan -------------------------------------------

def test_fetch_and_plan_honor_cancellation(store):
    import pytest as _pytest
    from hakubun.sync.models import SyncCancelled

    mal = FakeLib('mal', [show('mal', 1, 'Frieren')])
    engine = make_engine(store, {'mal': mal})
    assert engine.fetch(should_cancel=lambda: True) == {}
    assert store.remote_get('mal', '1') == {}   # nothing ingested

    assert engine.fetch() == {}
    with _pytest.raises(SyncCancelled):
        engine.plan(should_cancel=lambda: True)


# -- N+1 regression guard ---------------------------------------------

def _query_count(store, fn):
    count = 0

    def trace(_stmt):
        nonlocal count
        count += 1

    store._conn.set_trace_callback(trace)
    try:
        fn()
    finally:
        store._conn.set_trace_callback(None)
    return count


def test_overlay_and_plan_issue_constant_queries(store):
    """The display overlay runs on the UI thread on every list repaint
    (and the planner over the whole library): both must load in bulk,
    not per show. Pin that with a hard query-count ceiling that a
    reintroduced per-show query pattern (50+ shows here) would blow
    through immediately."""
    from hakubun.sync.overlay import build_overlay
    from conftest import MEDIAINFO

    shows = [show('mal', i, 'Show %03d' % i, progress=i % 10)
             for i in range(1, 51)]
    engine = make_engine(store, {'mal': FakeLib('mal', shows)})
    assert engine.fetch() == {}

    n = _query_count(store, lambda: build_overlay(
        store, 'mal', MEDIAINFO['mal'],
        provider_mediainfo={'mal': MEDIAINFO['mal']}))
    assert n <= 10, 'overlay ran %d queries for 50 shows' % n

    n = _query_count(store, engine.plan)
    assert n <= 10, 'plan ran %d queries for 50 shows' % n


# -- structure-mismatch: the base converts like the remote ------------

def test_unchanged_movie_side_is_not_a_phantom_remote_change(store):
    """Merge bases for progress are stored in the provider's RAW
    structure; the diff must view them through the same conversion as
    the remote. A completed 1/1 movie base against a 4-episode local
    listing must not read as 'the remote changed' when the movie side
    never moved -- in PULL mode that phantom change would overwrite a
    deliberate local rewind."""
    mal = FakeLib('mal', [show('mal', 1, 'Kaguya First Kiss',
                               progress=4, total=4,
                               status='completed')])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'Kaguya First Kiss',
                                   progress=1, total=1, mal_id=1,
                                   status='completed')])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']
    # Settled: kitsu's base is its RAW 1 (movie complete == 4/4).
    assert store.base_get(uid, 'kitsu')['progress'] == 1

    engine.edit_local(uid, 'progress', 3)   # deliberate local rewind

    from hakubun.sync.models import SyncMode
    plan = engine.plan(SyncMode.PULL)
    pulled = [c for c in plan.changes
              if c.field == 'progress' and c.target == 'local']
    assert pulled == [], 'phantom remote change pulled: %s' % pulled
    assert store.local_get(uid)['progress'][0] == 3


# -- PROVIDER ownership survives past the folding plan ----------------

def test_owner_guard_holds_after_the_folded_value_commits(store):
    """A primary-side edit folds into local state but must not reach
    the field's PROVIDER owner -- not in the plan that folds it (the
    one-cycle source check) and not in ANY later plan, where the
    committed value would otherwise reappear as a plain local-side
    change. Stored provenance keeps the guard durable. A direct local
    edit (set_local_field) stays authoritative and does push."""
    from hakubun.sync.models import FieldPolicy, PolicyKind

    mal = FakeLib('mal', [show('mal', 1, 'Bebop', score=7)])
    anilist = FakeLib('anilist', [show('anilist', 9, 'Bebop',
                                       mal_id=1, score=70)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist})
    engine.primary = 'mal'
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER,
                                             'anilist'))
    assert engine.fetch() == {}
    uid = store.mapping_for('mal', '1')['uuid']

    mal.shows['1']['my_score'] = 9      # app-side edit on the primary
    for _cycle in range(2):             # fold cycle, then re-plan cycle
        assert engine.fetch() == {}
        plan = engine.plan()
        assert [c for c in plan.changes
                if c.field == 'score' and c.target == 'anilist'] == []
        engine.apply(plan)
    assert anilist.shows['9']['my_score'] == 70   # owner never touched
    assert store.local_get(uid)['score'][0] == 9.0

    engine.set_local_field(uid, 'score', 8.0)     # authoritative edit
    plan = engine.plan()
    engine.apply(plan)
    assert anilist.shows['9']['my_score'] == 80


# -- identity: a quarantined mapping reopens its conflict -------------

def test_quarantined_mapping_reopens_resolved_conflict(store):
    uid = store.create_entity('Foo', media_type='anime')
    store.add_mapping(uid, 'kitsu', '9', confirmed=True)
    # The row was resolved once (user confirmed the mapping)...
    store.identity_upsert('kitsu', '9', 'Foo', [], status='resolved')
    assert store.identity_open() == []

    # ...but the mapped entity turns out to be the wrong media type:
    # the mapping is quarantined and the conflict must become visible
    # again, not stay invisibly 'resolved'.
    resolver = IdentityResolver(store)
    entry = NormalizedEntry(provider='kitsu', provider_id='9',
                            title='Foo', media_type='manga')
    resolver.resolve_entry(entry)

    assert store.mapping_for('kitsu', '9') is None \
        or store.mapping_for('kitsu', '9')['uuid'] != uid
    assert any(r['provider'] == 'kitsu' and r['provider_id'] == '9'
               for r in store.identity_open()) \
        or store.identity_get('kitsu', '9')['status'] != 'resolved'
