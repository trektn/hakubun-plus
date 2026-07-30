"""Cross-provider id atlas built from the anime-relations database.

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
"""

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


class RelationsAtlas:
    def __init__(self):
        self._by_key = {}   # (provider, id) -> {other provider: id}

    def __len__(self):
        return len(self._by_key)

    @classmethod
    def from_file(cls, path=None):
        atlas = cls()
        try:
            with open(path or default_path()) as f:
                for line in f:
                    match = _RULE.match(line.strip())
                    if not match:
                        continue
                    groups = match.groups()
                    atlas._add(groups[0:3])
                    atlas._add(groups[3:6])
        except OSError:
            pass  # the atlas is an optional enrichment
        return atlas

    def _add(self, triple):
        ids = {provider: raw for provider, raw in zip(PROVIDERS, triple)
               if raw not in ('?', '~')}
        if len(ids) < 2:
            return
        for provider, provider_id in ids.items():
            self._by_key.setdefault((provider, provider_id), {}).update(
                {p: v for p, v in ids.items() if p != provider})

    def lookup(self, provider, provider_id):
        """Other providers' ids for this entry, {} when unknown."""
        return dict(self._by_key.get((provider, str(provider_id)), {}))
