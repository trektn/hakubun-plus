"""Annict models progress as a set of per-episode Record objects, not as
an episodes-watched counter, so libannict has to translate in both
directions. Getting that translation wrong is not a display bug -- it
writes records to the user's public activity feed -- so the rules are
pinned here.
"""
import pytest

from hakubun import utils
from hakubun.lib.libannict import libannict


class _SilentMessenger:
    def __init__(self):
        self.warnings = []

    def with_classname(self, classname):
        return self

    def debug(self, *msgs):
        pass

    def info(self, *msgs):
        pass

    def warn(self, *msgs):
        self.warnings.append(' '.join(str(m) for m in msgs))


@pytest.fixture
def api():
    messenger = _SilentMessenger()
    lib = libannict(messenger, {'username': 'u', 'password': 'token'},
                    {'mediatype': 'anime'})
    lib.msg = messenger
    lib.userid = 1
    # Covers are scraped from annict.com and cached on disk; neither
    # belongs in a unit test. Tests about covers set these themselves.
    lib._posters = {}
    lib._poster_url = lambda annict_id: None
    lib._save_poster_cache = lambda: None
    return lib


def episodes(tracked, count=8, numbers=None):
    """A work's episode list, with `tracked` (a set of episode numbers)
    already recorded."""
    return [
        {'annictId': 1000 + i,
         'id': 'Episode-%d' % (1000 + i),
         'number': (numbers[i - 1] if numbers else i),
         'sortNumber': i,
         'viewerDidTrack': i in tracked}
        for i in range(1, count + 1)
    ]


def work(**overrides):
    base = {
        'annictId': 42,
        'id': 'V29yay00Mg==',
        'title': 'テスト',
        'titleEn': 'Test',
        'titleKana': 'てすと',
        'titleRo': None,
        'malAnimeId': '999',
        'media': 'TV',
        'episodesCount': 8,
        'noEpisodes': False,
        'seasonYear': 2020,
        'seasonName': 'SPRING',
        'watchersCount': 1234,
        'satisfactionRate': 84.75,
        'officialSiteUrl': '',
        'wikipediaUrl': '',
        'image': {'recommendedImageUrl': 'https://example/i.png',
                  'facebookOgImageUrl': ''},
        'viewerStatusState': 'WATCHING',
        'episodes': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                     'nodes': episodes({1, 2, 3})},
    }
    base.update(overrides)
    return base


def entry(state='WATCHING', **work_overrides):
    return {'id': 'LibraryEntry-1', 'note': '',
            'status': {'state': state} if state else None,
            'nextEpisode': None,
            'work': work(**work_overrides)}


# --------------------------------------------------------------------
# Progress derivation
# --------------------------------------------------------------------

def test_progress_is_the_contiguous_prefix_not_the_maximum(api):
    """A sparse set is the whole reason this rule exists: reporting the
    highest tracked episode would make the next update_show() fill in
    every gap below it with brand new records."""
    assert api._derive_progress(episodes({1, 2, 3, 7}, count=8)) == 3


def test_progress_zero_when_nothing_tracked(api):
    assert api._derive_progress(episodes(set())) == 0


def test_progress_zero_when_first_episode_is_missing(api):
    assert api._derive_progress(episodes({2, 3, 4})) == 0


def test_progress_falls_back_to_position_when_number_is_null(api):
    eps = episodes({1, 2}, count=4, numbers=[None] * 4)
    assert api._derive_progress(eps) == 2


def test_progress_uses_the_episode_number_not_the_position(api):
    # Some works number their episodes from 0, or skip.
    eps = episodes({1, 2, 3}, count=4, numbers=[0, 1, 2, 3])
    assert api._derive_progress(eps) == 2


# --------------------------------------------------------------------
# List parsing
# --------------------------------------------------------------------

def test_parse_entry_maps_the_work(api):
    show = api._parse_entry(entry())

    assert show['id'] == 42
    assert show['title'] == 'テスト'
    assert show['aliases'] == ['Test', 'てすと']
    assert show['type'] == utils.Type.TV
    assert show['total'] == 8
    assert show['my_status'] == 'WATCHING'
    assert show['my_progress'] == 3
    assert show['mal_id'] == 999
    assert show['url'] == 'https://annict.com/works/42'
    assert show['platform_score'] == '85%'
    assert show['popularity'] == -1234


def test_parse_entry_has_no_mal_id_when_annict_has_none(api):
    # malAnimeId is a String and is "" -- not null -- when absent.
    assert api._parse_entry(entry(malAnimeId=''))['mal_id'] is None


def test_a_movie_counts_as_one_episode(api):
    """Annict registers no episodes for any movie, so there is nothing
    to record against -- but MAL/AniList/Kitsu all model a film as 1/1,
    and reporting 0/0 would push that zero out through multisync."""
    ep = entry(state='WATCHED', noEpisodes=True, episodesCount=0)
    ep['work']['episodes']['nodes'] = []
    show = api._parse_entry(ep)

    assert show['total'] == 1
    assert show['my_progress'] == 1


def test_an_unwatched_movie_reports_no_progress(api):
    ep = entry(state='WANNA_WATCH', noEpisodes=True, episodesCount=0)
    ep['work']['episodes']['nodes'] = []
    show = api._parse_entry(ep)

    assert (show['total'], show['my_progress']) == (1, 0)


def test_progress_on_a_movie_is_not_an_error(api):
    """There is no episode to create a record for; the status already
    carries it. Must not read as a failure the user should see."""
    api._parse_entry(entry(noEpisodes=True, episodesCount=0))
    api._episodes[42] = []
    api._request = recorder = _Recorder()

    api._sync_progress(42, 1)

    assert recorder.queries == []
    assert not api.msg.warnings


def test_missing_episodes_on_a_normal_work_is_still_reported(api):
    api._episodes[42] = []
    api._request = _Recorder()

    api._sync_progress(42, 1)

    assert api.msg.warnings


def test_completed_show_without_records_still_reports_full_progress(api):
    """Recording episodes is optional on Annict. A WATCHED show with no
    records at all must not read as 0/8, or multisync would push that
    zero to every other provider."""
    ep = entry(state='WATCHED')
    ep['work']['episodes']['nodes'] = episodes(set())

    assert api._parse_entry(ep)['my_progress'] == 8


def test_completed_show_keeps_a_higher_derived_progress(api):
    """The inference only ever raises. A work whose episodesCount lags
    the episodes it actually lists keeps the recorded number."""
    ep = entry(state='WATCHED', episodesCount=4)
    ep['work']['episodes']['nodes'] = episodes({1, 2, 3, 4, 5, 6}, count=8)

    assert api._parse_entry(ep)['my_progress'] == 6


def test_fetch_list_asks_for_every_status(api):
    """Clearing a status leaves a NO_STATE library entry behind, so the
    query must filter -- and the filter must not quietly drop the
    finished and dropped shows."""
    seen = {}

    def fake_request(query, variables=None):
        seen.update(variables)
        return {'viewer': {'libraryEntries': {
            'pageInfo': {'hasNextPage': False, 'endCursor': None},
            'nodes': [entry()]}}}

    api._request = fake_request
    showlist = api.fetch_list()

    assert set(seen['states']) == {'WATCHING', 'WATCHED', 'ON_HOLD',
                                   'STOP_WATCHING', 'WANNA_WATCH'}
    assert list(showlist) == [42]


def test_fetch_list_follows_pagination(api):
    pages = [
        {'viewer': {'libraryEntries': {
            'pageInfo': {'hasNextPage': True, 'endCursor': 'cur'},
            'nodes': [entry()]}}},
        {'viewer': {'libraryEntries': {
            'pageInfo': {'hasNextPage': False, 'endCursor': None},
            'nodes': [entry(annictId=43)]}}},
    ]
    calls = []

    def fake_request(query, variables=None):
        calls.append(variables['after'])
        return pages[len(calls) - 1]

    api._request = fake_request
    showlist = api.fetch_list()

    assert calls == [None, 'cur']
    assert sorted(showlist) == [42, 43]


def test_truncated_episode_list_is_paginated_not_silently_short(api):
    """The episodes connection is nested inside the work, so a long
    runner exceeds the page size and the derived progress would be
    quietly wrong rather than raise."""
    tail = episodes({1, 2, 3, 4}, count=4)
    for i, ep in enumerate(tail, start=9):
        ep['number'] = i
        ep['sortNumber'] = i

    api._request = lambda query, variables=None: {'node': {'episodes': {
        'pageInfo': {'hasNextPage': False, 'endCursor': None},
        'nodes': tail}}}

    node = entry()
    node['work']['episodesCount'] = 12
    node['work']['episodes']['nodes'] = episodes({1, 2, 3, 4, 5, 6, 7, 8})
    node['work']['episodes']['pageInfo'] = {
        'hasNextPage': True, 'endCursor': 'cur'}

    show = api._parse_entry(node)

    assert len(api._episodes[42]) == 12
    assert show['my_progress'] == 12


# --------------------------------------------------------------------
# Images
# --------------------------------------------------------------------

def test_social_media_images_are_never_used_as_cover_art(api):
    """Annict's recommendedImageUrl / facebookOgImageUrl /
    twitterAvatarUrl are picked by opening the anime's Facebook OG
    image, Twitter card and Twitter *avatar* and keeping whichever has
    the most pixels -- a studio banner or an account icon, not artwork.
    Annict itself never shows them; a work with no registered image gets
    a placeholder. So neither do we."""
    node = entry()
    node['work']['image'] = {
        'recommendedImageUrl': 'https://site/banner.jpg',
        'facebookOgImageUrl': 'https://site/og.jpg',
        'twitterAvatarUrl': 'https://twitter.com/x/profile_image',
    }

    assert api._parse_entry(node)['image'] == ''


def test_a_cached_cover_is_used_for_the_list(api):
    api._posters = {'42': 'https://image.annict.com/sig/s:640:853/plain/x.jpg'}
    show = api._parse_entry(entry())

    assert show['image'] == 'https://image.annict.com/sig/s:640:853/plain/x.jpg'
    assert show['image_thumb'] == show['image']


def test_a_work_known_to_have_no_cover_stays_blank(api):
    # Cached as None, not missing -- so it is not re-fetched every sync.
    api._posters = {'42': None}

    assert api._parse_entry(entry())['image'] == ''


def test_the_poster_is_picked_out_of_the_page(api):
    """The square images on a work page are commenters' avatars; the
    poster is the one fitted to a 3:4 box. Largest wins."""
    page = """
      <img src="https://image.annict.com/aaa/rs:fill-down:50:50/plain/s3://x/a.jpg">
      <img src="https://image.annict.com/bbb/s:170:226/plain/s3://x/w.jpg">
      <img src="https://image.annict.com/ccc/s:640:853/plain/s3://x/w.jpg">
    """
    matches = [(int(m.group(1)), m.group(0))
               for m in api._POSTER_RE.finditer(page)]
    portrait = [(w, u) for w, u in matches if 's:640:853' in u or 's:170:226' in u]

    assert not any('fill-down' in u for _, u in matches)
    assert max(portrait)[1].endswith('/s:640:853/plain/s3://x/w.jpg')


def test_details_upgrade_to_annicts_own_cover(api):
    """The API exposes none of the artwork annict.com shows -- the one
    field that would, WorkImage.internalUrl, is admin-only -- so the
    details path scrapes the work page for it."""
    api._request = lambda query, variables=None: {
        'searchWorks': {'nodes': [work()]}}
    api._posters = {}
    api._poster_url = lambda i: 'https://image.annict.com/s/s:640:853/plain/x.jpg'

    info = api.request_info([{'id': 42}])[0]

    assert info['image'] == 'https://image.annict.com/s/s:640:853/plain/x.jpg'
    assert info['image_thumb'] == info['image']


def test_a_bulk_refresh_never_scrapes(api):
    """data.py's cache-fill path can ask about a whole library at once;
    that must not become a page load per show."""
    nodes = [work(annictId=i) for i in range(1, api.POSTER_SCRAPE_LIMIT + 2)]
    api._posters = {}
    api._request = lambda query, variables=None: {'searchWorks': {'nodes': nodes}}

    def fail(annict_id):
        raise AssertionError('scraped during a bulk refresh')

    api._poster_url = fail
    infolist = api.request_info([{'id': n['annictId']} for n in nodes])

    assert len(infolist) == len(nodes)


# --------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------

class _Recorder:
    """Stands in for _request during mutation tests."""

    def __init__(self, records=()):
        self.queries = []
        self._records = list(records)

    def __call__(self, query, variables=None):
        self.queries.append((query, variables))
        if 'createRecord' in query:
            return {'createRecord': {'record': {'annictId': 1, 'id': 'r'}}}
        if 'deleteRecord' in query:
            return {'deleteRecord': {'clientMutationId': None}}
        if 'updateStatus' in query:
            return {'updateStatus': {'work': {'annictId': 42}}}
        if 'records(' in query:
            return {'viewer': {'records': {
                'pageInfo': {'hasNextPage': False, 'endCursor': None},
                'nodes': self._records}}}
        raise AssertionError('unexpected query: %s' % query)

    def mutations(self, name):
        return [v for q, v in self.queries if name in q]


def test_advancing_progress_only_records_the_missing_episodes(api):
    api._episodes[42] = episodes({1, 2, 3})
    api._request = recorder = _Recorder()

    api._sync_progress(42, 5)

    created = recorder.mutations('createRecord')
    assert [v['episodeId'] for v in created] == ['Episode-1004', 'Episode-1005']


def test_records_are_never_created_for_an_already_tracked_episode(api):
    """Annict allows several records per episode -- that's how it models
    rewatching -- so an unguarded create duplicates rather than no-ops."""
    api._episodes[42] = episodes({1, 2, 3, 4, 5})
    api._request = recorder = _Recorder()

    api._sync_progress(42, 5)

    assert recorder.mutations('createRecord') == []


def test_creates_never_share_to_social_networks(api):
    api._episodes[42] = episodes(set(), count=1)
    api._request = recorder = _Recorder()

    api._sync_progress(42, 1)

    query = recorder.queries[0][0]
    assert 'shareTwitter: false' in query
    assert 'shareFacebook: false' in query


def test_lowering_progress_deletes_the_records_above_it(api):
    api._episodes[42] = episodes({1, 2, 3, 4, 5})
    api._request = recorder = _Recorder(records=[
        {'id': 'Record-3', 'episode': {'annictId': 1003}},
        {'id': 'Record-4', 'episode': {'annictId': 1004}},
        {'id': 'Record-5', 'episode': {'annictId': 1005}},
    ])

    api._sync_progress(42, 2)

    deleted = {v['recordId'] for v in recorder.mutations('deleteRecord')}
    assert deleted == {'Record-3', 'Record-4', 'Record-5'}
    assert recorder.mutations('createRecord') == []
    assert [e['viewerDidTrack'] for e in api._episodes[42][:5]] == \
        [True, True, False, False, False]


def test_unfindable_records_are_reported_rather_than_passed_over(api):
    """User.records takes no episode filter, so the search for what to
    delete is a bounded scan of recent history and can come up empty."""
    api._episodes[42] = episodes({1, 2, 3})
    api._request = _Recorder(records=[])

    api._sync_progress(42, 1)

    assert api.msg.warnings
    assert '1002' in api.msg.warnings[0] and '1003' in api.msg.warnings[0]


def test_the_inferred_progress_is_not_written_back_as_records(api):
    """A WATCHED show with no records reads as complete. If that number
    is ever handed back as an update target it's this class's own read
    echoing round -- not a request to post the whole series to the
    user's activity feed."""
    ep = entry(state='WATCHED')
    ep['work']['episodes']['nodes'] = episodes(set())
    show = api._parse_entry(ep)
    api._request = recorder = _Recorder()

    api._sync_progress(42, show['my_progress'])

    assert recorder.queries == []


def test_a_real_advance_past_the_inferred_progress_still_records(api):
    ep = entry(state='WATCHED', episodesCount=4)
    ep['work']['episodes']['nodes'] = episodes(set(), count=8)
    api._parse_entry(ep)          # infers 4
    api._request = recorder = _Recorder()

    api._sync_progress(42, 5)

    assert len(recorder.mutations('createRecord')) == 5


def test_the_inference_is_forgotten_once_records_exist(api):
    api._parse_entry(entry(state='WATCHED'))    # infers 8
    api._parse_entry(entry(state='WATCHING'))   # derives 3

    assert 42 not in api._inferred


def test_a_whole_series_is_never_recorded_in_one_go(api):
    """Every record is a public post, so a bulk jump -- adding an
    already-finished long-runner through multisync -- is refused and
    reported rather than carried out quietly."""
    api._episodes[42] = episodes(set(), count=50)
    api._request = recorder = _Recorder()

    api._sync_progress(42, 50)

    assert recorder.mutations('createRecord') == []
    assert api.msg.warnings


def test_a_burst_under_the_limit_is_recorded_normally(api):
    api._episodes[42] = episodes(set(), count=13)
    api._request = recorder = _Recorder()

    api._sync_progress(42, 13)

    assert len(recorder.mutations('createRecord')) == 13
    assert not api.msg.warnings


def test_delete_show_clears_the_status_and_keeps_the_records(api):
    api._episodes[42] = episodes({1, 2})
    api._request = recorder = _Recorder()

    api.delete_show({'id': 42, 'title': 'x'})

    assert recorder.mutations('updateStatus') == [
        {'workId': 'V29yay00Mg==', 'state': 'NO_STATE'}]
    assert recorder.mutations('deleteRecord') == []


def test_adding_a_finished_show_sets_the_status_without_records(api):
    """A WATCHED status with no records already reads back as full
    progress, so the records would be feed noise for nothing new."""
    api._episodes[42] = episodes(set(), count=12)
    api._request = recorder = _Recorder()

    api.add_show({'id': 42, 'title': 'x',
                  'my_status': 'WATCHED', 'my_progress': 12})

    assert recorder.mutations('updateStatus') == [
        {'workId': 'V29yay00Mg==', 'state': 'WATCHED'}]
    assert recorder.mutations('createRecord') == []


def test_adding_a_partly_watched_show_records_its_episodes(api):
    api._episodes[42] = episodes(set(), count=12)
    api._request = recorder = _Recorder()

    api.add_show({'id': 42, 'title': 'x',
                  'my_status': 'WATCHING', 'my_progress': 3})

    assert len(recorder.mutations('createRecord')) == 3


def test_deleting_a_show_forgets_what_was_inferred_about_it(api):
    api._parse_entry(entry(state='WATCHED'))
    api._request = _Recorder()

    api.delete_show({'id': 42, 'title': 'x'})

    assert 42 not in api._inferred


def test_update_show_pushes_status_and_progress(api):
    api._episodes[42] = episodes({1})
    api._request = recorder = _Recorder()

    api.update_show({'id': 42, 'title': 'x',
                     'my_status': 'WATCHING', 'my_progress': 2})

    assert recorder.mutations('updateStatus') == [
        {'workId': 'V29yay00Mg==', 'state': 'WATCHING'}]
    assert len(recorder.mutations('createRecord')) == 1


# --------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------

def test_relay_ids_are_reconstructed_from_numeric_ids(api):
    assert api._gid('Work', 6522) == 'V29yay02NTIy'
    assert api._gid('Episode', 116592) == 'RXBpc29kZS0xMTY1OTI='


def test_graphql_errors_in_a_200_are_not_mistaken_for_data(api):
    with pytest.raises(utils.APIError):
        api._parse_body('{"data": null, "errors": [{"message": "nope"}]}')


def test_partial_data_is_kept_and_the_errors_warned_about(api):
    data = api._parse_body(
        '{"data": {"viewer": {}}, "errors": [{"message": "a field failed"}]}')

    assert data == {'viewer': {}}
    assert api.msg.warnings


def test_a_non_json_body_is_an_api_error_not_a_traceback(api):
    # Annict answers a malformed season string with a 500 and a Japanese
    # HTML error page.
    with pytest.raises(utils.APIError):
        api._parse_body('<!DOCTYPE html><html><body>500</body></html>')


# --------------------------------------------------------------------
# Odds and ends
# --------------------------------------------------------------------

def test_season_is_translated_to_annicts_spelling(api):
    assert api.season_translate[utils.Season.FALL] == 'autumn'
    assert api.rev_season_translate['AUTUMN'] == utils.Season.FALL


def test_airing_status_is_inferred_from_the_season(api):
    # Annict has no airing-status field of its own.
    assert api._derive_status({'seasonYear': 1999, 'seasonName': 'SPRING'}) == \
        utils.Status.FINISHED
    assert api._derive_status({'seasonYear': 2999, 'seasonName': 'SPRING'}) == \
        utils.Status.NOTYET
    assert api._derive_status({'seasonYear': None, 'seasonName': None}) == \
        utils.Status.UNKNOWN


def test_scores_are_declared_unsupported_but_still_bounded(api):
    """Annict's only rating lives on an episode Record, so there is no
    show score -- but engine.py and the Qt score widget index score_max
    and score_step unconditionally."""
    info = api.media_info()
    assert info['can_score'] is False
    assert info['score_max'] and info['score_step']
