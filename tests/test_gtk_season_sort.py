"""GTK list Season-column sort, wired through the real widget stack
(no display needed).

Regression coverage for a bug where the Season column sorted its
display text ('Summer 2026') as plain strings, comparing the season
name before the year. The sort func has to be installed on the
Gtk.TreeModelSort that MainView puts in front of the ShowListStore --
TreeModelSort implements GtkTreeSortable independently of its child
model, so a sort func on the store alone is silently never consulted.
"""

import pytest

gi = pytest.importorskip('gi')
try:
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
    from hakubun.ui.gtk.showtreeview import ShowListStore, sort_by_season
except Exception:  # GTK present but unusable
    pytest.skip('GTK3 not usable', allow_module_level=True)

from hakubun import utils


def _show(id_, extra_season):
    return {
        'id': id_, 'title': 'show-%d' % id_, 'my_progress': 0, 'my_score': 0,
        'total': 12, 'status': utils.Status.FINISHED, 'type': 'TV',
        'start_date': None, 'end_date': None,
        'my_start_date': None, 'my_finish_date': None, 'my_status': None,
        'platform_score': None, 'mal_score': None,
        'extra': [('Season', extra_season)],
    }


def _titles_in_sort_order(model):
    return [row[ShowListStore.column('title')] for row in model]


def test_year_dominates_over_season_name_through_treemodelsort():
    store = ShowListStore()
    store.append(_show(1, 'Fall 2011'))
    store.append(_show(2, 'Spring 2006'))

    sorted_model = Gtk.TreeModelSort(model=store)
    sorted_model.set_sort_func(ShowListStore.column('season'), sort_by_season)
    sorted_model.set_sort_column_id(
        ShowListStore.column('season'), Gtk.SortType.ASCENDING)

    assert _titles_in_sort_order(sorted_model) == ['show-2', 'show-1']


def test_season_name_breaks_ties_within_a_year():
    """Within a year, Winter sorts before Summer chronologically."""
    store = ShowListStore()
    store.append(_show(1, 'Winter 2026'))
    store.append(_show(2, 'Summer 2026'))

    sorted_model = Gtk.TreeModelSort(model=store)
    sorted_model.set_sort_func(ShowListStore.column('season'), sort_by_season)
    sorted_model.set_sort_column_id(
        ShowListStore.column('season'), Gtk.SortType.ASCENDING)

    assert _titles_in_sort_order(sorted_model) == ['show-1', 'show-2']
