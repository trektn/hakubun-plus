"""Qt NowPlayingWidget: the poster shown above the progress bar/details
area, no display needed.

Regression coverage for an added feature -- a poster wasn't shown at
all until a show's details finished loading (self.details.show_image,
hidden while the empty state is active). self.image_label is an
independent widget shown unconditionally, so it needs to be visible in
both states, and self.details.show_image needs to stay hidden so the
poster isn't shown twice.
"""

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6.QtWidgets import QApplication

from hakubun import utils
from hakubun.ui.qt.nowplaying import NowPlayingWidget


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeEngine:
    api_info = {'shortname': 'test', 'mediatype': 'anime'}

    def get_show_info(self, showid):
        raise utils.HakubunError('not found')


class _FakeWorker:
    engine = _FakeEngine()

    def set_function(self, *args, **kwargs):
        pass


def test_poster_visible_in_empty_and_recognized_states(qapp):
    widget = NowPlayingWidget(None, _FakeWorker())
    widget.show()

    assert widget.image_label.isHidden() is False
    # The reused DetailsWidget's own poster would otherwise duplicate
    # image_label's -- must stay hidden in both states.
    assert widget.details.show_image.isHidden() is True

    widget.update_status({
        'state': utils.Tracker.PLAYING,
        'show': ({'id': 1, 'title': 'Test Show', 'url': 'http://x',
                  'image': None}, 3),
        'viewOffset': 100, 'length': 200,
    })

    assert widget.image_label.isHidden() is False
    assert widget.details.show_image.isHidden() is True
