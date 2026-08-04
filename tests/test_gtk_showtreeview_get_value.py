import gi

gi.require_version('Gtk', '3.0')
from gi.repository import GObject, Gtk

import pytest

from hakubun.ui.gtk.showtreeview import ShowListFilter, ShowListStore

_DEFAULTS = {int: 0, float: 0.0, str: '', GObject.TYPE_PYOBJECT: []}


def _store_with_one_row():
    """A ShowListStore with one row, built via the raw Gtk.ListStore API
    (bypassing ShowListStore.append(), which expects a full engine 'show'
    dict) so this test only depends on the column layout, not on engine
    internals."""
    store = ShowListStore()
    cols = list(store._ShowListStore__cols)
    row = [_DEFAULTS[col_type] for _, col_type in cols]
    row[[k for k, _ in cols].index('id')] = 1
    row[[k for k, _ in cols].index('title')] = 'Example Show'
    Gtk.ListStore.append(store, row)
    return store


def test_column_returns_index_for_known_key():
    assert ShowListStore.column('title') == 1


def test_column_returns_none_for_unknown_key():
    """Regression: column() built its "not found" case on next() raising
    StopIteration, but only caught ValueError -- so an unknown key raised
    an uncaught StopIteration instead of returning None as documented."""
    assert ShowListStore.column('no_such_column') is None


def test_get_value_returns_correct_value_for_known_key():
    store = _store_with_one_row()
    tree_filter = ShowListFilter(child_model=store)
    path = Gtk.TreePath.new_from_string('0')
    assert tree_filter.get_value(path, 'title') == 'Example Show'


def test_get_value_returns_none_for_unknown_key():
    store = _store_with_one_row()
    tree_filter = ShowListFilter(child_model=store)
    path = Gtk.TreePath.new_from_string('0')
    assert tree_filter.get_value(path, 'no_such_column') is None


def test_get_value_returns_none_for_stale_path():
    store = _store_with_one_row()
    tree_filter = ShowListFilter(child_model=store)
    stale_path = Gtk.TreePath.new_from_string('99')
    assert tree_filter.get_value(stale_path, 'title') is None


def test_get_value_propagates_unexpected_errors_instead_of_swallowing_them():
    """Regression: get_value() used to catch bare Exception, so a real bug
    (e.g. child_model raising something other than a stale-path/bad-key
    error) would silently come back as None instead of surfacing."""
    store = _store_with_one_row()
    tree_filter = ShowListFilter(child_model=store)
    path = Gtk.TreePath.new_from_string('0')

    class Boom(RuntimeError):
        pass

    def raise_boom(_key):
        raise Boom("not a stale-path/bad-key situation")

    tree_filter.props.child_model.column = raise_boom

    with pytest.raises(Boom):
        tree_filter.get_value(path, 'title')
