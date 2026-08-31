"""Language-aware primary anime titles from AOD and AniDB.

AOD supplies the exact MAL/Kitsu/AniList -> AniDB identity bridge. AniDB's
public ``anime-titles.xml(.gz)`` dump then supplies language-tagged titles.
Both databases stay local and optional; a missing or malformed file simply
leaves the tracker-provided title in place.
"""

import gzip
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache

from hakubun import i18n, utils
from hakubun.sync.relations import aod_paths, iter_aod_records

_XML_LANG = '{http://www.w3.org/XML/1998/namespace}lang'
ANIDB_TITLES_URL = 'https://anidb.net/api/anime-titles.xml.gz'
_DOWNLOAD_LOCK = threading.Lock()

_SOURCE_PATTERNS = {
    'mal': re.compile(
        r'https?://(?:www\.)?myanimelist\.net/anime/(\d+)'),
    'anilist': re.compile(
        r'https?://(?:www\.)?anilist\.co/anime/(\d+)'),
    'kitsu': re.compile(
        r'https?://(?:www\.)?kitsu\.(?:app|io)/anime/(\d+)'),
    'anidb': re.compile(
        r'https?://(?:www\.)?anidb\.net/(?:anime/|perl-bin/animedb\.pl\?[^#]*\baid=)(\d+)'),
}


def manual_anidb_title_paths():
    """User-supplied title dumps, which automatic refresh never overwrites."""
    return (
        utils.to_config_path('anime-titles.xml.gz'),
        utils.to_config_path('anime-titles.xml'),
        utils.to_data_path('anime-titles.xml.gz'),
        utils.to_data_path('anime-titles.xml'),
    )


def automatic_anidb_title_path():
    return utils.to_data_path('anime-titles-auto.xml.gz')


def anidb_title_paths():
    """Candidate local AniDB title dumps, user override first."""
    return manual_anidb_title_paths() + (automatic_anidb_title_path(),)


def _existing(paths):
    return next((os.fspath(path) for path in paths if os.path.isfile(path)), None)


def _signature(path):
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (os.path.abspath(path), stat.st_mtime_ns, stat.st_size)


def _source_id(url, provider):
    match = _SOURCE_PATTERNS[provider].search(url or '')
    return match.group(1) if match else None


class TitleDataError(Exception):
    pass


def _validate_anidb_dump(path):
    """Consume the complete dump, checking XML structure and gzip CRC."""
    opener = gzip.open if path.endswith('.gz') else open
    anime_count = 0
    with opener(path, 'rb') as source:
        for _event, element in ET.iterparse(source, events=('end',)):
            if element.tag == 'anime':
                anime_count += 1
                element.clear()
    if not anime_count:
        raise TitleDataError('AniDB title dump contains no anime records.')


def refresh_anidb_titles(url=ANIDB_TITLES_URL, max_age_days=7,
                         target=None, manual_paths=None):
    """Fetch AniDB's title dump atomically when absent or stale.

    Returns ``(path, changed)``. A user-supplied dump always wins and is never
    overwritten; automatic data uses its own filename in Hakubun's data
    directory. Failed downloads preserve any previous cached copy.
    """
    manual_paths = (manual_anidb_title_paths() if manual_paths is None
                    else tuple(map(os.fspath, manual_paths)))
    manual = _existing(manual_paths)
    if manual:
        return manual, False

    target = os.fspath(target or automatic_anidb_title_path())
    with _DOWNLOAD_LOCK:
        if os.path.isfile(target):
            age = time.time() - os.path.getmtime(target)
            if age < max_age_days * 86400:
                return target, False

        directory = os.path.dirname(target)
        utils.make_dir(directory)
        fd, temporary = tempfile.mkstemp(
            dir=directory, prefix='.anime-titles-', suffix='.xml.gz')
        try:
            request = urllib.request.Request(
                url, headers={'User-Agent': 'Hakubun+/%s' % utils.VERSION})
            with os.fdopen(fd, 'wb') as output:
                fd = -1
                with urllib.request.urlopen(request, timeout=60) as response:
                    shutil.copyfileobj(response, output)
                output.flush()
                os.fsync(output.fileno())
            _validate_anidb_dump(temporary)
            os.replace(temporary, target)
        except (OSError, EOFError, ET.ParseError,
                urllib.error.URLError) as error:
            raise TitleDataError(
                'Could not refresh AniDB title data: %s' % error) from error
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return target, True


class TitleDatabase:
    """In-memory ID bridge and language-tagged AniDB title index."""

    def __init__(self, aod_path=None, anidb_path=None):
        self._anidb_by_provider = {name: {} for name in ('mal', 'kitsu', 'anilist')}
        self._anilist_by_provider = {
            name: {} for name in ('mal', 'kitsu', 'anilist')}
        self._titles = {}
        if aod_path:
            self._load_aod(os.fspath(aod_path))
        if anidb_path:
            self._load_anidb(os.fspath(anidb_path))

    @classmethod
    def default(cls):
        aod_path = _existing(aod_paths())
        anidb_path = _existing(anidb_title_paths())
        return _cached_database(_signature(aod_path), _signature(anidb_path))

    @property
    def available(self):
        return bool(self._titles and any(self._anidb_by_provider.values()))

    def _load_aod(self, path):
        for record in iter_aod_records(path):
            sources = record.get('sources') or []
            aid = next(filter(None, (_source_id(url, 'anidb')
                                     for url in sources)), None)
            anilist_id = next(filter(
                None, (_source_id(url, 'anilist') for url in sources)), None)
            for provider in self._anidb_by_provider:
                provider_id = next(filter(
                    None, (_source_id(url, provider) for url in sources)),
                    None)
                if provider_id:
                    if aid:
                        self._anidb_by_provider[provider][provider_id] = aid
                    if anilist_id:
                        self._anilist_by_provider[provider][provider_id] = \
                            anilist_id

    def _load_anidb(self, path):
        wanted = {aid for by_id in self._anidb_by_provider.values()
                  for aid in by_id.values()}
        if not wanted:
            return
        opener = gzip.open if path.endswith('.gz') else open
        with opener(path, 'rb') as source:
            for _event, element in ET.iterparse(source, events=('end',)):
                if element.tag != 'anime':
                    continue
                aid = element.get('aid')
                if aid in wanted:
                    values = []
                    for title in element.findall('title'):
                        text = (title.text or '').strip()
                        lang = title.get(_XML_LANG)
                        if text and lang:
                            values.append((lang, title.get('type') or '', text))
                    if values:
                        self._titles[aid] = values
                element.clear()

    def title_for(self, provider, provider_id, mode='auto'):
        """Selected title, or ``None`` when either database has no match."""
        if mode == 'auto':
            mode = i18n.default_title_mode(i18n.active_language())
        aid = self._anidb_by_provider.get(provider, {}).get(str(provider_id))
        values = self._titles.get(aid, ())
        return _select_title(values, mode)

    def native_title_for(self, provider, provider_id):
        """AniDB native title without its normal romaji fallback."""
        aid = self._anidb_by_provider.get(provider, {}).get(str(provider_id))
        return _native_title(self._titles.get(aid, ()))

    def exact_title_for(self, provider, provider_id, mode='auto'):
        """Requested AniDB language title without cross-language fallback."""
        if mode == 'auto':
            mode = i18n.default_title_mode(i18n.active_language())
        aid = self._anidb_by_provider.get(provider, {}).get(str(provider_id))
        return _select_exact_title(self._titles.get(aid, ()), mode)

    def anilist_id_for(self, provider, provider_id):
        """AniList anime ID linked to a tracker entry by AOD."""
        return self._anilist_by_provider.get(provider, {}).get(
            str(provider_id))

    def titles_for(self, shows, provider, mode='auto'):
        """Return ``{show id: localized primary title}`` for matched shows."""
        selected = {}
        for show in shows:
            title = self.title_for(provider, show.get('id'), mode)
            if title and title != show.get('title'):
                selected[show['id']] = title
        return selected


@lru_cache(maxsize=4)
def _cached_database(aod_signature, anidb_signature):
    aod_path = aod_signature[0] if aod_signature else None
    anidb_path = anidb_signature[0] if anidb_signature else None
    try:
        return TitleDatabase(aod_path, anidb_path)
    except (OSError, ValueError, TypeError, ET.ParseError):
        return TitleDatabase()


def _matching(values, languages, types):
    for title_type in types:
        for language in languages:
            language_folded = language.casefold()
            for lang, current_type, text in values:
                if current_type == title_type and lang.casefold() == language_folded:
                    return text
    return None


def _select_title(values, mode):
    """Apply the user-facing title mode and its documented fallback."""
    romaji = lambda: _matching(values, ('x-jat',),
                                ('main', 'official', 'syn', 'short'))
    native = lambda: _native_title(values)

    if mode == 'english':
        return _select_exact_title(values, mode) or romaji()
    if mode == 'spanish':
        return _select_exact_title(values, mode) or romaji()
    if mode == 'zh-Hans':
        return _select_exact_title(values, mode) or romaji()
    if mode == 'zh-Hant':
        return _select_exact_title(values, mode) or romaji()
    if mode == 'native':
        return native() or romaji()
    if mode == 'romaji':
        return romaji() or native()
    return None


def _native_title(values):
    return _matching(values, ('ja',), ('official', 'main', 'syn'))


def _select_exact_title(values, mode):
    if mode == 'english':
        return _matching(values, ('en',), ('official',))
    if mode == 'spanish':
        return _matching(values, ('es', 'es-419', 'es-MX', 'es-ES',
                                  'es-CA', 'es-GA', 'es-PV'),
                         ('official',))
    if mode == 'zh-Hans':
        return _matching(values, ('zh-Hans', 'zh-CN', 'zh-SG', 'zh'),
                         ('official', 'main', 'syn'))
    if mode == 'zh-Hant':
        return _matching(values, ('zh-Hant', 'x-zht', 'zh-TW', 'zh-HK',
                                  'zh-MO'),
                         ('official', 'main', 'syn'))
    if mode == 'native':
        return _native_title(values)
    if mode == 'romaji':
        return _matching(values, ('x-jat',),
                         ('main', 'official', 'syn', 'short'))
    return None
