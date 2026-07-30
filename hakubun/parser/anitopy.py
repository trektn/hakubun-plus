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

import re
import os
import anitopy
from decimal import Decimal


class AnitopyWrapper():
    """
    Wrapper class around Anitopy, the anime filename parser.
    Exists mainly for compatibility reasons, but also to work around
    some edge cases that Anitopy cannot solve.
    """

    # Season/type/episode token: 'S01E13', 's01e13', 'S01OVA02', ...
    #
    # Anchored on both sides against alphanumerics on purpose. Without
    # the guards this happily matches INSIDE a release group -- the
    # 'S1TH3' of '...H.265-LYS1TH3A' parsed as season 1, type 'TH',
    # episode 3, which then rewrote the name into nonsense and made
    # every Fate/stay night movie report episode '3A'.
    #
    # Case-insensitive on purpose too: lowercase 's01e13' used to slip
    # through unrewritten and hand raw Anitopy a filename it crashes on
    # ("'NoneType' object has no attribute 'category'"), so the whole
    # file was dropped as unrecognized.
    _SEASON_TOKEN = re.compile(
        r'(?<![A-Za-z0-9])S(?P<season>[0-9]+)(?P<type>[A-Za-z]+)'
        r'(?P<episode>[0-9]+)(?![A-Za-z0-9])', re.IGNORECASE)

    def __init__(self, msg, file_name):
        self.msg = msg.with_classname('Parser')
        self.original_file_name = file_name

        # Episode number taken straight from an SxxEyy token, when the
        # name had one. Authoritative: we matched it explicitly, so we
        # do not have to hope Anitopy re-derives it from the rewritten
        # string (it often doesn't -- see __extractEpisodeNumber).
        self.token_episode = None

        file_name = self.__preProcessFileName(file_name)
        file_name = self.__trimFileName(file_name)
        self.file_name = file_name

        # Defaults so these attributes always exist -- getName()/getEpisode()
        # are called unconditionally by callers, and Anitopy can raise on
        # some filenames (see below). An empty parse leaves them as-is,
        # which the extractors treat as "unrecognized".
        self.episode_number = None
        self.anime_title = None

        try:
            self.msg.debug(f"Parsing {file_name}")
            data = anitopy.parse(file_name)
        except Exception as e:
            # Anitopy itself crashes on certain filenames (e.g. an sXXeYY
            # name whose episode token is the last one). Treat the file as
            # unrecognized rather than aborting the whole library scan;
            # falls through with data = {}. The full traceback is kept at
            # debug level for diagnosing the underlying Anitopy bug.
            import traceback
            self.msg.warn(f"Anitopy failed to parse '{self.original_file_name}': {e}")
            self.msg.debug(traceback.format_exc())
            data = {}

        self.episode_number = self.__extractEpisodeNumber(data)
        self.anime_title = self.__extractAnimeTitle(data)

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
            # Returning None here would read as "episode 0/unknown" to
            # callers that do arithmetic on it; 1 matches the no-episode
            # default above and keeps the file merely unmatched rather
            # than mis-matched.
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

    # A version marker that is part of the TITLE, not a version of the
    # release: 'NieR:Automata Ver1.1a', 'Ver.2.0'. Anitopy reads the
    # '1.1a' as the episode number and stops looking, so every episode
    # of such a show parsed as episode 1 (or failed outright). Joining
    # the digits into one token leaves it looking like a word instead --
    # and 'Ver11a' actually scores HIGHER against the real list title
    # than the truncated 'Ver1' Anitopy produced before.
    _TITLE_VERSION = re.compile(
        r'\bVer\.?\s?[0-9]+(?:\.[0-9]+)+[A-Za-z]?\b', re.IGNORECASE)

    def __preProcessFileName(self, file_name):
        # Make some adjustments to the file name to increase parsing accuracy
        file_name = self._TITLE_VERSION.sub(
            lambda m: m.group().replace('.', ''), file_name)

        # If full path is provided to Anitopy, all [brackets with contents]
        # adjacent to the path separators should be moved to the VERY beginning.
        # Or Anitopy won't be able to parse them out.
        file_name = os.path.sep + file_name
        for m in re.finditer(
                r'(?<={0})\[.*?\]|\[.*?\](?={0})'.format(os.path.sep),
                file_name):
            file_name = (m.group() + file_name[:m.start()]
                              + file_name[m.end():])

        # Remove all the path separators (except the last one, we'll need it later)
        parts = file_name.split(os.path.sep)
        file_name = ' '.join(parts)

        # Anitopy can parse S01E01 properly, but not S01OVA01, S01S01, S01NCOP01 etc.
        # So we'll need to break things down for the parser.
        m = self._SEASON_TOKEN.search(file_name)
        if m:
            groups = m.groupdict()
            kind = groups['type'].upper()
            # Remember what the token itself said. Rewriting the name and
            # letting Anitopy re-find the number is not good enough: for
            # 'NieR.Automata.Ver1.1a.S01E13...' it locks onto the '1a' of
            # the TITLE and reports episode 1, so episode 13 would be
            # recorded as episode 1 -- a wrong update, not a failed one.
            self.token_episode = groups['episode']
            # 'S01E01' -> 'Season 01 - 01'
            if kind == 'E':
                kind = ''
            # 'S01S01' -> 'Season 01 Specials - 01'
            elif kind == 'S':
                kind = 'Specials'
            # for all other cases:
            # 'S01{type}01' -> 'Season 01 {type} - 01'
            file_name = (
                file_name[:m.start()] + 'Season ' + groups['season']
                + ' ' + kind + ' - ' + groups['episode']
                + file_name[m.end():])

        return file_name

    @staticmethod
    def __trimFileName(file_name):
        # If the same title appears in the parent directory and the file name,
        # we might want to remove the duplicate one.
        # Otherwise, Anitopy would concatenate them together.
        try:
            # Temporarily replace all punctuations with spaces
            temp = re.sub(r'[^\w\s{0}\(\)\{{\}}\[\]]'.format(os.path.sep),
                          r' ', file_name, flags=re.ASCII)
            # Search and remove the longest duplicate
            m = max(
                [x for x in re.finditer(
                    r'(\b.{{3,}}\b)(?=.*?{0}.*?(?P<DUP>\1))'.format(os.path.sep),
                    temp, flags=re.IGNORECASE)],
                key = lambda y: y.end() - y.start()
            )
            if m:
                file_name = file_name[:m.start('DUP')] + ' ' + file_name[m.end('DUP'):]
        except ValueError:
            pass

        # Remove the remaining path separator(s)
        file_name = file_name.replace(os.path.sep, ' ')
        # Remove empty ( ) brackets
        file_name = re.sub(r'[\[\{\(][\s\._]*[\)\}\]]', r'', file_name)
        # Trim unnecessary     spaces
        file_name = re.sub(r'\s{2,}', r' ', file_name.strip())

        return file_name

    @staticmethod
    def __extractAnimeTitle(data):
        # Deal with anime title related stuff that Anitopy left out
        if 'anime_title' not in data:
            return None
        anime_title = data['anime_title']

        # Append anime season to the title (if needed)
        anime_season = data.get('anime_season')
        if anime_season:
            if isinstance(anime_season, list):
                anime_season = anime_season[0]
            if int(anime_season) > 1:
                anime_title += ' Season ' + anime_season

        # Solve 'Season X Part Y' cases
        if 'episode_title' in data:
            m = re.search(r'^Part [2-9]\b', data['episode_title'], flags=re.IGNORECASE)
            if m:
                anime_title += ' ' + m.group(0)

        # Append anime type to the title (if needed)
        anitype_invalid = ('OP', 'NCOP', 'OPENING', 'ED', 'NCED', 'ENDING', 'PV', 'PREVIEW')
        anitype_specials = ('OAD', 'OAV', 'ONA', 'OVA', 'SPECIAL', 'SPECIALS')
        anitype = data.get('anime_type')
        if anitype:
            if not isinstance(anitype, list):
                anitype = [anitype]
            for t in anitype:
                # Ignore non-episodes such as openings, endings, previews etc.
                if t.upper() in anitype_invalid:
                    return None
                if t not in anime_title and t.upper() in anitype_specials:
                    anime_title += ' ' + t
        else:
            # Fix anime type being detected as episode title
            if 'episode_title' in data:
                for t in anitype_specials:
                    m = re.search(r'{0}\b'.format(re.escape(t)), data['episode_title'],
                                  flags=re.IGNORECASE)
                    if m:
                        anime_title += ' ' + m.group(0)

        # Append anime year to the title (if needed)
        anime_year = data.get('anime_year')
        if anime_year and anime_year not in anime_title:
            anime_title += ' (' + anime_year + ')'

        return anime_title

    # A part suffix on an otherwise numeric episode: '1a', '20A', '01c',
    # 'NCOP1d'. Anime lists have no way to express them, so the part
    # letter is dropped and the number kept.
    _PART_SUFFIX = re.compile(r'(?<=[0-9])[A-Za-z]$')

    @classmethod
    def __stripPartSuffix(cls, value):
        if isinstance(value, str):
            return cls._PART_SUFFIX.sub('', value)
        if isinstance(value, list):
            return [cls.__stripPartSuffix(v) for v in value]
        return value

    def __extractEpisodeNumber(self, data):
        # Deal with episode related stuff that Anitopy left out
        episode_number = data.get('episode_number')

        # Handle cases like: "[Judas] Naruto - S05E01 (186).mkv"
        # Anitopy should detect the consecutive episode number (186) properly.
        # Just set that as the original episode number, and remove the season value.
        if 'episode_number_alt' in data:
            episode_number = data['episode_number_alt']
            del data['episode_number_alt']
            if 'anime_season' in data:
                del data['anime_season']
        elif self.token_episode is not None:
            # An explicit SxxEyy token beats whatever Anitopy inferred:
            # we matched the episode ourselves, character for character.
            # Only an _alt number (the absolute-numbering case above)
            # outranks it.
            episode_number = self.token_episode

        if episode_number is None:
            return None

        # Unfortunately, we can't have episode numbers like 1A, 1B, 1C
        # etc. NOTE: this used to be re.sub(r'ABCabc', '', ...) -- a
        # LITERAL six-character pattern that never matched anything, so
        # the stripping had simply never worked and every part-numbered
        # file failed with "Unable to parse episode number '1a'".
        return self.__stripPartSuffix(episode_number)
