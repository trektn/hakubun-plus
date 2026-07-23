"""UI-agnostic bridge: list overlay + owner-system score write."""

from hakubun.sync import uibridge
from hakubun.sync.models import FieldPolicy, PolicyKind
from hakubun.sync.store import SyncStore

from conftest import MEDIAINFO


def _seed_shared(db):
    """A shared entity (Kitsu 77 + AniList 9), Score owned by AniList,
    reconciled score 8.4 -- mirrors the overlay/editor scenario."""
    store = SyncStore(db)
    uid = store.create_entity('Bebop')
    store.add_mapping(uid, 'kitsu', '77')
    store.add_mapping(uid, 'anilist', '9')
    store.set_ownership('score', FieldPolicy(PolicyKind.PROVIDER, 'anilist'))
    store.local_set(uid, 'score', 8.4)
    store.close()
    return uid


def _anilist_decimal():
    mi = dict(MEDIAINFO['anilist'])
    mi['score_max'], mi['score_step'] = 10, 0.1
    return mi


def test_build_list_overlay_owner_system(tmp_path, monkeypatch):
    db = str(tmp_path / 'multisync-anime.db')
    _seed_shared(db)
    monkeypatch.setattr(uibridge, 'store_path', lambda mt: db)

    class FakeAdapter:
        def __init__(self, mi):
            self.mediainfo = mi

    def fake_adapter_from_account(account, msg, media_type=None):
        return FakeAdapter(_anilist_decimal() if account['api'] == 'anilist'
                           else MEDIAINFO['kitsu'])
    monkeypatch.setattr(uibridge, 'adapter_from_account',
                        fake_adapter_from_account)

    accounts = [(0, {'api': 'kitsu'}), (1, {'api': 'anilist'})]
    overlay, pmi = uibridge.build_list_overlay(
        accounts, 'kitsu', MEDIAINFO['kitsu'], 'anime', msg=None)
    # Signed into Kitsu, the AniList-owned score shows in AniList's system.
    assert overlay[77]['_score_display'] == 8.4
    assert overlay[77]['_score_owner'] == 'anilist'
    assert pmi['anilist']['score_max'] == 10


def test_build_list_overlay_absent_db(tmp_path, monkeypatch):
    monkeypatch.setattr(uibridge, 'store_path',
                        lambda mt: str(tmp_path / 'nope.db'))
    overlay, pmi = uibridge.build_list_overlay(
        [], 'kitsu', MEDIAINFO['kitsu'], 'anime', msg=None)
    assert overlay == {} and pmi == {}


def test_write_owned_score_to_local(tmp_path, monkeypatch):
    db = str(tmp_path / 'multisync-anime.db')
    uid = _seed_shared(db)
    monkeypatch.setattr(uibridge, 'store_path', lambda mt: db)

    # Rate 9.0 in AniList's decimal system while "signed into Kitsu".
    ok = uibridge.write_owned_score('anime', 'kitsu', 77, 9.0,
                                    _anilist_decimal())
    assert ok is True
    store = SyncStore(db)
    try:
        # canonical_score(9.0, score_max=10) == 9.0
        assert store.local_get(uid)['score'][0] == 9.0
        # Base seeded for each provider -> the edit is a clean push.
        assert 'score' in store.base_get(uid, 'anilist')
        assert 'score' in store.base_get(uid, 'kitsu')
    finally:
        store.close()


def test_write_owned_score_no_mapping(tmp_path, monkeypatch):
    db = str(tmp_path / 'multisync-anime.db')
    _seed_shared(db)
    monkeypatch.setattr(uibridge, 'store_path', lambda mt: db)
    # An id this account doesn't map -> fall back (False), nothing written.
    assert uibridge.write_owned_score('anime', 'kitsu', 999, 9.0,
                                      _anilist_decimal()) is False
