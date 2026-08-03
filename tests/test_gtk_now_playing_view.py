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

import pytest

gi = pytest.importorskip('gi')
try:
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
    from hakubun.ui.gtk.nowplayingview import NowPlayingView
except Exception:  # GTK present but unusable
    pytest.skip('GTK3 not usable', allow_module_level=True)


def test_details_box_stays_hidden_through_ancestor_show_all():
    view = NowPlayingView()
    assert view.details_box.get_visible() is False

    container = Gtk.Box()
    container.pack_start(view, True, True, 0)
    container.show_all()

    assert view.details_box.get_visible() is False
    assert view.empty_box.get_visible() is True
