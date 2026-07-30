"""Settings' multi-sync Mode used to have a fourth entry, 'Fetch & Plan
only', which forced a choice between picking a reconciliation and
reviewing before it applied. Reviewing is now its own checkbox, and
these pin down that existing configs still mean what they meant."""
import pytest

from hakubun.sync import present
from hakubun.sync.models import SyncMode


@pytest.mark.parametrize('mode_key, expected', [
    ('merge', SyncMode.MERGE),
    ('pull', SyncMode.PULL),
    ('push', SyncMode.MIRROR),
])
def test_each_mode_resolves_to_its_engine_mode(mode_key, expected):
    assert present.settings_sync_mode(
        {'multisync_mode': mode_key, 'multisync_plan_only': False}) \
        == (expected, False)


def test_the_review_checkbox_is_independent_of_the_mode():
    # The whole point of the split: you can now review a pull, which the
    # old dropdown made impossible.
    assert present.settings_sync_mode(
        {'multisync_mode': 'pull', 'multisync_plan_only': True}) \
        == (SyncMode.PULL, True)


def test_a_legacy_plan_only_config_still_means_merge_and_review():
    # What 'plan_only' always did: fetch and plan under Merge, the most
    # conservative reconciliation, and never auto-apply.
    assert present.settings_sync_mode({'multisync_mode': 'plan_only'}) \
        == (SyncMode.MERGE, True)


def test_a_legacy_plan_only_config_ignores_a_stale_checkbox_value():
    # An old config can't have meant "plan_only mode, but auto-apply".
    assert present.settings_sync_mode(
        {'multisync_mode': 'plan_only', 'multisync_plan_only': False}) \
        == (SyncMode.MERGE, True)


def test_an_empty_config_gets_the_safe_beta_posture():
    assert present.settings_sync_mode({}) == (SyncMode.MERGE, True)


def test_an_unrecognized_mode_falls_back_to_merge():
    assert present.settings_sync_mode(
        {'multisync_mode': 'rebase', 'multisync_plan_only': False}) \
        == (SyncMode.MERGE, False)


def test_rebase_is_still_not_reachable_from_settings():
    # Deliberately absent: a one-shot from the sync window, never
    # something the headless Sync button can be configured to do.
    assert SyncMode.REBASE not in present.SETTINGS_MODES.values()


def test_the_defaults_agree_with_the_resolver():
    from hakubun import utils
    for defaults in (utils.gtk_defaults, utils.qt_defaults):
        assert present.settings_sync_mode(defaults) == (SyncMode.MERGE, True)
