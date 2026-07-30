import pytest

anitomy_ng = pytest.importorskip("anitomy_ng")

from hakubun.messenger import Messenger
from hakubun.parser.anitomy_ng import AnitomyNgWrapper


@pytest.fixture
def msg():
    return Messenger(None, 'Test')


def parse(msg, filename):
    return AnitomyNgWrapper(msg, filename)


def test_version_number_title_not_mistaken_for_episode(msg):
    # NieR:Automata Ver1.1a -- real upstream Anitomy misreads "1.1a" as the
    # episode number and either mis-tags every episode as 1 or crashes.
    # anitomy-ng separates the version marker from the season/episode
    # token natively.
    w = parse(msg, "NieR.Automata.Ver1.1a.S01E13.1080p.B-Global.WEB-DL"
                   ".AAC2.0.H.265-NanDesuKa.mkv")
    assert w.getEpisode() == 13


def test_lowercase_season_episode_token(msg):
    # anitomy-ng's season/episode matching is case-sensitive and misses a
    # bare lowercase 's01e02' entirely (confirmed against real upstream
    # Anitomy too). The wrapper upcases the token before parsing.
    w = parse(msg, "[HiCost] Paranoia Agent - s01e02 - The Golden Shoes.mkv")
    assert w.getName() == "Paranoia Agent"
    assert w.getEpisode() == 2


def test_parent_directory_duplicate_title_is_stripped(msg):
    w = parse(msg, "Bleach USBD Remux AB/Bleach S05 USBD Remux/"
                   "Bleach S05E14 2004 1080p Bluray REMUX AVC DTS-HD MA "
                   "2.0 Dual Audio -ZR-.mkv")
    assert w.getName() == "Bleach USBD Season 05 (2004)"
    assert w.getEpisode() == 14


def test_no_credit_opening_is_excluded(msg):
    w = parse(msg, "NCOP #01.mkv")
    assert w.getName() is None


def test_checksum_starting_with_ed_is_not_mistaken_for_ending_type(msg):
    # An 8-hex-digit checksum starting with "ED" (both valid hex digits)
    # can be misread as the ED (ending) type keyword, silently swallowing
    # the rest of the checksum and wrongly excluding a real episode. Only
    # trust a bare ED/OP type when a separate file_checksum element was
    # also found, confirming it wasn't a checksum fragment.
    w = parse(msg, "[SubsPlease] Yoru no Kurage wa Oyogenai - 12 (1080p) "
                   "[ED8648EA].mkv")
    assert w.getName() == "Yoru no Kurage wa Oyogenai"
    assert w.getEpisode() == 12


def test_real_ending_clip_with_genuine_checksum_is_still_excluded(msg):
    # Contrast with the case above: here a real file_checksum element
    # (F94F020D) is present alongside the OP type, confirming it's a
    # genuine ending/opening clip rather than a checksum collision.
    w = parse(msg, "[Coalgirls]_Yuru_Yuri_(1920x1080_Blu-Ray_FLAC)/"
                   "[Coalgirls]_Yuru_Yuri_OP_(1920x1080_Blu-Ray_FLAC)_"
                   "[F94F020D].mkv")
    assert w.getName() is None
