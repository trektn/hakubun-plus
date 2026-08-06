"""Regressions for AnitopyWrapper, from a real library scan's warnings.

The wrapper exists to paper over Anitopy's edge cases, and four of its
patches were quietly broken -- two of them in the dangerous direction,
where a file parsed "successfully" as the WRONG episode.
"""
import pytest

anitopy = pytest.importorskip('anitopy')

from hakubun.parser.anitopy import AnitopyWrapper


class _Msg:
    """Messenger stand-in that records warnings instead of printing."""

    def __init__(self):
        self.warnings = []

    def with_classname(self, _name):
        return self

    def debug(self, *_a):
        pass

    def warn(self, text, *args):
        self.warnings.append(text % args if args else text)


def parse(name):
    msg = _Msg()
    return AnitopyWrapper(msg, name), msg


@pytest.mark.parametrize('name, episode', [
    # An explicit SxxEyy token is authoritative. Anitopy locked onto the
    # '1.1a' of the TITLE and reported episode 1, so watching episode 13
    # would have marked episode 1 watched -- a wrong update, not a
    # failed one.
    ('NieR.Automata.Ver1.1a.S01E13.1080p.B-Global.WEB-DL.AAC2.0.H.265'
     '-NanDesuKa.mkv', 13),
    ('NieR.Automata.Ver1.1a.S01E20.NieR.Automata.Ver1.1a.1080p.AMZN.'
     'WEB-DL.DDP2.0.H.264-VARYG.mkv', 20),
    # Same show, no season token: the version marker must stop looking
    # like an episode number on its own.
    ('[SubsPlease] NieR Automata Ver1.1a - 16 (1080p) [292E6BE3].mkv', 16),
    # Lowercase sXXeYY was never rewritten, so raw Anitopy got it and
    # crashed ("'NoneType' object has no attribute 'category'").
    ('[HiCost] Paranoia Agent - s01e13 - The Final Episode.mkv', 13),
    # Part suffixes: the strip was re.sub('ABCabc', ...) -- a literal
    # six-character pattern that never matched anything.
    ('[TardSubs] Nihon Animator Mihonichi 20A - ME!ME!ME! Chronic '
     '(BD 1080p) [62A4324E].mkv', 20),
    # Absolute numbering still outranks the season token.
    ('[Judas] Naruto - S05E01 (186).mkv', 186),
    # Ordinary names must be unaffected.
    ('[HorribleSubs] Boku no Hero Academia - 01 [1080p].mkv', 1),
    ('Show.Name.S02E05.mkv', 5),
])
def test_episode_number(name, episode):
    wrapper, _msg = parse(name)
    assert wrapper.getEpisode() == episode


def test_release_group_is_not_a_season_token():
    """'...H.265-LYS1TH3A' contains 'S1TH3', which the unanchored
    season regex matched as season 1 / type 'TH' / episode 3 -- it
    rewrote the filename into nonsense and every Fate/stay night movie
    reported episode '3A'."""
    name = ('Fate.stay.night.Heavens.Feel.III.Spring.Song.2020.1080p.'
            'BluRay.Opus5.1.H.265-LYS1TH3A.mkv')
    wrapper, msg = parse(name)
    assert wrapper.token_episode is None       # regex must not fire
    assert wrapper.getEpisode() == 1
    assert 'Heavens Feel III Spring Song' in wrapper.getName()
    assert not msg.warnings


@pytest.mark.parametrize('name', [
    '[Vodes] Jujutsu Kaisen NCOP1d.mkv',
    '[OZR] Bakemonogatari - NCED1a (BD 1080p Hi10 Opus) [128D62D5].mkv',
    '[Moxie] Tari Tari - NCED 01a (BD 1080p Remux FLAC) [525C9DED].mkv',
])
def test_creditless_openings_are_still_rejected(name):
    """Part suffixes now parse, but NCOP/NCED are not episodes and must
    keep returning no title so nothing matches them to a show."""
    wrapper, _msg = parse(name)
    assert wrapper.getName() is None


def test_part_numbered_files_no_longer_warn():
    """The whole class of 'Unable to parse episode number' spam."""
    for name in ('[SubsPlease] NieR Automata Ver1.1a - 01 (1080p).mkv',
                 'NieR.Automata.Ver1.1a.S01E06.1080p.WEBRip.mkv'):
        _wrapper, msg = parse(name)
        assert not [w for w in msg.warnings
                    if 'Unable to parse episode number' in w], name


def test_unparseable_episode_falls_back_to_one():
    """getEpisode() used to fall off the end and return None on a bad
    number, which callers then did arithmetic on."""
    wrapper, msg = parse('[Group] Some Show - 01 [1080p].mkv')
    wrapper.episode_number = 'not-a-number'
    assert wrapper.getEpisode() == 1
    assert any('Unable to parse episode number' in w for w in msg.warnings)
