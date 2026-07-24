import datetime

from PyQt6 import QtCore, QtGui

from hakubun import utils
from hakubun.ui.qt.thumbs import ThumbManager
from hakubun.ui.qt.util import IN_LIST_COLOR, getColor, getIcon


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
    COL_RELEASE_STATUS = 17
    COL_SYNCED_SCORE = 18

    columns = ['ID', 'Title', 'Progress', 'Score',
               'Percent', 'Next Episode', 'Start date', 'End date',
               'My start', 'My finish', 'Tags', 'Status', 'Last updated', 'Season',
               'Type', 'Platform Score', 'MAL Score', 'Release Status',
               'Synced Score']

    editable_columns = [COL_MY_PROGRESS, COL_MY_SCORE]

    # column -> overlayable my_* field (for the reconciled-value cue).
    _COL_OVERLAY_KEY = {
        COL_MY_PROGRESS: 'my_progress',
        COL_PERCENT: 'my_progress',
        COL_MY_SCORE: 'my_score',
        COL_MY_STATUS: 'my_status',
        COL_MY_START: 'my_start_date',
        COL_MY_FINISH: 'my_finish_date',
        COL_MY_TAGS: 'my_tags',
        # The Synced Score cell is only ever populated for an owned
        # (cross-tracker) entry, so italicise it like the other
        # reconciled cells whenever it shows a value.
        COL_SYNCED_SCORE: 'my_score',
    }

    common_flags = \
        QtCore.Qt.ItemFlag.ItemIsSelectable | \
        QtCore.Qt.ItemFlag.ItemIsEnabled | \
        QtCore.Qt.ItemFlag.ItemNeverHasChildren

    date_format = "%Y-%m-%d"

    progressChanged = QtCore.pyqtSignal(QtCore.QVariant, float)
    scoreChanged = QtCore.pyqtSignal(QtCore.QVariant, float)

    # my_* fields whose displayed value the multi-sync overlay can
    # replace with the reconciled/owned value (e.g. AniList's rating
    # while signed into Kitsu). Empty overlay -> every _v() falls back
    # to the raw show value, i.e. behaviour identical to no overlay.
    OVERLAY_FIELDS = ('my_progress', 'my_score', 'my_status',
                      'my_start_date', 'my_finish_date', 'my_tags')

    # Columns whose cell reflects an overlayable my_* field (for the
    # italic 'reconciled value' cue). Built after the COL_* constants.

    def __init__(self, parent=None, palette=None):
        self.showlist = None
        self.palette = palette
        self.playing = set()
        self.mediainfo = {}
        self.overlay = {}       # {show_id: {my_field: value}}

        super().__init__(parent)

    def set_overlay(self, overlay):
        """Install the multi-sync reconciled-value overlay (or {} to
        clear it) and repaint. Read-only: it changes what the list
        DISPLAYS, never the underlying show dicts or what an edit
        pushes."""
        self.beginResetModel()
        self.overlay = overlay or {}
        # Row colours key off progress (the 'new episode' highlight),
        # which the overlay can change -- recompute against it.
        if self.showlist:
            for row, show in enumerate(self.showlist):
                self._calculate_color(row, show)
        self.endResetModel()

    def refresh_overlay(self, overlay):
        """Replace the overlay and repaint WITHOUT a model reset, so the
        current selection survives. Used after an in-place owner-score
        edit; row colours key off progress, not score, so they're left
        untouched here."""
        self.overlay = overlay or {}
        if self.showlist:
            top = self.index(0, 0)
            bottom = self.index(len(self.showlist) - 1,
                                self.columnCount() - 1)
            self.dataChanged.emit(top, bottom)

    def overlay_for(self, show_id):
        """The overlay dict for a show id -- editor logic and tooltips
        consult it for owner-score context (_score_owner, _uuid, ...) --
        or {} when there's no overlay entry for it. Keyed by the active
        provider's show id (both int and str forms are stored)."""
        return self.overlay.get(show_id) or {}

    def _v(self, show, key):
        """The value to DISPLAY for a my_* field: the multi-sync
        overlay's if present, else the show's own."""
        if self.overlay:
            over = self.overlay.get(show['id'])
            if over is not None and key in over:
                return over[key]
        return show.get(key)

    def _overlaid(self, show, key):
        over = self.overlay.get(show['id']) if self.overlay else None
        if over is None:
            return False
        if key in over:
            return True
        # The score column is 'overlaid' (owner's system) even when only
        # its display, not its raw my_score, is overridden.
        return key == 'my_score' and '_score_display' in over

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
        elif self.library.get(show['id']) and max(self.library.get(show['id'])) > self._v(show, 'my_progress'):
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
                return "{} / {}".format(self._v(show, 'my_progress'), show['total'] or '?')
            elif column == ShowListModel.COL_MY_SCORE:
                over = self.overlay.get(show['id']) if self.overlay else None
                if over and '_score_display' in over:
                    # Reconciled score shown in its OWNER's rating system.
                    return over['_score_display']
                return utils.score_to_display(self._v(show, 'my_score'), self.mediainfo)
            elif column == ShowListModel.COL_PERCENT:
                progress = self._v(show, 'my_progress')
                if show['total']:
                    total = show['total']
                else:
                    total = (int(progress/12)+1) * \
                        12  # Round up to the next cour

                if row in self.eps:
                    return (progress, total, self.eps[row][0], self.eps[row][1])
                else:
                    return (progress, total, None, None)
            elif column == ShowListModel.COL_NEXT_EP:
                return self.next_ep.get(row, '-')
            elif column == ShowListModel.COL_START_DATE:
                return self._date(show['start_date'])
            elif column == ShowListModel.COL_END_DATE:
                return self._date(show['end_date'])
            elif column == ShowListModel.COL_MY_START:
                return self._date(self._v(show, 'my_start_date'))
            elif column == ShowListModel.COL_LAST_UPDATED:
                return utils.format_local_time(show.get('my_last_update'))
            elif column == ShowListModel.COL_MY_FINISH:
                return self._date(self._v(show, 'my_finish_date'))
            elif column == ShowListModel.COL_MY_TAGS:
                return self._v(show, 'my_tags') or '-'
            elif column == ShowListModel.COL_MY_STATUS:
                return self.mediainfo['statuses_dict'][self._v(show, 'my_status')]
            elif column == ShowListModel.COL_SEASON:
                return utils.get_season_label(show)
            elif column == ShowListModel.COL_TYPE:
                return str(show['type'])
            elif column == ShowListModel.COL_PLATFORM_SCORE:
                return show.get('platform_score') or '-'
            elif column == ShowListModel.COL_MAL_SCORE:
                return show.get('mal_score') or '-'
            elif column == ShowListModel.COL_RELEASE_STATUS:
                return utils.release_status_label(show.get('status'))
            elif column == ShowListModel.COL_SYNCED_SCORE:
                # Dedicated Synced Score column: the reconciled score in
                # the OWNER's own rating system for a shared entry, an en
                # dash when owned-elsewhere but unrated, and blank for a
                # platform-specific entry (nothing else owns it) -- so the
                # column itself reads as the owned-vs-platform indicator.
                over = self.overlay.get(show['id']) if self.overlay else None
                if not over or not over.get('_score_owner'):
                    return ''
                disp = over.get('_score_display')
                return ('%g' % disp) if disp is not None else '–'
        elif role == QtCore.Qt.ItemDataRole.FontRole:
            key = ShowListModel._COL_OVERLAY_KEY.get(column)
            if key and self._overlaid(show, key):
                # Italicise a cell showing a reconciled/owned value
                # instead of this account's own, so it's distinguishable.
                font = QtGui.QFont()
                font.setItalic(True)
                return font
        elif role == QtCore.Qt.ItemDataRole.BackgroundRole:
            return self.colors.get(row)
        elif role == QtCore.Qt.ItemDataRole.DecorationRole:
            if column == ShowListModel.COL_TITLE and show['id'] in self.playing:
                return getIcon('media-playback-start')
        elif role == QtCore.Qt.ItemDataRole.TextAlignmentRole:
            if column in [ShowListModel.COL_MY_PROGRESS, ShowListModel.COL_MY_SCORE]:
                return QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignVCenter
        elif role == QtCore.Qt.ItemDataRole.ToolTipRole:
            if column == ShowListModel.COL_PERCENT:
                tooltip = "Watched: %d<br>" % self._v(show, 'my_progress')
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
            elif column == ShowListModel.COL_SYNCED_SCORE:
                over = self.overlay.get(show['id']) if self.overlay else None
                owner = over.get('_score_owner') if over else None
                if owner:
                    return ('Synced from %s, in its rating system '
                            '(via multi-sync).' % owner.capitalize())
                return 'Platform-specific entry — not synced to another tracker.'
            key = ShowListModel._COL_OVERLAY_KEY.get(column)
            if key and self._overlaid(show, key):
                over = self.overlay.get(show['id']) or {}
                if column == ShowListModel.COL_MY_SCORE \
                        and over.get('_score_owner'):
                    return ('Score owned by %s, shown in its rating '
                            'system (via multi-sync).'
                            % over['_score_owner'].capitalize())
                return ('Reconciled value from multi-sync (owned by '
                        'another provider).')
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
            # An owner-overlaid Score cell displays another provider's
            # rating system (e.g. AniList's 8.4 while signed into
            # Kitsu); an inline editor would edit the ACTIVE account's
            # raw value on a different scale and bypass ownership
            # entirely -- block inline editing for that cell. The
            # bottom-bar score editor handles owner-system rating.
            if index.column() == ShowListModel.COL_MY_SCORE \
                    and self.overlay and self.showlist:
                over = self.overlay.get(self.showlist[index.row()]['id'])
                if over and ('_score_owner' in over
                             or '_score_display' in over):
                    return self.common_flags
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
                return item.get('total', '?')
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
    def lessThan(self, left, right):
        leftData = self.sourceModel().data(left, QtCore.Qt.ItemDataRole.DisplayRole)
        rightData = self.sourceModel().data(right, QtCore.Qt.ItemDataRole.DisplayRole)

        return leftData['type'] < rightData['type']


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
