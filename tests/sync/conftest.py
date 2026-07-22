"""Shared fixtures: fake provider libs behind the real adapters."""

import pytest

from hakubun import utils
from hakubun.sync.adapters import ProviderAdapter
from hakubun.sync.engine import SyncEngine
from hakubun.sync.store import SyncStore

MEDIAINFO = {
    'mal': {'mediatype': 'anime', 'score_max': 10, 'score_step': 1,
            'can_score': True, 'can_status': True, 'can_update': True,
            'can_date': True, 'can_tag': False,
            'statuses_dict': {'watching': 'Watching',
                              'completed': 'Completed',
                              'on_hold': 'On hold',
                              'dropped': 'Dropped',
                              'plan_to_watch': 'Plan to Watch'}},
    'anilist': {'mediatype': 'anime', 'score_max': 100, 'score_step': 1,
                'can_score': True, 'can_status': True, 'can_update': True,
                'can_date': True, 'can_tag': True,
                'statuses_dict': {'CURRENT': 'Watching',
                                  'COMPLETED': 'Completed',
                                  'PAUSED': 'Paused',
                                  'DROPPED': 'Dropped',
                                  'PLANNING': 'Planning'}},
    'kitsu': {'mediatype': 'anime', 'score_max': 5, 'score_step': 0.25,
              'can_score': True, 'can_status': True, 'can_update': True,
              'can_date': True, 'can_tag': False,
              'statuses_dict': {'current': 'Currently Watching',
                                'completed': 'Completed',
                                'on_hold': 'On Hold',
                                'dropped': 'Dropped',
                                'planned': 'Want to Watch'}},
}


class FakeLib:
    """Duck-typed lib/ instance: in-memory shows, failure injection."""

    def __init__(self, provider, shows=None):
        self.provider = provider
        self.shows = {str(s['id']): dict(s) for s in (shows or [])}
        self.updates = []          # update_show items received
        self.fail_fetch = False
        self.fail_update = False

    def media_info(self):
        return dict(MEDIAINFO[self.provider])

    def check_credentials(self):
        if self.fail_fetch:
            raise utils.APIError('%s is down' % self.provider)
        return True

    def fetch_list(self):
        if self.fail_fetch:
            raise utils.APIError('%s is down' % self.provider)
        return {k: dict(v) for k, v in self.shows.items()}

    def update_show(self, item):
        if self.fail_update:
            raise utils.APIError('%s rejected the update' % self.provider)
        # Every real lib (libmal, libanilist, libkitsu, ...) does
        # exactly this -- item['title'] for a log line, unconditional.
        # A minimal {id, my_*} patch dict with no title crashes here
        # with a bare KeyError('title'); replicate that so a caller
        # that forgets to supply one is actually caught by the suite.
        _ = item['title']
        self.updates.append(dict(item))
        self.shows.setdefault(str(item['id']), {'id': item['id']})
        self.shows[str(item['id'])].update(item)

    def search(self, criteria, method=None):
        return [dict(s) for s in self.shows.values()
                if criteria.lower() in s.get('title', '').lower()]

    def logout(self):
        pass


def show(provider, sid, title, progress=0, score=0, status=None,
         total=12, airing=1, **extra):
    """Provider-scale show dict in trackma shape."""
    defaults = {'mal': 'watching', 'anilist': 'CURRENT', 'kitsu': 'current'}
    return {'id': sid, 'title': title, 'my_progress': progress,
            'my_score': score, 'my_status': status or defaults[provider],
            'my_start_date': None, 'my_finish_date': None,
            'total': total, 'status': airing, **extra}


@pytest.fixture
def store():
    s = SyncStore(':memory:')
    yield s
    s.close()


def make_engine(store, libs):
    adapters = {name: ProviderAdapter(name, lib)
                for name, lib in libs.items()}
    return SyncEngine(store, adapters)
