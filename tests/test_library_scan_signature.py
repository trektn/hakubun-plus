"""Regressions for the local-library scan-skip fingerprint.

_scan_library_if_changed() decides whether to re-walk the search
directories based on a signature. Before this fix that signature only
covered filesystem state (directory mtimes), so a tracker-list change --
a show added, removed, or retitled, with no local file ever touched --
was invisible to it: the cached library (and per-file guess cache) would
be reused forever, silently "un-finding" shows that were never scanned
against the new list. _tracker_list_signature() adds the missing half of
that fingerprint; these tests only exercise it directly, bypassing
Engine.__init__ (which needs a live account/API) since it depends on
nothing but self.config/self.mediainfo.
"""
import pytest

from hakubun.engine import Engine


def make_engine(scan_whole_list=False):
    engine = Engine.__new__(Engine)
    engine.config = {'scan_whole_list': scan_whole_list}
    engine.mediainfo = {
        'statuses': [1, 2, 3, 4, 5, 6],
        'statuses_start': [1, 2, 3],
        'statuses_library': [1, 2, 3, 4],
    }
    return engine


def tracker_list(shows):
    """shows: iterable of (id, titles-tuple)."""
    showlist = {i: {'id': i, 'title': t[0], 'titles': list(t),
                    'my_progress': 0, 'total': 0, 'type': None}
                for i, t in shows}
    return (showlist, {})


def test_scan_status_scope_honors_scan_whole_list():
    assert make_engine(scan_whole_list=True)._scan_status_scope() == [1, 2, 3, 4, 5, 6]
    assert make_engine(scan_whole_list=False)._scan_status_scope() == [1, 2, 3, 4]


def test_signature_is_deterministic():
    engine = make_engine()
    tl = tracker_list([(0, ('Cowboy Bebop',)), (1, ('Steins;Gate',))])
    assert engine._tracker_list_signature(tl) == engine._tracker_list_signature(tl)


def test_signature_changes_when_a_show_is_added():
    engine = make_engine()
    before = tracker_list([(0, ('Cowboy Bebop',))])
    after = tracker_list([(0, ('Cowboy Bebop',)), (1, ('Koukaku Kidoutai',))])
    assert engine._tracker_list_signature(before) != engine._tracker_list_signature(after)


def test_signature_changes_when_a_title_or_alias_changes():
    engine = make_engine()
    before = tracker_list([(0, ('Spy x Family',))])
    after = tracker_list([(0, ('Spy x Family Season 2',))])
    assert engine._tracker_list_signature(before) != engine._tracker_list_signature(after)


def test_signature_is_independent_of_dict_iteration_order():
    engine = make_engine()
    a = tracker_list([(0, ('Cowboy Bebop',)), (1, ('Steins;Gate', 'Steins Gate'))])
    b = tracker_list([(1, ('Steins Gate', 'Steins;Gate')), (0, ('Cowboy Bebop',))])
    assert engine._tracker_list_signature(a) == engine._tracker_list_signature(b)


def test_signature_is_stable_across_hash_randomization():
    # Python's built-in hash() is randomized per process (PYTHONHASHSEED),
    # so using it here would force a spurious rescan on every single
    # startup. Confirm the signature is a plain hex digest, not that.
    engine = make_engine()
    tl = tracker_list([(0, ('Cowboy Bebop',))])
    sig = engine._tracker_list_signature(tl)
    assert isinstance(sig, str)
    int(sig, 16)  # must be valid hex
