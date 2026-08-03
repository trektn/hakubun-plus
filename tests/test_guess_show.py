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
