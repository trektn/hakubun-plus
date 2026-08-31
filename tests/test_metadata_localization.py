import datetime

from hakubun import metadata, utils


def row_map(details):
    return dict(details['extra'])


def test_japanese_details_use_native_metadata_grammar_and_grouping():
    details = {
        'extra': [
            ('English', 'English title'),
            ('Season', 'Summer 2026'),
            ('Genres', ['Comedy', 'Girls Love', 'School', 'Video Game',
                        'Seinen', 'Unmapped']),
            ('Type', utils.Type.TV),
            ('Status', utils.Status.AIRING),
        ]}
    localized = metadata.localize_details(details, 'mal', 'ja')
    rows = row_map(localized)

    assert rows['Season'] == '2026年夏'
    assert rows['ジャンル'] == 'コメディ・百合'
    assert rows['テーマ'] == '学園・ゲーム'
    assert rows['対象'] == '青年'
    assert rows['Type'] == 'TVアニメ'
    assert rows['Status'] == '放送中'
    assert localized['unknown_genre_tags'] == {'mal': ['Unmapped']}
    assert 'Genres' not in rows


def test_type_labels_are_native_in_every_supported_language():
    assert metadata.localize_type(utils.Type.TV, 'ja') == 'TVアニメ'
    assert metadata.localize_type(utils.Type.MOVIE, 'ja') == '映画'
    assert metadata.localize_type(utils.Type.OVA, 'ja') == 'OVA'
    assert metadata.localize_type(utils.Type.ONA, 'ja') == 'ONA'
    assert metadata.localize_type(utils.Type.SPECIAL, 'ja') == 'スペシャル'
    assert metadata.localize_type(utils.Type.MUSIC, 'ja') == 'MV'
    assert metadata.localize_type(utils.Type.TV, 'zh_CN') == '电视动画'
    assert metadata.localize_type(utils.Type.MOVIE, 'zh_TW') == '電影'
    assert metadata.localize_type(utils.Type.TV, 'es') == 'Serie de TV'


def test_season_grammar_is_not_an_english_template_with_translated_words():
    assert metadata.localize_season('Summer 2026', 'en') == 'Summer 2026'
    assert metadata.localize_season('Summer 2026', 'ja') == '2026年夏'
    assert metadata.localize_season('Summer 2026', 'zh_CN') == '2026年夏季'
    assert metadata.localize_season('Summer 2026', 'zh_TW') == '2026年夏季'
    assert metadata.localize_season('Summer 2026', 'es') == 'Verano de 2026'


def test_localized_seasons_keep_chronological_sorting(monkeypatch):
    monkeypatch.setattr('hakubun.i18n._active_language', 'ja')
    assert utils.season_sort_key('2026年冬') \
        < utils.season_sort_key('2026年夏')
    monkeypatch.setattr('hakubun.i18n._active_language', 'es')
    assert utils.season_sort_key('Primavera de 2026') \
        < utils.season_sort_key('Otoño de 2026')


def test_metadata_can_be_relocalized_without_duplicate_category_rows():
    source = {
        'type': utils.Type.TV,
        'status': utils.Status.AIRING,
        'extra': [
            ('Season', 'Summer 2026'),
            ('Genres', ['Comedy', 'School', 'Seinen']),
            ('Type', utils.Type.TV),
            ('Status', utils.Status.AIRING),
        ],
    }
    japanese = metadata.localize_details(source, 'mal', 'ja')
    spanish = metadata.localize_details(japanese, 'mal', 'es')
    rows = row_map(spanish)

    assert rows['Season'] == 'Verano de 2026'
    assert rows['Géneros'] == 'Comedia'
    assert rows['Temas'] == 'Escolar'
    assert rows['Demografía'] == 'Seinen'
    assert rows['Type'] == 'Serie de TV'
    assert rows['Status'] == 'En emisión'
    assert not any(key in rows for key in ('ジャンル', 'テーマ', '対象'))
