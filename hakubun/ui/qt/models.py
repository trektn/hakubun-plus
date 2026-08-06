import datetime

from PyQt6 import QtCore, QtGui

from hakubun import utils
from hakubun.ui.qt.thumbs import ThumbManager
from hakubun.ui.qt.util import IN_LIST_COLOR, getColor, getIcon

# Taiga's release-status dot (Anime List, leftmost column): green while
# airing, blue once finished, red before it's aired -- fixed colors
# rather than theme-relative ones, matching Taiga's own convention.
_RELEASE_STATUS_DOT_COLORS = {
    utils.Status.ONGOING: '#4CAF50',
    utils.Status.FINISHED: '#2196F3',
    utils.Status.NOTYET: '#F44336',
}
_release_status_dot_cache = {}


def _release_status_dot(status):
    color_hex = _RELEASE_STATUS_DOT_COLORS.get(status)
    if not color_hex:
        return None

    pixmap = _release_status_dot_cache.get(color_hex)
    if pixmap is None:
        pixmap = QtGui.QPixmap(10, 10)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(color_hex))
        painter.drawEllipse(0, 0, 10, 10)
        painter.end()
        _release_status_dot_cache[color_hex] = pixmap

    return pixmap


class ShowListModel(QtCore.QAbstractTableModel):
    """
    Main model used in the main window to show
    a list of shows in the user's list.
    """
    COL_ID = 0
    COL_TITLE = 1
    COL_MY_PROGRESS = 2
    COL_MY_SCORE = 3
    COL_PERCENT = 4
    COL_NEXT_EP = 5
    COL_START_DATE = 6
    COL_END_DATE = 7
    COL_MY_START = 8
    COL_MY_FINISH = 9
    COL_MY_TAGS = 10
    COL_MY_STATUS = 11
    COL_LAST_UPDATED = 12
    COL_SEASON = 13
    COL_TYPE = 14
    COL_PLATFORM_SCORE = 15
    COL_MAL_SCORE = 16
    # Appended rather than inserted at the front, so existing COL_*
    # indices (several of which are hardcoded elsewhere, e.g.
    # ShowsTableDelegate's column-4 check for COL_PERCENT) don't shift.
    # Taiga mode moves it to the front visually via
    # horizontalHeader().moveSection() instead.
    COL_RELEASE_STATUS = 17

    columns = ['ID', 'Title', 'Progress', 'Score',
               'Percent', 'Next Episode', 'Start date', 'End date',
               'My start', 'My finish', 'Tags', 'Status', 'Last updated', 'Season',
               'Type', 'Platform Score', 'MAL Score', '']

    editable_columns = [COL_MY_PROGRESS, COL_MY_SCORE]

    common_flags = \
        QtCore.Qt.ItemFlag.ItemIsSelectable | \
        QtCore.Qt.ItemFlag.ItemIsEnabled | \
        QtCore.Qt.ItemFlag.ItemNeverHasChildren

    date_format = "%Y-%m-%d"

    progressChanged = QtCore.pyqtSignal(QtCore.QVariant, float)
    scoreChanged = QtCore.pyqtSignal(QtCore.QVariant, float)

    def __init__(self, parent=None, palette=None):
        self.showlist = None
        self.palette = palette
        self.playing = set()
        self.mediainfo = {}

        super().__init__(parent)

    def setDateFormat(self, date_format):
        self.date_format = date_format

    def setMediaInfo(self, mediainfo):
        self.mediainfo = mediainfo

    def _date(self, obj):
        if obj:
            return obj.strftime(self.date_format)
        else:
            return '-'

    def _calculate_color(self, row, show):
        color = None

        if show['id'] in self.playing:
            color = 'is_playing'
        elif show.get('queued'):
            color = 'is_queued'
        elif self.library.get(show['id']) and max(self.library.get(show['id'])) > show['my_progress']:
            color = 'new_episode'
        elif show['status'] == utils.Status.AIRING:
            color = 'is_airing'
        elif show['status'] == utils.Status.NOTYET:
            color = 'not_aired'
        else:
            color = None

        if color:
            self.colors[row] = QtGui.QBrush(getColor(self.palette[color]))
        elif row in self.colors:
            del self.colors[row]

    def _calculate_next_ep(self, row, show):
        if self.mediainfo.get('date_next_ep'):
            if 'next_ep_time' in show:
                delta = show['next_ep_time'] - datetime.datetime.utcnow()
                self.next_ep[row] = "%i days, %02d hrs." % (
                    delta.days, delta.seconds/3600)
            elif row in self.next_ep:
                del self.next_ep[row]

    def _calculate_eps(self, row, show):
        aired_eps = utils.estimate_aired_episodes(show)
        library_eps = self.library.get(show['id'])

        if library_eps:
            library_eps = library_eps.keys()

        if aired_eps or library_eps:
            self.eps[row] = (aired_eps, library_eps)
        elif row in self.eps:
            del self.eps[row]

    def setShowList(self, showlist, altnames, library):
        self.beginResetModel()

        self.showlist = list(showlist)
        self.altnames = altnames
        self.library = library

        self.id_map = {}
        self.colors = {}
        self.next_ep = {}
        self.eps = {}

        for row, show in enumerate(self.showlist):
            self.id_map[show['id']] = row
            self._calculate_color(row, show)

            if self.mediainfo.get('can_play'):
                self._calculate_next_ep(row, show)
                self._calculate_eps(row, show)

        self.endResetModel()

    def update(self, showid, is_playing=None):
        if not self.showlist:
            return
        if showid not in self.id_map:
            # A stale async tracker callback for a show from the
            # account we've since switched away from (setShowList
            # already rebuilt id_map for the new account) -- nothing
            # to update, not a crash.
            return

        # Recalculate color and emit the changed signal
        row = self.id_map[showid]
        show = self.showlist[row]

        if is_playing is not None:
            if is_playing:
                self.playing.add(showid)
            else:
                self.playing.discard(showid)

        self._calculate_color(row, show)
        self.dataChanged.emit(self.index(
            row, 0), self.index(row, len(self.columns)-1))

    def rowCount(self, parent=QtCore.QModelIndex()):
        if self.showlist:
            return len(self.showlist)
        else:
            return 0

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role):
        if role == QtCore.Qt.ItemDataRole.DisplayRole and orientation == QtCore.Qt.Orientation.Horizontal:
            return self.columns[section]
        elif role == QtCore.Qt.ItemDataRole.ToolTipRole and orientation == QtCore.Qt.Orientation.Horizontal:
            if section == ShowListModel.COL_LAST_UPDATED:
                return 'Date and time of the last synced update'

    def setData(self, index, value, role):
        row, column = index.row(), index.column()
        show = self.showlist[row]

        if column == ShowListModel.COL_MY_PROGRESS:
            self.progressChanged.emit(show['id'], value)
        elif column == ShowListModel.COL_MY_SCORE:
            self.scoreChanged.emit(show['id'], utils.score_to_raw(value, self.mediainfo))

        return True

    def data(self, index, role):
        row, column = index.row(), index.column()
        show = self.showlist[row]

        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            if column == ShowListModel.COL_ID:
                return show['id']
            elif column == ShowListModel.COL_TITLE:
                title_str = show['title']
                if show['id'] in self.altnames:
                    title_str += " [%s]" % self.altnames[show['id']]
                return title_str
            elif column == ShowListModel.COL_MY_PROGRESS:
                return "{} / {}".format(show['my_progress'], show['total'] or '?')
            elif column == ShowListModel.COL_MY_SCORE:
                return utils.score_to_display(show['my_score'], self.mediainfo)
            elif column == ShowListModel.COL_PERCENT:
                # return "{:.0%}".format(show['my_progress'] / 100)
                if show['total']:
                    total = show['total']
                else:
                    total = (int(show['my_progress']/12)+1) * \
                        12  # Round up to the next cour

                # `total` above is only ever a real episode count or a
                # made-up bar-width denominator (rounded up to the next
                # 12-episode block) -- fine for proportioning the bar
                # itself, but showing it as e.g. "7/12" text (Taiga
                # mode's text_fraction) would claim a known total that
                # doesn't exist. show['total'] (real, possibly falsy) is
                # carried alongside it so the delegate can show "?"
                # instead when there's no real total, matching how
                # COL_MY_PROGRESS already formats this same case.
                if row in self.eps:
                    return (show['my_progress'], total, self.eps[row][0], self.eps[row][1],
                            show['total'])
                else:
                    return (show['my_progress'], total, None, None, show['total'])
            elif column == ShowListModel.COL_NEXT_EP:
                return self.next_ep.get(row, '-')
            elif column == ShowListModel.COL_START_DATE:
                return self._date(show['start_date'])
            elif column == ShowListModel.COL_END_DATE:
                return self._date(show['end_date'])
            elif column == ShowListModel.COL_MY_START:
                return self._date(show['my_start_date'])
            elif column == ShowListModel.COL_LAST_UPDATED:
                return utils.format_local_time(show.get('my_last_update'))
            elif column == ShowListModel.COL_MY_FINISH:
                return self._date(show['my_finish_date'])
            elif column == ShowListModel.COL_MY_TAGS:
                return show.get('my_tags', '-')
            elif column == ShowListModel.COL_MY_STATUS:
                return self.mediainfo['statuses_dict'][show['my_status']]
            elif column == ShowListModel.COL_SEASON:
                return utils.get_season_label(show)
            elif column == ShowListModel.COL_TYPE:
                return str(show['type'])
            elif column == ShowListModel.COL_PLATFORM_SCORE:
                return show.get('platform_score') or '-'
            elif column == ShowListModel.COL_MAL_SCORE:
                return show.get('mal_score') or '-'
        elif role == QtCore.Qt.ItemDataRole.BackgroundRole:
            return self.colors.get(row)
        elif role == QtCore.Qt.ItemDataRole.DecorationRole:
            if column == ShowListModel.COL_TITLE and show['id'] in self.playing:
                return getIcon('media-playback-start')
            elif column == ShowListModel.COL_RELEASE_STATUS:
                return _release_status_dot(show.get('status'))
        elif role == QtCore.Qt.ItemDataRole.TextAlignmentRole:
            if column in [ShowListModel.COL_MY_PROGRESS, ShowListModel.COL_MY_SCORE]:
                return QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignVCenter
        elif role == QtCore.Qt.ItemDataRole.ToolTipRole:
            if column == ShowListModel.COL_PERCENT:
                tooltip = "Watched: %d<br>" % show['my_progress']
                if self.eps.get(row):
                    (aired_eps, library_eps) = self.eps.get(row)
                    if aired_eps:
                        tooltip += "Aired (estimated): %d<br>" % aired_eps
                    if library_eps:
                        tooltip += "Latest available: %d<br>" % max(
                            library_eps)
                tooltip += "Total: %d" % show['total']

                return tooltip
            elif column == ShowListModel.COL_LAST_UPDATED:
                return utils.format_local_time(show.get('my_last_update'))
        elif role == QtCore.Qt.ItemDataRole.EditRole:
            if column == ShowListModel.COL_MY_PROGRESS:
                return (show['my_progress'], show['total'], 0, 1)
            elif column == ShowListModel.COL_MY_SCORE:
                display_max, display_step, decimals = utils.score_display_range(self.mediainfo)
                display_value = utils.score_to_display(show['my_score'], self.mediainfo)

                return (display_value, display_max, decimals, display_step)
        elif role == QtCore.Qt.ItemDataRole.UserRole:
            if column == ShowListModel.COL_LAST_UPDATED:
                dt = show.get('my_last_update')
                return dt.timestamp() if dt is not None else 0

    def flags(self, index):
        if index.column() in self.editable_columns:
            return self.common_flags | QtCore.Qt.ItemFlag.ItemIsEditable
        else:
            return self.common_flags


class AddTableModel(QtCore.QAbstractTableModel):
    columns = ["Name", "Type", "Season", "Total", "In Your List"]

    def __init__(self, parent=None, mylist=None, statuses_dict=None):
        self.results = None
        self.mylist = mylist or {}
        self.statuses_dict = statuses_dict or {}

        super().__init__(parent)

    def set_mylist(self, mylist):
        """See AddListDelegate.set_mylist -- refreshes the "In Your
        List" column/tint for a view that outlives a single search
        (Taiga mode's Search page), instead of the one-shot snapshot a
        modal takes."""
        self.mylist = mylist or {}
        if self.results:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self.results) - 1, self.columnCount(None) - 1))

    def setResults(self, new_results):
        self.beginResetModel()
        self.results = new_results
        self.endResetModel()

    def rowCount(self, parent):
        if self.results:
            return len(self.results)
        else:
            return 0

    def columnCount(self, parent):
        return 5

    def headerData(self, section, orientation, role):
        if role == QtCore.Qt.ItemDataRole.DisplayRole and orientation == QtCore.Qt.Orientation.Horizontal:
            return self.columns[section]

    def _mylist_entry(self, item):
        return self.mylist.get(item.get('id'))

    def data(self, index, role):
        row, column = index.row(), index.column()

        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            item = self.results[row]

            if column == 0:
                return item.get('title')
            elif column == 1:
                return str(item.get('type', '?'))
            elif column == 2:
                return utils.get_season_label(item)
            elif column == 3:
                return item.get('total') or '?'
            elif column == 4:
                entry = self._mylist_entry(item)
                if entry:
                    return self.statuses_dict.get(entry['my_status'], '?')
                return ''
        elif role == QtCore.Qt.ItemDataRole.BackgroundRole:
            item = self.results[row]
            if self._mylist_entry(item):
                return IN_LIST_COLOR


class AddListModel(QtCore.QAbstractListModel):
    """
    List model meant to be used with the Add show list view.

    It manages thumbnails and queues their downloads with the
    ThumbManager as necessary.
    """

    def __init__(self, parent=None, api_info=None):
        self.results = None
        self.thumbs = {}
        self.api_info = api_info

        self.pool = ThumbManager()
        self.pool.itemFinished.connect(self.gotThumb)

        super().__init__(parent)

    def gotThumb(self, iid, thumb):
        iid = int(iid)
        self.thumbs[iid] = thumb.scaled(
            100, 140, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)

        self.dataChanged.emit(self.index(iid), self.index(iid))

    def setResults(self, new_results):
        """ This method will process a new list of shows and get their
        thumbnails if necessary. """

        self.beginResetModel()

        self.results = new_results

        self.thumbs.clear()

        if self.results:
            for row, item in enumerate(self.results):
                if item.get('image'):
                    utils.make_dir(utils.to_cache_path())
                    filename = utils.to_cache_path("%s_%s_f_%s.jpg" % (
                        self.api_info['shortname'], self.api_info['mediatype'], item['id']))

                    if self.pool.exists(filename):
                        self.thumbs[row] = self.pool.getThumb(filename).scaled(
                            100, 140, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
                    else:
                        self.pool.queueDownload(row, item['image'], filename)

        self.endResetModel()

    def rowCount(self, parent):
        if self.results:
            return len(self.results)
        else:
            return 0

    def data(self, index, role):
        row = index.row()
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return self.results[row]
        elif role == QtCore.Qt.ItemDataRole.DecorationRole:
            return self.thumbs.get(row)
        elif role == QtCore.Qt.ItemDataRole.BackgroundRole:
            t = self.results[row].get('type')
            if t == utils.Type.TV:
                return QtGui.QColor(202, 253, 150)
            elif t == utils.Type.MOVIE:
                return QtGui.QColor(150, 202, 253)
            elif t == utils.Type.OVA or t == utils.Type.ONA:
                return QtGui.QColor(253, 253, 150)
            elif t == utils.Type.SP:
                return QtGui.QColor(253, 150, 150)
            else:
                return QtGui.QColor(250, 250, 250)

        return None


class AddListProxy(QtCore.QSortFilterProxyModel):
    """Sorts (and optionally groups) AddCardView's results.

    Grouping isn't a real section/header feature -- QListView's flow
    grid has no clean way to render one without becoming its own project
    (see Seasons page groundwork notes). Instead "group by" is a primary
    sort tier: same-group shows cluster together, in group order, then
    within a group by whatever "sort by" key is active.

    Sort values are computed so plain AscendingOrder always yields
    "most relevant first" per key (e.g. Score is stored negated, since
    higher is better but ascending order should still put it first) --
    keeps callers from needing a separate direction toggle per key.
    """

    GROUP_NONE = 'none'
    GROUP_AIRING_STATUS = 'airing_status'
    GROUP_LIST_STATUS = 'list_status'
    GROUP_TYPE = 'type'

    SORT_TYPE = 'type'
    SORT_AIRING_DATE = 'airing_date'
    SORT_EPISODES = 'episodes'
    SORT_POPULARITY = 'popularity'
    SORT_SCORE = 'score'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._group_key = self.GROUP_NONE
        self._sort_key = self.SORT_TYPE
        self._mylist = {}
        self._statuses = []

    def set_mylist(self, mylist):
        self._mylist = mylist or {}
        self.invalidate()

    def set_statuses(self, statuses):
        """Ordered list-status values (mediainfo['statuses']) -- needed
        to group by list status, since the raw value's type/ordering
        differs per backend (ints for MAL/AniList, strings for Kitsu)
        and isn't comparable across accounts on its own."""
        self._statuses = statuses or []
        self.invalidate()

    def set_group_key(self, key):
        self._group_key = key
        self.invalidate()

    def set_sort_key(self, key):
        self._sort_key = key
        self.invalidate()

    def _group_value(self, show):
        if self._group_key == self.GROUP_AIRING_STATUS:
            return int(show.get('status') or utils.Status.UNKNOWN)
        if self._group_key == self.GROUP_TYPE:
            return int(show.get('type') or utils.Type.UNKNOWN)
        if self._group_key == self.GROUP_LIST_STATUS:
            entry = self._mylist.get(show.get('id'))
            if not entry or entry.get('my_status') not in self._statuses:
                # Not-in-list sorts first -- discovering what's new is
                # the main reason to browse a season in the first place.
                return -1
            return self._statuses.index(entry['my_status'])
        return 0

    def _sort_value(self, show):
        if self._sort_key == self.SORT_AIRING_DATE:
            return show.get('start_date') or datetime.date.max
        if self._sort_key == self.SORT_EPISODES:
            total = show.get('total')
            return total if total is not None else -1
        if self._sort_key == self.SORT_POPULARITY:
            popularity = show.get('popularity')
            # Already normalized ascending-is-more-popular at parse time
            # (see each lib's _parse_info) -- missing data sorts last.
            return popularity if popularity is not None else float('inf')
        if self._sort_key == self.SORT_SCORE:
            score = show.get('score_raw')
            # Negated so ascending order still puts the best score
            # first; missing data sorts last either way.
            return -score if score is not None else float('inf')
        return int(show.get('type') or utils.Type.UNKNOWN)

    def lessThan(self, left, right):
        leftData = self.sourceModel().data(left, QtCore.Qt.ItemDataRole.DisplayRole)
        rightData = self.sourceModel().data(right, QtCore.Qt.ItemDataRole.DisplayRole)

        leftGroup = self._group_value(leftData)
        rightGroup = self._group_value(rightData)
        if leftGroup != rightGroup:
            return leftGroup < rightGroup

        return self._sort_value(leftData) < self._sort_value(rightData)


class ShowListProxy(QtCore.QSortFilterProxyModel):
    filter_columns = None
    filter_status = None
    filter_invert = False

    def setFilterStatus(self, status):
        self.filter_status = status
        self.invalidateFilter()

    def clearColumnFilters(self):
        self.filters = {}

    def setFilterColumns(self, columns):
        self.filter_columns = columns
        self.invalidateFilter()

    def setFilterInvert(self, invert):
        self.filter_invert = invert
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        if self.filter_status is not None and self.sourceModel().showlist[source_row]['my_status'] != self.filter_status:
            return False

        if self.filter_columns:
            for col in range(self.sourceModel().columnCount(source_parent)):
                index = self.sourceModel().index(source_row, col)
                if (col in self.filter_columns and
                        self.filter_columns[col] not in str(self.sourceModel().data(index, QtCore.Qt.ItemDataRole.DisplayRole))):
                    return self.filter_invert

        return self.filter_invert != super(ShowListProxy, self).filterAcceptsRow(source_row, source_parent)

    def lessThan(self, left, right):
        col = left.column()

        if col == ShowListModel.COL_LAST_UPDATED:
            lv = self.sourceModel().data(left, QtCore.Qt.ItemDataRole.UserRole)
            rv = self.sourceModel().data(right, QtCore.Qt.ItemDataRole.UserRole)

            lnum = lv if isinstance(lv, (int, float)) else 0
            rnum = rv if isinstance(rv, (int, float)) else 0

            return int(lnum) < int(rnum)

        return super().lessThan(left, right)
