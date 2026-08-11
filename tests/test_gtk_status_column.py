import gi

gi.require_version('Gtk', '3.0')

from hakubun import utils
from hakubun.ui.gtk.showtreeview import ShowListStore, status_label


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


def test_airing_status_column_sorts_on_the_status_enum_not_the_label():
    """Sorting on the label text (26) would order the list
    alphabetically; sorting on utils.Status (16) groups it by where each
    show is in its run. available_columns is built per-instance inside
    __init__, so this reads it there rather than constructing a widget."""
    import inspect

    from hakubun.ui.gtk.showtreeview import ShowTreeView

    assert "('Airing Status', 16)" in inspect.getsource(ShowTreeView.__init__)
