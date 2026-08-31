"""Unit tests for the reconciliation strategy interface: each strategy
returns Resolved / Conflict / NoChange over participant values, using
base history (context.changed) only as data."""

from hakubun.sync.normalize import values_equal
from hakubun.sync.strategies import (Conflict, NoChange, ReconcileContext,
                                     Resolved, STRATEGIES, get_strategy)


def ctx(field, changed=None):
    return ReconcileContext(field=field, changed=changed or {},
                            equal=lambda a, b: values_equal(field, a, b))


def test_registry_and_fallback():
    assert set(STRATEGIES) == {'manual', 'union', 'max', 'min', 'progress'}
    assert get_strategy('nonsense') is STRATEGIES['manual']
    assert get_strategy(None) is STRATEGIES['manual']


# -- manual -----------------------------------------------------------

def test_manual_agreement_is_no_change():
    r = STRATEGIES['manual'].reconcile(
        'score', {'local': 8.0, 'mal': 8.0}, ctx('score'))
    assert isinstance(r, NoChange)


def test_manual_single_sided_change_resolves():
    r = STRATEGIES['manual'].reconcile(
        'score', {'local': 8.0, 'mal': 8.0, 'kitsu': 9.0},
        ctx('score', {'mal': False, 'kitsu': True, 'local': False}))
    assert isinstance(r, Resolved) and r.value == 9.0
    assert 'Kitsu' in r.reason


def test_manual_local_edit_resolves():
    r = STRATEGIES['manual'].reconcile(
        'score', {'local': 7.0, 'mal': 8.0},
        ctx('score', {'mal': False, 'local': True}))
    assert isinstance(r, Resolved) and r.value == 7.0


def test_manual_two_sided_change_conflicts():
    r = STRATEGIES['manual'].reconcile(
        'score', {'local': 7.0, 'mal': 8.0, 'kitsu': 9.0},
        ctx('score', {'mal': True, 'kitsu': True, 'local': False}))
    assert isinstance(r, Conflict)


def test_manual_no_history_conflicts():
    # First sync with disagreeing sides: no base to attribute the
    # difference, so a human decides -- nothing silently overwrites.
    r = STRATEGIES['manual'].reconcile(
        'score', {'local': 7.0, 'mal': 8.0},
        ctx('score', {'mal': None, 'local': None}))
    assert isinstance(r, Conflict)


# -- union ------------------------------------------------------------

def test_union_of_lists():
    r = STRATEGIES['union'].reconcile(
        'tags', {'local': ['a'], 'anilist': ['b', 'a']}, ctx('tags'))
    assert isinstance(r, Resolved) and r.value == ['a', 'b']


def test_union_agreement_is_no_change():
    r = STRATEGIES['union'].reconcile(
        'tags', {'local': ['a', 'b'], 'anilist': ['b', 'a']}, ctx('tags'))
    assert isinstance(r, NoChange)


def test_union_of_booleans_is_or():
    r = STRATEGIES['union'].reconcile(
        'favorite', {'local': False, 'anilist': True}, ctx('favorite'))
    assert isinstance(r, Resolved) and r.value is True


# -- max / min --------------------------------------------------------

def test_max_picks_highest_and_ignores_empties():
    r = STRATEGIES['max'].reconcile(
        'rewatches', {'local': 2, 'mal': None, 'kitsu': 3},
        ctx('rewatches'))
    assert isinstance(r, Resolved) and r.value == 3


def test_min_picks_lowest():
    r = STRATEGIES['min'].reconcile(
        'start_date', {'local': '2024-05-01', 'mal': '2024-04-01'},
        ctx('start_date'))
    assert isinstance(r, Resolved) and r.value == '2024-04-01'


def test_extremum_all_empty_is_no_change():
    r = STRATEGIES['max'].reconcile(
        'rewatches', {'local': None, 'mal': 0}, ctx('rewatches'))
    assert isinstance(r, NoChange)


# -- progress ---------------------------------------------------------

def test_progress_single_sided_regression_propagates():
    # A deliberate rewind on one side (rewatch reset) is honoured.
    r = STRATEGIES['progress'].reconcile(
        'progress', {'local': 3, 'mal': 12, 'kitsu': 12},
        ctx('progress', {'mal': False, 'kitsu': False, 'local': True}))
    assert isinstance(r, Resolved) and r.value == 3


def test_progress_multi_sided_takes_furthest():
    r = STRATEGIES['progress'].reconcile(
        'progress', {'local': 10, 'mal': 11, 'kitsu': 12},
        ctx('progress', {'mal': True, 'kitsu': True, 'local': False}))
    assert isinstance(r, Resolved) and r.value == 12


def test_progress_no_history_takes_furthest():
    # First sync: watching is monotone, so the highest value is safe --
    # no conflict spam for the most common field.
    r = STRATEGIES['progress'].reconcile(
        'progress', {'local': 4, 'mal': 7},
        ctx('progress', {'mal': None, 'local': None}))
    assert isinstance(r, Resolved) and r.value == 7
