"""web_url: best-effort links to a provider's own page for an entry."""

from hakubun.sync.adapters import web_url


def test_known_providers_anime():
    assert web_url('mal', 'anime', '1') == 'https://myanimelist.net/anime/1'
    assert web_url('anilist', 'anime', '1') == 'https://anilist.co/anime/1'
    assert web_url('kitsu', 'anime', '1') == 'https://kitsu.app/anime/1'


def test_known_providers_manga():
    assert web_url('mal', 'manga', '2') == 'https://myanimelist.net/manga/2'
    assert web_url('anilist', 'manga', '2') == 'https://anilist.co/manga/2'
    assert web_url('kitsu', 'manga', '2') == 'https://kitsu.app/manga/2'


def test_unrecognized_provider_or_missing_id():
    assert web_url('shikimori', 'anime', '1') is None
    assert web_url('mal', 'anime', '') is None
    assert web_url('mal', 'anime', None) is None


def test_unknown_media_type_defaults_to_anime():
    assert web_url('mal', None, '1') == 'https://myanimelist.net/anime/1'
    assert web_url('mal', 'movie', '1') == 'https://myanimelist.net/anime/1'
