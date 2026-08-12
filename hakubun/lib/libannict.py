# This file is part of Hakubun.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

import base64
import datetime
import json
import re
import socket
import time
import urllib.error
import urllib.request

from hakubun import utils
from hakubun.lib.lib import lib


class libannict(lib):
    """
    API class to communicate with Annict

    Website: https://annict.com
    API docs: https://developers.annict.com/docs/graphql-api/beta/

    Annict does not model progress the way every other backend Hakubun
    talks to does. There is no "episodes watched" counter: watching an
    episode creates a *Record* object attached to that episode, and the
    only thing readable per episode is the boolean `viewerDidTrack`.
    Progress is therefore a *set*, not a high-water mark, and the whole
    of this class is the translation between the two. See
    _derive_progress() for the rule and why it is the conservative one.

    messenger: Messenger object to send useful messages to
    """
    name = 'libannict'
    msg = None
    logged_in = False

    api_info = {'name': 'Annict', 'shortname': 'annict',
                'version': '1', 'merge': False}

    mediatypes = dict()
    mediatypes['anime'] = {
        'has_progress': True,
        'can_add': True,
        'can_delete': True,
        # Annict's only rating is RatingState (GREAT/GOOD/AVERAGE/BAD)
        # and it lives on an individual episode Record, not on the work
        # -- there is no per-show score to read back or write. score_max
        # / score_step are still declared because engine.py and the Qt
        # score widget index them unconditionally.
        'can_score': False,
        'can_status': True,
        'can_update': True,
        'can_play': True,
        'statuses_start': ['WATCHING'],
        'statuses_finish': ['WATCHED'],
        'statuses_library': ['WATCHING', 'WANNA_WATCH', 'ON_HOLD'],
        'statuses': ['WATCHING', 'WATCHED', 'ON_HOLD',
                     'STOP_WATCHING', 'WANNA_WATCH'],
        'statuses_dict': {
            'WATCHING': 'Watching',
            'WATCHED': 'Completed',
            'ON_HOLD': 'On-Hold',
            'STOP_WATCHING': 'Dropped',
            'WANNA_WATCH': 'Plan to Watch',
        },
        'score_max': 10,
        'score_step': 1,
        'search_methods': [utils.SearchMethod.KW, utils.SearchMethod.SEASON],
    }
    default_mediatype = 'anime'

    # Supported signals for the data handler. userconfig_changed is
    # declared explicitly because _refresh_user_info() emits it and
    # _emit_signal() raises on any key it doesn't know about.
    signals = {'show_info_changed': None, 'userconfig_changed': None, }

    url = "https://annict.com"
    query_url = "https://api.annict.com/graphql"

    # For the day this grows a registered OAuth app instead of a personal
    # access token (see utils.available_libs). Verified against the live
    # service: both hosts answer /oauth/token, but api.annict.com's
    # /oauth/authorize 301s to annict.com's, so annict.com is canonical
    # for the browser leg. The token endpoint accepts a JSON body as well
    # as form encoding, and issues no refresh token -- Annict's access
    # tokens don't expire, which is why check_credentials() has no
    # refresh branch. (Endpoints cross-checked against ci7lus/imau.)
    auth_url = "https://annict.com/oauth/authorize"
    token_url = "https://api.annict.com/oauth/token"
    user_agent = 'Hakubun-Plus/{}'.format(utils.VERSION)

    # How many library entries to pull per page, and how many episodes to
    # ask for inside each of those entries. The endpoint has no query
    # complexity or depth limit and no rate-limit headers, so these are
    # chosen to keep single responses reasonable rather than to satisfy a
    # server-side cap. Works with more episodes than EPISODES_PAGE are
    # paginated separately -- see _complete_episodes().
    ENTRIES_PAGE = 50
    EPISODES_PAGE = 200

    # Upper bound on how far back _find_record_ids() will page through the
    # viewer's records looking for the ones to delete. Annict offers no
    # way to fetch "my record for episode X" directly (User.records takes
    # no episode filter, and Episode.records is not viewer-scoped), so
    # un-watching is a bounded scan of recent history.
    RECORD_SCAN_PAGES = 5
    RECORD_SCAN_PAGE = 100

    # Pages of 100 to walk when browsing a season. A busy season runs to
    # several hundred works once shorts and specials are counted.
    SEASON_PAGES = 6

    # Most records a single progress change is allowed to create. Every
    # record is a post on the user's public activity feed, so a jump that
    # would manufacture a whole series' worth of them at once -- adding
    # an already-finished show through multisync, say -- is refused and
    # reported rather than carried out silently. Watching normally, an
    # episode at a time, never comes near this.
    RECORD_BURST_LIMIT = 26

    # Most works a single request_info() will scrape covers for. Details
    # and search ask about one or a handful; data.py's bulk refresh path
    # can ask about the whole list, which must not become a page load per
    # show.
    POSTER_SCRAPE_LIMIT = 8

    # Covers fetched per fetch_list() for works not yet in the cache.
    # A large library fills in over several syncs instead of turning one
    # into hundreds of page loads.
    POSTER_BACKFILL = 20

    # Seconds between consecutive pages of a paginated walk.
    PAGE_INTERVAL = 0.5

    type_translate = {
        None: utils.Type.UNKNOWN,
        'TV': utils.Type.TV,
        'OVA': utils.Type.OVA,
        'MOVIE': utils.Type.MOVIE,
        'WEB': utils.Type.ONA,
        'OTHER': utils.Type.OTHER,
    }

    season_translate = {
        utils.Season.WINTER: 'winter',
        utils.Season.SPRING: 'spring',
        utils.Season.SUMMER: 'summer',
        utils.Season.FALL: 'autumn',
    }
    rev_season_translate = {
        'WINTER': utils.Season.WINTER,
        'SPRING': utils.Season.SPRING,
        'SUMMER': utils.Season.SUMMER,
        'AUTUMN': utils.Season.FALL,
    }
    _season_order = {'WINTER': 0, 'SPRING': 1, 'SUMMER': 2, 'AUTUMN': 3}

    _work_fragment = '''
    annictId
    id
    title
    titleEn
    titleKana
    titleRo
    malAnimeId
    media
    episodesCount
    noEpisodes
    seasonYear
    seasonName
    watchersCount
    satisfactionRate
    officialSiteUrl
    wikipediaUrl
    viewerStatusState
'''

    def __init__(self, messenger, account, userconfig):
        """Initializes the API"""
        super(libannict, self).__init__(messenger, account, userconfig)

        self.pin = (account['password'] or '').strip()
        self.userid = self._get_userconfig('userid')

        # annictId -> ordered list of episode dicts, as last seen by
        # fetch_list(). update_show() diffs the requested progress
        # against this to work out which records to create or delete.
        self._episodes = {}
        # annictId -> the work's Relay node ID, for mutations.
        self._workids = {}
        # annictId -> progress this class inferred from a WATCHED status
        # rather than read from actual records. See _parse_entry().
        self._inferred = {}
        # str(annictId) -> scraped cover URL, or None when the work has
        # none. Loaded from disk on first use; see _poster_url().
        self._posters = None
        self._posters_dirty = False
        # Works Annict registers no episodes for (every movie, plus
        # shorts): progress on these is carried by the status alone,
        # since there is nothing to attach a record to.
        self._no_episodes = set()

        self.opener = urllib.request.build_opener()
        self.opener.addheaders = [('User-agent', self.user_agent)]

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _request(self, query, variables=None):
        if not self.pin:
            raise utils.APIFatal("No access token.")

        payload = {'query': query}
        if variables:
            payload['variables'] = variables

        request = urllib.request.Request(
            self.query_url, json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        request.get_method = lambda: 'POST'
        request.add_header('Content-Type', 'application/json')
        request.add_header('Accept', 'application/json')
        request.add_header('Authorization', 'Bearer {}'.format(self.pin))

        try:
            response = self.opener.open(request, timeout=15)
            body = response.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            raise self._http_error(e)
        except urllib.error.URLError as e:
            raise utils.APIError("HTTP connection error: %s" % e.reason)
        except socket.timeout:
            raise utils.APIError("Connection timed out.")

        return self._parse_body(body)

    def _http_error(self, e):
        """Turn an HTTPError into the right Hakubun exception.

        Doorkeeper (Annict's OAuth layer) distinguishes the two 401 cases
        in WWW-Authenticate: error="invalid_token" means the token is
        dead and the user has to re-authorize, while error="invalid_request"
        means we sent a malformed request, which is our bug and not
        something re-authorizing would fix.
        """
        try:
            body = e.read().decode('utf-8', 'replace')
        except Exception:
            body = ''

        if e.code == 401:
            challenge = ''
            if e.headers:
                challenge = e.headers.get('WWW-Authenticate') or ''
            if 'invalid_token' in challenge:
                return utils.APIFatal(
                    "Annict rejected the access token. Please create a new "
                    "one at %s/settings/apps and re-enter it." % self.url)
            return utils.APIError("Unauthorized: %s" % (challenge or body))

        if e.code == 404:
            err = utils.APIError("Not found: %s" % body)
            err.not_found = True
            return err

        # Annict answers a malformed argument (a bad season string, for
        # one) with a 500 and a Japanese HTML error page rather than a
        # GraphQL error document, so don't assume the body is readable.
        return utils.APIError("HTTP error status %d: %s" % (e.code, body[:200]))

    def _parse_body(self, body):
        try:
            data = json.loads(body)
        except ValueError:
            raise utils.APIError(
                "Unexpected non-JSON response from Annict: %s" % body[:200])

        errors = data.get('errors')
        if errors:
            messages = '; '.join(
                str(err.get('message', err)) for err in errors)
            # GraphQL reports errors with HTTP 200 and may still return
            # usable data alongside them (a single null field in an
            # otherwise complete list). Only fail outright when there's
            # nothing left to work with.
            if not data.get('data'):
                raise utils.APIError("Annict API error: %s" % messages)
            self.msg.warn("Annict API returned errors: %s" % messages)

        return data.get('data') or {}

    @staticmethod
    def _gid(kind, annict_id):
        """Reconstruct a Relay node ID from a numeric Annict ID.

        Only used as a fallback when we haven't already been handed the
        real `id` by a previous query.
        """
        return base64.b64encode(
            '{}-{}'.format(kind, annict_id).encode('utf-8')).decode('ascii')

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def check_credentials(self):
        if not self.pin:
            raise utils.APIFatal("No access token.")

        if not self.userid:
            self._refresh_user_info()

        self.logged_in = True
        return True

    def _refresh_user_info(self):
        self.msg.info('Refreshing user details...')
        data = self._request('{ viewer { annictId username name } }')
        viewer = data.get('viewer')
        if not viewer:
            raise utils.APIFatal("Annict did not return a user for this token.")

        self.userid = viewer['annictId']
        self._set_userconfig('userid', viewer['annictId'])
        self._set_userconfig('username', viewer['username'])
        self._emit_signal('userconfig_changed')

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def fetch_list(self):
        self.check_credentials()
        self.msg.info('Downloading list...')

        query = '''query ($after: String, $states: [StatusState!], $entries: Int!, $episodes: Int!) {
  viewer {
    libraryEntries(first: $entries, after: $after, states: $states) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        note
        status { state }
        nextEpisode { annictId number sortNumber }
        work {
          %s
          episodes(first: $episodes, orderBy: {field: SORT_NUMBER, direction: ASC}) {
            pageInfo { hasNextPage endCursor }
            nodes { annictId id number sortNumber title viewerDidTrack }
          }
        }
      }
    }
  }
}''' % self._work_fragment

        showlist = {}
        self._episodes = {}
        self._workids = {}

        after = None
        while True:
            variables = {
                'after': after,
                # Every state the mediatype knows about -- filtering this
                # down would silently drop completed/dropped shows from
                # the list. An explicit list is still required: clearing
                # a show's status leaves a NO_STATE library entry behind
                # (status: null), and an unfiltered query returns those.
                'states': self.mediatypes[self.mediatype]['statuses'],
                'entries': self.ENTRIES_PAGE,
                'episodes': self.EPISODES_PAGE,
            }
            data = self._request(query, variables)
            entries = (data.get('viewer') or {}).get('libraryEntries')
            if not entries:
                break

            for node in entries['nodes']:
                show = self._parse_entry(node)
                if show:
                    showlist[show['id']] = show

            if not entries['pageInfo']['hasNextPage']:
                break
            after = entries['pageInfo']['endCursor']
            # Annict publishes no rate limit and sends no rate-limit
            # headers, so there is nothing to react to -- pace the walk
            # instead of finding the ceiling the hard way behind
            # Cloudflare. (ci7lus/imau spaces its own pages the same.)
            time.sleep(self.PAGE_INTERVAL)

        self._backfill_posters(showlist)
        return showlist

    def _parse_entry(self, node):
        work = node.get('work')
        if not work:
            return None

        episodes = self._store_episodes(work)
        fields = self._common_work_fields(work)
        state = (node.get('status') or {}).get('state') \
            or self.mediatypes[self.mediatype]['statuses_start'][0]
        progress = self._derive_progress(episodes)

        # Recording every episode is optional on Annict -- plenty of
        # users just flip a finished show to WATCHED and never touch the
        # episodes. Taking the derived 0 at face value there would show a
        # completed show as 0/24, and multisync would then push that zero
        # out to every other provider. The status is the more reliable
        # statement of what was watched, so let it win.
        if state == 'WATCHED' and fields['total'] and progress < fields['total']:
            progress = fields['total']
            # Remember that this number was inferred, not recorded. If it
            # ever comes back to us as an update target it's our own read
            # echoing back, not a request to go and create the records
            # the inference stood in for.
            self._inferred[work['annictId']] = progress
        else:
            self._inferred.pop(work['annictId'], None)

        show = utils.show()
        show.update(fields)
        show.update({
            'my_id': node.get('id'),
            'my_status': state,
            'my_progress': progress,
        })

        next_ep = node.get('nextEpisode')
        if next_ep and next_ep.get('number'):
            show['next_ep_number'] = next_ep['number']

        return show

    def _common_work_fields(self, work):
        """The parts of a show dict that come from a Work, shared by the
        list, search and info paths."""
        # Deliberately not filled from the API's own image fields.
        # recommendedImageUrl / facebookOgImageUrl / twitterAvatarUrl are
        # scraped from the anime's *social media* -- Annict picks
        # "recommended" by opening the Facebook OG image, the Twitter
        # card and the Twitter profile avatar and keeping whichever has
        # the most pixels (annict/annict: lib/tasks/work_image.rake,
        # via a service class named Deprecated::SnsImageService). That
        # is a studio banner or an account icon, not cover art, which is
        # why they so rarely look like the show. Annict itself never
        # displays them: a work with no registered image gets
        # no-work-image.png. Covers come from _poster_url().
        image = self._poster_cache().get(str(work['annictId'])) or ''
        aliases = [t for t in (work.get('titleEn'), work.get('titleRo'),
                               work.get('titleKana')) if t]

        fields = {
            'id': work['annictId'],
            'title': work.get('title') or '',
            'aliases': aliases,
            'type': self.type_translate.get(work.get('media'), utils.Type.UNKNOWN),
            'status': self._derive_status(work),
            # A noEpisodes work has no episode list to record against at
            # all -- true of every movie Annict lists, plus shorts and
            # ONAs. It is one unit of watching, which is exactly how MAL,
            # AniList and Kitsu already model a film, so counting it as
            # a single episode keeps a watched movie reading as 1/1
            # rather than 0/0 (and stops multisync pushing that 0 out).
            'total': 1 if work.get('noEpisodes')
                     else (work.get('episodesCount') or 0),
            'url': '{}/works/{}'.format(self.url, work['annictId']),
            'image': image,
            'image_thumb': image,
            'mal_id': self._mal_id(work.get('malAnimeId')),
        }

        rate = work.get('satisfactionRate')
        if rate:
            fields['platform_score'] = '%d%%' % round(rate)
            fields['score_raw'] = rate

        watchers = work.get('watchersCount')
        if watchers is not None:
            # Negated so the Seasons page's "sort by popularity" can use
            # one ascending convention across every backend -- same
            # treatment libanilist gives its raw favourites count.
            fields['popularity'] = -watchers
            fields['popularity_label'] = '{:,} users'.format(watchers)

        return fields

    def _store_episodes(self, work):
        """Cache a work's episode list and return it.

        Episodes come back in a Relay connection nested inside the work,
        so a long-runner can exceed EPISODES_PAGE and get silently
        truncated -- which would make the derived progress quietly wrong
        rather than raise. _complete_episodes() closes that gap.
        """
        showid = work['annictId']
        self._workids[showid] = work.get('id')
        if work.get('noEpisodes'):
            self._no_episodes.add(showid)
        else:
            self._no_episodes.discard(showid)

        connection = work.get('episodes') or {}
        episodes = list(connection.get('nodes') or [])
        page_info = connection.get('pageInfo') or {}

        if page_info.get('hasNextPage'):
            episodes = self._complete_episodes(
                work, episodes, page_info.get('endCursor'))

        self._episodes[showid] = episodes
        return episodes

    def _complete_episodes(self, work, episodes, after):
        query = '''query ($id: ID!, $after: String, $first: Int!) {
  node(id: $id) {
    ... on Work {
      episodes(first: $first, after: $after, orderBy: {field: SORT_NUMBER, direction: ASC}) {
        pageInfo { hasNextPage endCursor }
        nodes { annictId id number sortNumber title viewerDidTrack }
      }
    }
  }
}'''
        workid = work.get('id') or self._gid('Work', work['annictId'])
        self.msg.debug("Paginating episodes for work %s..." % work['annictId'])

        while after:
            data = self._request(
                query, {'id': workid, 'after': after, 'first': self.EPISODES_PAGE})
            connection = (data.get('node') or {}).get('episodes')
            if not connection:
                break
            episodes.extend(connection.get('nodes') or [])
            page_info = connection.get('pageInfo') or {}
            if not page_info.get('hasNextPage'):
                break
            after = page_info.get('endCursor')

        return episodes

    @staticmethod
    def _episode_number(episode, index):
        """The number Hakubun should use for an episode.

        Annict's `number` is nullable (specials and some ONAs have none),
        so fall back to the episode's 1-based position in sort order.
        Both the progress derivation and the update diff go through here,
        so the two can't disagree about what "episode 5" means.
        """
        number = episode.get('number')
        if number is None:
            return index + 1
        return number

    def _derive_progress(self, episodes):
        """Collapse Annict's set-of-watched-episodes into a high-water mark.

        The rule is the *contiguous prefix*: the last episode such that
        every episode before it is also tracked. Deliberately not the
        highest tracked episode -- on an account with 1, 2, 3 and 12
        tracked, reporting 12 would make the very next update_show()
        create records for 4 through 11, spraying eight entries onto the
        user's public activity feed off the back of a read. The prefix
        rule matches what Annict itself means by nextEpisode, and it
        heals: advancing 2 -> 3 over a {1,2,4} set leaves {1,2,3,4}.
        """
        progress = 0
        for index, episode in enumerate(episodes):
            if not episode.get('viewerDidTrack'):
                break
            progress = self._episode_number(episode, index)
        return progress

    def _derive_status(self, work):
        """Annict has no airing-status field, so infer it from the season.

        Works with no season at all stay UNKNOWN rather than being
        guessed at.
        """
        year = work.get('seasonYear')
        name = work.get('seasonName')
        if not year:
            return utils.Status.UNKNOWN

        today = datetime.date.today()
        current_year = today.year
        current_season = (today.month - 1) // 3  # 0=winter .. 3=autumn

        if year < current_year:
            return utils.Status.FINISHED
        if year > current_year:
            return utils.Status.NOTYET
        if name is None:
            return utils.Status.UNKNOWN

        order = self._season_order.get(name)
        if order is None:
            return utils.Status.UNKNOWN
        if order < current_season:
            return utils.Status.FINISHED
        if order > current_season:
            return utils.Status.NOTYET
        return utils.Status.AIRING

    # Poster URLs on the work page. Annict serves every image through
    # imgproxy; work posters are resized into a 3:4 box ("s:640:853"),
    # while the square "rs:fill-down:50:50" ones on the same page are
    # commenters' avatars. Matching the box shape is what tells them
    # apart. (annict/annict: go/internal/image/helper.go builds these,
    # and WorkImageHeight is width*4/3.)
    _POSTER_RE = re.compile(
        r'https://image\.annict\.com/[A-Za-z0-9_-]+/s:(\d+):(\d+)/[^"\s\\]+')

    def _poster_url(self, annict_id):
        """Annict's own cover art for a work, scraped from its page.

        There is no API route to this, by design rather than oversight.
        The poster lives in work_images.image_data and is served as an
        HMAC-signed imgproxy URL, so it can't be constructed; the one
        GraphQL field that would return it, WorkImage.internalUrl, is
        gated on `context[:doorkeeper_token].owner.role.admin?` and so
        answers null for every ordinary token (annict/annict:
        rails/app/graphql/beta/types/objects/work_image_type.rb).

        Scraping the page is therefore the only way to show the artwork
        the site shows. Results are cached on disk (see
        _poster_cache_path) because it costs a page load per work.
        """
        cache = self._poster_cache()
        key = str(annict_id)
        if key in cache:
            return cache[key]

        url = None
        try:
            request = urllib.request.Request(
                '{}/works/{}'.format(self.url, annict_id))
            with self.opener.open(request, timeout=10) as response:
                page = response.read(262144).decode('utf-8', 'replace')
            best = None
            for match in self._POSTER_RE.finditer(page):
                width, height = int(match.group(1)), int(match.group(2))
                # Portrait box only -- see _POSTER_RE. Largest wins.
                if height > width and (best is None or width > best[0]):
                    best = (width, match.group(0))
            if best:
                url = best[1]
        except Exception as e:
            # Cosmetic: a details view must still open without its cover.
            self.msg.debug("Couldn't read the poster for work %s: %s"
                           % (annict_id, e))
            return None

        # A work with no registered image is cached as None too: Annict
        # shows a placeholder for those and will keep doing so, and
        # re-fetching the page every sync to rediscover that is waste.
        cache[key] = url
        self._posters_dirty = True
        return url

    def _poster_cache_path(self):
        return utils.to_cache_path('annict_posters.json')

    def _poster_cache(self):
        if self._posters is None:
            try:
                with open(self._poster_cache_path(), encoding='utf-8') as f:
                    self._posters = json.load(f)
                if not isinstance(self._posters, dict):
                    self._posters = {}
            except (OSError, ValueError):
                self._posters = {}
        return self._posters

    def _save_poster_cache(self):
        if not self._posters_dirty:
            return
        try:
            utils.make_dir(utils.to_cache_path())
            with open(self._poster_cache_path(), 'w', encoding='utf-8') as f:
                json.dump(self._posters, f)
            self._posters_dirty = False
        except OSError as e:
            self.msg.debug("Couldn't save the poster cache: %s" % e)

    def _backfill_posters(self, showlist):
        """Fill in covers for works whose poster isn't cached yet.

        Capped per run rather than done for the whole library at once: a
        first sync of several hundred shows would otherwise be several
        hundred page loads. The cache is permanent, so a library fills
        itself in over a handful of syncs and costs nothing after that.
        """
        cache = self._poster_cache()
        pending = [s for s in showlist.values() if str(s['id']) not in cache]
        if not pending:
            return

        for show in pending[:self.POSTER_BACKFILL]:
            url = self._poster_url(show['id'])
            if url:
                show['image'] = url
                show['image_thumb'] = url

        if len(pending) > self.POSTER_BACKFILL:
            self.msg.debug(
                "%d more covers still to fetch; they'll fill in on later syncs."
                % (len(pending) - self.POSTER_BACKFILL))
        self._save_poster_cache()

    @staticmethod
    def _mal_id(value):
        # malAnimeId is a String on Annict and is "" rather than null for
        # works that have no MAL entry.
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add_show(self, item):
        self.check_credentials()
        self.msg.info("Adding item %s..." % item['title'])

        state = item.get('my_status') or \
            self.mediatypes[self.mediatype]['statuses_start'][0]
        self._update_status(item['id'], state)

        # A finished entry needs no records: the status alone reads back
        # as full progress (see _parse_entry), so creating a whole
        # series' worth would only be feed noise for information Annict
        # already has.
        if item.get('my_progress') and \
                state not in self.mediatypes[self.mediatype]['statuses_finish']:
            self._sync_progress(item['id'], item['my_progress'])

        # Annict has no way to look up a library entry by work, so my_id
        # is left for the next fetch_list() to fill in. Nothing in this
        # class needs it -- every mutation keys off the work.
        return None

    def update_show(self, item):
        self.check_credentials()
        self.msg.info("Updating item %s..." % item['title'])

        if 'my_status' in item and item['my_status']:
            self._update_status(item['id'], item['my_status'])

        if 'my_progress' in item:
            self._sync_progress(item['id'], item['my_progress'])

        return datetime.datetime.now(tz=datetime.timezone.utc)

    def delete_show(self, item):
        self.check_credentials()
        self.msg.info("Deleting item %s..." % item['title'])

        # Annict has no "remove from library" -- clearing the status to
        # NO_STATE is the closest equivalent. The library entry itself
        # survives with a null status, which is why fetch_list() always
        # passes an explicit states filter. Any records the user made
        # are deliberately left alone; deleting those would throw away
        # watch history the UI never asked to discard.
        self._update_status(item['id'], 'NO_STATE')
        self._episodes.pop(item['id'], None)
        self._inferred.pop(item['id'], None)

    def _update_status(self, showid, state):
        query = '''mutation ($workId: ID!, $state: StatusState!) {
  updateStatus(input: {workId: $workId, state: $state}) {
    work { annictId viewerStatusState }
  }
}'''
        workid = self._workids.get(showid) or self._gid('Work', showid)
        self._request(query, {'workId': workid, 'state': state})

    def _sync_progress(self, showid, progress):
        """Bring the set of tracked episodes in line with a progress number.

        Everything at or below `progress` gets a record; anything above it
        that has one loses it.
        """
        if progress == self._inferred.get(showid):
            # Our own inference, handed back to us. The status already
            # says the show is finished; manufacturing the records it
            # stood in for would post the whole series to the feed.
            self.msg.debug(
                "Progress %d for work %s is the inferred value; nothing to do."
                % (progress, showid))
            return

        episodes = self._load_episodes(showid)
        if not episodes:
            if showid in self._no_episodes:
                # Nothing to record against, by design -- the status
                # already carries "watched" for these. Not a problem.
                self.msg.debug(
                    "Work %s has no episodes; its status carries progress."
                    % showid)
            else:
                self.msg.warn(
                    "No episodes known for work %s; progress not applied."
                    % showid)
            return

        to_create = []
        to_delete = []
        for index, episode in enumerate(episodes):
            number = self._episode_number(episode, index)
            tracked = bool(episode.get('viewerDidTrack'))
            if number <= progress and not tracked:
                to_create.append(episode)
            elif number > progress and tracked:
                to_delete.append(episode)

        if len(to_create) > self.RECORD_BURST_LIMIT:
            self.msg.warn(
                "Not recording %d episodes of work %s at once -- each one is a "
                "post on your Annict activity feed. The status was still "
                "updated; mark them on annict.com if you want the records."
                % (len(to_create), showid))
            to_create = []

        for episode in to_create:
            self._create_record(episode)
        if to_delete:
            self._delete_records(to_delete)

    def _load_episodes(self, showid):
        episodes = self._episodes.get(showid)
        if episodes is not None:
            return episodes

        query = '''query ($id: ID!, $first: Int!) {
  node(id: $id) {
    ... on Work {
      annictId
      id
      noEpisodes
      episodes(first: $first, orderBy: {field: SORT_NUMBER, direction: ASC}) {
        pageInfo { hasNextPage endCursor }
        nodes { annictId id number sortNumber title viewerDidTrack }
      }
    }
  }
}'''
        workid = self._workids.get(showid) or self._gid('Work', showid)
        data = self._request(query, {'id': workid, 'first': self.EPISODES_PAGE})
        work = data.get('node')
        if not work:
            return []
        return self._store_episodes(work)

    def _create_record(self, episode):
        query = '''mutation ($episodeId: ID!) {
  createRecord(input: {episodeId: $episodeId, shareTwitter: false, shareFacebook: false}) {
    record { annictId id }
  }
}'''
        episodeid = episode.get('id') or self._gid('Episode', episode['annictId'])
        self._request(query, {'episodeId': episodeid})
        # Annict permits several records per episode -- that's how it
        # models rewatches -- so a create is never idempotent. Keep the
        # cache honest so a second pass doesn't duplicate them.
        episode['viewerDidTrack'] = True

    def _delete_records(self, episodes):
        wanted = {e['annictId']: e for e in episodes if e.get('annictId')}
        if not wanted:
            return

        for record_id, annict_id in self._find_record_ids(set(wanted)):
            query = '''mutation ($recordId: ID!) {
  deleteRecord(input: {recordId: $recordId}) { clientMutationId }
}'''
            self._request(query, {'recordId': record_id})
            wanted[annict_id]['viewerDidTrack'] = False

        still_tracked = [str(e['annictId'])
                         for e in wanted.values() if e.get('viewerDidTrack')]
        if still_tracked:
            # Better to say so than to report a clean success: without an
            # episode filter on User.records the scan is bounded, so an
            # old record simply won't be found.
            self.msg.warn(
                "Couldn't find records to remove for episode(s) %s; they may be "
                "older than the last %d records." % (
                    ', '.join(still_tracked),
                    self.RECORD_SCAN_PAGES * self.RECORD_SCAN_PAGE))

    def _find_record_ids(self, episode_ids):
        """Scan recent records for ones belonging to the given episodes.

        Annict offers no direct "my record for episode X" lookup, so this
        walks the viewer's records newest-first and stops as soon as
        everything is accounted for or the scan budget runs out.
        """
        query = '''query ($after: String, $first: Int!) {
  viewer {
    records(first: $first, after: $after, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { id episode { annictId } }
    }
  }
}'''
        remaining = set(episode_ids)
        after = None

        for _ in range(self.RECORD_SCAN_PAGES):
            if not remaining:
                return
            data = self._request(
                query, {'after': after, 'first': self.RECORD_SCAN_PAGE})
            records = (data.get('viewer') or {}).get('records')
            if not records:
                return

            for record in records['nodes']:
                annict_id = (record.get('episode') or {}).get('annictId')
                if annict_id in remaining:
                    yield record['id'], annict_id
                    # An episode can carry several records (rewatches);
                    # keep scanning this page for the rest of them.

            if not records['pageInfo']['hasNextPage']:
                return
            after = records['pageInfo']['endCursor']

    # ------------------------------------------------------------------
    # Search / info
    # ------------------------------------------------------------------

    def search(self, criteria, method):
        self.check_credentials()
        self.msg.info("Searching for {}...".format(criteria))

        if method == utils.SearchMethod.SEASON:
            nodes = self._search_season(*criteria)
        else:
            nodes = self._search_keyword(criteria)

        infolist = [self._parse_info(work) for work in nodes]
        self._emit_signal('show_info_changed', infolist)
        return infolist

    def _search_season(self, season, year):
        season_str = '{}-{}'.format(year, self.season_translate[season])
        query = '''query ($seasons: [String!], $first: Int!, $after: String) {
  searchWorks(seasons: $seasons, first: $first, after: $after,
              orderBy: {field: WATCHERS_COUNT, direction: DESC}) {
    pageInfo { hasNextPage endCursor }
    nodes { %s }
  }
}''' % self._work_fragment

        nodes = []
        after = None
        # A season comfortably exceeds one page, and the Seasons page
        # wants the whole thing -- but stop somewhere rather than
        # walking forever if the cursor misbehaves.
        for _ in range(self.SEASON_PAGES):
            data = self._request(
                query, {'seasons': [season_str], 'first': 100, 'after': after})
            connection = data.get('searchWorks') or {}
            nodes.extend(connection.get('nodes') or [])
            page_info = connection.get('pageInfo') or {}
            if not page_info.get('hasNextPage'):
                break
            after = page_info.get('endCursor')

        return nodes

    def _search_keyword(self, criteria):
        # searchWorks matches the Japanese `title` only, as a substring
        # -- it will not find a romaji or English name. Nothing else in
        # the API will either (the REST v1 filter_title behaves
        # identically), so keyword search here is only useful for
        # Japanese input. Local tracking is unaffected: fetch_list() puts
        # titleEn/titleRo/titleKana in the show's aliases, which is what
        # filename matching uses.
        query = '''query ($titles: [String!], $first: Int!) {
  searchWorks(titles: $titles, first: $first) {
    nodes { %s }
  }
}''' % self._work_fragment

        data = self._request(query, {'titles': [criteria], 'first': 50})
        return (data.get('searchWorks') or {}).get('nodes') or []

    def request_info(self, itemlist):
        self.check_credentials()

        ids = [show['id'] for show in itemlist]
        if not ids:
            return []

        query = '''query ($annictIds: [Int!], $first: Int!) {
  searchWorks(annictIds: $annictIds, first: $first) {
    nodes { %s }
  }
}''' % self._work_fragment

        data = self._request(query, {'annictIds': ids, 'first': len(ids)})
        nodes = (data.get('searchWorks') or {}).get('nodes') or []

        # Preserve the order the caller asked in; searchWorks doesn't.
        by_id = {work['annictId']: work for work in nodes}
        infolist = [self._parse_info(by_id[i]) for i in ids if i in by_id]

        # The details view is the one place worth paying an extra request
        # per work for a cover the API refuses to hand over. Bounded, so
        # a bulk refresh of a whole list can never turn into hundreds of
        # page loads.
        if len(infolist) <= self.POSTER_SCRAPE_LIMIT:
            for info in infolist:
                poster = self._poster_url(info['id'])
                if poster:
                    info['image'] = poster
                    info['image_thumb'] = poster
            self._save_poster_cache()

        self._emit_signal('show_info_changed', infolist)
        return infolist

    def _parse_info(self, work):
        info = utils.show()
        fields = self._common_work_fields(work)

        season = self.rev_season_translate.get(work.get('seasonName'))
        if season and work.get('seasonYear'):
            season_label = '{!s} {}'.format(season, work['seasonYear'])
        else:
            season_label = work.get('seasonYear')

        state = work.get('viewerStatusState')
        if state and state in self.mediatypes[self.mediatype]['statuses']:
            fields['my_status'] = state
        else:
            fields['my_status'] = \
                self.mediatypes[self.mediatype]['statuses_start'][0]

        rate = work.get('satisfactionRate')
        fields['extra'] = [
            # Annict stores no synopsis of any kind, so there is nothing
            # to show under one.
            ('English',            work.get('titleEn')),
            ('Romaji',             work.get('titleRo')),
            ('Japanese',           work.get('title')),
            ('Kana',               work.get('titleKana')),
            ('Season',             season_label),
            ('Type',               fields['type']),
            ('Episodes',           work.get('episodesCount')),
            ('Satisfaction rate',  '%.1f%%' % rate if rate else None),
            ('Watchers',           work.get('watchersCount')),
            ('Official site',      work.get('officialSiteUrl') or None),
            ('Wikipedia',          work.get('wikipediaUrl') or None),
        ]

        info.update(fields)
        return info

    def media_info(self):
        """Return information about the currently selected mediatype."""
        return self.mediatypes[self.mediatype]
