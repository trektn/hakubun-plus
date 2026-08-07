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
    # anitomy-ng >= 1.0.8 matches the season/episode token case-insensitively;
    # before that the wrapper had to upcase it first.
    w = parse(msg, "[HiCost] Paranoia Agent - s01e02 - The Golden Shoes.mkv")
    assert w.getName() == "Paranoia Agent"
    assert w.getEpisode() == 2


def test_parent_directory_duplicate_title_is_stripped(msg):
    # parse_path strips the directory prefix. 'USBD' is classified as a
    # release/source tag rather than part of the title, which is what a
    # tracker's database expects to match against.
    w = parse(msg, "Bleach USBD Remux AB/Bleach S05 USBD Remux/"
                   "Bleach S05E14 2004 1080p Bluray REMUX AVC DTS-HD MA "
                   "2.0 Dual Audio -ZR-.mkv")
    assert w.getName() == "Bleach Season 05 (2004)"
    assert w.getEpisode() == 14


def test_no_credit_opening_is_excluded(msg):
    w = parse(msg, "NCOP #01.mkv")
    assert w.getName() is None


def test_checksum_starting_with_ed_is_not_mistaken_for_ending_type(msg):
    # An 8-hex-digit checksum starting with "ED" (both valid hex digits) used
    # to be misread as the ED (ending) type keyword, swallowing the checksum
    # and wrongly excluding a real episode. Fixed in anitomy-ng 1.0.8.
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


def test_episode_before_episode_title_without_a_dash(msg):
    # From a real MPRIS tracker log. Before anitomy-ng 1.0.8 both the episode
    # number and the episode title were folded into the title when the number
    # had no dash in front of it, so getEpisode() fell back to 1 and the file
    # was recorded as episode 1 of Beyblade X.
    w = parse(msg, "[HnY] Beyblade X 11 - Kadovar's Test (1080p) v2.mkv")
    assert w.getName() == "Beyblade X"
    assert w.getEpisode() == 11
    assert w.getEpisodeNumbers(True) == (11, 11)


def test_episode_before_a_single_word_episode_title(msg):
    # The old failure was not about how many words the episode title has; it
    # only looked intermittent because some episode titles are recognised by
    # keyword ("Episode ...") and recovered by accident.
    w = parse(msg, "[HnY] Beyblade X 11 - Test (1080p) v2.mkv")
    assert w.getName() == "Beyblade X"
    assert w.getEpisode() == 11


def test_dashed_episode_number_still_parses(msg):
    # The shape that always worked must not regress.
    w = parse(msg, "[HnY] Beyblade X - 11 - Kadovar's Test (1080p).mkv")
    assert w.getName() == "Beyblade X"
    assert w.getEpisode() == 11


def test_bare_trailing_episode_number_still_parses(msg):
    w = parse(msg, "[HnY] Beyblade X 11 (1080p) v2.mkv")
    assert w.getName() == "Beyblade X"
    assert w.getEpisode() == 11


def test_year_before_a_title_is_not_taken_as_an_episode(msg):
    # A four-digit year must not be salvaged as an episode number.
    w = parse(msg, "[Grp] Show 2011 - Movie Title (1080p).mkv")
    assert w.getEpisode() == 1


def test_path_with_no_shared_text_between_folder_and_file(msg):
    # parse_path: the folder and filename share no text, so there is nothing
    # to strip and the file's own episode/title still parse normally.
    w = parse(msg, "Random Folder Name/[SubsPlease] Frieren - 05 (1080p).mkv")
    assert w.getEpisode() == 5


def test_path_duplicate_folder_title_is_stripped(msg):
    # Contrast with the above: the parent folder's title is genuinely
    # duplicated in the filename, and must not survive into the title.
    w = parse(msg, "Beyblade X/Beyblade X - 11 - Kadovar's Test (1080p).mkv")
    assert w.getName() == "Beyblade X"
    assert w.getEpisode() == 11


def test_bare_episode_under_a_duplicate_folder_keeps_no_prefix(msg):
    # The plain 'Title - NN.mkv' shape, with no bracketed or parenthesised
    # token to anchor on. anitomy-ng 1.0.8 left the whole directory prefix
    # in the title ('Attack on Titan/Season 2/Attack on Titan'), which
    # matches nothing in a tracker database. Fixed in 1.0.9; pinned, so
    # assert it rather than trust the pin.
    w = parse(msg, "Attack on Titan/Season 2/Attack on Titan - 05.mkv")
    assert w.getName() == "Attack on Titan Season 2"
    assert w.getEpisode() == 5


def test_season_living_only_in_the_parent_folder_is_preserved(msg):
    # The season exists nowhere in the filename. 1.0.8 emitted no season
    # element for this, so __buildTitle silently dropped it and the file
    # recorded progress against season 1 of the same show.
    w = parse(msg, "Attack on Titan/Season 2/Attack on Titan - 05 (1080p).mkv")
    assert w.getName() == "Attack on Titan Season 2"
    assert w.getEpisode() == 5


def test_title_containing_op_is_not_read_as_an_opening_type(msg):
    # 'op.' inside the title region was tokenized as the OP (opening) type,
    # so __buildTitle excluded the file as a clip and every episode of the
    # show vanished from the library. Same class as 'Co-Ed' in an episode
    # title. Genuine standalone OP/ED clips must still be excluded -- see
    # test_no_credit_opening_is_excluded.
    w = parse(msg, "Takt op.Destiny (2021)/Season 01/Takt op.Destiny (2021) "
                   "- S01E01 - Overture [Bluray-1080p].mkv")
    assert w.getName() == "Takt op.Destiny (2021)"
    assert w.getEpisode() == 1

    w = parse(msg, "Cromartie High School - 1x03 - Cromartie High (Co-Ed).mkv")
    assert w.getName() == "Cromartie High School"
    assert w.getEpisode() == 3
