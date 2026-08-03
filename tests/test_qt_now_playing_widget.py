"""Qt NowPlayingWidget: the poster shown above the title/progress bar,
no display needed.

Regression coverage for two things:
- A poster wasn't shown at all until a show's details finished loading
  (DetailsWidget's own show_image is hidden until self.details itself
  is shown, i.e. never in the empty state). image_label needs to stay
  visible unconditionally, above the title -- same position GTK's
  NowPlayingView shows its own image_box in.
- image_label *is* self.details.show_image, reparented rather than
  loaded a second time (two ImageWorker threads would otherwise race
  to download and write the same cache file for the same show). It
  must end up removed from details' own top_row, not just reparented,
  or that layout keeps a stale, dangling item for it.
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


def test_poster_is_reused_detailswidget_image_shown_above_title(qapp):
    widget = NowPlayingWidget(None, _FakeWorker())

    assert widget.image_label is widget.details.show_image
    assert widget.details.top_row.indexOf(widget.image_label) == -1

    top_level_widgets = [
        widget.layout().itemAt(i).widget()
        for i in range(widget.layout().count())
    ]
    assert top_level_widgets.index(widget.title_label) > 0, (
        'poster must come before the title in the top-level layout')


def test_poster_visible_in_empty_and_recognized_states(qapp):
    widget = NowPlayingWidget(None, _FakeWorker())
    widget.show()

    assert widget.image_label.isHidden() is False

    widget.update_status({
        'state': utils.Tracker.PLAYING,
        'show': ({'id': 1, 'title': 'Test Show', 'url': 'http://x',
                  'image': None}, 3),
        'viewOffset': 100, 'length': 200,
    })

    assert widget.image_label.isHidden() is False
