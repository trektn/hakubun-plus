"""Entry existence and provenance.

Whether an entry should EXIST on a provider is a separate question from
where its fields sync from: a creation proposal is justified only by
actual provider entries (its provenance), never by Hakubun's own local
state; a declined or user-deleted creation stays declined across
fetches; a provider-only entity never propagates anywhere."""

from conftest import FakeLib, make_engine, show


def creates(plan, target=None):
    return [c for c in plan.changes if c.creates_entry
            and (target is None or c.target == target)]


def test_creation_candidate_carries_single_provenance(store):
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', progress=5,
                                   mal_id=77)])
    mal = FakeLib('mal', [])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    offers = creates(engine.plan(), 'mal')
    assert offers
    for c in offers:
        assert c.provenance == ('kitsu',)
        assert c.source == 'kitsu'          # never 'local'
        assert 'exists on Kitsu' in c.reason
        assert 'Mal has no entry' in c.reason
        assert c.selected is False


def test_creation_candidate_carries_multi_provenance(store):
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST',
                                       progress=5, mal_id=77)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', progress=5,
                                   mal_id=77)])
    mal = FakeLib('mal', [])
    engine = make_engine(store, {'mal': mal, 'anilist': anilist,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    offers = creates(engine.plan(), 'mal')
    assert offers
    for c in offers:
        assert c.provenance == ('anilist', 'kitsu')
        assert 'exists on Anilist and Kitsu' in c.reason


def test_local_only_entity_is_never_offered_for_creation(store):
    """An entity no provider actually lists (a mapping without any
    fetched entry) has nothing establishing its existence: local state
    is not a provider."""
    mal = FakeLib('mal', [])
    engine = make_engine(store, {'mal': mal})
    uid = store.create_entity('Orphan')
    store.add_mapping(uid, 'mal', '99')
    engine.set_local_field(uid, 'progress', 5)
    assert creates(engine.plan()) == []


def test_declined_creation_stays_declined_across_fetches(store):
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', progress=5,
                                   mal_id=77)])
    mal = FakeLib('mal', [])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    offers = creates(engine.plan(), 'mal')
    assert offers
    uid = offers[0].uuid

    engine.decline_create(uid, 'mal')
    # Declining is a MEMBERSHIP decision, and specifically 'ignore'
    # ("leave Mal alone"), never 'absent' -- "don't add it there" must
    # not silently become "delete it from there".
    assert store.membership_of(uid)['mal'] == 'ignore'
    assert store.membership_reason(uid, 'mal') == 'declined'
    assert creates(engine.plan(), 'mal') == []
    assert engine.fetch() == {}                # a fetch changes nothing
    assert creates(engine.plan(), 'mal') == []

    engine.allow_create(uid, 'mal')            # explicit change of mind
    assert creates(engine.plan(), 'mal')


def test_user_deleted_entry_is_not_recreated(store):
    mal = FakeLib('mal', [show('mal', 1, 'GHOST', progress=3),
                          show('mal', 2, 'Other', progress=1)])
    # A second kitsu show keeps the post-delete fetch non-empty -- an
    # entirely empty list is deliberately treated as a possible API
    # hiccup and forgets nothing.
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', progress=3,
                                   mal_id=1),
                              show('kitsu', 'k2', 'Other', progress=1,
                                   mal_id=2)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('kitsu', 'k1')['uuid']

    del kitsu.shows['k1']                      # removed on the website
    assert engine.fetch() == {}
    assert store.membership_of(uid)['kitsu'] == 'ignore'
    assert store.membership_reason(uid, 'kitsu') == 'deleted'
    assert creates(engine.plan(), 'kitsu') == []


def test_provider_only_entity_propagates_nowhere(store):
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', progress=5,
                                   mal_id=77)])
    mal = FakeLib('mal', [])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('kitsu', 'k1')['uuid']
    store.update_entity_meta(uid, provider_only='kitsu')
    plan = engine.plan()
    assert creates(plan) == []
    assert [c for c in plan.changes if c.target == 'mal'] == []


def test_mixed_field_creation_bundles_fields_under_one_provenance(store):
    """The §15 case: score from AniList, status and start date edited
    locally, Kitsu missing -- one creation proposal (per field, one
    provenance and reason) initialized with all three values, applied
    as a single add."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'Sono Bisque Doll',
                                       score=50, mal_id=77)])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k9'})
    engine = make_engine(store, {'anilist': anilist, 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('anilist', '9')['uuid']
    engine.set_local_field(uid, 'status', 'dropped')
    engine.set_local_field(uid, 'start_date', '2022-11-27')

    plan = engine.plan()
    offers = creates(plan, 'kitsu')
    fields = {c.field: c.new for c in offers}
    assert fields['score'] == 5.0
    assert fields['status'] == 'dropped'
    assert fields['start_date'] == '2022-11-27'
    assert {c.provenance for c in offers} == {('anilist',)}
    assert {c.reason for c in offers} \
        == {'exists on Anilist; Kitsu has no entry'}

    for c in offers:
        c.selected = True
    for c in plan.changes:                     # isolate the creation
        if not c.creates_entry:
            c.selected = False
    result = engine.apply(plan)
    assert result['errors'] == {}
    added = kitsu.shows['k9']
    assert added['my_score'] == 2.5            # 5.0 on kitsu's scale
    assert added['my_status'] == 'dropped'
