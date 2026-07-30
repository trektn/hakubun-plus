"""Identity inspector: resolved / unresolved / unknown, via labels,
atlas cross-check -- the tool exists so the user can audit multisync's
own claims, so every field here must be traceable to real store rows.
"""

from hakubun.sync.inspect import inspect_entry
from hakubun.sync.relations import RelationsAtlas

from conftest import FakeLib, make_engine, show


def _atlas(tmp_path, rules):
    path = tmp_path / 'anime-relations'
    path.write_text(rules)
    return RelationsAtlas.from_file(str(path))


def test_unknown_id_reports_no_record(store):
    r = inspect_entry(store, 'mal', '99999')
    assert r.found is False
    assert 'No record' in r.note
    assert r.identity_issue is None


def test_unresolved_open_conflict_reports_status_and_candidates(store):
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Ghost in the Shell')]),
            'kitsu': FakeLib('kitsu',
                             [show('kitsu', 77, 'Ghost in the Shell X')])}
    eng = make_engine(store, libs)
    eng.fetch()
    r = inspect_entry(store, 'kitsu', '77')
    assert r.found is False
    assert 'Identity tab' in r.note
    assert r.identity_issue is not None
    assert r.identity_issue['status'] == 'open'
    assert r.identity_issue['candidates']


def test_resolved_entry_shows_mappings_fields_and_via(store):
    eng, = (make_engine(store,
                        {'mal': FakeLib('mal', [show('mal', 1, 'Bebop',
                                                     progress=5,
                                                     score=8)]),
                         'anilist': FakeLib(
                             'anilist',
                             [show('anilist', 9, 'Bebop', mal_id=1,
                                  progress=5, score=80)])}),)
    assert eng.fetch() == {}
    r = inspect_entry(store, 'mal', '1')
    assert r.found is True
    assert r.title == 'Bebop'
    providers = {m.provider: m for m in r.mappings}
    assert set(providers) == {'mal', 'anilist'}
    # mal is the entity's own first-seen mapping; anilist got linked
    # via its published mal_id -- both traceable.
    assert 'first seen on mal' in providers['mal'].via
    assert 'published' in providers['anilist'].via
    assert providers['anilist'].confirmed is True
    progress_row = next(f for f in r.fields if f.field == 'progress')
    assert progress_row.local == 5
    assert progress_row.per_provider['mal']['remote'] == 5
    assert progress_row.per_provider['anilist']['remote'] == 5


def test_confirmed_mapping_reports_user_confirmed_via(store):
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Ghost in the Shell')]),
            'kitsu': FakeLib('kitsu',
                             [show('kitsu', 77, 'Ghost in the Shell X')])}
    eng = make_engine(store, libs)
    eng.fetch()
    conflict = store.identity_open()[0]
    target = conflict['candidates'][0]['uuid']
    from hakubun.sync.normalize import normalize_show
    from conftest import MEDIAINFO
    entry = normalize_show('kitsu', show('kitsu', 77, 'Ghost in the Shell X'),
                           MEDIAINFO['kitsu'])
    eng.identity.resolve_conflict(conflict['id'], 'confirm', entry=entry,
                                  target_uuid=target)
    r = inspect_entry(store, 'kitsu', '77')
    assert r.found is True
    kitsu_mapping = next(m for m in r.mappings if m.provider == 'kitsu')
    assert kitsu_mapping.via == 'confirmed by user'
    assert kitsu_mapping.confirmed is True


def test_atlas_hint_shown_regardless_of_resolution_state(store, tmp_path):
    atlas = _atlas(tmp_path, '- 1|4814|9260:1 -> 1|4814|9260:2\n')
    # mal=1 is never fetched/mapped -- purely unresolved -- but the
    # atlas hint must still surface for cross-checking trust.
    r = inspect_entry(store, 'mal', '1', atlas=atlas)
    assert r.found is False
    assert r.atlas_hint == {'kitsu': '4814', 'anilist': '9260'}


def test_atlas_hint_alongside_a_resolved_entry(store, tmp_path):
    atlas = _atlas(tmp_path, '- 1|?|9:1 -> 1|?|9:1!\n')
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Bebop')])}
    eng = make_engine(store, libs)
    eng.identity._atlas = atlas
    eng.fetch()
    r = inspect_entry(store, 'mal', '1', atlas=atlas)
    assert r.found is True
    assert r.atlas_hint == {'anilist': '9'}
