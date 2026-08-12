"""arm supplies the Annict half of the cross-provider id atlas.

It is the only way an Annict entry can be linked at all: Annict's titles
are Japanese-only and it publishes no AniList id, so the title-similarity
fallback identity resolution normally relies on cannot rescue it.
"""
import json
import os
import socket
import threading

import pytest

from hakubun.sync import arm
from hakubun.sync.relations import RelationsAtlas


ROWS = [
    # Frieren, as arm actually records it.
    {"mal_id": 52991, "anilist_id": 154587, "annict_id": 10079,
     "syobocal_tid": 6776, "anidb_id": 17617,
     "animeplanet_id": "frieren-beyond-journeys-end"},
    # Annict + MAL only -- arm has no AniList id for plenty of works.
    {"mal_id": 33123, "annict_id": 11477},
    # No Annict id: relates providers that already link fine without it.
    {"mal_id": 21, "anilist_id": 21},
    # Annict alone relates nothing to anything.
    {"annict_id": 99999},
]


@pytest.fixture
def armfile(tmp_path):
    path = tmp_path / 'arm.json'
    path.write_text(json.dumps(ROWS))
    arm._cache.clear()
    return str(path)


def test_ids_are_strings(armfile):
    """arm stores ids as JSON integers; the atlas keys on strings, so an
    un-coerced int would simply never match a lookup."""
    rows = arm.load(armfile)

    assert all(isinstance(v, str) for row in rows for v in row.values())


def test_only_rows_naming_annict_are_ingested(armfile):
    """Widening this to arm's MAL/AniList-only rows would change how
    existing accounts resolve identity -- a much bigger blast radius
    than adding a provider that has none."""
    rows = arm.load(armfile)

    assert all('annict' in row for row in rows)
    assert not any(row.get('mal') == '21' for row in rows)


def test_a_row_naming_only_annict_is_dropped(armfile):
    assert not any(row == {'annict': '99999'} for row in arm.load(armfile))


def test_unknown_providers_are_not_carried(armfile):
    # arm relates AniDB/Anime-Planet/Syoboi too; there is no backend for
    # those, and an unrecognized provider key would only confuse the
    # Inspector.
    assert set(arm.load(armfile)[0]) == {'mal', 'anilist', 'annict'}


def test_a_missing_file_yields_nothing(tmp_path):
    assert arm.load(str(tmp_path / 'nope.json')) == []


def test_a_corrupt_file_yields_nothing(tmp_path):
    path = tmp_path / 'arm.json'
    path.write_text('<!DOCTYPE html>not json')
    arm._cache.clear()

    assert arm.load(str(path)) == []


def test_parsing_is_cached_across_calls(armfile):
    first = arm.load(armfile)

    assert arm.load(armfile) is first


def test_two_files_do_not_evict_each_other(tmp_path, armfile):
    """A config-dir copy alongside a stale data-dir one would otherwise
    make every sync-window open re-parse several megabytes."""
    other = tmp_path / 'other.json'
    other.write_text(json.dumps(ROWS[:1]))

    first = arm.load(armfile)
    arm.load(str(other))

    assert arm.load(armfile) is first


def test_a_rewritten_file_is_reread(tmp_path):
    path = tmp_path / 'arm.json'
    path.write_text(json.dumps(ROWS[:1]))
    arm._cache.clear()
    assert len(arm.load(str(path))) == 1

    path.write_text(json.dumps(ROWS))
    os.utime(str(path), (0, 0))   # force a different mtime

    assert len(arm.load(str(path))) == 2


# --------------------------------------------------------------------
# Atlas integration
# --------------------------------------------------------------------

def test_annict_resolves_to_the_other_providers(armfile):
    atlas = RelationsAtlas().add_arm(armfile)

    assert atlas.lookup('annict', 10079) == {'mal': '52991',
                                             'anilist': '154587'}


def test_the_link_works_in_reverse(armfile):
    atlas = RelationsAtlas().add_arm(armfile)

    assert atlas.lookup('mal', 52991)['annict'] == '10079'


def test_an_int_id_looks_up_the_same_as_a_string(armfile):
    atlas = RelationsAtlas().add_arm(armfile)

    assert atlas.lookup('annict', 10079) == atlas.lookup('annict', '10079')


def test_arm_joins_up_with_anime_relations(tmp_path, armfile):
    """The two sources are additive: arm knows Annict 11477 is MAL
    33123, anime-relations knows that MAL id's Kitsu and AniList ids, so
    Annict reaches providers arm never named."""
    relations = tmp_path / 'anime-relations.txt'
    relations.write_text('- 33123|444|555:1-12 -> 33123|444|555:1-12\n')

    atlas = RelationsAtlas.from_sources(str(relations), armfile)

    assert atlas.lookup('annict', 11477)['mal'] == '33123'
    assert atlas.lookup('mal', 33123) == {'kitsu': '444', 'anilist': '555',
                                          'annict': '11477'}


def test_an_absent_arm_leaves_anime_relations_intact(tmp_path):
    relations = tmp_path / 'anime-relations.txt'
    relations.write_text('- 1|2|3:1-12 -> 1|2|3:1-12\n')

    atlas = RelationsAtlas.from_sources(str(relations),
                                        str(tmp_path / 'nope.json'))

    assert atlas.lookup('mal', 1) == {'kitsu': '2', 'anilist': '3'}


# --------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------

def test_an_arm_link_is_attributed_to_arm(armfile):
    """The Inspector exists so the user can weigh an atlas opinion, and
    the two databases are not equally authoritative -- reporting an arm
    link as anime-relations would be a lie in the one place built for
    checking."""
    atlas = RelationsAtlas().add_arm(armfile)

    assert atlas.lookup_sources('annict', 10079) == {'mal': 'arm',
                                                     'anilist': 'arm'}


def test_anime_relations_keeps_its_own_attribution(tmp_path, armfile):
    relations = tmp_path / 'anime-relations.txt'
    relations.write_text('- 1|2|3:1-12 -> 1|2|3:1-12\n')

    atlas = RelationsAtlas.from_sources(str(relations), armfile)

    assert atlas.lookup_sources('mal', 1) == {'kitsu': 'anime-relations',
                                              'anilist': 'anime-relations'}
    assert atlas.lookup_sources('annict', 10079)['mal'] == 'arm'


def test_anime_relations_wins_where_the_two_overlap(tmp_path, armfile):
    """Existing MAL/Kitsu/AniList accounts must resolve exactly as
    before -- same ids, same attribution. arm only fills gaps."""
    relations = tmp_path / 'anime-relations.txt'
    relations.write_text('- 52991|777|999:1-12 -> 52991|777|999:1-12\n')

    atlas = RelationsAtlas.from_sources(str(relations), armfile)

    assert atlas.lookup('mal', 52991)['anilist'] == '999'
    assert atlas.lookup_sources('mal', 52991)['anilist'] == 'anime-relations'
    # and arm's own contribution survives alongside it
    assert atlas.lookup('mal', 52991)['annict'] == '10079'


def test_the_inspector_names_the_database(armfile):
    from hakubun.sync.inspect import InspectionResult, atlas_label

    atlas = RelationsAtlas().add_arm(armfile)
    result = InspectionResult(
        provider='annict', provider_id='10079', found=False,
        atlas_hint=atlas.lookup('annict', 10079),
        atlas_sources=atlas.lookup_sources('annict', 10079))

    assert atlas_label(result) == 'arm atlas'


def test_the_inspector_names_both_when_both_contributed(tmp_path, armfile):
    from hakubun.sync.inspect import InspectionResult, atlas_label

    relations = tmp_path / 'anime-relations.txt'
    relations.write_text('- 52991|777|154587:1-12 -> 52991|777|154587:1-12\n')
    atlas = RelationsAtlas.from_sources(str(relations), armfile)

    result = InspectionResult(
        provider='mal', provider_id='52991', found=False,
        atlas_hint=atlas.lookup('mal', 52991),
        atlas_sources=atlas.lookup_sources('mal', 52991))

    assert atlas_label(result) == 'id atlas (anime-relations, arm)'


def test_an_atlas_with_no_provenance_still_has_a_label():
    from hakubun.sync.inspect import InspectionResult, atlas_label

    assert atlas_label(InspectionResult(provider='mal', provider_id='1',
                                        found=False)) == 'id atlas'


# --------------------------------------------------------------------
# Syncing
# --------------------------------------------------------------------

def test_syncing_is_skipped_when_disabled():
    assert arm.sync({'arm_time': 0, 'arm_url': 'https://example/arm.json'}) \
        is False
    assert arm.sync({'arm_time': 7, 'arm_url': ''}) is False


@pytest.fixture
def blackhole():
    """A server that accepts a connection and then says nothing, ever."""
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    held = []

    def accept():
        try:
            held.append(server.accept()[0])
        except OSError:
            pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield 'http://%s:%d/arm.json' % server.getsockname()
    server.close()
    for conn in held:
        conn.close()


def test_a_server_that_never_answers_cannot_hang_startup(tmp_path, blackhole,
                                                         monkeypatch):
    """This runs from Engine.start(). utils.sync_file passes no timeout,
    so it would block the app launch forever rather than raise."""
    monkeypatch.setattr(arm, 'DOWNLOAD_TIMEOUT', 1)

    assert arm._download(blackhole, str(tmp_path / 'arm.json')) is False


def test_a_failed_download_leaves_no_file_behind(tmp_path, blackhole,
                                                 monkeypatch):
    """A truncated JSON array that still parses would become a partial
    atlas -- quietly wrong, which is worse than having none."""
    monkeypatch.setattr(arm, 'DOWNLOAD_TIMEOUT', 1)
    path = tmp_path / 'arm.json'

    arm._download(blackhole, str(path))

    assert not path.exists()
    assert not (tmp_path / 'arm.json.tmp').exists()


def test_a_failed_refresh_keeps_the_existing_copy(tmp_path, blackhole,
                                                  monkeypatch):
    monkeypatch.setattr(arm, 'DOWNLOAD_TIMEOUT', 1)
    path = tmp_path / 'arm.json'
    path.write_text(json.dumps(ROWS))

    assert arm._download(blackhole, str(path)) is False
    assert json.loads(path.read_text()) == ROWS
