"""Qt list Season-column sort, wired through the real ShowListModel +
ShowListProxy stack (no display needed).

Regression coverage for a bug where the Season column sorted its
display text ('Summer 2026') as plain strings, comparing the season
name before the year -- e.g. 'Fall 2011' sorted before 'Spring 2006'.
"""

import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PyQt6')

from PyQt6 import QtCore
from PyQt6.QtWidgets import QApplication

from hakubun import utils
from hakubun.ui.qt.models import ShowListModel, ShowListProxy


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def _show(id_, extra_season):
    return {
        'id': id_, 'title': 'show-%d' % id_, 'my_progress': 0, 'my_score': 0,
        'total': 12, 'status': utils.Status.FINISHED, 'type': 'TV',
        'start_date': None, 'end_date': None,
        'my_start_date': None, 'my_finish_date': None, 'my_status': None,
        'my_tags': None, 'my_last_update': None,
        'platform_score': None, 'mal_score': None,
        'extra': [('Season', extra_season)],
    }


def _sorted_titles(showlist):
    model = ShowListModel(palette={})
    model.setShowList(showlist, {}, {})
    proxy = ShowListProxy()
    proxy.setSourceModel(model)
    proxy.sort(ShowListModel.COL_SEASON, QtCore.Qt.SortOrder.AscendingOrder)
    return [proxy.data(proxy.index(r, ShowListModel.COL_TITLE))
            for r in range(proxy.rowCount())]


def test_year_dominates_over_season_name(qapp):
    order = _sorted_titles([_show(1, 'Fall 2011'), _show(2, 'Spring 2006')])
    assert order == ['show-2', 'show-1']


def test_season_name_breaks_ties_within_a_year(qapp):
    order = _sorted_titles([_show(1, 'Winter 2026'), _show(2, 'Summer 2026')])
    assert order == ['show-2', 'show-1']
