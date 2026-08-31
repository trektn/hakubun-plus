import json

from hakubun.genres import get_genre_label, group_genres, normalize_genres
from hakubun.genres.labels import GENRE_LABELS
from hakubun.genres.mappings import CANONICAL_TAGS
from hakubun.lib.libkitsu import libkitsu


def by_id(result, tag_id):
    return next(tag for tag in result.tags if tag['id'] == tag_id)


def test_tracker_aliases_merge_into_one_canonical_genre_with_provenance():
    result = normalize_genres(
        mal=['Girls Love'], anilist=['Yuri'], kitsu=['Yuri'])

    assert by_id(result, 'girls_love') == {
        'id': 'girls_love',
        'category': 'genre',
        'sources': ['mal', 'anilist', 'kitsu'],
    }


def test_duplicates_from_one_source_do_not_duplicate_a_tag_or_source():
    result = normalize_genres(mal=['Comedy', 'Comedy'])
    assert result.tags == [{
        'id': 'comedy', 'category': 'genre', 'sources': ['mal']}]


def test_union_keeps_a_tag_supplied_by_only_one_tracker():
    result = normalize_genres(
        mal=['Comedy'], anilist=['Romance'], kitsu=['School Life'])
    assert [tag['id'] for tag in result.tags] == [
        'comedy', 'romance', 'school']


def test_unknown_raw_values_are_preserved_but_not_canonicalized():
    result = normalize_genres(kitsu=['Yuri', 'Some Unmapped Category'])
    assert [tag['id'] for tag in result.tags] == ['girls_love']
    assert result.unknown_tags == {
        'kitsu': ['Some Unmapped Category']}


def test_label_fallback_is_requested_locale_then_english_then_id():
    assert get_genre_label('girls_love', 'ja') == '百合'
    assert get_genre_label('girls_love', 'fr') == "Girls' Love"
    assert get_genre_label('not_mapped', 'ja') == 'not_mapped'


def test_category_grouping_uses_native_labels_and_punctuation():
    result = normalize_genres(
        mal=['Comedy', 'Girls Love', 'School', 'Video Game', 'Seinen'])
    assert group_genres(result, 'ja') == [
        ('ジャンル', 'コメディ・百合'),
        ('テーマ', '学園・ゲーム'),
        ('対象', '青年'),
    ]
    assert group_genres(result, 'en') == [
        ('Genres', "Comedy · Girls' Love"),
        ('Themes', 'School · Video Games'),
        ('Demographic', 'Seinen'),
    ]


def test_every_canonical_id_has_all_supported_display_labels():
    assert set(GENRE_LABELS) == set(CANONICAL_TAGS)
    for labels in GENRE_LABELS.values():
        assert set(labels) == {'en', 'ja', 'zh-Hans', 'zh-Hant', 'es'}


def test_legacy_kitsu_details_fetch_and_retain_category_provenance():
    client = libkitsu.__new__(libkitsu)
    client.prefix = 'https://example.invalid/api'
    client.mediatype = 'anime'
    requested = []
    emitted = []
    client.msg = type('Msg', (), {'debug': lambda *_args: None})()

    def request(method, url, get=None):
        requested.append((method, url, get))
        return json.dumps({
            'data': {'id': '1', 'attributes': {}},
            'included': [
                {'type': 'categories',
                 'attributes': {'title': 'School Life'}},
                {'type': 'categories', 'attributes': {'title': 'Yuri'}},
            ],
        })

    client._request = request
    client._parse_info = lambda _media: {
        'id': 1, 'extra': [('Synopsis', 'English')]}
    client._emit_signal = lambda *args: emitted.append(args)

    result = client.request_info([{'id': 1}])
    assert requested == [(
        'GET', 'https://example.invalid/api/anime/1',
        {'include': 'categories'})]
    assert result[0]['genre_sources'] == {
        'kitsu': ['School Life', 'Yuri']}
    assert ('Genres', ['School Life', 'Yuri']) in result[0]['extra']
    assert emitted == [('show_info_changed', result)]
