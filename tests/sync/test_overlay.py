"""Overlay builder: reconciled local state -> active-provider my_* map."""

import datetime

from hakubun.sync.adapters import ProviderAdapter
from hakubun.sync.engine import SyncEngine
from hakubun.sync.overlay import build_overlay
from hakubun.sync.models import FieldPolicy, PolicyKind

from conftest import MEDIAINFO, FakeLib, show


def _engine(store, libs):
    return SyncEngine(store, {n: ProviderAdapter(n, l)
                              for n, l in libs.items()})


def test_overlay_surfaces_owned_value_of_another_provider(store):
    """Signed into Kitsu, score owned by AniList: the overlay hands the
    Kitsu list AniList's rating (converted to Kitsu's scale) for the
    Kitsu show id."""
    libs = {'kitsu': FakeLib('kitsu', [show('kitsu', 77, 'Bebop', mal_id=1,
                                            score=0)]),
            'anilist': FakeLib('anilist', [show('anilist', 9, 'Bebop',
                                               mal_id=1, score=0)])}
    eng = _engine(store, libs)
    eng.fetch()
    uid = store.entities()[0]['uuid']
    # AniList owns score; its score is 8.0 canonical, local reflects it.
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'anilist'))
    eng.edit_local(uid, 'score', 8.0, source='anilist')

    overlay = build_overlay(store, 'kitsu', MEDIAINFO['kitsu'])
    assert 77 in overlay
    # 8.0 canonical -> Kitsu 0-5 scale = 4.0.
    assert overlay[77]['my_score'] == 4.0
    # Both string and int ids present for lookup safety.
    assert '77' in overlay


def test_overlay_skips_fields_the_account_already_matches(store):
    """A field whose reconciled value equals what the active account
    reports adds nothing -- no needless override/cue."""
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Bebop', progress=5)])}
    eng = _engine(store, libs)
    eng.fetch()
    overlay = build_overlay(store, 'mal', MEDIAINFO['mal'])
    # Everything already agrees with MAL (the only provider) -> empty.
    assert overlay == {}


def test_overlay_only_covers_requested_shows(store):
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'A', progress=1),
                                   show('mal', 2, 'B', progress=1)]),
            'anilist': FakeLib('anilist', [show('anilist', 9, 'A', mal_id=1,
                                               progress=1),
                                          show('anilist', 8, 'B', mal_id=2,
                                               progress=1)])}
    eng = _engine(store, libs)
    eng.fetch()
    for e in store.entities():
        eng.edit_local(e['uuid'], 'progress', 12, source='anilist')
    only_1 = build_overlay(store, 'mal', MEDIAINFO['mal'], show_ids=[1])
    assert set(k for k in only_1 if isinstance(k, int)) == {1}


def test_overlay_status_and_dates_convert_to_active_provider(store):
    libs = {'mal': FakeLib('mal', [show('mal', 1, 'Bebop', status='watching')]),
            'anilist': FakeLib('anilist', [show('anilist', 9, 'Bebop',
                                               mal_id=1, status='CURRENT')])}
    eng = _engine(store, libs)
    eng.fetch()
    uid = store.entities()[0]['uuid']
    eng.edit_local(uid, 'status', 'completed', source='anilist')
    eng.edit_local(uid, 'start_date', '2026-07-14', source='anilist')
    overlay = build_overlay(store, 'mal', MEDIAINFO['mal'])
    # canonical 'completed' -> MAL's own status key.
    assert overlay[1]['my_status'] in MEDIAINFO['mal']['statuses_dict']
    assert isinstance(overlay[1]['my_start_date'], datetime.date)


def test_score_shown_in_owners_rating_system(store):
    """Signed into Kitsu, Score owned by AniList: the Kitsu list shows
    the reconciled score in AniList's system (decimal 8.4), not Kitsu's
    rounding -- with provider_mediainfo supplying AniList's format."""
    libs = {'kitsu': FakeLib('kitsu', [show('kitsu', 77, 'Bebop', mal_id=1)]),
            'anilist': FakeLib('anilist', [show('anilist', 9, 'Bebop',
                                               mal_id=1)])}
    eng = _engine(store, libs)
    eng.fetch()
    uid = store.entities()[0]['uuid']
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'anilist'))
    eng.edit_local(uid, 'score', 8.4, source='anilist')

    # AniList in POINT_10_DECIMAL: 10/0.1, factor 1 -> displays 8.4.
    anilist_mi = dict(MEDIAINFO['anilist'])
    anilist_mi['score_max'] = 10
    anilist_mi['score_step'] = 0.1
    overlay = build_overlay(store, 'kitsu', MEDIAINFO['kitsu'],
                            provider_mediainfo={'anilist': anilist_mi,
                                                'kitsu': MEDIAINFO['kitsu']})
    assert overlay[77]['_score_display'] == 8.4
    assert overlay[77]['_score_owner'] == 'anilist'


def test_score_owner_display_absent_without_mediainfo(store):
    """No provider_mediainfo -> no owner-system score display (falls
    back to the account's own rendering)."""
    libs = {'kitsu': FakeLib('kitsu', [show('kitsu', 77, 'Bebop', mal_id=1)]),
            'anilist': FakeLib('anilist', [show('anilist', 9, 'Bebop',
                                               mal_id=1)])}
    eng = _engine(store, libs)
    eng.fetch()
    uid = store.entities()[0]['uuid']
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'anilist'))
    eng.edit_local(uid, 'score', 8.4, source='anilist')
    overlay = build_overlay(store, 'kitsu', MEDIAINFO['kitsu'])
    assert '_score_display' not in overlay.get(77, {})


def test_score_owned_by_active_provider_not_reformatted(store):
    """If the active account itself owns Score, no owner-system
    override (it's already its own system)."""
    libs = {'anilist': FakeLib('anilist', [show('anilist', 9, 'Bebop')])}
    eng = _engine(store, libs)
    eng.fetch()
    uid = store.entities()[0]['uuid']
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'anilist'))
    eng.edit_local(uid, 'score', 8.4, source='anilist')
    overlay = build_overlay(store, 'anilist', MEDIAINFO['anilist'],
                            provider_mediainfo={'anilist': MEDIAINFO['anilist']})
    assert '_score_display' not in overlay.get(9, {})
