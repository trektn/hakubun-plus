import json

from hakubun import utils
from hakubun.synopses import (
    SynopsisResolver, _bangumi_synopsis, synopsis_from, with_synopsis,
)
from hakubun.titles import TitleDatabase


def show(**extra):
    value = {
        'id': 1,
        'title': 'Tracker Title',
        'aliases': ['Native Title'],
        'start_date': '2026-01-01',
        'type': utils.Type.TV,
        'extra': [('Synopsis', 'English synopsis')],
    }
    value.update(extra)
    return value


def resolver(tmp_path, **config):
    return SynopsisResolver(
        {'tmdb_api_key': 'configured', **config},
        cache_path=tmp_path / 'synopses.json')


def test_source_priorities_include_spanish_mexico_first(tmp_path):
    value = resolver(tmp_path)
    assert value._priorities('ja') == (
        ('madb', None), ('tmdb', 'ja-JP'))
    assert value._priorities('zh_CN') == (
        ('bangumi', 'zh-CN'), ('tmdb', 'zh-CN'))
    assert value._priorities('zh_TW') == (
        ('tmdb', 'zh-TW'), ('bangumi', 'zh-TW'))
    assert value._priorities('es') == (
        ('tmdb', 'es-MX'), ('tmdb', 'es-ES'))


def test_japanese_uses_madb_before_tmdb(tmp_path, monkeypatch):
    value = resolver(tmp_path)
    calls = []
    monkeypatch.setattr(value, '_madb',
                        lambda *_args: calls.append('madb') or '日本語の概要')
    monkeypatch.setattr(value, '_tmdb',
                        lambda *_args: calls.append('tmdb') or 'TMDB')
    assert value.resolve(show(), 'mal', 'ja') == '日本語の概要'
    assert calls == ['madb']


def test_simplified_chinese_uses_bangumi_before_tmdb(tmp_path, monkeypatch):
    value = resolver(tmp_path)
    calls = []
    monkeypatch.setattr(
        value, '_bangumi',
        lambda *_args: calls.append('bangumi') or '简体简介')
    monkeypatch.setattr(value, '_tmdb',
                        lambda *_args: calls.append('tmdb') or 'TMDB')
    assert value.resolve(show(), 'mal', 'zh_CN') == '简体简介'
    assert calls == ['bangumi']


def test_traditional_chinese_converts_bangumi_after_tmdb_miss(
        tmp_path, monkeypatch):
    value = resolver(tmp_path)
    calls = []
    monkeypatch.setattr(value, '_tmdb',
                        lambda _show, locale: calls.append(locale) or None)
    monkeypatch.setattr(value, '_bangumi',
                        lambda *_args: calls.append('bangumi') or '简体简介')
    monkeypatch.setattr(value, '_to_traditional',
                        lambda text: calls.append('opencc') or '繁體簡介')
    assert value.resolve(show(), 'mal', 'zh_TW') == '繁體簡介'
    assert calls == ['zh-TW', 'bangumi', 'opencc']


def test_spanish_falls_through_mexico_to_spain(tmp_path, monkeypatch):
    value = resolver(tmp_path)
    calls = []

    def tmdb(_show, locale):
        calls.append(locale)
        return 'Sinopsis española' if locale == 'es-ES' else None

    monkeypatch.setattr(value, '_tmdb', tmdb)
    assert value.resolve(show(), 'mal', 'es') == 'Sinopsis española'
    assert calls == ['es-MX', 'es-ES']


class _Titles:
    def native_title_for(self, provider, show_id):
        return 'ネイティブ題名'

    def exact_title_for(self, provider, show_id, mode):
        return '简体标题' if mode == 'zh-Hans' else None


def test_each_service_receives_its_intended_matching_title(
        tmp_path, monkeypatch):
    value = resolver(tmp_path)
    requests = []
    monkeypatch.setattr(TitleDatabase, 'default', lambda: _Titles())

    def request(url, params=None, method='GET', payload=None):
        requests.append((url, params, method, payload))
        if 'animedb' in url:
            return {'result': [{
                'title': 'ネイティブ題名', 'start_date': '2026/01/01',
                'story': '日本語'}]}
        if 'bgm.tv' in url:
            return {'data': [{
                'name_cn': '简体标题', 'date': '2026-01-01',
                'summary': '中文'}]}
        return {'results': [{
            'name': 'Localized', 'original_name': 'Tracker Title',
            'first_air_date': '2026-01-01', 'overview': 'TMDB'}]}

    monkeypatch.setattr(value, '_request_json', request)
    assert value._madb(show(), 'mal') == '日本語'
    assert requests[-1][1]['title'] == 'ネイティブ題名'
    assert value._bangumi(show(), 'mal') == '中文'
    assert requests[-1][3]['keyword'] == '简体标题'
    assert value._tmdb(show(), 'es-MX') == 'TMDB'
    assert requests[-1][1]['query'] == 'Tracker Title'
    assert requests[-1][1]['language'] == 'es-MX'


def test_enrich_replaces_provider_synopsis_and_persists_cache(
        tmp_path, monkeypatch):
    value = resolver(tmp_path)
    entries = [show()]
    monkeypatch.setattr(value, '_tmdb',
                        lambda *_args: 'Sinopsis localizada')
    value.enrich(entries, 'mal', 'es')

    assert synopsis_from(entries[0]) == 'Sinopsis localizada'
    saved = json.loads((tmp_path / 'synopses.json').read_text())
    assert next(iter(saved.values()))['synopsis'] == 'Sinopsis localizada'


def test_tracker_synopsis_is_the_final_fallback(tmp_path, monkeypatch):
    value = resolver(tmp_path)
    monkeypatch.setattr(value, '_tmdb', lambda *_args: None)
    assert value.resolve(show(), 'mal', 'es') == 'English synopsis'


def test_synopsis_helpers_canonicalize_description_rows():
    original = show(extra=[('Type', 'TV'), ('Description', 'Old')])
    localized = with_synopsis(original, 'New')
    assert localized is not original
    assert localized['extra'] == [('Type', 'TV'), ('Synopsis', 'New')]
    assert synopsis_from(localized) == 'New'


def test_bangumi_summary_drops_the_appended_original_language_section():
    assert _bangumi_synopsis(
        '翻译后的简介。\n\n[简介原文]\n日本語の原文。') == '翻译后的简介。'
    assert _bangumi_synopsis(
        '繁體簡介。\n【簡介原文】\n日本語。') == '繁體簡介。'
