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
    from hakubun import utils
    from .application import HakubunApplication

    signal.signal(signal.SIGINT, signal.SIG_DFL)

    print("Hakubun+ GTK v{}".format(utils.VERSION))
    app = HakubunApplication()
    sys.exit(app.run(sys.argv))
