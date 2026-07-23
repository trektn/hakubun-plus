"""GTK list overlay cell computation (pure; no display needed).

Skipped where GTK3 isn't installed. overlay_cells is what turns the
multi-sync overlay into the Score/Progress/date cells the GTK list
renders -- notably an owned Score shown in the owner's rating system.
"""

import pytest

gi = pytest.importorskip('gi')
try:
    gi.require_version('Gtk', '3.0')
    gi.require_version('Gdk', '3.0')
    from hakubun.ui.gtk.showtreeview import overlay_cells, _fmt_owner_score
except Exception:  # GTK present but unusable
    pytest.skip('GTK3 not usable', allow_module_level=True)


def _show(**kw):
    base = {'id': 1, 'title': 'Bebop', 'my_progress': 3, 'my_score': 0.0,
            'total': 26, 'my_start_date': None, 'my_finish_date': None}
    base.update(kw)
    return base


def test_fmt_owner_score_trims_zeros():
    assert _fmt_owner_score(8.4) == '8.4'
    assert _fmt_owner_score(8.0) == '8'
    assert _fmt_owner_score(8.5) == '8.5'
    assert _fmt_owner_score(84.0) == '84'


def test_no_overlay_uses_active_scale():
    # Kitsu displayed 0-10/.5: raw 4.25 * factor 2 = 8.50.
    c = overlay_cells(_show(my_score=4.25), None, decimals=2, factor=2)
    assert c['score_str'] == '8.50'
    assert c['score_italic'] is False and c['score_owner'] == ''


def test_owner_score_display_and_italic():
    over = {'my_score': 4.25, '_score_display': 8.4, '_score_owner': 'anilist'}
    c = overlay_cells(_show(my_score=4.25), over, decimals=2, factor=2)
    assert c['score_str'] == '8.4'          # owner's system, not Kitsu's 8.50
    assert c['score_italic'] is True
    assert c['score_owner'] == 'anilist'


def test_progress_overlay_feeds_episodes_and_percent():
    c = overlay_cells(_show(my_progress=3, total=26), {'my_progress': 13}, 0, 1)
    assert c['my_progress'] == 13
    assert c['episodes_str'] == '13 / 26'
    assert c['percent'] == 50.0


def test_unrated_shared_owner_context_has_owner_but_no_display():
    # Score owned elsewhere but unrated: owner name for the tooltip, but
    # nothing rated -> no italic, active-scale rendering of the empty score.
    over = {'_score_owner': 'anilist', '_score_owner_raw': 0}
    c = overlay_cells(_show(my_score=0.0), over, 0, 1)
    assert c['score_owner'] == 'anilist'
    assert c['score_italic'] is False
    assert c['score_str'] == '0'
