"""utils.season_sort_key: the Season column's sort key.

Regression coverage for a bug where the GTK/Qt list views sorted the
Season column as plain text ('Summer 2026'), which compares the season
name before the year -- e.g. 'Fall 2011' sorted before 'Spring 2006'
even though 2006 is the earlier year.
"""

from hakubun import utils


def test_year_dominates_over_season_name():
    key_spring_2006 = utils.season_sort_key('Spring 2006')
    key_fall_2011 = utils.season_sort_key('Fall 2011')
    assert key_spring_2006 < key_fall_2011


def test_season_name_breaks_ties_within_a_year():
    """Within a year, seasons order chronologically (Winter, Spring,
    Summer, Fall) -- not alphabetically."""
    key_winter_2026 = utils.season_sort_key('Winter 2026')
    key_spring_2026 = utils.season_sort_key('Spring 2026')
    key_summer_2026 = utils.season_sort_key('Summer 2026')
    key_fall_2026 = utils.season_sort_key('Fall 2026')
    assert key_winter_2026 < key_spring_2026 < key_summer_2026 < key_fall_2026


def test_year_only_label():
    assert utils.season_sort_key('2020') == (2020, -1)


def test_unknown_label_sorts_last():
    known = utils.season_sort_key('Winter 2009')
    unknown = utils.season_sort_key('?')
    assert known < unknown
