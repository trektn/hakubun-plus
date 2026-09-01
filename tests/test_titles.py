import gzip
import io
import json
import os
from types import SimpleNamespace

import pytest

from hakubun import i18n
from hakubun import titles as titles_module
from hakubun.engine import Engine
from hakubun.titles import TitleDataError, TitleDatabase


def _database(tmp_path):
    aod = tmp_path / 'anime-offline-database.json'
    aod.write_text(json.dumps({'data': [
        {'sources': [
            'https://anidb.net/anime/10',
            'https://myanimelist.net/anime/100',
            'https://anilist.co/anime/200',
            'https://kitsu.app/anime/300',
        ]},
        {'sources': [
            'https://anidb.net/anime/20',
            'https://myanimelist.net/anime/101',
            'https://anilist.co/anime/201',
        ]},
    ]}), encoding='utf-8')
    titles = tmp_path / 'anime-titles.xml'
    titles.write_text('''<?xml version="1.0" encoding="UTF-8"?>
<animetitles>
  <anime aid="10">
    <title type="main" xml:lang="x-jat">Kusuriya no Hitorigoto</title>
    <title type="official" xml:lang="en">The Apothecary Diaries</title>
    <title type="syn" xml:lang="es">Los diarios de la boticaria</title>
    <title type="official" xml:lang="ja">薬屋のひとりごと</title>
    <title type="syn" xml:lang="zh-Hans">药屋少女的呢喃</title>
    <title type="official" xml:lang="zh-Hant">藥師少女的獨語</title>
  </anime>
  <anime aid="20">
    <title type="main" xml:lang="x-jat">Sousou no Frieren</title>
    <title type="syn" xml:lang="en">Frieren</title>
    <title type="official" xml:lang="es-419">Frieren: Más allá del final</title>
  </anime>
</animetitles>''', encoding='utf-8')
    return TitleDatabase(aod, titles)


def test_aod_maps_each_supported_tracker_to_anidb(tmp_path):
    database = _database(tmp_path)
    assert database.title_for('mal', 100, 'english') == 'The Apothecary Diaries'
    assert database.title_for('anilist', 200, 'native') == '薬屋のひとりごと'
    assert database.title_for('kitsu', 300, 'romaji') == 'Kusuriya no Hitorigoto'
    assert database.anilist_id_for('mal', 100) == '200'
    assert database.anilist_id_for('kitsu', 300) == '200'


def test_title_modes_use_official_and_language_specific_fallbacks(tmp_path):
    database = _database(tmp_path)
    assert database.title_for('mal', 100, 'zh-Hans') == '药屋少女的呢喃'
    assert database.title_for('mal', 100, 'zh-Hant') == '藥師少女的獨語'
    # English and Spanish intentionally require an official localized
    # title; a synonym is not promoted over AniDB's canonical romaji.
    assert database.title_for('mal', 100, 'spanish') == 'Kusuriya no Hitorigoto'
    assert database.title_for('mal', 101, 'english') == 'Sousou no Frieren'
    assert database.title_for('mal', 101, 'spanish') == \
        'Frieren: Más allá del final'


def test_exact_native_lookup_does_not_hide_a_missing_title_with_romaji(tmp_path):
    database = _database(tmp_path)
    assert database.native_title_for('mal', 100) == '薬屋のひとりごと'
    assert database.native_title_for('mal', 101) is None
    assert database.title_for('mal', 101, 'native') == 'Sousou no Frieren'


def test_anilist_fallback_mapping_does_not_require_an_anidb_link(tmp_path):
    aod = tmp_path / 'anime-offline-database.json'
    aod.write_text(json.dumps({'data': [{'sources': [
        'https://myanimelist.net/anime/102',
        'https://anilist.co/anime/202',
    ]}]}), encoding='utf-8')
    database = TitleDatabase(aod_path=aod)

    assert database.native_title_for('mal', 102) is None
    assert database.anilist_id_for('mal', 102) == '202'


def _native_engine(database, monkeypatch):
    monkeypatch.setattr(TitleDatabase, 'default', lambda: database)
    shows = {
        100: {'id': 100, 'title': 'Tracker A'},
        101: {'id': 101, 'title': 'Tracker B'},
    }
    engine = Engine.__new__(Engine)
    engine.account = {'api': 'mal'}
    engine.config = {
        'title_language': 'native',
        'chinese_native_title_fallback': True,
    }
    engine.data_handler = SimpleNamespace(
        userconfig={'mediatype': 'anime'}, get=lambda: shows)
    engine._anilist_native_titles = {}
    engine._anilist_native_looked_up = set()
    engine._title_fetch_epoch = 1
    return engine


def test_native_mode_prefers_anidb_then_anilist_then_romaji(
        tmp_path, monkeypatch):
    database = _database(tmp_path)
    engine = _native_engine(database, monkeypatch)
    engine._anilist_native_titles = {
        100: 'AniList must not replace AniDB',
        101: '葬送のフリーレン',
    }

    assert engine.primary_titles() == {
        100: '薬屋のひとりごと',
        101: '葬送のフリーレン',
    }
    assert engine.primary_title(engine.data_handler.get()[101]) == \
        '葬送のフリーレン'


def test_chinese_mode_can_fall_back_to_native_before_romaji(
        tmp_path, monkeypatch):
    database = _database(tmp_path)
    engine = _native_engine(database, monkeypatch)
    engine.config['title_language'] = 'zh-Hans'
    engine._anilist_native_titles = {101: '葬送のフリーレン'}

    assert engine.primary_titles() == {
        100: '药屋少女的呢喃',
        101: '葬送のフリーレン',
    }

    engine.config['chinese_native_title_fallback'] = False
    assert engine.primary_title(engine.data_handler.get()[101]) == \
        'Sousou no Frieren'


def test_details_use_primary_title_and_expose_tracker_title(
        tmp_path, monkeypatch):
    database = _database(tmp_path)
    engine = _native_engine(database, monkeypatch)
    show = engine.data_handler.get()[101]
    engine._anilist_native_titles = {101: '葬送のフリーレン'}
    engine.data_handler.info_get = lambda _show: {
        'title': 'Tracker B', 'extra': [('Type', 'TV')]}
    engine.get_next_airing = lambda _show: None
    engine.get_show_folder = lambda _show_id: None

    details = engine.get_show_details(show)

    assert details['title'] == '葬送のフリーレン'
    assert details['extra'] == [
        ('Type', 'TV'), ('Tracker title', 'Tracker B')]


def test_missing_native_titles_are_fetched_from_anilist_in_one_batch(
        tmp_path, monkeypatch):
    database = _database(tmp_path)
    engine = _native_engine(database, monkeypatch)
    calls = []
    signals = []
    engine.msg = SimpleNamespace(warn=lambda message: pytest.fail(message))
    engine._emit_signal = lambda name: signals.append(name)

    def query(_query, variables):
        calls.append(variables)
        return {'data': {'Page': {'media': [
            {'id': 201, 'title': {'native': '葬送のフリーレン'}},
        ]}}}

    engine._anilist_public_query = query
    engine._fetch_anilist_native_titles(1)

    assert calls == [{'ids': [201], 'perPage': 50}]
    assert engine._anilist_native_titles == {101: '葬送のフリーレン'}
    assert signals == ['titles_changed']


def test_titles_for_leaves_unmatched_tracker_titles_alone(tmp_path):
    database = _database(tmp_path)
    shows = [
        {'id': 100, 'title': 'Tracker title'},
        {'id': 999, 'title': 'Unmapped title'},
    ]
    assert database.titles_for(shows, 'mal', 'english') == {
        100: 'The Apothecary Diaries',
    }


def test_title_mode_matrix_follows_ui_language():
    assert [mode for mode, _label in i18n.title_mode_options('en')] == [
        'english', 'romaji', 'native']
    assert [mode for mode, _label in i18n.title_mode_options('ja')] == [
        'native']
    assert [mode for mode, _label in i18n.title_mode_options('zh_CN')] == [
        'zh-Hans', 'zh-Hant', 'native']
    assert [mode for mode, _label in i18n.title_mode_options('zh_TW')] == [
        'zh-Hant', 'zh-Hans', 'native']
    assert [mode for mode, _label in i18n.title_mode_options('es')] == [
        'spanish', 'romaji', 'native']


def test_regional_ui_languages_collapse_to_supported_modes():
    assert i18n.effective_language('es-MX') == 'es'
    assert i18n.effective_language('zh-HK') == 'zh_TW'
    assert i18n.effective_language('zh-SG') == 'zh_CN'


def _compressed_title_dump():
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode='wb') as compressed:
        compressed.write(b'''<?xml version="1.0" encoding="UTF-8"?>
<animetitles><anime aid="1"><title type="main" xml:lang="x-jat">Test</title>
</anime></animetitles>''')
    return output.getvalue()


def test_anidb_dump_is_downloaded_atomically_and_cached(tmp_path, monkeypatch):
    target = tmp_path / 'anime-titles-auto.xml.gz'
    calls = []

    def urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return io.BytesIO(_compressed_title_dump())

    monkeypatch.setattr(titles_module.urllib.request, 'urlopen', urlopen)
    path, changed = titles_module.refresh_anidb_titles(
        target=target, manual_paths=())
    assert (path, changed) == (str(target), True)
    assert calls == [(titles_module.ANIDB_TITLES_URL, 60)]
    with gzip.open(target, 'rt', encoding='utf-8') as downloaded:
        assert '<anime aid="1">' in downloaded.read()

    # A fresh automatic cache makes no second network request.
    path, changed = titles_module.refresh_anidb_titles(
        target=target, manual_paths=())
    assert (path, changed) == (str(target), False)
    assert len(calls) == 1


def test_failed_refresh_preserves_previous_cache(tmp_path, monkeypatch):
    target = tmp_path / 'anime-titles-auto.xml.gz'
    original = _compressed_title_dump()
    target.write_bytes(original)
    os.utime(target, (0, 0))
    monkeypatch.setattr(
        titles_module.urllib.request, 'urlopen',
        lambda *_args, **_kwargs: io.BytesIO(b'not a gzip file'))

    with pytest.raises(TitleDataError):
        titles_module.refresh_anidb_titles(
            max_age_days=1, target=target, manual_paths=())
    assert target.read_bytes() == original


def test_manual_anidb_dump_is_never_overwritten(tmp_path, monkeypatch):
    manual = tmp_path / 'anime-titles.xml.gz'
    manual.write_bytes(_compressed_title_dump())
    monkeypatch.setattr(
        titles_module.urllib.request, 'urlopen',
        lambda *_args, **_kwargs: pytest.fail('network should not be used'))
    assert titles_module.refresh_anidb_titles(
        target=tmp_path / 'auto.xml.gz', manual_paths=(manual,)) == \
        (str(manual), False)
