# -*- coding: utf-8 -*-
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

"""
Media list provider using Kitsu's GraphQL API <https://kitsu.app/api/graphql>.

This is the newer counterpart to libkitsu (which uses Kitsu's older REST /
JSON:API endpoints). Both talk to the same kitsu.app account over the same
OAuth2 credentials -- which of the two is used is chosen by the
'kitsu_api' setting (see data.py's lib loading), so an existing Kitsu
account works with either backend transparently.

The GraphQL API has no public documentation; the schema was mapped by
introspection. Notable differences from the REST API handled here:
  - The endpoint is behind a Cloudflare check that 403s the default
    urllib/curl User-Agent, but passes with Hakubun+'s own UA.
  - Kitsu's GraphQL dropped the 'drama' media type (ANIME/MANGA only).
  - Media info fields like subtype/episodeCount live on the concrete
    Anime/Manga types, reached via inline fragments on the Media interface.
  - LibraryEntry.rating is on the same 1-20 scale as the REST
    ratingTwenty field (so my_score = rating / 4).
"""

import datetime
import gzip
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from hakubun import utils
from hakubun.lib.lib import lib


class libkitsu_graphql(lib):
    """
    API class to communicate with Kitsu over its GraphQL API.

    Website: https://kitsu.app/
    """
    # Distinct from libkitsu's name so debug logs make clear which of the
    # two Kitsu backends is actually in use.
    name = 'libkitsu-gql'
    user_agent = 'Hakubun-Plus/{}'.format(utils.VERSION)

    logged_in = False

    api_info = {
        'name': 'Kitsu',
        'shortname': 'kitsu',
        'version': 'gql-0.1',
        'merge': True
    }

    default_mediatype = 'anime'
    default_statuses = ['current', 'completed',
                        'on_hold', 'dropped', 'planned']
    default_statuses_dict = {
        'current': 'Watching',
        'completed': 'Completed',
        'on_hold': 'On Hold',
        'dropped': 'Dropped',
        'planned': 'Plan to Watch'
    }

    mediatypes = dict()
    mediatypes['anime'] = {
        'has_progress': True,
        'can_add': True,
        'can_delete': True,
        'can_score': True,
        'can_status': True,
        'can_update': True,
        'can_play': True,
        'can_reference_mal': True,
        'statuses_start': ['current'],
        'statuses_finish': ['completed'],
        'statuses_library': ['current', 'on_hold', 'planned'],
        'statuses': default_statuses,
        'statuses_dict': default_statuses_dict,
        'score_max': 5,
        'score_step': 0.25,
        # Unlike the REST client (libkitsu.py), Kitsu's GraphQL schema has
        # no season/year argument anywhere on Query.anime or
        # Query.searchAnimeByTitle (checked against kitsu-server's
        # app/graphql/types/query_type.rb) -- season search genuinely isn't
        # possible over this API, so GraphQL-backend accounts don't get it.
        'search_methods': [utils.SearchMethod.KW],
    }
    mediatypes['manga'] = {
        'has_progress': True,
        'can_add': True,
        'can_delete': True,
        'can_score': True,
        'can_status': True,
        'can_update': True,
        'can_play': False,
        'can_reference_mal': True,
        'statuses_start': ['current'],
        'statuses_finish': ['completed'],
        'statuses': default_statuses,
        'statuses_dict': {
            'current': 'Reading',
            'completed': 'Completed',
            'on_hold': 'On Hold',
            'dropped': 'Dropped',
            'planned': 'Plan to Read'
        },
        'score_max': 5,
        'score_step': 0.25,
        'search_methods': [utils.SearchMethod.KW],
    }

    oauth_url = 'https://kitsu.app/api/oauth/token'
    graphql_url = 'https://kitsu.app/api/graphql'
    # Kitsu's GraphQL schema permits connection pages up to 2,000 entries.
    # This lets nearly all libraries complete in
    # one request while cursor pagination still handles larger libraries.
    library_page_size = 2000

    _client_id = 'dd031b32d2f56c990b1425efe6c42ad847e7fe3ab46bf1299f05ecd856bdb7dd'
    _client_secret = '54d7307928f63414defd96399fc31ba847961ceaecef3a5fd93144e960c0e151'

    # Kitsu media subtype -> Hakubun+ Type. GraphQL returns these uppercase.
    type_translate = {
        'TV': utils.Type.TV,
        'ONA': utils.Type.ONA,
        'OVA': utils.Type.OVA,
        'MOVIE': utils.Type.MOVIE,
        'MUSIC': utils.Type.MUSIC,
        'SPECIAL': utils.Type.SP,
    }

    # Kitsu ReleaseStatusEnum -> Hakubun+ Status.
    status_translate = {
        'CURRENT': utils.Status.AIRING,
        'FINISHED': utils.Status.FINISHED,
        'UPCOMING': utils.Status.NOTYET,
        'UNRELEASED': utils.Status.NOTYET,
        'TBA': utils.Status.NOTYET,
    }

    # externalSite value on a media's mappings that points at MyAnimeList.
    _mal_sites = ('MYANIMELIST_ANIME', 'MYANIMELIST_MANGA')

    # Fields needed to render and merge a library entry.  Keep this list
    # intentionally small: ``library.all`` returns it for every entry and
    # is paginated at 100 entries by Kitsu.  In particular, a synopsis is
    # often many KiB, while it is only needed in the details view.
    _MEDIA_BASE_FIELDS = '''
      id
      slug
      averageRating
      startDate
      endDate
      status
      ageRating
      ageRatingGuide
      tba
      # `alternatives` is declared non-null in Kitsu's schema but their
      # server actually returns null for it on some shows -- which,
      # since it's non-null, nullifies the *entire* bulk response
      # instead of just that field. Left out until/unless Kitsu fixes
      # that; `translated` alone (genuinely nullable) already covers
      # the actual gap (an English title to match against).
      titles { canonical preferred romanized translated }
      posterImage { views { name url } }
      mappings(first: 20) { nodes { externalSite externalId } }
    '''

    _MEDIA_DETAIL_FIELDS = '''
      description(locales: ["en", "en_us", "en_jp"])
    '''

    # Genre (category) and studio (production) data for the details view.
    # Only requested through findById (request_info), never in the
    # paginated library download where response size matters. Kept behind
    # a runtime fallback (see request_info) so an unexpected schema change
    # degrades the details view instead of breaking it. Category.title is
    # a locale->string map; MediaProduction.role is one of PRODUCER,
    # LICENSOR, STUDIO, SERIALIZATION.
    _MEDIA_EXTRA_FIELDS = '''
      categories(first: 20) { nodes { title(locales: ["en"]) } }
      productions(first: 20) { nodes { role company { name } } }
    '''
    # Flipped off for the session the first time Kitsu rejects the extra
    # selections above, so we stop paying the failed round-trip.
    _details_enriched = True

    def _media_fields(self, concrete=None, include_description=True):
        """Returns the media field selection. When the field returns the
        Media *interface* (the library query), subtype/count are pulled
        via inline fragments; when it returns a concrete Anime/Manga type
        (search, findById), they're selected directly -- a fragment on
        the other type would be rejected as un-spreadable there.

        Library downloads omit descriptions because they dominate the
        response size. Search and explicit detail requests include them.
        """
        fields = self._MEDIA_BASE_FIELDS
        if include_description:
            fields += self._MEDIA_DETAIL_FIELDS
        if concrete == 'anime':
            return fields + '\n      subtype\n      episodeCount\n      episodeLength\n'
        if concrete == 'manga':
            return fields + '\n      subtype\n      chapterCount\n'
        return fields + '''
      ... on Anime { subtype episodeCount episodeLength }
      ... on Manga { subtype chapterCount }
    '''

    def __init__(self, messenger, account, userconfig):
        super(libkitsu_graphql, self).__init__(messenger, account, userconfig)

        self.username = account['username']
        self.password = account['password']

        self.opener = urllib.request.build_opener()
        self.opener.addheaders = [
            ('User-Agent',      self.user_agent),
            ('Accept',          'application/json'),
            ('Accept-Encoding', 'gzip'),
            ('Accept-Charset',  'utf-8'),
        ]

    # -- HTTP helpers -----------------------------------------------------

    def _rest_post(self, url, params):
        """Plain form-urlencoded POST, used only for the OAuth token
        endpoint (which isn't part of the GraphQL API)."""
        data = urllib.parse.urlencode(params).encode('utf-8')
        request = urllib.request.Request(url, data)
        request.add_header('Content-Type', 'application/x-www-form-urlencoded')
        try:
            response = self.opener.open(request)
            if response.info().get('content-encoding') == 'gzip':
                body = gzip.GzipFile(fileobj=response).read().decode('utf-8')
            else:
                body = response.read().decode('utf-8')
            return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise utils.APIError("Incorrect credentials.")
            raise utils.APIError("Connection error: %s" % e)
        except urllib.error.URLError as e:
            raise utils.APIError("URL error: %s" % e)
        except socket.timeout:
            raise utils.APIError("Operation timed out.")

    def _gql(self, query, variables=None, auth=False):
        """Runs a GraphQL query/mutation and returns its `data`, raising
        utils.APIError on transport errors or GraphQL-level errors."""
        payload = {'query': query}
        if variables is not None:
            payload['variables'] = variables

        request = urllib.request.Request(
            self.graphql_url, json.dumps(payload).encode('utf-8'))
        request.add_header('Content-Type', 'application/json')

        if auth:
            request.add_header('Authorization', '{0} {1}'.format(
                self._get_userconfig('token_type').capitalize(),
                self._get_userconfig('access_token'),
            ))

        try:
            response = self.opener.open(request)
            if response.info().get('content-encoding') == 'gzip':
                body = gzip.GzipFile(fileobj=response).read().decode('utf-8')
            else:
                body = response.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise utils.APIError("Incorrect credentials.")
            raise utils.APIError("Connection error: %s" % e)
        except urllib.error.URLError as e:
            raise utils.APIError("URL error: %s" % e)
        except socket.timeout:
            raise utils.APIError("Operation timed out.")

        data = json.loads(body)
        if data.get('errors'):
            messages = '; '.join(
                err.get('message', str(err)) for err in data['errors'])
            raise utils.APIError("API error: %s" % messages)
        return data['data']

    # -- Authentication ---------------------------------------------------

    def _request_access_token(self, refresh=False):
        params = {
            'client_id':     self._client_id,
            'client_secret': self._client_secret,
        }

        if refresh:
            self.msg.info('Refreshing access token...')
            params['grant_type'] = 'refresh_token'
            params['refresh_token'] = self._get_userconfig('refresh_token')
        else:
            self.msg.info('Requesting access token...')
            params['grant_type'] = 'password'
            params['username'] = self.username
            params['password'] = self.password

        data = self._rest_post(self.oauth_url, params)

        timestamp = int(time.time())

        self._set_userconfig('access_token',  data['access_token'])
        self._set_userconfig('token_type',    data['token_type'])
        self._set_userconfig('expires',       timestamp + data['expires_in'])
        self._set_userconfig('refresh_token', data['refresh_token'])

        self.logged_in = True
        self._refresh_user_info()
        self._emit_signal('userconfig_changed')

    def _refresh_user_info(self):
        self.msg.info('Refreshing user details...')
        data = self._gql(
            '{ currentAccount { id profile { id name } } }', auth=True)
        account = data['currentAccount']
        self._set_userconfig('userid', account['id'])
        self._set_userconfig('username', account['profile']['name'])

    def check_credentials(self):
        """
        Log into Kitsu. If there isn't an access token, request it, or
        refresh it if necessary.
        """
        timestamp = int(time.time())

        if not self._get_userconfig('access_token'):
            self._request_access_token(False)
        elif (timestamp + 60) > self._get_userconfig('expires'):
            self._request_access_token(True)
        else:
            self.logged_in = True

        return True

    # -- Reading ----------------------------------------------------------

    def _gql_mediatype(self):
        return 'MANGA' if self.mediatype == 'manga' else 'ANIME'

    def fetch_list(self):
        """Queries the full library from the remote server."""
        self.check_credentials()
        self.msg.info('Downloading list...')

        query = '''
        query ($first: Int!, $after: String, $mediaType: MediaTypeEnum!) {
          currentProfile {
            library {
              all(first: $first, after: $after, mediaType: $mediaType) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  rating
                  status
                  progress
                  startedAt
                  finishedAt
                  updatedAt
                  media { %s }
                }
              }
            }
          }
        }''' % self._media_fields(None, include_description=False)

        showlist = {}
        infolist = []
        after = None
        page = 1

        while True:
            self.msg.info('Getting page {}...'.format(page))
            variables = {
                'first': self.library_page_size,
                'after': after,
                'mediaType': self._gql_mediatype(),
            }
            data = self._gql(query, variables, auth=True)
            conn = data['currentProfile']['library']['all']

            for entry in conn['nodes']:
                media = entry['media']
                showid = int(media['id'])

                rating = entry['rating']
                showlist[showid] = utils.show()
                showlist[showid].update({
                    'id': showid,
                    'my_id': entry['id'],
                    'my_progress': entry['progress'] or 0,
                    'my_score': float(rating) / 4.0 if rating else 0.0,
                    'my_status': self._status_from_gql(entry['status']),
                    'my_start_date': self._iso2date(entry['startedAt']),
                    'my_finish_date': self._iso2date(entry['finishedAt']),
                    'my_last_update': self._iso2datetime(entry['updatedAt']),
                })

                infolist.append(self._parse_info(media, partial=True))

            page_info = conn['pageInfo']
            if not page_info['hasNextPage']:
                break
            after = page_info['endCursor']
            page += 1

        # Emitted once at the end with the full accumulated list, not
        # per-page -- the handler (data.py's info_update) does a full disk
        # write of the whole info cache on every call.
        if infolist:
            self._emit_signal('show_info_changed', infolist)

        return showlist

    def request_info(self, item_list):
        self.check_credentials()

        find_field = 'findMangaById' if self.mediatype == 'manga' else 'findAnimeById'
        infolist = []
        # A separate request per missing item pays Kitsu's connection and
        # Cloudflare overhead repeatedly. GraphQL aliases let one operation
        # fetch independent IDs. Keep batches small enough for query limits.
        for offset in range(0, len(item_list), 20):
            batch = item_list[offset:offset + 20]

            def run_batch(enriched):
                fields = self._media_fields(self.mediatype)
                if enriched:
                    fields += self._MEDIA_EXTRA_FIELDS
                variable_defs = []
                selections = []
                variables = {}
                for index, item in enumerate(batch):
                    name = 'media{}'.format(index)
                    variable_defs.append('${}: ID!'.format(name))
                    selections.append(
                        '{}: {}(id: ${}) {{ {} }}'.format(
                            name, find_field, name, fields))
                    variables[name] = str(item['id'])
                query = 'query ({}) {{ {} }}'.format(
                    ', '.join(variable_defs), ' '.join(selections))
                return self._gql(query, variables, auth=True)

            if self._details_enriched:
                try:
                    data = run_batch(True)
                except utils.APIError as e:
                    self.msg.warn("Kitsu rejected details enrichment (%s); "
                                  "falling back to base fields." % e)
                    self._details_enriched = False
                    data = run_batch(False)
            else:
                data = run_batch(False)

            for index in range(len(batch)):
                media = data.get('media{}'.format(index))
                if media:
                    infolist.append(self._parse_info(media))

        self._emit_signal('show_info_changed', infolist)
        return infolist

    def search(self, query_text, method):
        self.check_credentials()
        self.msg.info("Searching for %s..." % query_text)

        search_field = 'searchMangaByTitle' if self.mediatype == 'manga' else 'searchAnimeByTitle'
        query = '''
        query ($title: String!, $first: Int!) {
          %s(title: $title, first: $first) {
            nodes { %s }
          }
        }''' % (search_field, self._media_fields(self.mediatype))

        data = self._gql(query, {'title': query_text, 'first': 20}, auth=True)
        nodes = data[search_field]['nodes']

        infolist = [self._parse_info(media) for media in nodes]
        self._emit_signal('show_info_changed', infolist)

        if not infolist:
            raise utils.APIError('No results.')

        return infolist

    # -- Writing ----------------------------------------------------------

    def add_show(self, item):
        self.check_credentials()
        self.msg.info("Adding show %s..." % item['title'])

        input_fields = {
            'mediaId': str(item['id']),
            'mediaType': self._gql_mediatype(),
        }
        self._apply_entry_fields(input_fields, item)

        query = '''
        mutation ($input: LibraryEntryCreateInput!) {
          libraryEntry {
            create(input: $input) {
              libraryEntry { id }
              errors { ... on GenericError { message } }
            }
          }
        }'''
        data = self._gql(query, {'input': input_fields}, auth=True)
        payload = data['libraryEntry']['create']
        self._check_mutation_errors(payload, 'adding')
        return int(payload['libraryEntry']['id'])

    def update_show(self, item):
        self.check_credentials()
        self.msg.info("Updating show %s..." % item['title'])

        input_fields = {'id': str(item['my_id'])}
        self._apply_entry_fields(input_fields, item)

        query = '''
        mutation ($input: LibraryEntryUpdateInput!) {
          libraryEntry {
            update(input: $input) {
              libraryEntry { updatedAt }
              errors { ... on GenericError { message } }
            }
          }
        }'''
        data = self._gql(query, {'input': input_fields}, auth=True)
        payload = data['libraryEntry']['update']
        self._check_mutation_errors(payload, 'updating')
        return self._iso2datetime(payload['libraryEntry']['updatedAt'])

    def delete_show(self, item):
        self.check_credentials()
        self.msg.info("Deleting show %s..." % item['title'])

        query = '''
        mutation ($input: GenericDeleteInput!) {
          libraryEntry {
            delete(input: $input) {
              errors { ... on GenericError { message } }
            }
          }
        }'''
        data = self._gql(
            query, {'input': {'id': str(item['my_id'])}}, auth=True)
        self._check_mutation_errors(data['libraryEntry']['delete'], 'deleting')

    def _apply_entry_fields(self, input_fields, item):
        """Copies the mutable library-entry fields from a Hakubun+ item
        into a GraphQL input dict, matching the REST backend's behaviour
        of only syncing progress/status/rating."""
        if 'my_progress' in item:
            input_fields['progress'] = item['my_progress']
        if 'my_status' in item:
            input_fields['status'] = self._status_to_gql(item['my_status'])
        if 'my_score' in item:
            # Same 1-20 scale as the REST ratingTwenty field; 0 clears it.
            input_fields['rating'] = int(item['my_score'] * 4) or None

    def _check_mutation_errors(self, payload, action):
        errors = payload.get('errors')
        if errors:
            messages = '; '.join(
                e.get('message', str(e)) for e in errors)
            raise utils.APIError('Error %s: %s' % (action, messages))

    def merge(self, show, info):
        show['title'] = info['title']
        show['aliases'] = info['aliases']
        show['url'] = info['url']
        show['total'] = info['total']
        show['image'] = info['image']
        show['image_thumb'] = info['image_thumb']
        show['start_date'] = info['start_date']
        show['end_date'] = info['end_date']
        show['status'] = info['status']
        show['type'] = info['type']
        show['platform_score'] = info['platform_score']
        show['duration'] = info.get('duration')
        # Carry the cross-referenced MAL id through so the engine's MAL
        # score feature works for Kitsu accounts too.
        show['mal_id'] = info.get('mal_id')

    # -- Parsing helpers --------------------------------------------------

    def _status_from_gql(self, status):
        # LibraryEntryStatusEnum (CURRENT, ON_HOLD, ...) -> Hakubun+'s
        # lowercase status keys (current, on_hold, ...).
        return status.lower()

    def _status_to_gql(self, status):
        return status.upper()

    @staticmethod
    def _category_titles(categories):
        # Category.title is a locale->string map, e.g. {"en": "Action"};
        # prefer English, fall back to whatever locale is present.
        nodes = (categories or {}).get('nodes') or []
        names = []
        for node in nodes:
            title = node.get('title') or {}
            name = title.get('en') or next(iter(title.values()), None)
            if name:
                names.append(name)
        return names

    @staticmethod
    def _production_studios(productions):
        # MediaProduction.role is one of PRODUCER/LICENSOR/STUDIO/
        # SERIALIZATION; only STUDIO entries are the animation studios.
        nodes = (productions or {}).get('nodes') or []
        return [n['company']['name'] for n in nodes
                if n.get('role') == 'STUDIO' and n.get('company')]

    @staticmethod
    def _season_from_date(start_date):
        # Kitsu has no seasonal-anime field, so derive the standard airing
        # season (Winter/Spring/Summer/Fall) from the start month, matching
        # how AniList labels it. start_date is an ISO 'YYYY-MM-DD' string.
        if not start_date:
            return None
        try:
            parts = start_date.split('-')
            year, month = parts[0], int(parts[1])
        except (ValueError, IndexError, AttributeError):
            return None
        season = {1: 'Winter', 2: 'Winter', 3: 'Winter',
                  4: 'Spring', 5: 'Spring', 6: 'Spring',
                  7: 'Summer', 8: 'Summer', 9: 'Summer',
                  10: 'Fall', 11: 'Fall', 12: 'Fall'}.get(month)
        return '%s %s' % (season, year) if season else year

    def _parse_info(self, media, partial=False):
        info = utils.show()

        subtype = media.get('subtype')
        total = media.get('episodeCount') or media.get('chapterCount') or 0

        titles = media.get('titles') or {}
        title = (titles.get('canonical') or titles.get('preferred')
                 or titles.get('romanized') or '')

        # canonical/preferred/romanized are frequently all the same
        # Japanese romanization (e.g. "Kusuriya no Hitorigoto 2nd
        # Season") with no English title anywhere among them -- without
        # `translated`, the tracker can never match a release using the
        # English title, which is what most fansub/encode groups
        # actually name files.
        aliases = list(filter(None, [
            titles.get('canonical'),
            titles.get('preferred'),
            titles.get('romanized'),
            titles.get('translated'),
        ]))
        # De-duplicate while preserving order.
        aliases = list(dict.fromkeys(aliases))

        image = self._image_by_names(
            media.get('posterImage'), ('small', 'medium', 'large', 'original'))
        image_thumb = self._image_by_names(
            media.get('posterImage'), ('tiny', 'small'))

        description = utils.clean_synopsis(
            self._pick_description(media.get('description')))
        average = media.get('averageRating')
        status = self.status_translate.get(
            media.get('status'), utils.Status.UNKNOWN)

        # Mirror AniList's details layout. Kitsu's title fields don't map
        # cleanly to English/Romaji/Japanese, but `translated` is the
        # English title and `romanized` (or canonical) the romaji; the rest
        # become synonyms. Genres/studios come from the enriched detail
        # request (categories/productions) and are simply absent -- and so
        # filtered out of the view -- on bulk/search entries.
        english = titles.get('translated')
        romaji = titles.get('romanized') or titles.get('canonical')
        shown = set(filter(None, (title, english, romaji)))
        synonyms = [a for a in aliases if a not in shown]

        genres = self._category_titles(media.get('categories'))
        studios = self._production_studios(media.get('productions'))
        season = self._season_from_date(media.get('startDate'))
        platform_score = '%.2f%%' % float(average) if average else None

        age_rating = None
        if media.get('ageRating'):
            guide = media.get('ageRatingGuide')
            age_rating = ('%s (%s)' % (media['ageRating'], guide)
                          if guide else media['ageRating'])

        info.update({
            'id':          int(media['id']),
            'title':       title,
            'total':       total,
            'image':       image,
            'image_thumb': image_thumb,
            'start_date':  self._str2date(media.get('startDate')),
            'end_date':    self._str2date(media.get('endDate')),
            'type':        self.type_translate.get(
                (subtype or '').upper(), utils.Type.UNKNOWN),
            'status':      status,
            'platform_score': platform_score,
            'duration':    media.get('episodeLength'),
            'mal_id':      self._mal_id_from_mappings(media.get('mappings')),
            'url': "https://kitsu.app/{}/{}".format(
                self.mediatype, media.get('slug')),
            'aliases':     aliases,
            'extra': [
                ('English',     english),
                ('Romaji',      romaji),
                ('Synonyms',    synonyms),
                ('Season',      season),
                ('Genres',      genres),
                ('Studios',     studios),
                ('Synopsis',    description),
                ('Type',        subtype),
                ('Score',       platform_score),
                ('Age Rating',  age_rating),
                ('Status',      status),
            ],
        })

        # WORKAROUND: Shows with 1 episode (movies, specials, OVAs) end
        # the same day they start.
        if total == 1:
            info['end_date'] = info['start_date']

        if media.get('status') in ('UPCOMING', 'UNRELEASED', 'TBA'):
            info['extra'].append(('Expected Release', media.get('tba', '?')))

        # Bulk library fetches omit the (often large) description field --
        # see _media_fields(). Flag those entries so data.py knows to
        # transparently fetch the full details the first time they're
        # actually needed, instead of caching a permanently-blank synopsis.
        if partial:
            info['_details_pending'] = True

        return info

    def _mal_id_from_mappings(self, mappings):
        if not mappings:
            return None
        for node in mappings.get('nodes', []):
            if node.get('externalSite') in self._mal_sites:
                try:
                    return int(node['externalId'])
                except (ValueError, TypeError):
                    return None
        return None

    def _image_by_names(self, poster_image, names):
        """Picks the first available poster image URL by view name, in
        preference order."""
        if not poster_image:
            return ''
        by_name = {v['name']: v['url']
                   for v in (poster_image.get('views') or []) if v.get('url')}
        for name in names:
            if name in by_name:
                return by_name[name]
        return ''

    def _pick_description(self, description):
        if not description:
            return ''
        for locale in ('en', 'en_us', 'en_jp'):
            if description.get(locale):
                return description[locale]
        # Fall back to whatever locale is present.
        for value in description.values():
            if value:
                return value
        return ''

    def _str2date(self, string):
        if not string:
            return None
        try:
            return datetime.datetime.strptime(string, "%Y-%m-%d")
        except Exception:
            self.msg.debug('Invalid date {}'.format(string))
            return None

    def _iso2date(self, string):
        dt = self._iso2datetime(string)
        return dt.date() if dt else None

    def _iso2datetime(self, string):
        if not string:
            return None
        try:
            return datetime.datetime.fromisoformat(string.replace('Z', '+00:00'))
        except Exception:
            self.msg.debug('Invalid datetime {}'.format(string))
            return None
