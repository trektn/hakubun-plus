"""Kitsu GraphQL's find_by_mal_id: the reverse of libkitsu's forward
mal_id (media -> its MAL id) -- given a MAL id, is there a Kitsu entry?
Backs multisync's cross-provider discovery (engine._discover_cross_ids)
the same way libanilist.find_by_mal_id does for AniList.
"""
from hakubun import messenger
from hakubun.lib.libkitsu_graphql import libkitsu_graphql

MSG = messenger.Messenger(None, 'Tests')


def _lib(mediatype='anime'):
    lib = libkitsu_graphql(MSG, {'username': 'u', 'password': 'p',
                                 'api': 'kitsu'}, {'mediatype': mediatype})
    lib.check_credentials = lambda: True   # no real OAuth round trip
    return lib


def test_graphql_capabilities_support_date_writes():
    assert libkitsu_graphql.mediatypes['anime']['can_date'] is True
    assert libkitsu_graphql.mediatypes['manga']['can_date'] is True


def test_find_by_mal_id_hit(monkeypatch):
    lib = _lib()
    seen = {}

    def fake_gql(query, variables=None, auth=False):
        seen['variables'] = variables
        return {'lookupMapping': {'id': '47546'}}

    monkeypatch.setattr(lib, '_gql', fake_gql)
    assert lib.find_by_mal_id(56566) == '47546'
    assert seen['variables'] == {'id': '56566', 'site': 'MYANIMELIST_ANIME'}


def test_find_by_mal_id_miss_returns_none(monkeypatch):
    lib = _lib()
    monkeypatch.setattr(
        lib, '_gql', lambda query, variables=None, auth=False:
        {'lookupMapping': None})
    assert lib.find_by_mal_id(999999999) is None


def test_find_by_mal_id_uses_manga_site_for_manga(monkeypatch):
    lib = _lib(mediatype='manga')
    seen = {}

    def fake_gql(query, variables=None, auth=False):
        seen['site'] = variables['site']
        return {'lookupMapping': None}

    monkeypatch.setattr(lib, '_gql', fake_gql)
    lib.find_by_mal_id(1)
    assert seen['site'] == 'MYANIMELIST_MANGA'


def test_add_defaults_missing_required_status_to_planned(monkeypatch):
    """Kitsu's create input rejects a null status even when callers have
    intentionally omitted status from the fields they synchronize."""
    lib = _lib()
    seen = {}

    def fake_gql(query, variables=None, auth=False):
        seen['input'] = variables['input']
        return {'libraryEntry': {'create': {
            'libraryEntry': {'id': '123'}, 'errors': []}}}

    monkeypatch.setattr(lib, '_gql', fake_gql)
    assert lib.add_show({'id': 9, 'title': 'GHOST', 'my_score': 4.5}) == 123
    assert seen['input']['status'] == 'PLANNED'


def test_add_keeps_explicit_status(monkeypatch):
    lib = _lib()
    seen = {}

    def fake_gql(query, variables=None, auth=False):
        seen['input'] = variables['input']
        return {'libraryEntry': {'create': {
            'libraryEntry': {'id': '123'}, 'errors': []}}}

    monkeypatch.setattr(lib, '_gql', fake_gql)
    lib.add_show({'id': 9, 'title': 'GHOST', 'my_status': 'completed'})
    assert seen['input']['status'] == 'COMPLETED'


def test_graphql_writes_start_and_finish_dates(monkeypatch):
    import datetime
    lib = _lib()
    seen = {}

    def fake_gql(query, variables=None, auth=False):
        seen['input'] = variables['input']
        return {'libraryEntry': {'update': {
            'libraryEntry': {'updatedAt': '2026-08-30T00:00:00Z'},
            'errors': []}}}

    monkeypatch.setattr(lib, '_gql', fake_gql)
    lib.update_show({
        'id': 9, 'my_id': 'entry-9', 'title': 'GHOST',
        'my_start_date': datetime.date(2024, 1, 2),
        'my_finish_date': datetime.date(2024, 2, 3)})
    assert seen['input']['startedAt'] == '2024-01-02T00:00:00Z'
    assert seen['input']['finishedAt'] == '2024-02-03T00:00:00Z'
