"""Qt PlaybackBar (the Now Playing sidebar's progress bar): the bar and
percent label must reset to empty whenever there's no position data to
show, instead of freezing at whatever they last displayed.

Regression coverage for a bug where update_status() only ever wrote to
self.bar/self.percent_label inside the "playing and we have a real
length/offset" branch, with no else clause. Trackers that never report
a position at all (inotify/polling -- confirmed in tracker.py, neither
sets self.view_offset/self.length, which default to None) left the bar
permanently empty even while a recognized show was actively playing;
worse, if position data was available and then became unavailable again
(e.g. a transient MPRIS position-read failure), the bar kept showing the
stale percentage from the last time it had real data.

Also covers a related case: some MPRIS players never expose mpris:length
in their metadata at all, even though position updates normally --
elapsed time is known, but there's no total duration to compute a
percentage against. The bar switches to Qt's indeterminate/"busy" mode
(range 0-0) instead of going blank, since format_playback_position (the
separate position label next to it) already shows the ticking elapsed
time in this exact case -- a blank bar beside a visibly-updating time
label reads as broken.
"""

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6.QtWidgets import QApplication

from hakubun import utils
from hakubun.ui.qt.widgets import PlaybackBar


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def _status(state, offset=None, length=None):
    return {
        'state': state,
        'show': ({'id': 1, 'title': 'Test Show'}, 1),
        'viewOffset': offset,
        'length': length,
    }


def test_playing_without_position_data_leaves_bar_empty(qapp):
    """inotify/polling trackers never set view_offset/length -- this
    must not look any different from "nothing playing" on the bar."""
    bar = PlaybackBar()
    bar.update_status(_status(utils.Tracker.PLAYING))
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 100)
    assert bar.bar.value() == 0
    assert bar.percent_label.text() == ''


def test_playing_with_position_data_shows_percent(qapp):
    bar = PlaybackBar()
    bar.update_status(_status(utils.Tracker.PLAYING, offset=500000, length=1000000))
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 100)
    assert bar.bar.value() == 50
    assert bar.percent_label.text() == '50%'


def test_playing_with_position_but_no_length_shows_indeterminate_bar(qapp):
    bar = PlaybackBar()
    bar.update_status(_status(utils.Tracker.PLAYING, offset=300000))
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 0)
    assert bar.percent_label.text() == ''


def test_length_arriving_later_switches_back_to_determinate(qapp):
    bar = PlaybackBar()
    bar.update_status(_status(utils.Tracker.PLAYING, offset=300000))
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 0)

    bar.update_status(_status(utils.Tracker.PLAYING, offset=500000, length=1000000))
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 100)
    assert bar.bar.value() == 50


def test_losing_position_data_resets_the_bar_instead_of_freezing(qapp):
    bar = PlaybackBar()
    bar.update_status(_status(utils.Tracker.PLAYING, offset=500000, length=1000000))
    assert bar.bar.value() == 50

    bar.update_status(_status(utils.Tracker.PLAYING))
    assert bar.bar.value() == 0
    assert bar.percent_label.text() == ''


def test_no_longer_playing_resets_the_bar(qapp):
    bar = PlaybackBar()
    bar.update_status(_status(utils.Tracker.PLAYING, offset=500000, length=1000000))
    assert bar.bar.value() == 50

    bar.update_status(_status(utils.Tracker.NOVIDEO))
    assert bar.bar.value() == 0
    assert bar.percent_label.text() == ''


def test_stopping_from_indeterminate_state_restores_the_normal_range(qapp):
    bar = PlaybackBar()
    bar.update_status(_status(utils.Tracker.PLAYING, offset=300000))
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 0)

    bar.update_status(_status(utils.Tracker.NOVIDEO))
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 100)
    assert bar.bar.value() == 0


def test_clear_resets_the_bar(qapp):
    bar = PlaybackBar()
    bar.update_status(_status(utils.Tracker.PLAYING, offset=500000, length=1000000))
    assert bar.bar.value() == 50

    bar.clear()
    assert bar.bar.value() == 0
    assert bar.percent_label.text() == ''
