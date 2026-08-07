"""Regressions for guess_show's fuzzy matcher.

guess_show dominates library scan time -- it runs once per unique parsed
title against every tracked title and alias -- so it has two fast paths:
rapidfuzz when installed, a pruned difflib sweep otherwise. Both must agree
with the naive ratio() sweep they replaced.
"""
import difflib
import random

import pytest

from hakubun import utils


def naive(show_title, showlist):
    """The matcher as it was originally written, kept as the oracle."""
    highest_ratio = (None, 0)
    matcher = difflib.SequenceMatcher()
    matcher.set_seq1(show_title.lower())
    for item in showlist.values():
        for title in item['titles']:
            matcher.set_seq2(title.lower())
            ratio = matcher.ratio()
            if ratio > highest_ratio[1]:
                highest_ratio = (item, ratio)
    if highest_ratio[1] > utils.GUESS_SHOW_CUTOFF:
        return highest_ratio[0]
    return None


TITLES = [
    ['Cowboy Bebop', 'カウボーイビバップ'],
    ['Steins;Gate', 'Steins Gate', 'シュタインズ・ゲート'],
    ['Steins;Gate 0', 'Steins Gate Zero'],
    ['Mushishi', 'Mushi-Shi', '蟲師'],
    ['Mushishi Zoku Shou', 'Mushishi Season 2'],
    ['Serial Experiments Lain', 'Lain'],
    ['Monogatari Series: Second Season'],
    ['Bakemonogatari'],
    ['Legend of the Galactic Heroes', 'Ginga Eiyuu Densetsu'],
    ['Ping Pong the Animation', 'Ping Pong'],
]


@pytest.fixture
def showlist():
    return {i: {'id': i, 'title': t[0], 'titles': t, 'my_progress': 0,
                'total': 0, 'type': None}
            for i, t in enumerate(TITLES)}


@pytest.fixture
def tracker_list(showlist):
    return (showlist, {})


def test_exact_title_matches(tracker_list):
    assert utils.guess_show('Cowboy Bebop', tracker_list)['title'] == 'Cowboy Bebop'


def test_alias_matches(tracker_list):
    assert utils.guess_show('Ginga Eiyuu Densetsu', tracker_list)['id'] == 8


def test_altnames_map_short_circuits(showlist):
    # An altname wins outright, even pointing somewhere fuzzy matching wouldn't.
    tracker_list = (showlist, {'bebop': 5})
    assert utils.guess_show('Bebop', tracker_list)['id'] == 5


def test_unrelated_title_is_rejected(tracker_list):
    assert utils.guess_show('Sesame Street', tracker_list) is None


def test_empty_list_is_rejected():
    assert utils.guess_show('Cowboy Bebop', ({}, {})) is None


def test_near_miss_distinguishes_sequels(tracker_list):
    # Titles that differ only by a suffix are the case most at risk from a
    # cheaper metric -- the two Steins;Gate entries must not be confused.
    assert utils.guess_show('Steins Gate 0', tracker_list)['id'] == 2
    assert utils.guess_show('Steins;Gate', tracker_list)['id'] == 1


def test_parser_added_season_suffix_falls_back_to_the_bare_title(tracker_list):
    # Every parser wrapper blindly appends " Season N" for season > 1 (see
    # _PARSER_SUFFIX_RE). A franchise with one continuous tracker entry
    # (no "Season" in its own title) must still match once that decoration
    # is stripped as a fallback.
    assert utils.guess_show('Cowboy Bebop Season 2', tracker_list)['id'] == 0


def test_parser_added_year_and_type_suffix_falls_back(tracker_list):
    assert utils.guess_show('Ping Pong the Animation OVA (2014)', tracker_list)['id'] == 9


def test_suffix_fallback_never_overrides_a_real_direct_match(tracker_list):
    # A tracker title that legitimately contains "Season" text of its own
    # must keep matching on the unstripped title -- the fallback only
    # fires after the direct match has already failed.
    assert utils.guess_show(
        'Monogatari Series: Second Season', tracker_list)['id'] == 6


def test_suffix_fallback_does_not_rescue_an_unrelated_title(tracker_list):
    assert utils.guess_show('Sesame Street Season 2', tracker_list) is None


def _perturb(title, rng):
    chars = list(title)
    for _ in range(rng.randint(1, max(2, len(chars) // 4))):
        if not chars:
            break
        i = rng.randrange(len(chars))
        roll = rng.random()
        if roll < 0.4:
            chars.pop(i)
        elif roll < 0.7:
            chars.insert(i, rng.choice('abcdefgh ivx0123456789'))
        else:
            chars[i] = rng.choice('abcdefgh ivx0123456789')
    return ''.join(chars)


@pytest.mark.parametrize('seed', range(8))
def test_difflib_path_matches_the_naive_sweep(showlist, seed):
    """The pruning must be pure speed -- never a different answer."""
    rng = random.Random(seed)
    flat = [t for entry in TITLES for t in entry]
    for _ in range(60):
        query = _perturb(rng.choice(flat), rng)
        got = utils._guess_show_difflib(query, showlist)
        expected = naive(query, showlist)
        got = got[0] if got[1] > utils.GUESS_SHOW_CUTOFF else None
        assert got is expected, query


def test_title_index_is_rebuilt_when_the_list_changes(showlist):
    pytest.importorskip('rapidfuzz')

    assert utils.guess_show('Cowboy Bebop', (showlist, {}))['id'] == 0

    # A fresh list object with different contents must not reuse the index.
    other = {0: {'id': 99, 'title': 'Ping Pong the Animation',
                 'titles': ['Ping Pong the Animation'], 'my_progress': 0,
                 'total': 0, 'type': None}}
    assert utils.guess_show('Cowboy Bebop', (other, {})) is None
    assert utils.guess_show('Ping Pong the Animation', (other, {}))['id'] == 99


# --- the suffix-strip margin ------------------------------------------------
#
# Parser wrappers append " Season N"/type/"(YYYY)" to a title. Scoring only the
# decorated form lets that decoration carry a WRONG show over the cutoff;
# preferring the stripped form whenever it merely scores higher then breaks
# franchises the user tracks per season. Both directions are pinned here.

# Titles/aliases copied verbatim from a real AniList list, because the exact
# ratios are the whole point -- including the trailing space AniList actually
# stores on "Initial D Season 2 ".
_SUFFIX_LIST = {
    0: {'id': 0, 'title': 'NARUTO -ナルト-', 'titles': ['NARUTO -ナルト-', 'NARUTO'],
        'my_progress': 0, 'total': 0, 'type': None},
    1: {'id': 1, 'title': 'Fate/Zero 2ndシーズン',
        'titles': ['Fate/Zero 2ndシーズン', 'Fate/Zero Season 2'],
        'my_progress': 0, 'total': 0, 'type': None},
    2: {'id': 2, 'title': '頭文字[イニシャル]D',
        'titles': ['頭文字[イニシャル]D', 'Initial D'],
        'my_progress': 0, 'total': 0, 'type': None},
    3: {'id': 3, 'title': '頭文字[イニシャル]D SECOND STAGE',
        'titles': ['頭文字[イニシャル]D SECOND STAGE', 'Initial D Season 2 '],
        'my_progress': 0, 'total': 0, 'type': None},
}


def test_decoration_alone_cannot_carry_an_unrelated_show():
    """"Naruto Season 02" must not land on "Fate/Zero 2nd Season".

    The decorated title scores 0.71 against Fate/Zero's "Fate/Zero Season 2"
    alias purely on the shared season words -- over the cutoff, so a
    fallback-only strip never runs. Stripped, it scores 1.00 against NARUTO:
    a 0.29 margin, which has to be allowed to win.
    """
    got = utils.guess_show('Naruto Season 02', (_SUFFIX_LIST, {}))
    assert got is not None and got['id'] == 0


def test_a_season_entry_is_not_collapsed_onto_its_franchise():
    """"Initial D Season 2" must stay on Second Stage, not fall back to S1.

    Stripped scores 1.00 against plain "Initial D"; decorated scores 0.97
    against Second Stage's "Initial D Season 2 " alias. A bare "higher ratio
    wins" rule takes the 0.03 improvement and lands on the wrong season. The
    margin is what prevents it.
    """
    got = utils.guess_show('Initial D Season 2', (_SUFFIX_LIST, {}))
    assert got is not None and got['id'] == 3


def test_strip_is_skipped_on_an_exact_match():
    got = utils.guess_show('Naruto', (_SUFFIX_LIST, {}))
    assert got is not None and got['id'] == 0
