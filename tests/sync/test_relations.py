"""anime-relations id atlas: parsing and identity enrichment."""

from hakubun.sync.engine import SyncEngine
from hakubun.sync.relations import RelationsAtlas
from hakubun.sync.adapters import ProviderAdapter

from conftest import FakeLib, show

RULES = """
::meta
- version: 1.3.0

::rules

# Monogatari
- 5081|4814|5081:13 -> 9260|5520|9260:1!
# Long-runner with unknown kitsu column
- 20|?|20:26-51 -> 199|?|199:1-26
"""


def _atlas(tmp_path):
    path = tmp_path / 'anime-relations'
    path.write_text(RULES)
    return RelationsAtlas.from_file(str(path))


def test_atlas_parses_triples_both_sides(tmp_path):
    atlas = _atlas(tmp_path)
    assert atlas.lookup('mal', '5081') == {'kitsu': '4814',
                                           'anilist': '5081'}
    assert atlas.lookup('kitsu', '5520') == {'mal': '9260',
                                             'anilist': '9260'}
    # '?' columns are simply absent, not wrong links.
    assert atlas.lookup('mal', '20') == {'anilist': '20'}
    assert atlas.lookup('kitsu', '20') == {}
    assert atlas.lookup('mal', '99999') == {}


def test_missing_file_yields_empty_atlas(tmp_path):
    atlas = RelationsAtlas.from_file(str(tmp_path / 'nope'))
    assert len(atlas) == 0


def test_atlas_links_entries_without_published_ids(store, tmp_path):
    """A Kitsu entry with no provider-published mapping still links
    exactly via the community atlas -- no title matching involved."""
    libs = {'mal': FakeLib('mal', [show('mal', 5081, 'Bakemonogatari')]),
            'kitsu': FakeLib('kitsu',
                             [show('kitsu', 4814, 'Bakemonogatari TV')])}
    adapters = {n: ProviderAdapter(n, l) for n, l in libs.items()}
    eng = SyncEngine(store, adapters, relations=_atlas(tmp_path))
    assert eng.fetch() == {}
    assert len(store.entities()) == 1
    mapping = store.mapping_for('kitsu', '4814')
    assert mapping is not None and mapping['confirmed'] == 1
    assert store.identity_open() == []


def test_real_relations_file_parses_when_available():
    """The bundled copy is a git submodule that may not be checked out
    in every environment; when any real file resolves, it must parse
    into a substantial atlas."""
    import os
    import pytest
    from hakubun.sync import relations
    if not os.path.isfile(relations.default_path()):
        pytest.skip('anime-relations submodule not checked out')
    atlas = RelationsAtlas.from_file()
    assert len(atlas) > 100              # thousands of rules in practice
