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
import re
from decimal import Decimal

import anitomy_ng

ANITYPE_INVALID = ('OP', 'NCOP', 'OPENING', 'ED', 'NCED', 'ENDING', 'PV', 'PREVIEW')
ANITYPE_SPECIALS = ('OAD', 'OAV', 'ONA', 'OVA', 'SPECIAL', 'SPECIALS')


class AnitomyNgWrapper():
    """
    Wrapper class around anitomy-ng, a pure-Rust port of Anitomy.
    Exists mainly to bridge its element-list output to the interface the
    engine expects, and to work around edge cases it (and, in most cases,
    upstream Anitomy itself) cannot solve.
    """

    # anitomy-ng's season/episode token matching is case-sensitive, and
    # fails to detect a lowercase 's01e02'-style token at all -- confirmed
    # against real upstream Anitomy (v1.4.0 and the current v2 rewrite),
    # so this isn't an anitomy-ng-only bug, just one it inherits. Rather
    # than reimplement season/episode extraction ourselves, normalize the
    # season/episode letters to uppercase before handing off the string.
    _LOWERCASE_SEASON_EPISODE = re.compile(
        r'(?<![A-Za-z0-9])s(?P<season>[0-9]+)e(?P<episode>[0-9]+)(?![A-Za-z0-9])')

    def __init__(self, msg, file_name):
        self.msg = msg.with_classname('Parser')
        self.original_file_name = file_name

        file_name = self.__dedupePathTitle(file_name)
        file_name = self._LOWERCASE_SEASON_EPISODE.sub(
            lambda m: 'S{0}E{1}'.format(m.group('season'), m.group('episode')),
            file_name)

        try:
            self.msg.debug(f"Parsing {file_name}")
            elements = anitomy_ng.parse(file_name)
        except Exception:
            import traceback
            traceback.print_exc()
            elements = []

        title = episode = season = year = None
        episodes = []
        types = []
        has_checksum = False
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
            elif kind == 'file_checksum':
                has_checksum = True

        # A bare 'ED'/'OP' type value can be a false positive: an 8-hex-digit
        # checksum that happens to start with those two letters (both valid
        # hex digits) gets misread as the type keyword, silently swallowing
        # the rest of the checksum instead of producing a proper
        # file_checksum element. 'NCED'/'NCOP'/'OVA'/etc. aren't valid hex
        # sequences, so they can't collide this way and are always trusted.
        # A checksum element being present elsewhere confirms this ED/OP
        # was a real, standalone token, not a checksum fragment.
        if not has_checksum:
            types = [t for t in types if t.upper() not in ('ED', 'OP')]

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
    def __dedupePathTitle(file_name):
        # When library_full_path feeds a path like "Show S01/Show
        # S01E05.mkv", anitomy-ng (like upstream Anitomy, and unlike
        # AnitopyWrapper which handles this explicitly) has no concept of
        # path separators, and inconsistently leaves them -- and the
        # duplicated title either side of them -- in the title element.
        # Strip the longest run shared between two path segments before
        # the separator itself is discarded.
        file_name = os.path.sep + file_name
        try:
            temp = re.sub(r'[^\w\s{0}\(\)\{{\}}\[\]]'.format(re.escape(os.path.sep)),
                          r' ', file_name, flags=re.ASCII)
            m = max(
                (x for x in re.finditer(
                    r'(\b.{{3,}}\b)(?=.*?{0}.*?(?P<DUP>\1))'.format(re.escape(os.path.sep)),
                    temp, flags=re.IGNORECASE)),
                key=lambda y: y.end() - y.start(),
                default=None,
            )
            if m:
                file_name = file_name[:m.start('DUP')] + ' ' + file_name[m.end('DUP'):]
        except ValueError:
            pass

        parts = file_name.split(os.path.sep)
        file_name = ' '.join(parts).strip()
        return re.sub(r'\s{2,}', r' ', file_name)

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
