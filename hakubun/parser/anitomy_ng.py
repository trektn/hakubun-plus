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

from decimal import Decimal

import anitomy_ng

ANITYPE_INVALID = ('OP', 'NCOP', 'OPENING', 'ED', 'NCED', 'ENDING', 'PV', 'PREVIEW')
ANITYPE_SPECIALS = ('OAD', 'OAV', 'ONA', 'OVA', 'SPECIAL', 'SPECIALS')


class AnitomyNgWrapper():
    """
    Wrapper class around anitomy-ng, a pure-Rust port of Anitomy.
    Bridges its element-list output to the interface the engine expects.
    """

    def __init__(self, msg, file_name):
        self.msg = msg.with_classname('Parser')
        self.original_file_name = file_name

        try:
            self.msg.debug(f"Parsing {file_name}")
            # parse_path, not parse: the engine passes a full path when
            # library_full_path is set, and only parse_path strips the
            # directory prefix (and recovers a title that lives only in the
            # parent folder). Without a prefix it behaves exactly like parse.
            elements = anitomy_ng.parse_path(file_name)
        except Exception:
            import traceback
            traceback.print_exc()
            elements = []

        title = episode = season = year = None
        episodes = []
        types = []
        for e in elements:
            kind = e.kind.value
            if kind == 'title' and title is None:
                title = e.value
            elif kind == 'episode':
                episodes.append(e.value)
            elif kind == 'season' and season is None:
                season = e.value
            elif kind == 'year' and year is None:
                year = e.value
            elif kind == 'type':
                types.append(e.value)

        self.episode_number = episodes if len(episodes) > 1 else (
            episodes[0] if episodes else None)
        self.anime_title = self.__buildTitle(title, season, year, types)

    def getName(self):
        # Returns the anime title
        return self.anime_title

    def getEpisode(self):
        # Returns the first/only episode number
        if self.episode_number is None:
            return 1

        try:
            if type(self.episode_number) is list:
                return int(Decimal(self.episode_number[-1]))
            else:
                return int(Decimal(self.episode_number))
        except (ArithmeticError, ValueError, TypeError):
            self.msg.warn("Unable to parse episode number '{}' of: {}"
                          .format(self.episode_number, self.original_file_name))
            return 1

    def getEpisodeNumbers(self, force_numbers=False):
        # Returns the episode range as a tuple
        (ep_start, ep_end) = (None, None)

        if self.episode_number:
            try:
                if isinstance(self.episode_number, list):
                    ep_start = Decimal(self.episode_number[0])
                    ep_end = Decimal(self.episode_number[-1])
                else:
                    ep_start = Decimal(self.episode_number)
            except ArithmeticError:
                self.msg.warn("Unable to parse episode number '{}' of: {}"
                              .format(self.episode_number, self.original_file_name))

        if force_numbers:
            if ep_start is None:
                ep_start = 1
            if ep_end is None:
                ep_end = ep_start
            (ep_start, ep_end) = (int(ep_start), int(ep_end))

        return (ep_start, ep_end)

    @staticmethod
    def __buildTitle(title, season, year, types):
        if title is None:
            return None

        full_title = title
        if season:
            season_val = season[0] if isinstance(season, list) else season
            try:
                if int(season_val) > 1:
                    full_title += ' Season ' + str(season_val)
            except (ValueError, TypeError):
                pass

        if types:
            if not isinstance(types, list):
                types = [types]
            for t in types:
                if t.upper() in ANITYPE_INVALID:
                    # Not an episode -- an opening/ending/preview clip.
                    return None
                if t.upper() in ANITYPE_SPECIALS and t not in full_title:
                    full_title += ' ' + t

        if year and year not in full_title:
            full_title += ' (' + str(year) + ')'

        return full_title
