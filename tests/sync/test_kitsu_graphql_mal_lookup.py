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
