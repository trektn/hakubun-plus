"""Legacy Kitsu: dates and MAL ids via mappings include + adapter merge."""

import datetime
import json

from hakubun import messenger
from hakubun.lib.libkitsu import libkitsu
from hakubun.sync.adapters import ProviderAdapter

from conftest import MEDIAINFO, FakeLib, make_engine, show

MSG = messenger.Messenger(None, 'Tests')


def _lib():
    return libkitsu(MSG, {'username': 'u', 'password': 'p',
                          'api': 'kitsu'}, {'mediatype': 'anime'})


def _page():
    return {
        'data': [{
            'id': '900',
            'relationships': {'media': {'data': {'id': '77'}}},
            'attributes': {
                'ratingTwenty': 16, 'progress': 3, 'status': 'current',
                'startedAt': None, 'finishedAt': None,
                'updatedAt': '2026-07-01T00:00:00.000Z'},
        }],
        'included': [
            {'type': 'anime', 'id': '77',
             'attributes': {
                 'titles': {'en_jp': 'Sousou no Frieren',
                            'en': "Frieren: Beyond Journey's End"},
                 'canonicalTitle': 'Sousou no Frieren',
                 'episodeCount': 28, 'slug': 'sousou-no-frieren',
                 'description': 'x', 'status': 'finished',
                 'subtype': 'TV', 'posterImage': {},
                 'startDate': '2023-09-29', 'endDate': '2024-03-22',
                 'averageRating': '85.0', 'abbreviatedTitles': [],
             },
             'relationships': {'mappings': {'data': [
                 {'type': 'mappings', 'id': 'm1'},
                 {'type': 'mappings', 'id': 'm2'}]}}},
            {'type': 'mappings', 'id': 'm1',
             'attributes': {'externalSite': 'anilist/anime',
                            'externalId': '154587'}},
            {'type': 'mappings', 'id': 'm2',
             'attributes': {'externalSite': 'myanimelist/anime',
                            'externalId': '52991'}},
        ],
    }


def test_process_library_page_extracts_mal_id():
    lib = _lib()
    showlist, infolist = {}, []
    lib._process_library_page(_page(), showlist, infolist, 1)
    # Mapping resources are filtered out of the info parse, and the
    # media entry got its MAL id from the myanimelist mapping.
    assert len(infolist) == 1
    assert infolist[0]['mal_id'] == 52991
    lib.merge(showlist[77], infolist[0])
    assert showlist[77]['title'] == 'Sousou no Frieren'
    assert showlist[77]['mal_id'] == 52991
    assert 'Sousou no Frieren' in showlist[77]['aliases']


def test_legacy_kitsu_writes_start_and_finish_dates():
    payload = json.loads(_lib()._build_data({
        'id': 77, 'my_id': '900',
        'my_start_date': datetime.date(2024, 1, 2),
        'my_finish_date': datetime.date(2024, 2, 3)}))
    attrs = payload['data']['attributes']
    assert attrs['startedAt'] == '2024-01-02T00:00:00Z'
    assert attrs['finishedAt'] == '2024-02-03T00:00:00Z'


class FakeLegacyKitsuLib(FakeLib):
    """Mimics legacy Kitsu's shape: library entries carry no titles;
    media details arrive via show_info_changed and merge()."""

    def __init__(self, infos):
        super().__init__('kitsu', [])
        self._infos = infos

    def fetch_list(self):
        shows = {}
        for info in self._infos:
            shows[str(info['id'])] = {
                'id': info['id'], 'title': '', 'my_progress': 1,
                'my_score': 0, 'my_status': 'current',
                'my_start_date': None, 'my_finish_date': None,
                'total': None, 'status': 1}
        self.signals['show_info_changed'](list(self._infos))
        return shows

    def merge(self, show, info):
        show['title'] = info['title']
        show['aliases'] = info.get('aliases') or []
        show['mal_id'] = info.get('mal_id')


def test_adapter_merges_infolist_for_titleless_entries():
    infos = [{'id': 77, 'title': 'Sousou no Frieren',
              'aliases': ["Frieren: Beyond Journey's End"],
              'mal_id': 52991}]
    lib = FakeLegacyKitsuLib(infos)
    adapter = ProviderAdapter('kitsu', lib)
    entries = adapter.fetch()
    assert entries[0].title == 'Sousou no Frieren'
    assert entries[0].external_ids == {'mal': '52991'}


def test_kitsu_mal_mapping_links_without_title_matching(store):
    """The user's actual topology: MAL + legacy Kitsu. With mappings
    flowing, everything links by exact id -- zero identity conflicts
    even when the Kitsu title differs from MAL's."""
    mal = FakeLib('mal', [show('mal', 52991, 'Sousou no Frieren')])
    kitsu = FakeLegacyKitsuLib([
        {'id': 77, 'title': "Frieren: Beyond Journey's End",  # EN title
         'aliases': [], 'mal_id': 52991}])
    from hakubun.sync.engine import SyncEngine
    eng = SyncEngine(store, {'mal': ProviderAdapter('mal', mal),
                             'kitsu': ProviderAdapter('kitsu', kitsu)})
    assert eng.fetch() == {}
    assert len(store.entities()) == 1
    assert store.mapping_for('kitsu', '77') is not None
    assert store.identity_open() == []
