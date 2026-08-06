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

from PyQt6 import QtCore, QtGui, QtSvg, QtWidgets

from hakubun import utils


# Subtle highlight for a search result already present in the user's list,
# so it's not confused with a brand new show. Shared by the card view
# (AddListDelegate) and the table view (AddTableModel) of the Add dialog.
IN_LIST_COLOR = QtGui.QColor(210, 230, 255)


class FilterBar:
    """
    Constants relating to filter bar settings can live here.
    """
    # Position
    PositionHidden = 0
    PositionAboveLists = 1
    PositionBelowLists = 2


# Maps the freedesktop/XDG icon-theme names this app's call sites already
# use to Taiga's own icon set -- Google Material Symbols, vendored from
# erengy/taiga's GPLv3-licensed src/resources/icons/ into data/qtui/ (see
# THIRD_PARTY_NOTICES.md) -- so Taiga mode shows Taiga's actual
# iconography instead of whatever the user's desktop icon theme happens to
# have (or nothing at all, on platforms with no such theme). Names with no
# good equivalent in that set are left out on purpose and keep falling
# back to QIcon.fromTheme below, unchanged from before this map existed.
_TAIGA_ICON_MAP = {
    'media-playback-start': 'play_arrow',
    'media-playback-pause': 'pause',
    'media-skip-forward': 'skip_next',
    'edit-find': 'search',
    'edit-delete': 'delete',
    'application-exit': 'logout',
    'edit-undo': 'arrow_back',
    'edit-redo': 'arrow_forward',
    'view-refresh': 'sync',
    'preferences-system': 'settings',
    'help-about': 'help',
    'list-add': 'add_box',
    'folder': 'folder',
    'view-statistics': 'bar_chart',
    'view-list-details': 'list_alt',
    'view-grid': 'grid_view',
    'history': 'history',
}

# Raster sizes baked into each tinted QIcon so Qt can pick a sharp match
# instead of blur-scaling a single pixmap across toolbar/menu/list contexts.
_ICON_SIZES = (16, 24, 32, 48)


def _tinted_svg_icon(svg_path, color):
    """Renders a flat-color source SVG (no fill attribute -> defaults to
    black) into a QIcon tinted to `color`, at a few common sizes."""
    renderer = QtSvg.QSvgRenderer(svg_path)
    if not renderer.isValid():
        return None

    icon = QtGui.QIcon()
    for size in _ICON_SIZES:
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)

        painter = QtGui.QPainter(pixmap)
        renderer.render(painter)
        painter.setCompositionMode(
            QtGui.QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), color)
        painter.end()

        icon.addPixmap(pixmap)

    return icon


def getIcon(icon_name):
    material_name = _TAIGA_ICON_MAP.get(icon_name)
    if material_name is not None:
        svg_path = utils.DATADIR + '/qtui/{}.svg'.format(material_name)
        color = QtWidgets.QApplication.palette().color(
            QtGui.QPalette.ColorRole.WindowText)
        icon = _tinted_svg_icon(svg_path, color)
        if icon is not None:
            return icon

    fallback = QtGui.QIcon(utils.DATADIR + '/qtui/{}.png'.format(icon_name))
    return QtGui.QIcon.fromTheme(icon_name, fallback)


def getColor(colorString):
    # Takes a color string in either #RRGGBB format or group,role format (using QPalette int values)
    if colorString[0] == "#":
        return QtGui.QColor(colorString)
    else:
        (group, role) = [int(i) for i in colorString.split(',')]
        if (0 <= group <= 2) and (0 <= role <= 19):
            return QtGui.QColor(QtGui.QPalette().color(QtGui.QPalette.ColorGroup(group), QtGui.QPalette.ColorRole(role)))
        else:
            # Failsafe - return black
            return QtGui.QColor()
