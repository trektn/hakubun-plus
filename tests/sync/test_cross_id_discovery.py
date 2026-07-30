"""Cross-provider id discovery (SyncEngine._discover_cross_ids): after
a normal fetch, providers that expose an exact reverse lookup (e.g.
AniList's Media(idMal:...)) are asked whether they have an entity
whose MAL id is already known from elsewhere but that provider has no
mapping for yet -- the 'Kitsu knows the MAL id, but nothing ever
checked whether AniList has it too' gap.
"""
from hakubun.sync.models import SyncMode
from conftest import FakeLib, show, make_engine


def test_discovery_links_a_provider_that_never_independently_matched(store):
    # Beyblade X: only on Kitsu, which publishes mal_id=56566. MAL is
    # connected but its list doesn't have it (an empty fetch, same as
    # the real 'recognized but never pushed' bug report). AniList is
    # connected, doesn't have it in the fetched LIST either, but its
    # lib can answer "yes, idMal 56566 is AniList media 165159".
    mal = FakeLib('mal', [])
    anilist = FakeLib('anilist', [], mal_id_index={56566: 165159})
    kitsu = FakeLib('kitsu', [show('kitsu', 47546, 'Beyblade X',
                                   score=4, mal_id=56566)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist,
                                 'kitsu': kitsu})
    engine.fetch()

    uid = store.mapping_for('kitsu', '47546')['uuid']
    m = store.mapping_for('anilist', '165159')
    assert m is not None and m['uuid'] == uid
    assert m['confirmed'] == 0            # discovered, not user-confirmed
    assert 'MAL id lookup' in m['via']
    assert store.remote_get('anilist', '165159') == {}   # never fetched
    assert anilist.mal_id_lookups == ['56566']

    # Rebase now sees AniList as a create target too, not just MAL.
    plan = engine.plan(mode=SyncMode.REBASE)
    creates = {c.target for c in plan.changes if c.creates_entry}
    assert creates == {'mal', 'anilist'}


def test_discovery_remembers_a_genuine_miss(store):
    mal = FakeLib('mal', [])
    anilist = FakeLib('anilist', [], mal_id_index={})   # supports lookup, has nothing
    kitsu = FakeLib('kitsu', [show('kitsu', 1, 'Obscure Show',
                                   score=4, mal_id=999)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist,
                                 'kitsu': kitsu})
    engine.fetch()

    uid = store.mapping_for('kitsu', '1')['uuid']
    assert store.mapping_for('anilist', '999') is None
    assert store.absent_for_provider('anilist') == {uid}
    assert anilist.mal_id_lookups == ['999']

    # A second fetch must not repeat the network call.
    engine.fetch()
    assert anilist.mal_id_lookups == ['999']


def test_discovery_skips_providers_without_the_lookup(store):
    mal = FakeLib('mal', [])
    anilist = FakeLib('anilist', [])   # no mal_id_index -- no find_by_mal_id
    kitsu = FakeLib('kitsu', [show('kitsu', 1, 'X', score=4, mal_id=1)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist,
                                 'kitsu': kitsu})
    engine.fetch()   # must not raise (no find_by_mal_id on the fake)
    assert store.mapping_for('anilist', '1') is None
    assert store.absent_for_provider('anilist') == set()


def test_discovery_skips_entity_already_mapped_on_that_provider(store):
    # AniList genuinely has the show under its own fetch too -- the
    # ordinary exact-id link (identity.py) already handled it, so
    # discovery must not re-query or double-map it.
    mal = FakeLib('mal', [])
    anilist = FakeLib('anilist', [show('anilist', 165159, 'Beyblade X',
                                       score=80, mal_id=56566)],
                      mal_id_index={56566: 165159})
    kitsu = FakeLib('kitsu', [show('kitsu', 47546, 'Beyblade X',
                                   score=4, mal_id=56566)])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist,
                                 'kitsu': kitsu})
    engine.fetch()

    uid = store.mapping_for('kitsu', '47546')['uuid']
    assert store.mapping_for('anilist', '165159')['uuid'] == uid
    assert anilist.mal_id_lookups == []   # never asked -- already mapped
