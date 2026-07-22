"""adapter_from_account: real userconfig, isolated signals, kitsu swap."""

import json
import os

import pytest

from hakubun import messenger, utils
from hakubun.sync.adapters import adapter_from_account

MSG = messenger.Messenger(None, 'Tests')


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HAKUBUN_HOME', str(tmp_path))
    return tmp_path


def _write_userconfig(home, username, api, **extra):
    folder = home / ('%s.%s' % (username, api))
    folder.mkdir()
    cfg = dict(utils.userconfig_defaults)
    cfg.update({'mediatype': 'anime', **extra})
    (folder / 'user.json').write_text(json.dumps(cfg))
    return folder / 'user.json'


def test_loads_persisted_userconfig_with_tokens(home):
    """The stored user.json (OAuth tokens!) must be what the lib gets --
    a throwaway userconfig made MAL re-redeem a consumed auth code
    (HTTP 400 invalid_grant)."""
    _write_userconfig(home, 'tester', 'anilist', userid=4242)
    account = {'username': 'tester', 'password': 'token', 'api': 'anilist'}
    adapter = adapter_from_account(account, MSG)
    assert adapter.lib.userconfig['userid'] == 4242
    assert adapter.lib.userconfig['mediatype'] == 'anime'


def test_signals_isolated_from_running_app(home):
    """lib.signals is a class attribute; without shadowing, this lib
    would share the app Data instance's connected callbacks and invoke
    them against the wrong lib ("Call to undefined signal" crashes)."""
    _write_userconfig(home, 'tester', 'anilist')
    account = {'username': 'tester', 'password': 'token', 'api': 'anilist'}
    adapter = adapter_from_account(account, MSG)
    lib = adapter.lib

    # Simulate the running app having connected its own callback on the
    # class dict AFTER our adapter exists.
    calls = []
    type(lib).signals['show_info_changed'] = lambda infos: calls.append(1)
    try:
        lib._emit_signal('show_info_changed', [])   # ours: dropped
        assert calls == []
        assert type(lib).signals is not lib.signals
    finally:
        type(lib).signals['show_info_changed'] = None


def test_userconfig_changed_persists_token_refresh(home):
    path = _write_userconfig(home, 'tester', 'anilist')
    account = {'username': 'tester', 'password': 'token', 'api': 'anilist'}
    adapter = adapter_from_account(account, MSG)
    adapter.lib._set_userconfig('access_token', 'refreshed-token')
    adapter.lib._emit_signal('userconfig_changed')
    on_disk = json.loads(path.read_text())
    assert on_disk['access_token'] == 'refreshed-token'


def test_kitsu_backend_follows_app_setting(home):
    _write_userconfig(home, 'tester', 'kitsu')
    account = {'username': 'tester', 'password': 'pw', 'api': 'kitsu'}
    # Default (legacy)
    adapter = adapter_from_account(account, MSG)
    assert type(adapter.lib).__name__ == 'libkitsu'
    # GraphQL selected in the app config
    cfg_path = utils.to_config_path('config.json')
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    cfg = dict(utils.config_defaults)
    cfg['kitsu_api'] = 'graphql'
    with open(cfg_path, 'w') as f:
        json.dump(cfg, f)
    adapter = adapter_from_account(account, MSG)
    assert type(adapter.lib).__name__ == 'libkitsu_graphql'
    assert adapter.name == 'kitsu'


def test_forced_media_type_builds_adapter_for_that_type(home):
    """A Kitsu account stored as MANGA still builds as anime for an
    anime multisync -- otherwise it (and its Ownership column) vanish
    when MAL/AniList is the active account."""
    _write_userconfig(home, 'tester', 'kitsu', mediatype='manga')
    account = {'username': 'tester', 'password': 'pw', 'api': 'kitsu'}
    adapter = adapter_from_account(account, MSG, media_type='anime')
    assert adapter.lib.mediatype == 'anime'
    assert adapter.mediainfo['mediatype'] == 'anime'


def test_forced_media_type_does_not_clobber_stored_mediatype(home):
    """Building an anime adapter from a manga account, then a token
    refresh, must NOT rewrite the account's own user.json mediatype --
    the main app still sees it as manga."""
    import json
    path = _write_userconfig(home, 'tester', 'anilist', mediatype='manga')
    account = {'username': 'tester', 'password': 'token', 'api': 'anilist'}
    adapter = adapter_from_account(account, MSG, media_type='anime')
    assert adapter.lib.mediatype == 'anime'          # this sync: anime
    # A token refresh persists...
    adapter.lib._set_userconfig('access_token', 'refreshed')
    adapter.lib._emit_signal('userconfig_changed')
    on_disk = json.loads(path.read_text())
    assert on_disk['access_token'] == 'refreshed'    # tokens saved
    assert on_disk['mediatype'] == 'manga'           # but type preserved
