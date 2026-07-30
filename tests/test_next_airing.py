"""The details view resolves a show's next episode from whichever of
three sources is cheapest, and must not hit the network more than it has
to -- or fail a details load when that network call goes wrong."""
import datetime

import pytest

from hakubun import utils
from hakubun.engine import Engine


AIRING_AT = 1785000000  # some fixed instant, as AniList would send it


class FakeEngine(Engine):
    """Engine with just enough state for get_next_airing, built without
    touching config/data files."""

    def __init__(self, shortname='anilist', response=None, error=None):
        self.api_info = {'shortname': shortname}
        self._next_airing_cache = {}
        self.msg = _SilentMessenger()
        self._response = response
        self._error = error
        self.queries = 0

    def _anilist_public_query(self, query, variables):
        self.queries += 1
        if self._error:
            raise self._error
        return self._response


class _SilentMessenger:
    def debug(self, *args, **kwargs):
        pass


def media(next_ep):
    return {'data': {'Media': {'nextAiringEpisode': next_ep}}}


def airing_show(**extra):
    show = {'id': 1, 'mal_id': 999, 'status': utils.Status.AIRING}
    show.update(extra)
    return show


def test_a_show_that_is_not_airing_is_never_looked_up():
    engine = FakeEngine(response=media({'airingAt': AIRING_AT, 'episode': 5}))
    show = airing_show(status=utils.Status.FINISHED)
    assert engine.get_next_airing(show) is None
    assert engine.queries == 0


def test_the_show_dict_wins_over_the_network():
    # AniList accounts get next_ep_time with the list itself, for free.
    when = datetime.datetime(2026, 8, 2, 12, 0)
    engine = FakeEngine(response=media({'airingAt': AIRING_AT, 'episode': 5}))
    show = airing_show(next_ep_time=when, next_ep_number=7)

    (episode, airing_at) = engine.get_next_airing(show)

    assert engine.queries == 0
    assert episode == 7
    # Normalized to aware UTC on the way out, since libanilist's is naive.
    assert airing_at == when.replace(tzinfo=datetime.timezone.utc)


def test_a_show_with_no_resolvable_mal_id_is_not_looked_up():
    engine = FakeEngine(response=media({'airingAt': AIRING_AT, 'episode': 5}))
    assert engine.get_next_airing(airing_show(mal_id=None)) is None
    assert engine.queries == 0


def test_a_mal_account_uses_the_shows_own_id():
    # 'mal_id' is only ever set by the cross-reference for OTHER
    # backends; on MAL, 'id' already is the MAL id.
    engine = FakeEngine(shortname='mal',
                        response=media({'airingAt': AIRING_AT, 'episode': 5}))
    show = {'id': 4224, 'status': utils.Status.AIRING}

    assert engine.get_next_airing(show)[0] == 5
    assert 4224 in engine._next_airing_cache


def test_a_looked_up_answer_is_cached_rather_than_re_fetched():
    engine = FakeEngine(response=media({'airingAt': AIRING_AT, 'episode': 5}))
    show = airing_show()

    first = engine.get_next_airing(show)
    second = engine.get_next_airing(show)

    assert first == second
    assert engine.queries == 1


def test_a_no_schedule_answer_is_cached_too():
    # Otherwise every details open re-asks AniList about a show it has
    # already said it knows nothing about.
    engine = FakeEngine(response=media(None))
    show = airing_show()

    assert engine.get_next_airing(show) is None
    assert engine.get_next_airing(show) is None
    assert engine.queries == 1


def test_a_stale_cache_entry_is_re_fetched():
    engine = FakeEngine(response=media({'airingAt': AIRING_AT, 'episode': 5}))
    show = airing_show()
    engine.get_next_airing(show)

    (fetched_at, episode, at) = engine._next_airing_cache[999]
    engine._next_airing_cache[999] = (
        fetched_at - Engine.NEXT_AIRING_CACHE_SECONDS - 1, episode, at)

    engine.get_next_airing(show)
    assert engine.queries == 2


def test_a_failed_lookup_does_not_break_the_details_load():
    engine = FakeEngine(error=utils.EngineError('AniList is down'))
    assert engine.get_next_airing(airing_show()) is None


def test_a_malformed_response_does_not_raise():
    engine = FakeEngine(response={'data': None})
    assert engine.get_next_airing(airing_show()) is None


def test_the_returned_time_is_aware_utc():
    engine = FakeEngine(response=media({'airingAt': AIRING_AT, 'episode': 5}))
    (_episode, airing_at) = engine.get_next_airing(airing_show())
    assert airing_at == datetime.datetime.fromtimestamp(
        AIRING_AT, tz=datetime.timezone.utc)
