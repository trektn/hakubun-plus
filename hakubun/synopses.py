"""Language-aware anime synopsis lookup and persistent caching.

Tracker text remains the final fallback. External services are consulted only
for non-English UI languages and failures never make search/details fail.
"""

import datetime
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

from hakubun import i18n, utils
from hakubun.titles import TitleDatabase

MADB_SEARCH_URL = 'https://api.animedb.moe/madb/anime/search'
BANGUMI_SEARCH_URL = 'https://api.bgm.tv/v0/search/subjects'
TMDB_API_URL = 'https://api.themoviedb.org/3'

_POSITIVE_MAX_AGE = 30 * 86400
_NEGATIVE_MAX_AGE = 7 * 86400


def synopsis_from(show):
    """Return a provider synopsis/description from a show-info dictionary."""
    for key, value, *_rest in show.get('extra') or ():
        if key in ('Synopsis', 'Description') and isinstance(value, str):
            cleaned = utils.clean_synopsis(value)
            if cleaned:
                return cleaned
    return None


def with_synopsis(show, synopsis):
    """Return a copy whose prose uses one canonical ``Synopsis`` row."""
    if not synopsis:
        return show
    updated = dict(show)
    extra = [row for row in show.get('extra') or ()
             if row and row[0] not in ('Synopsis', 'Description')]
    extra.append(('Synopsis', utils.clean_synopsis(synopsis)))
    updated['extra'] = extra
    return updated


class SynopsisResolver:
    """Resolve localized synopses using the configured language priorities."""

    def __init__(self, config, cache_path=None):
        self.config = config
        self.cache_path = os.fspath(
            cache_path or utils.to_cache_path('localized-synopses-v1.json'))
        self._lock = threading.RLock()
        self._cache = self._load_cache()
        self._dirty = False

    def enrich(self, shows, provider, language=None):
        """Mutate search-result dictionaries with localized synopsis rows."""
        language = i18n.effective_language(
            language or i18n.active_language())
        if language == 'en' or not shows:
            return shows

        workers = min(6, len(shows))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            resolved = list(pool.map(
                lambda show: self.resolve(
                    show, provider, language, save=False), shows))
        for show, synopsis in zip(shows, resolved):
            if synopsis:
                localized = with_synopsis(show, synopsis)
                show.clear()
                show.update(localized)
        self.save()
        return shows

    def resolve(self, show, provider, language=None, save=True):
        """Localized synopsis, with the tracker's English text as fallback."""
        english = synopsis_from(show)
        language = i18n.effective_language(
            language or i18n.active_language())
        if language == 'en':
            return english

        key = self._cache_key(show, provider, language)
        cached, found = self._cached(key)
        if found:
            return cached or english

        localized = None
        for source, locale in self._priorities(language):
            try:
                if source == 'madb':
                    localized = self._madb(show, provider)
                elif source == 'bangumi':
                    localized = self._bangumi(show, provider)
                    if localized and locale == 'zh-TW':
                        localized = self._to_traditional(localized)
                else:
                    localized = self._tmdb(show, locale)
            except (OSError, ValueError, TypeError, urllib.error.URLError,
                    json.JSONDecodeError):
                localized = None
            if localized:
                break

        self._remember(key, localized)
        if save:
            self.save()
        return localized or english

    @staticmethod
    def _priorities(language):
        if language == 'ja':
            return (('madb', None), ('tmdb', 'ja-JP'))
        if language == 'zh_CN':
            return (('bangumi', 'zh-CN'), ('tmdb', 'zh-CN'))
        if language == 'zh_TW':
            return (('tmdb', 'zh-TW'), ('bangumi', 'zh-TW'))
        if language == 'es':
            return (('tmdb', 'es-MX'), ('tmdb', 'es-ES'))
        return ()

    def _madb(self, show, provider):
        title = self._native_title(show, provider)
        if not title:
            return None
        data = self._request_json(MADB_SEARCH_URL, params={
            'fields': ('anime_series_id,title,title_alias,title_english,'
                       'title_romaji_hepburn,start_date,story,description'),
            'title': title,
            'limit': 10,
        })
        match = self._best_match(
            data.get('result') or [], show,
            ('title', 'title_alias', 'title_english',
             'title_romaji_hepburn'), ('start_date',), preferred=title)
        return ((match or {}).get('story')
                or (match or {}).get('description') or None)

    def _bangumi(self, show, provider):
        database = TitleDatabase.default()
        title = (database.exact_title_for(provider, show.get('id'), 'zh-Hans')
                 or self._native_title(show, provider))
        if not title:
            return None
        data = self._request_json(
            BANGUMI_SEARCH_URL, params={'limit': 10}, method='POST',
            payload={'keyword': title, 'sort': 'match',
                     'filter': {'type': [2]}})
        match = self._best_match(
            data.get('data') or [], show, ('name_cn', 'name'), ('date',),
            preferred=title)
        return _bangumi_synopsis((match or {}).get('summary'))

    def _tmdb(self, show, locale):
        api_key = (self.config.get('tmdb_api_key')
                   or os.environ.get('HAKUBUN_TMDB_API_KEY'))
        if not api_key:
            return None

        title = (show.get('title') or '').strip()
        if not title:
            return None
        year = _year_of(show.get('start_date'))
        media = ('movie', 'tv') if show.get('type') == utils.Type.MOVIE \
            else ('tv', 'movie')
        for media_type in media:
            params = {
                'api_key': api_key,
                'query': title,
                'language': locale,
                'include_adult': 'false',
            }
            if year:
                params['primary_release_year' if media_type == 'movie'
                       else 'first_air_date_year'] = year
            data = self._request_json(
                '%s/search/%s' % (TMDB_API_URL, media_type), params=params)
            match = self._best_match(
                data.get('results') or [], show,
                ('name', 'original_name', 'title', 'original_title'),
                ('first_air_date', 'release_date'), preferred=title,
                trust_first=True)
            overview = (match or {}).get('overview')
            if overview:
                return overview
        return None

    def _native_title(self, show, provider):
        database = TitleDatabase.default()
        return (database.native_title_for(provider, show.get('id'))
                or show.get('title_native')
                or next((alias for alias in show.get('aliases') or ()
                         if _has_cjk(alias)), None))

    def _best_match(self, candidates, show, title_fields, date_fields,
                    preferred=None, trust_first=False):
        if not candidates:
            return None
        titles = [preferred, show.get('title')]
        titles.extend(show.get('aliases') or ())
        titles = [title for title in titles if title]
        wanted_year = _year_of(show.get('start_date'))
        ranked = []
        for index, candidate in enumerate(candidates):
            names = [candidate.get(field) for field in title_fields]
            score = max((_title_score(a, b) for a in titles for b in names
                         if a and b), default=0)
            candidate_year = next(filter(None, (
                _year_of(candidate.get(field)) for field in date_fields)), None)
            year_match = bool(wanted_year and candidate_year == wanted_year)
            ranked.append((score + (0.2 if year_match else 0), -index,
                           year_match, candidate))
        score, _position, year_match, match = max(ranked, key=lambda row: row[:2])
        if score >= 0.72 or (trust_first and (year_match or match is candidates[0])):
            return match
        return None

    def _request_json(self, url, params=None, method='GET', payload=None):
        if params:
            url = '%s?%s' % (url, urllib.parse.urlencode(params))
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'Hakubun-Plus/%s' % utils.VERSION,
        })
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode('utf-8'))

    @staticmethod
    def _to_traditional(text):
        try:
            from opencc import OpenCC
            return OpenCC('s2twp').convert(text)
        except (ImportError, OSError, ValueError):
            return text

    def _cache_key(self, show, provider, language):
        api_key = (self.config.get('tmdb_api_key')
                   or os.environ.get('HAKUBUN_TMDB_API_KEY') or '')
        key_marker = hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:8] \
            if api_key else 'none'
        return '%s:%s:%s:%s' % (
            language, provider, show.get('id'), key_marker)

    def _cached(self, key):
        with self._lock:
            entry = self._cache.get(key)
            if not isinstance(entry, dict):
                return None, False
            max_age = (_POSITIVE_MAX_AGE if entry.get('synopsis')
                       else _NEGATIVE_MAX_AGE)
            if time.time() - entry.get('time', 0) > max_age:
                self._cache.pop(key, None)
                self._dirty = True
                return None, False
            return entry.get('synopsis'), True

    def _remember(self, key, synopsis):
        with self._lock:
            self._cache[key] = {
                'synopsis': utils.clean_synopsis(synopsis) if synopsis else None,
                'time': time.time(),
            }
            self._dirty = True

    def _load_cache(self):
        try:
            with open(self.cache_path, encoding='utf-8') as source:
                data = json.load(source)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def save(self):
        with self._lock:
            if not self._dirty:
                return
            utils.make_dir(os.path.dirname(self.cache_path))
            utils.save_config(self._cache, self.cache_path)
            self._dirty = False


def _year_of(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.year
    if value:
        try:
            return int(str(value)[:4])
        except (TypeError, ValueError):
            pass
    return None


def _normalized_title(value):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ''.join(character for character in value if character.isalnum())


def _title_score(left, right):
    left = _normalized_title(left)
    right = _normalized_title(right)
    if not left or not right:
        return 0
    if left == right:
        return 1
    return SequenceMatcher(None, left, right).ratio()


def _has_cjk(value):
    return any('\u3040' <= character <= '\u30ff'
               or '\u3400' <= character <= '\u9fff'
               for character in str(value or ''))


def _bangumi_synopsis(value):
    """Drop Bangumi's appended source-language quotation.

    Some Chinese summaries include a translated synopsis followed by a
    ``[简介原文]``/``[簡介原文]`` block. That block is provenance, not a second
    synopsis, and displaying it makes the details page look duplicated.
    """
    if not value:
        return None
    translated = re.split(
        r'\s*[\[【［](?:简介|簡介)原文[\]】］]\s*', str(value),
        maxsplit=1)[0]
    return utils.clean_synopsis(translated) or None
