"""Provider score formats: AniList's five incompatible scales (0-3
smileys, 1-5 stars, 1-10, 1-10 decimal, 1-100) are only known once the
account's live format is detected -- and can change after that. A
frozen mediainfo snapshot silently keeps using whatever was true (or
just the class default) before that detection ever happened.
"""

from hakubun.sync import normalize
from hakubun.sync.adapters import ProviderAdapter
from hakubun.sync.engine import SyncEngine

from conftest import FakeLib, show


class DriftingFormatLib(FakeLib):
    """Mimics libanilist: media_info() starts at the class default
    (POINT_100, 100/1) and only reflects the account's REAL format
    (here POINT_10) after fetch_list() has actually talked to the
    'server' -- exactly like _apply_scoreformat mutating the shared
    mediatypes dict partway through a real fetch_list() call."""

    def __init__(self, shows):
        super().__init__('anilist', shows)
        self._info = {'mediatype': 'anime', 'score_max': 100,
                      'score_step': 1, 'can_score': True,
                      'can_status': True, 'can_update': True,
                      'can_date': True, 'can_tag': True,
                      'statuses_dict': {'CURRENT': 'Watching',
                                        'COMPLETED': 'Completed'}}

    def media_info(self):
        return dict(self._info)

    def fetch_list(self):
        shows = super().fetch_list()
        # The account's real format is discovered here, same point in
        # the real fetch_list() where the live query result arrives.
        self._info['score_max'] = 10
        self._info['score_step'] = 1
        return shows


def test_mediainfo_is_read_live_not_snapshotted():
    lib = DriftingFormatLib([show('anilist', 1, 'Bebop')])
    adapter = ProviderAdapter('anilist', lib)
    assert adapter.mediainfo['score_max'] == 100   # stale class default
    adapter.fetch()
    assert adapter.mediainfo['score_max'] == 10    # now reflects reality
    assert adapter.mediainfo['score_step'] == 1


def test_score_normalized_correctly_once_real_format_is_known(store):
    """The account's AniList score is POINT_10 (raw '7' means '7 out of
    10'). A stale 100/1 snapshot taken before the first fetch would
    misread that same raw '7' as 7/100 -- canonical 0.7, wildly wrong.
    After a real fetch, the SAME entity's canonical score must reflect
    the account's true format."""
    lib = DriftingFormatLib([show('anilist', 1, 'Bebop', score=7)])
    adapter = ProviderAdapter('anilist', lib)
    eng = SyncEngine(store, {'anilist': adapter})
    assert eng.fetch() == {}
    uid = store.entities()[0]['uuid']
    # Canonical must read as 7.0 (POINT_10 raw 7 -> 7/10*10), not 0.7
    # (what a frozen POINT_100 snapshot would have produced).
    assert store.local_get(uid)['score'][0] == 7.0


def test_push_projects_into_the_true_live_format():
    """Pushing a canonical 6.5 to an AniList account whose real format
    turns out to be POINT_10 (integer only) must round through THAT
    scale (6.5 -> 7, rounding half up), not carry forward whatever the
    stale 100/1 default would have computed (65)."""
    lib = DriftingFormatLib([show('anilist', 1, 'Bebop')])
    adapter = ProviderAdapter('anilist', lib)
    adapter.fetch()   # discovers the real format, as a real sync would
    sent = adapter.push('1', {'score': 6.5})
    assert lib.updates[-1]['my_score'] == 7          # POINT_10, half up
    assert sent['score'] == 7.0


def test_all_five_anilist_formats_round_trip():
    # Expected values are hand-computed for HALF-UP rounding (a value
    # exactly on a step boundary goes to the larger step).
    cases = [
        ('POINT_3', 3, 1, 6.5, 2),         # 1.95 -> 2
        ('POINT_5', 5, 1, 6.5, 3),         # 3.25 -> 3
        ('POINT_10', 10, 1, 6.5, 7),       # 6.5 rounds half up (7)
        ('POINT_10_DECIMAL', 10, 0.1, 6.53, 6.5),
        ('POINT_100', 100, 1, 6.5, 65),
    ]
    for name, smax, step, canonical, expected_raw in cases:
        raw = normalize.provider_score(canonical, smax, step)
        assert raw == expected_raw, (name, raw, expected_raw)
        # And it must round-trip back to a sane canonical value.
        back = normalize.canonical_score(raw, smax)
        assert 0 <= back <= 10
