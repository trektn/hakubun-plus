# This file is part of Hakubun.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import os

gtk_dir = os.path.dirname(__file__)


def main():
    import signal
    import sys
    from hakubun import i18n, utils

    # Read the GTK-specific config and install translations *before*
    # importing .application, which imports window.py -- whose
    # Gtk.Template.from_file class decorator parses window.ui (and, via
    # nested templates, the other .ui files) at import time. GtkBuilder's
    # translatable="yes" strings are resolved against the process-wide
    # gettext catalog at that parse, so the catalog has to be installed
    # first or those strings would be baked in untranslated.
    gtk_config = utils.parse_config(
        utils.to_config_path('ui-Gtk.json'), utils.gtk_defaults)
    i18n.install(gtk_config.get('language', 'auto'))

    from .application import HakubunApplication

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    print("Hakubun+ GTK v{}".format(utils.VERSION))
    app = HakubunApplication()
    sys.exit(app.run(sys.argv))
