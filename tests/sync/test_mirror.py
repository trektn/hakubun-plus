"""Mirror: converge the TRACKERS to what Ownership says they should be.

Mirror is not "Sync, but harder". Sync is incremental and history-
driven; Mirror ignores history and makes current tracker state conform
to the ownership configuration. The properties these tests pin down:

  * Hakubun/local is never a tracker side -- it never appears in a
    mirror comparison, a mirror conflict or a mirror row, though it
    still converges silently as reconciliation state.
  * Ownership decides the desired tracker state; there is no second
    ownership system and no "owner -> Hakubun -> the others" hop.
  * Membership (does the entry exist there at all?) is its own model
    with its own decisions: add, remove, or leave alone.
  * A REMOVAL only ever comes from an explicit user decision, and is
    gated again at apply time. Nothing infers a deletion.
"""

import pytest

from conftest import FakeLib, make_engine, show
from hakubun.sync.models import FieldPolicy, PolicyKind


def own(store, **fields):
    """Set field ownership: own(store, score='provider:anilist')."""
    for field, policy in fields.items():
        store.set_ownership(field, FieldPolicy.parse(policy))


def three_trackers(store, kitsu_shows=None, **kw):
    """AniList + MAL both listing GHOST, Kitsu listing whatever is
    given (nothing, by default -- the missing-membership case)."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', mal_id=77,
                                       **kw.get('anilist', {}))])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', **kw.get('mal', {}))])
    kitsu = FakeLib('kitsu', kitsu_shows or [])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    return engine, anilist, mal, kitsu


# -- 1. trackers only, never Hakubun ----------------------------------

def test_mirror_never_compares_against_hakubun(store):
    """The core concept: local state is reconciliation state, not a
    tracker. No mirror operation may be sourced from or targeted at it
    in the tracker-facing categories."""
    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 50}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    plan = engine.mirror_plan()
    assert plan.updates, 'expected the trackers to be brought into line'
    for op in plan.updates + plan.adds:
        assert op.target != 'local'
        assert op.source != 'local'
    for conflict in plan.conflicts:
        assert 'local' not in conflict.values


def test_mirror_conflicts_list_only_trackers(store):
    """A manual-reconcile field where two trackers genuinely disagree
    is a decision between TRACKERS. Hakubun holds a value too -- it
    must not appear as a third opinion."""
    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='reconcile:manual')

    plan = engine.mirror_plan()
    score = [c for c in plan.conflicts if c.field == 'score']
    assert score, 'trackers disagree on score; expected a decision'
    assert 'local' not in score[0].values
    assert set(score[0].values) <= {'anilist', 'mal', 'kitsu'}


def test_mirror_still_converges_local_state_silently(store):
    """Local is not a tracker, but it must not be left stale either:
    an ordinary Sync afterwards would read local as the side that
    moved and push the old value straight back out."""
    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')
    # Make local genuinely stale, the way a drifted database is.
    uid = store.mapping_for('anilist', '9')['uuid']
    store.local_set(uid, 'score', 1.0, source='local')

    plan = engine.mirror_plan()
    assert [o for o in plan.local if o.field == 'score'], \
        'local should converge to the owner too'
    assert all(o.target == 'local' for o in plan.local)

    engine.apply_mirror(plan)
    assert store.local_get(uid)['score'][0] == pytest.approx(9.0)


# -- 2. ownership is the master ---------------------------------------

def test_ownership_decides_the_desired_tracker_state(store):
    """Score = AniList means Kitsu.Score -> AniList.Score and
    MAL.Score -> AniList.Score. Directly, tracker to tracker."""
    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    plan = engine.mirror_plan()
    targets = {o.target: o for o in plan.updates if o.field == 'score'}
    assert set(targets) == {'kitsu', 'mal'}
    for op in targets.values():
        assert op.source == 'anilist'
        assert op.new == pytest.approx(9.0)
        assert 'Anilist owns score' in op.reason


def test_mirror_ignores_history_where_sync_would_defer(store):
    """The point of Mirror: it converges current tracker state even
    when nothing 'changed'. Sync sees matching bases and does nothing;
    Mirror still brings the non-owners into line."""
    engine, anilist, mal, kitsu = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    # Ownership is chosen AFTER the first sync established the bases --
    # exactly the case where incremental sync has nothing to attribute.
    own(store, score='provider:anilist')

    assert engine.mirror_plan().updates
    engine.apply_mirror(engine.mirror_plan())
    assert kitsu.shows['k1']['my_score'] == pytest.approx(4.5)
    assert mal.shows['77']['my_score'] == 9


def test_owner_without_an_entry_asserts_nothing(store):
    """If the authoritative tracker doesn't hold the entry there is no
    authoritative value -- synthesizing one from a non-authority is the
    generic merge ownership exists to prevent."""
    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='provider:annict')     # a tracker that isn't here

    assert [o for o in engine.mirror_plan().updates
            if o.field == 'score'] == []


# -- 3. membership: missing entries -----------------------------------

def test_missing_entry_is_a_tracker_membership_discrepancy(store):
    """The acceptance scenario. AniList and MAL have it, Kitsu does
    not: Mirror reports that as a discrepancy between trackers, with
    the ownership-derived values the new entry would carry."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=50,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', status='completed')])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:anilist', status='provider:mal')

    plan = engine.mirror_plan()
    issue = next(i for i in plan.membership if i.title == 'GHOST')
    assert issue.present == ['anilist', 'mal']
    assert issue.missing == ['kitsu']
    assert issue.addable == ['kitsu']
    assert issue.values['score'] == pytest.approx(5.0)
    assert issue.values['status'] == 'completed'

    adds = {o.field: o for o in plan.adds if o.target == 'kitsu'}
    assert adds['score'].new == pytest.approx(5.0)
    assert adds['status'].new == 'completed'
    # Justified by the trackers that actually list it, never by us.
    assert adds['score'].provenance == ('anilist', 'mal')
    for op in adds.values():
        assert op.selected is False       # opt-in, always


def test_unmapped_tracker_is_an_identity_gap_not_an_add(store):
    """adapter.add needs an id. A tracker identity has never matched
    cannot be added to, so Mirror must report an identity gap rather
    than offer a button that would quietly do nothing."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST')])
    mal = FakeLib('mal', [])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}

    plan = engine.mirror_plan()
    issue = next(i for i in plan.membership if i.title == 'GHOST')
    assert issue.unmapped == ['mal']
    assert issue.addable == []
    assert [o for o in plan.adds if o.target == 'mal'] == []


def test_settled_discrepancy_is_not_reproposed(store):
    """A decision persists: the next mirror does not rediscover it."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST')])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('anilist', '9')['uuid']

    assert [o for o in engine.mirror_plan().adds if o.target == 'kitsu']
    engine.set_membership(uid, 'kitsu', 'ignore')
    plan = engine.mirror_plan()
    assert [o for o in plan.adds if o.target == 'kitsu'] == []
    # Still SHOWN, with the decision, rather than silently dropped.
    issue = next(i for i in plan.membership if i.title == 'GHOST')
    assert issue.missing == ['kitsu']
    assert issue.decisions['kitsu'] == 'ignore'


# -- 4. membership: unwanted entries ----------------------------------

def test_removal_requires_an_explicit_decision(store):
    """Nothing infers a deletion. With all three trackers holding the
    entry and no decision recorded, Mirror proposes no removal."""
    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', mal_id=77)])
    assert engine.fetch() == {}
    assert engine.mirror_plan().removes == []


def test_marking_a_tracker_absent_proposes_removal(store):
    """'Kitsu should not have this' is a distinct state from 'never
    add this to Kitsu', and it produces a Remove operation."""
    engine, _al, _mal, kitsu = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', mal_id=77)])
    assert engine.fetch() == {}
    uid = store.mapping_for('kitsu', 'k1')['uuid']

    engine.set_membership(uid, 'kitsu', 'absent')
    plan = engine.mirror_plan()
    assert [(r.provider, r.title) for r in plan.removes] \
        == [('kitsu', 'GHOST')]
    assert plan.removes[0].selected is False    # opt-in, always

    for op in plan.removes:
        op.selected = True
    result = engine.apply_mirror(plan, allow_removes=True)
    assert result['removed'] == 1
    assert 'k1' not in kitsu.shows
    assert kitsu.deletes[0]['my_id'] == 'entry-k1'


def test_declining_a_creation_never_becomes_a_deletion(store):
    """The distinction the old single 'never add here' state could not
    express. Declining, then the entry legitimately appearing on that
    tracker later, must NOT produce a removal proposal."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST')])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('anilist', '9')['uuid']

    engine.decline_create(uid, 'kitsu')
    assert store.membership_of(uid)['kitsu'] == 'ignore'

    # The user adds it on kitsu.app themselves afterwards.
    kitsu.shows['k1'] = show('kitsu', 'k1', 'GHOST', mal_id=77)
    assert engine.fetch() == {}
    assert engine.mirror_plan().removes == []


def test_website_deletion_never_becomes_a_deletion_proposal(store):
    """Same guarantee for the other historical reason: 'the user
    removed it on the website' is an observation, not a standing
    instruction to delete it again if it comes back."""
    mal = FakeLib('mal', [show('mal', 1, 'GHOST'),
                          show('mal', 2, 'Other')])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k1', 'GHOST', mal_id=1),
                              show('kitsu', 'k2', 'Other', mal_id=2)])
    engine = make_engine(store, {'mal': mal, 'kitsu': kitsu})
    assert engine.fetch() == {}
    uid = store.mapping_for('kitsu', 'k1')['uuid']

    del kitsu.shows['k1']
    assert engine.fetch() == {}
    assert store.membership_of(uid)['kitsu'] == 'ignore'

    kitsu.shows['k1'] = show('kitsu', 'k1', 'GHOST', mal_id=1)
    assert engine.fetch() == {}
    assert engine.mirror_plan().removes == []


def test_legacy_absence_rows_migrate_to_ignore_not_absent(store):
    """Databases written before membership existed recorded declines
    and website-deletions in resolved_absent. Migrating either into
    'absent' would turn a stale row into a deletion proposal for an
    entry the user may since have re-added on purpose."""
    from hakubun.sync.store import SyncStore

    uid = store.create_entity('GHOST')
    store._exec('INSERT INTO resolved_absent(uuid, provider, checked_at,'
                ' reason) VALUES(?,?,?,?)', (uid, 'kitsu', 0, 'declined'))
    store._exec('INSERT INTO resolved_absent(uuid, provider, checked_at,'
                ' reason) VALUES(?,?,?,?)', (uid, 'mal', 0, 'deleted'))
    store._exec('INSERT INTO resolved_absent(uuid, provider, checked_at,'
                ' reason) VALUES(?,?,?,?)',
                (uid, 'anilist', 0, 'lookup_miss'))

    store._migrate_membership()
    assert store.membership_of(uid) == {'kitsu': 'ignore',
                                        'mal': 'ignore'}
    # The lookup cache stays a lookup cache.
    assert store.absent_for_provider('anilist') == {uid}
    assert store.absent_for_provider('kitsu') == set()
    assert SyncStore  # imported for the docstring's sake


# -- 5. bulk gates ----------------------------------------------------

def test_adds_and_removes_are_gated_independently(store):
    """The user must be able to approve additions but not deletions,
    deletions but not additions, both, or neither -- and the gate is
    enforced in the ENGINE, not in a dialog, because there are two UIs
    and a headless path."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST')])
    kitsu = FakeLib('kitsu', [show('kitsu', 'k9', 'Doomed')],
                    mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    doomed = store.mapping_for('kitsu', 'k9')['uuid']
    engine.set_membership(doomed, 'kitsu', 'absent')

    def fresh():
        plan = engine.mirror_plan()
        for op in plan.adds + plan.removes:
            op.selected = True
        return plan

    # Default: neither. Ticking every box is still not enough.
    result = engine.apply_mirror(fresh())
    assert result['removed'] == 0 and kitsu.deletes == []
    assert 'k1' not in kitsu.shows
    assert result['skipped'] > 0

    # Additions only.
    result = engine.apply_mirror(fresh(), allow_adds=True)
    assert result['removed'] == 0 and kitsu.deletes == []
    assert 'k1' in kitsu.shows

    # Removals only.
    result = engine.apply_mirror(fresh(), allow_removes=True)
    assert result['removed'] == 1
    assert 'k9' not in kitsu.shows


def test_plan_counts_drive_the_confirmation_dialog(store):
    """The bulk confirmation quotes per-tracker numbers before
    anything is applied."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5)])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    counts = engine.mirror_plan().counts()
    assert counts['add'] == {'kitsu': 1}      # one ENTRY, not one field
    assert counts['update'].get('mal') == 1
    assert counts['remove'] == {}


def test_fields_are_not_pushed_to_an_entry_being_removed(store):
    """Pushing a score and then deleting the entry is wasted API calls
    and a confusing partial state."""
    engine, _al, _mal, kitsu = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 90})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')
    uid = store.mapping_for('kitsu', 'k1')['uuid']
    engine.set_membership(uid, 'kitsu', 'absent')

    plan = engine.mirror_plan()
    assert [o for o in plan.updates if o.target == 'kitsu'], \
        'the score push is planned; it must be dropped at apply time'
    for op in plan.removes:
        op.selected = True
    engine.apply_mirror(plan, allow_removes=True)
    assert kitsu.updates == []
    assert kitsu.deletes


def test_a_failed_delete_does_not_suppress_its_field_updates(store):
    """If the deletion did not actually happen, the entry is still
    there and still wrong -- its field updates must not be dropped on
    the assumption it is gone."""
    engine, _al, _mal, kitsu = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 90})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')
    uid = store.mapping_for('kitsu', 'k1')['uuid']
    engine.set_membership(uid, 'kitsu', 'absent')

    plan = engine.mirror_plan()
    for op in plan.removes:
        op.selected = True
    kitsu.fail_update = True                  # the delete fails
    result = engine.apply_mirror(plan, allow_removes=True)
    kitsu.fail_update = False
    assert result['removed'] == 0
    assert 'kitsu' in result['errors']
    assert [o for o in plan.updates if o.target == 'kitsu']


def test_provider_that_cannot_delete_reports_instead_of_pretending(store):
    engine, _al, _mal, kitsu = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', mal_id=77)])
    assert engine.fetch() == {}
    uid = store.mapping_for('kitsu', 'k1')['uuid']
    engine.set_membership(uid, 'kitsu', 'absent')
    kitsu.extra_info = {'can_delete': False}

    plan = engine.mirror_plan()
    for op in plan.removes:
        op.selected = True
    result = engine.apply_mirror(plan, allow_removes=True)
    assert result['removed'] == 0
    assert 'kitsu' in result['errors']
    assert kitsu.deletes == []


# -- 6. Mirror does not disturb ordinary Sync -------------------------

def test_mirror_does_not_change_the_ordinary_sync_plan_shape(store):
    """Sync keeps its own semantics: Hakubun still appears there as
    the app's working copy, and creations are still offered unticked
    in the New Entries category."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', mal_id=77)])
    mal = FakeLib('mal', [])
    engine = make_engine(store, {'anilist': anilist, 'mal': mal})
    assert engine.fetch() == {}
    plan = engine.plan()
    assert hasattr(plan, 'changes') and hasattr(plan, 'conflicts')
    assert all(c.selected is False
               for c in plan.changes if c.creates_entry)


def test_mirror_leaves_individual_fields_alone(store):
    """A field configured never to sync does not sync during a mirror
    either -- Mirror reads the same ownership configuration."""
    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='individual')
    assert [o for o in engine.mirror_plan().updates
            if o.field == 'score'] == []


# -- 7. presentation: never name Hakubun as a tracker -----------------

def test_mirror_presentation_never_names_hakubun(store):
    """§11: user-facing tracker reconciliation shows tracker sides and
    the rule that applies -- not Hakubun competing for ownership."""
    from hakubun.sync import present

    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='reconcile:manual')
    plan = engine.mirror_plan()

    adapters = engine.adapters
    for op in plan.updates:
        _direction, text = present.mirror_change_line(adapters, op)
        assert 'Hakubun' not in text
    assert plan.conflicts, 'expected a tracker-vs-tracker decision'
    for conflict in plan.conflicts:
        why = present.mirror_conflict_why(conflict)
        assert 'Hakubun' not in why
        assert 'between your trackers' in why
    assert 'Hakubun' not in present.mirror_plan_summary(plan)


def test_membership_presentation_reads_as_a_tracker_discrepancy(store):
    from hakubun.sync import present

    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST')])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}

    issue = next(i for i in engine.mirror_plan().membership
                 if i.title == 'GHOST')
    rows = present.mirror_membership_lines(issue)
    assert ('Anilist', True, '') in rows
    assert ('Kitsu', False, '') in rows
    why = present.mirror_membership_why(issue)
    assert 'Kitsu' in why and 'Hakubun' not in why
    assert present.mirror_remove_label('kitsu') == 'Remove from Kitsu'


def test_bulk_confirmation_quotes_per_tracker_numbers(store):
    from hakubun.sync import present

    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=5)])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')

    text = present.mirror_confirmation(engine.mirror_plan())
    assert 'Add:' in text and 'Kitsu: 1 entries' in text
    assert 'Update:' in text


# -- 8. resolving a mirror decision actually settles it ---------------

def test_resolving_a_mirror_conflict_settles_it_and_syncs_trackers(store):
    """The failure this guards: Sync records a resolution by writing
    local state and advancing bases, and Mirror reads NEITHER. Resolved
    that way, the identical conflict comes back on every preview -- the
    user clicks a side, the card reappears, forever."""
    engine, _al, mal, kitsu = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='reconcile:manual')

    conflict = next(c for c in engine.mirror_plan().conflicts
                    if c.field == 'score')
    engine.resolve_mirror_conflict(conflict, 'anilist')

    plan = engine.mirror_plan()
    assert [c for c in plan.conflicts if c.field == 'score'] == [], \
        'the resolved conflict must not come back'
    targets = {o.target: o.new for o in plan.updates if o.field == 'score'}
    assert targets['kitsu'] == pytest.approx(9.0)
    assert targets['mal'] == pytest.approx(9.0)

    engine.apply_mirror(plan)
    assert kitsu.shows['k1']['my_score'] == pytest.approx(4.5)
    assert mal.shows['77']['my_score'] == 9


def test_a_resolution_lapses_when_a_tracker_moves(store):
    """The decision answered a question about a particular state of the
    world. When that changes it is a new question, and replaying the
    old verdict silently would be wrong."""
    engine, _al, mal, _kitsu = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='reconcile:manual')

    conflict = next(c for c in engine.mirror_plan().conflicts
                    if c.field == 'score')
    engine.resolve_mirror_conflict(conflict, 'anilist')
    assert not [c for c in engine.mirror_plan().conflicts
                if c.field == 'score']

    mal.shows['77']['my_score'] = 3          # changed on the website
    assert engine.fetch() == {}
    assert [c for c in engine.mirror_plan().conflicts
            if c.field == 'score'], 'a moved tracker reopens the question'


def test_explicit_value_resolution_is_honoured(store):
    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=2.0, mal_id=77)],
        anilist={'score': 90}, mal={'score': 5})
    assert engine.fetch() == {}
    own(store, score='reconcile:manual')

    conflict = next(c for c in engine.mirror_plan().conflicts
                    if c.field == 'score')
    engine.resolve_mirror_conflict(conflict, 'value', value=6.0)
    plan = engine.mirror_plan()
    assert not [c for c in plan.conflicts if c.field == 'score']
    assert all(o.new == pytest.approx(6.0) for o in plan.updates
               if o.field == 'score')


def test_structural_mirror_conflicts_are_information_only(store):
    """Each tracker's progress is in its own episode structure: there
    is no single value to adopt and no honest conversion, so Mirror
    refuses rather than inventing one."""
    from hakubun.sync.models import FieldConflict, FieldPolicy, PolicyKind

    engine = make_engine(store, {})
    conflict = FieldConflict(
        'u1', 'progress', {'kitsu': 1, 'mal': 4},
        policy=FieldPolicy(PolicyKind.RECONCILE, strategy='progress'),
        structural=True)
    with pytest.raises(ValueError):
        engine.resolve_mirror_conflict(conflict, 'kitsu')


# -- 9. one plan, one answer per field --------------------------------

def test_a_created_entry_is_seeded_from_the_trackers_not_local(store):
    """A single plan must not carry two different answers for one
    field: the trackers that have the entry got the tracker-reconciled
    value while a newly created one got local's. They differ exactly
    when local is stale -- the case Mirror exists to fix."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=7)])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='reconcile:max')
    uid = store.mapping_for('anilist', '9')['uuid']
    store.local_set(uid, 'score', 1.0, source='local')   # stale

    plan = engine.mirror_plan()
    add = next(o for o in plan.adds
               if o.target == 'kitsu' and o.field == 'score')
    assert add.new == pytest.approx(9.0), \
        'the new entry must carry the tracker-derived value, not local'
    issue = next(i for i in plan.membership if i.title == 'GHOST')
    assert issue.values['score'] == pytest.approx(9.0)


def test_an_owned_field_the_owner_lacks_seeds_nothing(store):
    """No authoritative value means none is asserted -- for a created
    entry as much as for an existing one. Better a field left unset
    than one filled in from a tracker the configuration says does not
    decide it."""
    anilist = FakeLib('anilist', [show('anilist', 9, 'GHOST', score=90,
                                       mal_id=77)])
    mal = FakeLib('mal', [show('mal', 77, 'GHOST', score=7)])
    kitsu = FakeLib('kitsu', [], mal_id_index={77: 'k1'})
    engine = make_engine(store, {'anilist': anilist, 'mal': mal,
                                 'kitsu': kitsu})
    assert engine.fetch() == {}
    own(store, score='provider:annict')     # a tracker that isn't here

    plan = engine.mirror_plan()
    assert [o for o in plan.adds
            if o.target == 'kitsu' and o.field == 'score'] == []


# -- 10. local-only convergence is reachable --------------------------

def test_a_local_only_mirror_is_not_reported_as_clean(store):
    """Both windows gate their apply button on `clean`. A plan whose
    only work is bringing Hakubun's stale copy back into line is still
    work -- reported clean, it had a dead button and a summary claiming
    all was well."""
    from hakubun.sync import present

    engine, *_ = three_trackers(
        store,
        kitsu_shows=[show('kitsu', 'k1', 'GHOST', score=4.5, mal_id=77)],
        anilist={'score': 90}, mal={'score': 9})
    assert engine.fetch() == {}
    own(store, score='provider:anilist')
    uid = store.mapping_for('anilist', '9')['uuid']
    store.local_set(uid, 'score', 1.0, source='local')

    plan = engine.mirror_plan()
    assert plan.updates == [] and plan.adds == [] and plan.removes == []
    assert plan.local, 'only Hakubun is out of line'
    assert not plan.clean
    assert 'Hakubun' in present.mirror_plan_summary(plan)

    engine.apply_mirror(plan)
    assert store.local_get(uid)['score'][0] == pytest.approx(9.0)
