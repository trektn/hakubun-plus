"""Cross-provider id atlas built from community databases.

https://github.com/erengy/anime-relations (public domain; thanks to
erengy and contributors) -- the episode-redirection DB Taiga and this
app's tracker already use. Every rule line carries MAL|Kitsu|AniList id
triples, i.e. community-verified statements that three provider ids
denote the same entry. Identity resolution treats them as exact links,
exactly like provider-published ids -- these are precisely the messy
long-running/split entries where title matching fails.

(The episode ranges in the same rules are the future path for
translating *partial* progress between differing structures; today
those surface as structure conflicts.)

The same atlas can optionally ingest anime-offline-database (AOD) JSON.
AOD is loaded from a user/config data file rather than bundled: its database
is licensed ODbL 1.0 + DbCL 1.0 and is substantially larger than the small
CC0 anime-relations snapshot.
"""

import json
import os
import re

from hakubun import utils

PROVIDERS = ('mal', 'kitsu', 'anilist')

_ID = r'(\d+|[?~])'
_RULE = re.compile(
    r'- ' + r'\|'.join([_ID] * 3) + r':\S+'
    r' -> ' + r'\|'.join([_ID] * 3) + r':\S+')


def default_path():
    """Same resolution chain as the engine's redirections loader:
    user-provided copy, then the auto-synced one the tracker keeps
    fresh, then the bundled submodule snapshot."""
    candidates = (
        utils.to_config_path('anime-relations.txt'),
        utils.to_data_path('anime-relations.txt'),
        os.path.join(utils.DATADIR, 'anime-relations',
                     'anime-relations.txt'),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[-1]


def aod_paths():
    """Candidate local AOD files, from user override to app data cache."""
    return (
        utils.to_config_path('anime-offline-database.json'),
        utils.to_config_path('anime-offline-database-minified.json'),
        utils.to_config_path('anime-offline-database.jsonl'),
        utils.to_data_path('anime-offline-database.json'),
        utils.to_data_path('anime-offline-database-minified.json'),
        utils.to_data_path('anime-offline-database.jsonl'),
    )


def iter_aod_records(path):
    """Yield anime records from an AOD JSON or JSONL export."""
    path = os.fspath(path)
    if path.endswith('.jsonl'):
        with open(path, encoding='utf-8') as source:
            for line in source:
                item = json.loads(line)
                # JSONL's first line is database metadata, not an anime.
                if isinstance(item, dict) and 'sources' in item:
                    yield item
        return

    with open(path, encoding='utf-8') as source:
        root = json.load(source)
    records = root.get('data', []) if isinstance(root, dict) else root
    for item in records:
        if isinstance(item, dict):
            yield item


class RelationsAtlas:
    def __init__(self):
        self._by_key = {}   # (provider, id) -> {other provider: id}

    def __len__(self):
        return len(self._by_key)

    @classmethod
    def from_file(cls, path=None):
        atlas = cls()
        atlas.add_anime_relations(path)
        # Explicit paths are fixtures/user-selected anime-relations files;
        # normal construction also picks up the optional local AOD export.
        if path is None:
            atlas.add_aod()
        return atlas

    def add_anime_relations(self, path=None):
        try:
            with open(path or default_path()) as f:
                for line in f:
                    match = _RULE.match(line.strip())
                    if not match:
                        continue
                    groups = match.groups()
                    self._add(groups[0:3])
                    self._add(groups[3:6])
        except OSError:
            pass  # the atlas is an optional enrichment
        return self

    def _add(self, triple):
        ids = {provider: raw for provider, raw in zip(PROVIDERS, triple)
               if raw not in ('?', '~')}
        self._add_ids(ids, overwrite=True)

    def _add_ids(self, ids, overwrite=False):
        """Merge one statement that these provider IDs are equivalent.

        anime-relations is loaded first and has priority over AOD when the
        two community databases disagree.
        """
        if len(ids) < 2:
            return
        for provider, provider_id in ids.items():
            links = self._by_key.setdefault((provider, str(provider_id)), {})
            for other, other_id in ids.items():
                if other == provider:
                    continue
                if overwrite:
                    links[other] = str(other_id)
                else:
                    links.setdefault(other, str(other_id))

    def add_aod(self, path=None):
        """Add MAL/Kitsu/AniList links from a local AOD export."""
        path = path or next((p for p in aod_paths() if os.path.isfile(p)), None)
        if not path:
            return self
        try:
            for record in iter_aod_records(path):
                ids = {}
                for url in record.get('sources', []):
                    match = re.search(
                        r'https?://(?:www\.)?myanimelist\.net/anime/(\d+)',
                        url)
                    if match:
                        ids['mal'] = match.group(1)
                        continue
                    match = re.search(
                        r'https?://(?:www\.)?anilist\.co/anime/(\d+)', url)
                    if match:
                        ids['anilist'] = match.group(1)
                        continue
                    match = re.search(
                        r'https?://(?:www\.)?kitsu\.(?:app|io)/anime/(\d+)',
                        url)
                    if match:
                        ids['kitsu'] = match.group(1)
                self._add_ids(ids)
        except (OSError, ValueError, TypeError):
            pass  # optional enrichment must never prevent syncing
        return self

    def lookup(self, provider, provider_id):
        """Other providers' ids for this entry, {} when unknown."""
        return dict(self._by_key.get((provider, str(provider_id)), {}))
