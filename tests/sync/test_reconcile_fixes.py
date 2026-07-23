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
