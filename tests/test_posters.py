"""The shared cover-art cache behind both Mirror grids.

Cover art is decoration: the properties worth pinning are the ones that
keep it from becoming anything more than that -- it must never block a
preview, never re-fetch what it already has, and never retry a dead URL
for as long as the window is open.
"""

import os
import threading

import pytest

from hakubun import utils
from hakubun.ui import posters


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, 'to_cache_path',
                        lambda *parts: os.path.join(str(tmp_path), *parts))
    return tmp_path


def test_a_url_always_lands_in_the_same_place(cache):
    """The same work can be drawn from whichever provider happened to
    ship the art, so the URL is the only thing identifying a picture."""
    first = posters.cache_path('https://example.invalid/a.jpg')
    assert first == posters.cache_path('https://example.invalid/a.jpg')
    assert first != posters.cache_path('https://example.invalid/b.jpg')
    assert first.startswith(str(cache))


def test_a_cached_cover_is_returned_without_a_download(cache):
    """The common case after the first preview: no thread, no network,
    no waiting."""
    url = 'https://example.invalid/a.jpg'
    path = posters.cache_path(url)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(b'not really a jpeg, but it is on disk')

    served = []
    store = posters.PosterCache(lambda u, p: served.append(u))
    assert store.get(url) == path
    assert store._threads == [], 'nothing should have been downloaded'
    assert served == []


def test_a_dead_url_is_asked_for_once(cache, monkeypatch):
    """A grid rebuilds on every preview. Without remembering the
    failure, a URL that will never work is retried on each one."""
    url = 'https://example.invalid/gone.jpg'
    tries = []
    attempted = threading.Event()

    def failing(self, requested):
        tries.append(requested)
        attempted.set()
        return None

    monkeypatch.setattr(posters.PosterCache, '_download', failing)

    store = posters.PosterCache(lambda u, p: None)
    try:
        assert store.get(url) is None
        assert attempted.wait(timeout=5)

        # Asked for again -- as a rebuilt grid would -- the failure is
        # remembered, and nothing claims a cover exists either.
        assert store.get(url) is None
        assert tries == [url]
    finally:
        store.stop()


def test_nothing_is_attempted_without_an_imaging_library(cache,
                                                         monkeypatch):
    """Pillow is optional. Without it there are no posters, and that is
    a grid of titles -- not a stack of exceptions."""
    monkeypatch.setattr(posters, 'imaging_available', False)
    store = posters.PosterCache(lambda u, p: None)
    assert store.get('https://example.invalid/a.jpg') is None
    assert store._threads == []
