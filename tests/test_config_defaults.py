"""Every setting a Preferences window reads must have a default.

Both windows read most of their settings by subscript --
`self.config['remember_geometry']` -- which is a KeyError, not a
fallback, for anyone whose config file predates the setting. And it
fires while BUILDING the window, so one missing default takes
Preferences away entirely rather than degrading one checkbox.

The toolkits keep separate default sets (`gtk_defaults`,
`qt_defaults`), so a setting added to one and read by both is the easy
version of this mistake -- which is exactly how `show_subminer_toggle`
crashed GTK's Preferences while Qt's was fine.
"""

import re

import pytest

from hakubun import utils

# `self.config['x']`, `config['x']`, `self._config['x']` -- the forms
# the windows actually use. Deliberately not `.get('x', default)`:
# that one is safe, and is how the non-settings code reads these.
_SUBSCRIPT = re.compile(r"(?:self\.)?_?config\['([a-z_0-9]+)'\]")


def _keys_read(path):
    with open(path) as handle:
        return set(_SUBSCRIPT.findall(handle.read()))


@pytest.mark.parametrize('module,defaults', [
    ('hakubun/ui/gtk/settingswindow.py', 'gtk_defaults'),
    ('hakubun/ui/qt/settings.py', 'qt_defaults'),
])
def test_every_setting_read_by_name_has_a_default(module, defaults):
    known = set(getattr(utils, defaults)) | set(utils.config_defaults)
    missing = sorted(_keys_read(module) - known)
    assert not missing, (
        '%s reads %s, which is in neither %s nor config_defaults -- '
        'opening Preferences would raise KeyError'
        % (module, ', '.join(missing), defaults))


def test_the_toolkits_agree_about_shared_settings():
    """A setting both windows offer must mean the same thing in both.

    Not that the sets are equal -- each toolkit has settings the other
    has no widget for -- but that where they overlap, they agree on the
    default. Two Preferences windows disagreeing about what 'off' means
    is a bug the user experiences as the setting changing itself.
    """
    shared = set(utils.gtk_defaults) & set(utils.qt_defaults)
    differing = {k: (utils.gtk_defaults[k], utils.qt_defaults[k])
                 for k in shared
                 # Colors are deliberately per-toolkit palettes, and
                 # geometry is a remembered value, not a preference.
                 if k not in ('colors', 'last_width', 'last_height')
                 and utils.gtk_defaults[k] != utils.qt_defaults[k]}
    assert not differing, differing
