"""GTK NowPlayingView: the "nothing playing" state survives a container
show_all(), no display needed.

Regression coverage for a bug where the "Watch a Random Episode" button
became unreachable: NowPlayingView.__init__ ends by hiding details_box
(nothing is playing yet), but the window that embeds it calls
show_all() on its whole widget tree once at startup -- which silently
un-hides any child that isn't itself marked no_show_all, undoing that
hide. With both details_box and empty_box visible at once, competing
for space in an unscrolled box, the random-episode button ends up
pushed out of the window's viewable area.
"""

import time

import pytest

gi = pytest.importorskip('gi')
try:
    gi.require_version('Gtk', '3.0')
    from gi.repository import GLib, Gtk
    from hakubun import utils
    from hakubun.ui.gtk.nowplayingview import NowPlayingView
except Exception:  # GTK present but unusable
    pytest.skip('GTK3 not usable', allow_module_level=True)


class _FakeEngine:
    api_info = {'shortname': 'test', 'mediatype': 'anime', 'name': 'Test API'}

    def get_show_info(self, show_id):
        return {'id': show_id, 'title': 'Test Show', 'url': 'http://example.com',
                 'image': None, 'image_thumb': None}

    def get_show_details(self, show):
        return {'title': show['title'],
                'extra': [('Synopsis', 'A test synopsis.')]}


def _pump(timeout=3):
    ctx = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ctx.pending():
            break
        ctx.iteration(False)


def test_details_box_stays_hidden_through_ancestor_show_all():
    view = NowPlayingView()
    assert view.details_box.get_visible() is False

    container = Gtk.Box()
    container.pack_start(view, True, True, 0)
    container.show_all()

    assert view.details_box.get_visible() is False
    assert view.empty_box.get_visible() is True


def test_details_box_becomes_visible_once_something_is_recognized_playing():
    # Regression test: details_box carries no_show_all (see above) so
    # that an ancestor's startup show_all() can't un-hide it -- but that
    # same no_show_all also makes GTK silently ignore a *direct*
    # show_all() call on the widget itself, which is what
    # _show_details_state() used to call. The net effect was that the
    # facts/synopsis block never appeared no matter what was playing.
    view = NowPlayingView(_FakeEngine())
    status = {
        'state': utils.Tracker.PLAYING,
        'show': ({'id': 1, 'title': 'Test Show'}, 3),
        'viewOffset': 100,
        'length': 1400,
    }

    view.update_status(status)
    assert view.details_box.get_visible() is True
    assert view.empty_box.get_visible() is False

    _pump()
    assert 'A test synopsis.' in view.details_box.data_label.get_text()
