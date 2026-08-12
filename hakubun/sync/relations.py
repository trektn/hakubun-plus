"""Cross-provider id atlas, built from community id databases.

https://github.com/erengy/anime-relations (public domain; thanks to
erengy and contributors) -- the episode-redirection DB Taiga and this
app's tracker already use. Every rule line carries MAL|Kitsu|AniList id
triples, i.e. community-verified statements that three provider ids
denote the same entry. Identity resolution treats them as exact links,
exactly like provider-published ids -- these are precisely the messy
long-running/split entries where title matching fails.

Annict ids come from a second source, arm (see sync/arm.py), because
anime-relations has no Annict column and Annict is the one provider
title matching cannot rescue.

(The episode ranges in the anime-relations rules are the future path for
translating *partial* progress between differing structures; today
those surface as structure conflicts.)
"""

import os
import re

from hakubun import utils

# The column order of an anime-relations rule, not a whitelist -- the
# atlas itself is provider-agnostic (see _add_ids), which is how arm's
# Annict ids join it without touching this.
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
# Inspector and stored in a mapping's `via`, because the two are not
# equally authoritative -- anime-relations is hand-curated, arm is bulk
# community data -- and the whole point of showing an atlas opinion is
# letting the user judge it.
SOURCE_ANIME_RELATIONS = 'anime-relations'
SOURCE_ARM = 'arm'


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
        return atlas

    @classmethod
    def from_sources(cls, relations_path=None, arm_path=None):
        """Every id source the app knows about, merged.

        Both are optional and both fail quietly -- a missing database
        costs exact links, not correctness.

        arm goes in first so anime-relations overwrites it wherever the
        two overlap. That keeps every link an existing MAL/Kitsu/AniList
        account already had identical, in both id and attribution, and
        confines arm to filling gaps -- which for now means Annict.
        """
        atlas = cls()
        atlas.add_arm(arm_path)
        atlas.add_anime_relations(relations_path)
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

    def add_arm(self, path=None):
        from hakubun.sync import arm
        for ids in arm.load(path):
            self._add_ids(ids, SOURCE_ARM)
        return self

    def _add(self, triple):
        self._add_ids({provider: raw
                       for provider, raw in zip(PROVIDERS, triple)
                       if raw not in ('?', '~')},
                      SOURCE_ANIME_RELATIONS)

    def _add_ids(self, ids, source):
        """Merge one community statement that these provider ids are the
        same entry. Sources are additive: a row naming Annict and MAL
        joins up with an anime-relations rule naming the same MAL id and
        AniList, so Annict reaches AniList through it."""
        if len(ids) < 2:
            return
        for provider, provider_id in ids.items():
            others = {p: v for p, v in ids.items() if p != provider}
            key = (provider, provider_id)
            self._by_key.setdefault(key, {}).update(others)
            # Written in lockstep with the id above, so the attribution
            # always names whichever database supplied the id actually
            # being reported.
            self._sources.setdefault(key, {}).update(
                dict.fromkeys(others, source))

    def lookup(self, provider, provider_id):
        """Other providers' ids for this entry, {} when unknown."""
        return dict(self._by_key.get((provider, str(provider_id)), {}))

    def lookup_sources(self, provider, provider_id):
        """{provider: database name} for whatever lookup() returned."""
        return dict(self._sources.get((provider, str(provider_id)), {}))
