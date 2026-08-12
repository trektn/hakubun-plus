"""Mirror's preview LAYOUT: one card per work, not a pile of ops.

The engine's natural unit is the field operation. A person's unit is
the title: "what happens to Cowboy Bebop?" The first Mirror tab asked
them to answer that by reading three tabs and correlating rows, and
adding one entry to one tracker cost six checkboxes.

These tests pin the projection that fixes it -- the same shape MALSync's
list sync uses (an authoritative entry at the top of a card, trackers
below it as deltas), generalized so the top row is assembled per field
from each field's own owner rather than taken from one master list.

They also pin the two things that projection revealed:

  * an entry is created WHOLE -- one decision, every field -- because
    apply() batches a creation by (provider, entity) and would
    otherwise happily create an entry from an arbitrary ticked subset;
  * a completed work has a defined progress on every tracker (that
    tracker's own total), so differing episode structures stop being
    an unanswerable conflict on exactly the entries where the answer
    is obvious.
"""

import pytest

from conftest import FakeLib, make_engine, show
from hakubun.sync import present
from hakubun.sync.models import FieldPolicy


def own(store, **fields):
    for field, policy in fields.items():
        store.set_ownership(field, FieldPolicy.parse(policy))


def cards(engine, plan, category='all'):
    return present.mirror_cards(plan, engine.adapters, category)


def card_named(engine, plan, title, category='all'):
    return next(c for c in cards(engine, plan, category) if c.title == title)


def row(card, provider):
    return next(t for t in card.trackers if t.provider == provider)


# -- 1. one card per work ---------------------------------------------

def test_a_work_is_one_card_covering_every_tracker(store):
    """The whole point: one place to answer "what happens to this
    title", instead of correlating rows across tabs."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5)])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', score=2.0,
                                   mal_id=77)])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    plan = engine.mirror_plan()
    all_cards = cards(engine, plan)
    assert len(all_cards) == 1
    card = all_cards[0]
    assert card.title == 'GHOST'
    assert {t.provider for t in card.trackers} == {'anilist', 'mal', 'kitsu'}


def test_the_card_opens_with_what_ownership_says_it_should_be(store):
    """MALSync anchors each card on the master list's entry. Ownership's
    equivalent is synthesized per field from that field's own owner --
    which is the whole difference between the two models, so it has to
    be visible."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5,
                               status='completed')])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    own(store, score='provider:anilist', status='provider:mal')

    card = card_named(engine, engine.mirror_plan(), 'GHOST')
    desired = {name: (value, owner) for name, value, owner, _why
               in card.desired}
    # Two different owners, one row -- no single master could say this.
    assert desired['Score'] == ('9', 'Anilist')
    assert desired['Status'] == ('Completed', 'Mal')


def test_a_tracker_row_shows_its_whole_entry_not_only_the_deltas(store):
    """Arrows need something to stand against. "Score 5 -> 9" alone
    leaves the reader wondering what else is about to move."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       status='completed', mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5,
                               status='completed')])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    card = card_named(engine, engine.mirror_plan(), 'GHOST')
    mal_row = row(card, 'mal')
    shown = dict(mal_row.values)
    assert shown['Score'] == '5'            # its own scale, its own value
    assert shown['Status'] == 'Completed'   # unchanged, and still shown
    assert [c[0] for c in mal_row.changes] == ['Score']


def test_the_owning_tracker_says_so_on_its_own_row(store):
    """"Why is Kitsu changing and AniList not?" is answered in place."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5)])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    card = card_named(engine, engine.mirror_plan(), 'GHOST')
    assert row(card, 'anilist').owns == ['Score']
    assert row(card, 'mal').owns == []
    assert row(card, 'mal').changes


def test_progress_is_shown_against_the_tracker_it_belongs_to(store):
    """"12" means nothing without the structure it counts against."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', progress=12,
                                       total=26, mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', progress=3, total=26)])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    own(store, progress='provider:anilist')

    card = card_named(engine, engine.mirror_plan(), 'GHOST')
    assert dict(row(card, 'mal').values)['Watched Episodes'] == '3 / 26'
    change = row(card, 'mal').changes[0]
    assert change[1] == '3 / 26' and change[2] == '12 / 26'


# -- 2. a created entry is ONE decision -------------------------------

def test_adding_an_entry_is_one_row_not_one_row_per_field(store):
    """200 entries x 6 fields is 1200 checkboxes describing 200
    decisions."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       status='completed', progress=4,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=9,
                               status='completed', progress=4)])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    plan = engine.mirror_plan()
    assert len([o for o in plan.adds if o.provider == 'kitsu']) == 1

    card = card_named(engine, plan, 'GHOST')
    kitsu_row = row(card, 'kitsu')
    assert kitsu_row.action == 'add'
    assert kitsu_row.present is False
    # One row, carrying everything the entry will start with.
    assert len(dict(kitsu_row.add_values)) > 1


def test_an_entry_is_created_whole_or_not_at_all(store):
    """apply() batches a creation by (provider, entity) and sends
    whatever fields it was handed. With per-field selection a user could
    tick Score, leave Status, and get a real entry on a real account
    with a score and no status. One decision per entry makes that
    unrepresentable."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       status='completed', mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=9,
                               status='completed')])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    plan = engine.mirror_plan()
    add = next(o for o in plan.adds if o.provider == 'kitsu')
    add.selected = True
    engine.apply_mirror(plan, allow_adds=True)

    created = kitsu.shows['k1']
    assert created['my_score'] == pytest.approx(4.5)
    assert created['my_status'] is not None, \
        'a created entry must carry every field it was planned with'


# -- 3. completed works have a defined progress everywhere ------------

def test_a_completed_work_goes_to_each_trackers_own_total(store):
    """MAL lists two 13-episode seasons where AniList lists one run of
    26. Partial progress across those structures genuinely cannot be
    translated -- but "finished" can: it is that tracker's own total.
    Raising a conflict here asks a question that has an answer."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', progress=26,
                                       total=26, status='completed',
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', progress=5, total=13,
                               status='completed')])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    own(store, progress='provider:anilist', status='provider:anilist')

    plan = engine.mirror_plan()
    assert not [c for c in plan.conflicts if c.field == 'progress'], \
        'completed is answerable; it must not be a structural conflict'
    push = next(o for o in plan.updates
                if o.field == 'progress' and o.target == 'mal')
    assert push.new == 13, "MAL's own total, not AniList's 26"


def test_an_unfinished_work_still_refuses_to_guess(store):
    """The completed rule is narrow on purpose. 5/13 against a
    26-episode listing has no honest answer, and inventing one would
    silently rewrite the user's progress."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', progress=20,
                                       total=26, status='watching',
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', progress=5, total=13,
                               status='watching')])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    own(store, progress='provider:anilist', status='provider:anilist')

    plan = engine.mirror_plan()
    assert [c for c in plan.conflicts if c.field == 'progress']


# -- 4. the filter replaces the tabs, without losing them -------------

def test_a_category_filter_narrows_to_the_same_divisions_the_tabs_had(
        store):
    """The categories survive as a filter -- how you narrow a large
    plan -- rather than as five separate places to look."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77),
                                  show('anilist', 10, 'SOLO', score=80,
                                       mal_id=78)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5),
                          show('mal', 78, 'SOLO', score=8)])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1', 78: 'k2'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    plan = engine.mirror_plan()
    assert cards(engine, plan, 'add'), 'Kitsu is missing both entries'
    assert cards(engine, plan, 'remove') == [], 'nothing was marked absent'
    for card in cards(engine, plan, 'update'):
        assert any(t.changes for t in card.trackers)


def test_cards_that_do_nothing_are_not_shown(store):
    """A tracker that already agrees is not news."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=9)])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    assert cards(engine, engine.mirror_plan()) == []


def test_decisions_sort_above_everything_else(store):
    """Nothing else can be applied confidently until they are answered,
    so they are not at the bottom of a long list."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77),
                                  show('anilist', 10, 'SOLO', score=80,
                                       mal_id=78)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5),
                          show('mal', 78, 'SOLO', score=8)])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    # GHOST disagrees and cannot be settled; SOLO is a plain push.
    own(store, score='reconcile:manual')

    ordered = cards(engine, engine.mirror_plan())
    if ordered and any(c.conflicts for c in ordered):
        assert ordered[0].conflicts


# -- 5. the headline ---------------------------------------------------

def test_a_collapsed_card_says_what_happens_to_the_title(store):
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5)])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    card = card_named(engine, engine.mirror_plan(), 'GHOST')
    headline = present.mirror_card_headline(card)
    assert 'Kitsu' in headline and 'add' in headline
    assert 'Mal' in headline


def test_hakubun_is_never_a_tracker_row(store):
    """Local converges, and is disclosed -- but as this app's own copy,
    never as a peer of AniList and Kitsu."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=9)])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')
    uid = store.mapping_for('anilist', '9')['uuid']
    store.local_set(uid, 'score', 1.0, source='local')

    card = card_named(engine, engine.mirror_plan(), 'GHOST')
    assert 'local' not in {t.provider for t in card.trackers}
    assert card.local, "Hakubun's copy is stale and must be disclosed"
