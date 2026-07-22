"""Identity resolution: matching, missing external IDs, ambiguity."""

from hakubun.sync.normalize import normalize_show

from conftest import MEDIAINFO, FakeLib, make_engine, show


def _entry(provider, sid, title, mal_id=None, **kw):
    s = show(provider, sid, title, **kw)
    ext = {'mal': str(mal_id)} if mal_id else None
    return normalize_show(provider, s, MEDIAINFO[provider], ext)


def test_exact_external_id_links_automatically(store):
    """MAL id exists on AniList: the AniList entry links to the entity
    created from the MAL entry -- exact ids merge, no user needed."""
    libs = {'mal': FakeLib('mal', [show('mal', 100, 'Cowboy Bebop')]),
            'anilist': FakeLib('anilist',
                               [show('anilist', 900, 'COWBOY BEBOP',
                                     mal_id=100)])}
    eng = make_engine(store, libs)
    assert eng.fetch() == {}
    ents = store.entities()
    assert len(ents) == 1
    providers = {m['provider']: m['provider_id']
                 for m in store.mappings_of(ents[0]['uuid'])}
    assert providers == {'mal': '100', 'anilist': '900'}
    assert store.identity_open() == []


def test_missing_external_id_titles_differ_makes_conflict(store):
    """Kitsu (no MAL id) with a differing title: candidates are found
    via normalized title, but nothing is auto-merged."""
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Ghost in the Shell')]),
            'kitsu': FakeLib('kitsu',
                             [show('kitsu', 77, 'Ghost in the SHELL!')])}
    eng = make_engine(store, libs)
    eng.fetch()
    assert len(store.entities()) == 1          # kitsu entry NOT merged
    open_conflicts = store.identity_open()
    assert len(open_conflicts) == 1
    c = open_conflicts[0]
    assert (c['provider'], c['provider_id']) == ('kitsu', '77')
    assert c['candidates'][0]['providers'] == {'mal': '1'}


def test_no_candidates_creates_new_entity(store):
    libs = {'kitsu': FakeLib('kitsu', [show('kitsu', 5, 'Obscure OVA')])}
    eng = make_engine(store, libs)
    eng.fetch()
    ents = store.entities()
    assert len(ents) == 1 and ents[0]['title'] == 'Obscure OVA'
    assert store.identity_open() == []


def test_confirm_stores_mapping_permanently(store):
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Ghost in the Shell')]),
            'kitsu': FakeLib('kitsu',
                             [show('kitsu', 77, 'Ghost In The Shell')])}
    eng = make_engine(store, libs)
    eng.fetch()
    conflict = store.identity_open()[0]
    target = conflict['candidates'][0]['uuid']
    entry = _entry('kitsu', 77, 'Ghost In The Shell')
    eng.identity.resolve_conflict(conflict['id'], 'confirm', entry=entry,
                                  target_uuid=target)
    assert store.mapping_for('kitsu', '77')['uuid'] == target
    assert store.identity_open() == []
    # Permanent: the next fetch resolves silently.
    eng.fetch()
    assert store.identity_open() == []
    assert len(store.entities()) == 1


def test_provider_only_pins_and_never_cross_syncs(store):
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Ghost in the Shell')]),
            'kitsu': FakeLib('kitsu',
                             [show('kitsu', 77, 'ghost in the shell')])}
    eng = make_engine(store, libs)
    eng.fetch()
    conflict = store.identity_open()[0]
    entry = _entry('kitsu', 77, 'ghost in the shell')
    uid = eng.identity.resolve_conflict(conflict['id'], 'provider_only',
                                        entry=entry)
    ent = store.get_entity(uid)
    assert ent['provider_only'] == 'kitsu'
    plan = eng.plan()
    # The pinned entity plans only against kitsu, never mal.
    assert not [c for c in plan.changes
                if c.uuid == uid and c.target == 'mal']


def test_ignore_never_asks_again(store):
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Ghost in the Shell')]),
            'kitsu': FakeLib('kitsu',
                             [show('kitsu', 77, 'Ghost in the Shell')])}
    eng = make_engine(store, libs)
    eng.fetch()
    conflict = store.identity_open()[0]
    eng.identity.resolve_conflict(conflict['id'], 'ignore')
    eng.fetch()
    assert store.identity_open() == []
    assert store.mapping_for('kitsu', '77') is None


def test_deferred_resolves_itself_when_exact_id_appears(store):
    """'Create provider mappings later': the entry keeps watching; when
    the provider starts publishing a usable exact id, it links."""
    kitsu_show = show('kitsu', 77, 'GitS 2026 edition')  # title won't match
    mal_lib = FakeLib('mal', [show('mal', 1, 'Ghost in the Shell')])
    kitsu_lib = FakeLib('kitsu', [kitsu_show])
    eng = make_engine(store, {'mal': mal_lib, 'kitsu': kitsu_lib})
    eng.fetch()
    # New entity was created (no candidates: title differs completely).
    assert len(store.entities()) == 2
    # Simulate instead the airing-incomplete case: fresh store, defer.
    # Kitsu later publishes the MAL id -> exact link resolves silently.
    store2 = type(store)(':memory:')
    mal2 = FakeLib('mal', [show('mal', 1, 'Ghost in the Shell')])
    kitsu2 = FakeLib('kitsu', [show('kitsu', 77, 'Ghost in the Shell (2026)')])
    eng2 = make_engine(store2, {'mal': mal2, 'kitsu': kitsu2})
    eng2.fetch()
    conflict = store2.identity_open()[0]
    eng2.identity.resolve_conflict(conflict['id'], 'defer')
    assert store2.identity_open()[0]['status'] == 'deferred'
    kitsu2.shows['77']['mal_id'] = 1          # metadata filled in later
    eng2.fetch()
    assert store2.mapping_for('kitsu', '77') is not None
    assert store2.identity_open() == []
    store2.close()


def test_ambiguous_external_ids_never_merge_entities(store):
    """An entry claiming ids that live on two different entities is an
    ambiguity -> conflict, not an automatic entity merge."""
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Series A')]),
            'anilist': FakeLib('anilist', [show('anilist', 2, 'Series B')])}
    eng = make_engine(store, libs)
    eng.fetch()
    assert len(store.entities()) == 2
    # A kitsu entry claims BOTH (bad provider data).
    from hakubun.sync.models import NormalizedEntry
    entry = NormalizedEntry(provider='kitsu', provider_id='9',
                            title='Series AB',
                            external_ids={'mal': '1', 'anilist': '2'})
    assert eng.identity.resolve_entry(entry) is None
    assert len(store.entities()) == 2          # still two entities
    assert len(store.identity_open()) == 1
