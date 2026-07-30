"""The details view's next-episode row and the airing scheduler must
reduce a time the same way -- they share format_airtime_delta, and these
pin down the boundaries where the unit changes."""
import datetime

import pytest

from hakubun import utils


NOW = datetime.datetime(2026, 7, 30, 12, 0, tzinfo=datetime.timezone.utc)


def at(**kwargs):
    return NOW + datetime.timedelta(**kwargs)


@pytest.mark.parametrize('delta, expected', [
    ({'minutes': 1}, '1 minute'),
    ({'minutes': 12}, '12 minutes'),
    ({'minutes': 59}, '59 minutes'),
    ({'hours': 1}, '1 hour'),
    ({'hours': 5}, '5 hours'),
    ({'hours': 23}, '23 hours'),
    ({'days': 1}, '1 day'),
    ({'days': 3}, '3 days'),
])
def test_delta_reduces_to_the_largest_whole_unit(delta, expected):
    assert utils.format_airtime_delta(at(**delta), NOW) == expected


@pytest.mark.parametrize('delta', [{'seconds': 0}, {'minutes': -5},
                                   {'days': -2}])
def test_delta_has_no_answer_once_it_is_not_in_the_future(delta):
    assert utils.format_airtime_delta(at(**delta), NOW) is None


def test_a_few_seconds_out_still_rounds_up_to_a_minute():
    # Never "0 minutes": the floor is the smallest unit we name.
    assert utils.format_airtime_delta(at(seconds=5), NOW) == '1 minute'


def test_relative_airtime_is_the_delta_with_a_prefix():
    # The scheduler's phrasing and the details row's must not drift
    # apart -- they're the same reduction underneath.
    for delta in ({'minutes': 12}, {'hours': 5}, {'days': 3}):
        assert utils.format_relative_airtime(at(**delta), NOW) \
            == 'In %s' % utils.format_airtime_delta(at(**delta), NOW)


def test_relative_airtime_labels_the_non_future_cases():
    assert utils.format_relative_airtime(at(minutes=-5), NOW) == 'Airing now'
    assert utils.format_relative_airtime(at(hours=-5), NOW) == 'Aired'


def test_naive_datetimes_are_read_as_utc():
    # libanilist's 'next_ep_time' comes from utcfromtimestamp() and is
    # naive, while get_airing_schedule builds aware UTC. Subtracting one
    # from the other used to be a TypeError.
    naive = at(days=3).replace(tzinfo=None)
    assert utils.format_airtime_delta(naive, NOW) == '3 days'
    assert utils.format_relative_airtime(naive, NOW) == 'In 3 days'


def test_next_airing_names_the_quantity_and_anchors_it_to_a_date():
    text = utils.format_next_airing(at(days=3), NOW)
    assert text.startswith('3 days (')
    # The absolute part is local time, so assert on the date the given
    # instant actually falls on here rather than a hardcoded string.
    local = utils.as_utc(at(days=3)).astimezone()
    assert text == '3 days (%s)' % local.strftime('%a %b %d, %Y')


def test_next_airing_has_nothing_to_say_about_a_past_time():
    assert utils.format_next_airing(at(days=-1), NOW) is None
