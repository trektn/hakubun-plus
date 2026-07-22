"""Normalization: score scales/rounding, statuses, unknowns."""

from hakubun.sync import normalize
from hakubun.sync.models import NormalizedEntry

from conftest import MEDIAINFO, show


def test_score_to_canonical():
    assert normalize.canonical_score(7, 10) == 7.0
    assert normalize.canonical_score(84, 100) == 8.4
    assert normalize.canonical_score(4.25, 5) == 8.5
    assert normalize.canonical_score(0, 10) is None      # unrated
    assert normalize.canonical_score(None, 10) is None


def test_score_to_provider_rounds():
    # "MAL and Kitsu will be rounded"
    assert normalize.provider_score(8.4, 10, 1) == 8        # MAL
    assert normalize.provider_score(8.5, 10, 1) == 8 or \
           normalize.provider_score(8.5, 10, 1) == 9        # banker's ok
    assert normalize.provider_score(8.4, 100, 1) == 84      # AniList
    assert normalize.provider_score(8.4, 5, 0.25) == 4.25   # Kitsu quarter
    assert normalize.provider_score(None, 10, 1) == 0
    assert normalize.provider_score(11, 10, 1) == 10        # clamped


def test_status_roundtrip_all_providers():
    for provider, info in MEDIAINFO.items():
        sd = info['statuses_dict']
        seen = set()
        for key in sd:
            canon = normalize.canonical_status(key, sd)
            assert canon is not None, (provider, key)
            seen.add(canon)
            assert normalize.provider_status(canon, sd) is not None
        assert seen == {'watching', 'completed', 'on_hold',
                        'dropped', 'plan'}


def test_normalize_show_unknown_total_stays_none():
    s = show('mal', 1, 'Airing', total=None)
    entry = normalize.normalize_show('mal', s, MEDIAINFO['mal'])
    assert entry.total is None            # unknown, not 0


def test_normalize_show_builds_canonical_user_state():
    s = show('anilist', 9, 'Bebop', progress=13, score=84,
             my_notes='hi', my_tags='space, jazz')
    entry = normalize.normalize_show('anilist', s, MEDIAINFO['anilist'],
                                     {'mal': 1})
    assert isinstance(entry, NormalizedEntry)
    assert entry.user['score'] == 8.4
    assert entry.user['progress'] == 13
    assert entry.user['status'] == 'watching'
    assert entry.user['notes'] == 'hi'
    assert entry.user['tags'] == ['jazz', 'space']
    assert entry.external_ids == {'mal': '1'}


def test_normalize_title_for_matching():
    assert normalize.normalize_title('Ghost in the SHELL!') == \
        normalize.normalize_title('ghost in the shell')
    assert normalize.normalize_title('Re:ZERO -Starting Life-') == \
        normalize.normalize_title('re zero starting life')
