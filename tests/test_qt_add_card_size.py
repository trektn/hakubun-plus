"""AddListDelegate cards must leave room for the synopsis.

paint() draws the synopsis last, into whatever vertical space the fact
rows above it didn't use, so the card's height is the only thing keeping
it on screen. On the Seasons page every optional row (Score, Popularity,
Airs, In List) is populated, which is where the old fixed height ran out
and the synopsis got cut off.
"""
import pytest

from PyQt6 import QtCore

from hakubun.ui.qt import delegates
from hakubun.ui.qt.delegates import (FIXED_ROWS, MARGIN, OPTIONAL_ROWS,
                                     PADDING, SYNOPSIS_LINES, AddListDelegate)


@pytest.fixture(scope='module')
def qt_app():
    from PyQt6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _synopsis_height(delegate):
    """Replays paint()'s vertical walk down a fully-populated card and
    returns what's left for the synopsis at the bottom."""
    fh = delegate.fh
    card = delegate.sizeHint(None, None).height()
    base = card - 2 * (MARGIN + PADDING)
    # Title band, then one line per fact row.
    used = (fh + 10) + (FIXED_ROWS + OPTIONAL_ROWS) * (fh + 5)
    return base - used


def test_fully_populated_card_still_fits_the_synopsis(qt_app):
    delegate = AddListDelegate()
    assert _synopsis_height(delegate) >= SYNOPSIS_LINES * delegate.fh


def test_size_hint_is_uniform_across_entries(qt_app):
    """AddCardView sets uniformItemSizes, so Qt asks the delegate once
    and reuses the answer -- a height that varied per entry would clip
    every card taller than whichever one happened to be measured."""

    class FakeIndex:
        def __init__(self, show):
            self._show = show

        def data(self, role=None):
            return self._show

    delegate = AddListDelegate(
        mylist={1: {'my_status': 1}}, statuses_dict={1: 'Watching'})
    sparse = delegate.sizeHint(None, FakeIndex({'id': 2, 'title': 'Bare'}))
    rich = delegate.sizeHint(None, FakeIndex(
        {'id': 1, 'title': 'Everything', 'platform_score': '8.4',
         'popularity_label': '#12', 'airing_time': 'Sundays'}))
    assert sparse == rich
    assert isinstance(sparse, QtCore.QSize)


def test_card_is_at_least_the_declared_minimum(qt_app):
    assert AddListDelegate().sizeHint(None, None).height() >= delegates.MIN_HEIGHT
