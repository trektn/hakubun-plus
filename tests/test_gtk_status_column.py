import datetime

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')

from hakubun import utils
from hakubun.ui.gtk.showtreeview import ShowListStore, status_label


# _get_color() indexes these directly while building a row.
_COLORS = {'is_queued': '#000', 'new_episode': '#000',
           'is_airing': '#000', 'not_aired': '#000'}


def _show(**kw):
    """A show dict with everything ShowListStore.append()/update() read."""
    base = {'id': 1, 'title': 'Bebop', 'my_progress': 3, 'my_score': 0.0,
            'total': 26, 'my_start_date': None, 'my_finish_date': None,
            'my_status': 'watching', 'status': utils.Status.AIRING,
            'start_date': datetime.datetime(1998, 4, 3),
            'end_date': datetime.datetime(1999, 4, 24),
            'my_last_update': None, 'type': 'TV', 'extra': []}
    base.update(kw)
    return base


def test_labels_read_the_way_an_anime_list_talks():
    assert status_label(utils.Status.AIRING) == 'Airing'
    assert status_label(utils.Status.FINISHED) == 'Completed'
    assert status_label(utils.Status.NOTYET) == 'Upcoming'


def test_statuses_without_a_nicer_name_fall_back_to_their_own_value():
    """Cancelled/Other/Unknown have no anime-list-flavoured rename, but
    the cell must still say something rather than render blank."""
    assert status_label(utils.Status.CANCELLED) == 'Cancelled'
    assert status_label(utils.Status.UNKNOWN) == 'Unknown'


def test_unrecognized_status_renders_empty_rather_than_raising():
    assert status_label('nonsense') == ''


def test_status_text_is_the_last_column():
    """The store is indexed by hard-coded integers all over
    showtreeview.py, so a new cell has to be appended, never inserted."""
    cols = [key for key, _ in ShowListStore._ShowListStore__cols]
    assert cols[-1] == 'status-text'
    assert ShowListStore.column('status-text') == 26


def test_append_fills_the_status_cell():
    """Exercises the real append() path -- nothing else in the suite
    does, so this is the only cover on writing a utils.Status into the
    int column at 16 alongside its label at 26."""
    store = ShowListStore(colors=_COLORS)
    store.append(_show())
    row = store[0]
    assert row[26] == 'Airing'


def test_update_refreshes_a_status_that_changed_since_the_last_sync():
    """A show that finishes airing between two syncs goes through
    update(), not append() -- without refreshing 16 and 26 it would keep
    showing 'Airing' (and sorting as such) until a full repopulate."""
    store = ShowListStore(colors=_COLORS)
    store.append(_show())
    store.update(_show(status=utils.Status.FINISHED))
    row = store[0]
    assert row[26] == 'Completed'
    assert row[16] == int(utils.Status.FINISHED)


def test_airing_status_column_sorts_on_the_status_enum_not_the_label():
    """Sorting on the label text (26) would order the list
    alphabetically; sorting on utils.Status (16) groups it by where each
    show is in its run. available_columns is built per-instance inside
    __init__, so this reads it there rather than constructing a widget."""
    import inspect

    from hakubun.ui.gtk.showtreeview import ShowTreeView

    assert "('Airing Status', 16)" in inspect.getsource(ShowTreeView.__init__)
