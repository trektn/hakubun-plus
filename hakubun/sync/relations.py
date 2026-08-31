"""Cross-provider id atlas, built from community id databases.

https://github.com/erengy/anime-relations (public domain; thanks to
erengy and contributors) -- the episode-redirection DB Taiga and this
app's tracker already use. Every rule line carries MAL|Kitsu|AniList id
triples, i.e. community-verified statements that three provider ids
denote the same entry. Identity resolution treats them as exact links,
exactly like provider-published ids -- these are precisely the messy
long-running/split entries where title matching fails.

(The episode ranges in the anime-relations rules are the future path for
translating *partial* progress between differing structures; today
those surface as structure conflicts.)

The same atlas can optionally ingest anime-offline-database (AOD) JSON.
AOD is deliberately loaded from a user/data file rather than bundled: its
database is licensed ODbL 1.0 + DbCL 1.0 and is substantially larger than the
small CC0 anime-relations snapshot.
"""

import json
import os
import re

from hakubun import utils

# The column order of an anime-relations rule, not a whitelist -- the
# atlas itself is provider-agnostic (see _add_ids), so another id
# database could join it without touching this.
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


# Which database a link came from. Surfaced all the way to the
# Inspector and stored in a mapping's `via`: the whole point of showing
# an atlas opinion is letting the user judge it, which means naming
# where it came from.
SOURCE_ANIME_RELATIONS = 'anime-relations'
SOURCE_AOD = 'anime-offline-database'

_SOURCE_PRIORITY = {
    SOURCE_ANIME_RELATIONS: 20,
    SOURCE_AOD: 10,
}


def aod_paths():
    """Candidate local AOD files, from user override to app data cache."""
    return (
        utils.to_config_path('anime-offline-database.json'),
        utils.to_data_path('anime-offline-database.json'),
        utils.to_config_path('anime-offline-database.jsonl'),
        utils.to_data_path('anime-offline-database.jsonl'),
    )


class RelationsAtlas:
    def __init__(self):
        self._by_key = {}   # (provider, id) -> {other provider: id}
        self._sources = {}  # (provider, id) -> {other provider: source}

    def __len__(self):
        return len(self._by_key)

    @classmethod
    def from_file(cls, path=None):
        atlas = cls()
        atlas.add_anime_relations(path)
        # A caller supplying an explicit relations fixture/file gets only
        # that file; the normal no-argument construction loads both default
        # community databases.
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
        self._add_ids({provider: raw
                       for provider, raw in zip(PROVIDERS, triple)
                       if raw not in ('?', '~')},
                      SOURCE_ANIME_RELATIONS)

    def _add_ids(self, ids, source):
        """Merge one community statement that these provider ids are the
        same entry. Sources are additive: a row naming two providers
        joins up with any other row naming one of them, so an entry can
        reach a provider no single row named directly."""
        if len(ids) < 2:
            return
        for provider, provider_id in ids.items():
            others = {p: v for p, v in ids.items() if p != provider}
            key = (provider, provider_id)
            links = self._by_key.setdefault(key, {})
            old_sources = self._sources.setdefault(key, {})
            for other, other_id in others.items():
                old_source = old_sources.get(other)
                old_priority = _SOURCE_PRIORITY.get(old_source, 0)
                new_priority = _SOURCE_PRIORITY.get(source, 0)
                if other not in links or new_priority > old_priority:
                    links[other] = other_id
                    old_sources[other] = source
            # Written in lockstep with the id above, so the attribution
            # always names whichever database supplied the id actually
            # being reported.

    def add_aod(self, path=None):
        """Add cross-provider IDs from an AOD JSON or JSONL export.

        AOD source URLs are the stable interchange format. We only consume
        MAL, Kitsu, and AniList anime URLs; metadata and relatedAnime are
        intentionally ignored. JSONL's first metadata line is skipped.
        """
        path = path or next((p for p in aod_paths() if os.path.isfile(p)), None)
        if not path:
            return self
        path = os.fspath(path)
        try:
            if path.endswith('.jsonl'):
                with open(path, encoding='utf-8') as f:
                    records = []
                    for line in f:
                        item = json.loads(line)
                        if isinstance(item, dict) and 'sources' in item:
                            records.append(item)
            else:
                with open(path, encoding='utf-8') as f:
                    root = json.load(f)
                records = root.get('data', []) if isinstance(root, dict) else root
            for record in records:
                if not isinstance(record, dict):
                    continue
                ids = {}
                for url in record.get('sources', []):
                    match = re.search(r'https?://(?:www\.)?myanimelist\.net/anime/(\d+)', url)
                    if match:
                        ids['mal'] = match.group(1)
                        continue
                    match = re.search(r'https?://(?:www\.)?anilist\.co/anime/(\d+)', url)
                    if match:
                        ids['anilist'] = match.group(1)
                        continue
                    match = re.search(r'https?://(?:www\.)?kitsu\.(?:app|io)/anime/(\d+)', url)
                    if match:
                        ids['kitsu'] = match.group(1)
                self._add_ids(ids, SOURCE_AOD)
        except (OSError, ValueError, TypeError):
            pass  # optional enrichment must never prevent syncing
        return self

    def lookup(self, provider, provider_id):
        """Other providers' ids for this entry, {} when unknown."""
        return dict(self._by_key.get((provider, str(provider_id)), {}))

    def lookup_sources(self, provider, provider_id):
        """{provider: database name} for whatever lookup() returned."""
        return dict(self._sources.get((provider, str(provider_id)), {}))
