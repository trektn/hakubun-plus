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
    assert normalize.provider_score(8.5, 10, 1) == 9        # half up
    assert normalize.provider_score(2.5, 10, 1) == 3        # half up, not 2
    assert normalize.provider_score(8.4, 100, 1) == 84      # AniList
    assert normalize.provider_score(8.4, 5, 0.5) == 4.0     # Kitsu half-star
    assert normalize.provider_score(8.5, 5, 0.5) == 4.5     # lands on-grid
    # Every value Kitsu's half-star grid can hold must survive its wire
    # format: ratingTwenty = my_score * 4 and the API rejects odd values.
    for canonical in (x / 10 for x in range(0, 101)):
        raw = normalize.provider_score(canonical, 5, 0.5)
        assert (raw * 4) % 2 == 0, (canonical, raw)
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


def test_normalize_title_keeps_non_latin_scripts():
    # An earlier latin-only regex reduced CJK titles to '' -- matching
    # was silently dead for Native-title AniList users.
    assert normalize.normalize_title('葬送のフリーレン') == '葬送のフリーレン'
    assert normalize.normalize_title('「葬送のフリーレン」') == '葬送のフリーレン'
    # NFKC unifies full-width forms.
    assert normalize.normalize_title('ＦＲＩＥＲＥＮ') == 'frieren'


def test_provider_date_inverse_of_canonical():
    import datetime
    assert normalize.provider_date('2026-07-14') == datetime.date(2026, 7, 14)
    assert normalize.provider_date(None) is None
    # date/datetime objects pass through untouched.
    d = datetime.date(2024, 1, 2)
    assert normalize.provider_date(d) is d
    # Garbage degrades to None (clear the date), never a crash.
    assert normalize.provider_date('not a date') is None
    # Full roundtrip both ways.
    assert normalize.canonical_date(
        normalize.provider_date('2026-07-14')) == '2026-07-14'
