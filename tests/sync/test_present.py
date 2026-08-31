"""Presentation-layer wording and helpers shared by both sync windows:
the user-facing model must call the app's own state 'Hakubun' (never a
provider), summarize a plan as changes / decisions / new entries, and
offer per-field sync rules in plain language."""

from hakubun.sync import present
from hakubun.sync.models import (FieldConflict, FieldPolicy, PolicyKind,
                                 SyncOperation, SyncPlan)


def op(**kw):
    base = dict(uuid='u1', field='progress', old=1, new=2, target='mal',
                source='local', title='Frieren')
    base.update(kw)
    return SyncOperation(**base)


# -- Hakubun, never 'Local' or a provider -----------------------------

def test_local_side_is_named_after_the_app():
    assert present.local_label() == 'Hakubun'
    # The signed-in provider must NOT rename the app's own state.
    assert present.local_label('anilist') == 'Hakubun'
    assert present.source_label('local') == 'Hakubun'
    assert present.source_label('mal') == 'Mal'


def test_change_line_speaks_in_consequences():
    _dir, text = present.change_line({}, op(target='local', source='mal'))
    assert text.startswith('Update Hakubun from Mal')
    _dir, text = present.change_line({}, op(target='mal'))
    assert text.startswith('Update Mal')
    _dir, text = present.change_line(
        {}, op(target='mal', source='kitsu', old=None, new=5,
               creates_entry=True, selected=False,
               provenance=('anilist', 'kitsu')))
    assert text.startswith('Add to Mal')
    # A create names its provenance -- the provider entries that
    # establish existence -- never 'Hakubun'.
    assert 'exists on Anilist and Kitsu' in text
    assert 'Hakubun' not in text


def test_conflict_why_lists_each_side_by_name():
    conflict = FieldConflict(
        uuid='u1', field='score',
        values={'local': 8.0, 'anilist': 9.0, 'mal': 8.0},
        policy=FieldPolicy(PolicyKind.RECONCILE, strategy='manual'),
        title='Frieren')
    why = present.conflict_why(conflict)
    assert 'Hakubun:  8' in why
    assert 'Anilist:  9' in why
    assert 'local' not in why           # no engine jargon
    assert 'reconcile' not in why.lower()
    assert 'ask you' in why             # plain-language explanation


def test_conflict_choice_labels():
    conflict = FieldConflict(
        uuid='u1', field='score', values={'local': 8.0, 'mal': 7.0},
        policy=FieldPolicy(PolicyKind.RECONCILE, strategy='manual'))
    assert present.conflict_choice_label(conflict, 'local') \
        == 'Keep Hakubun: 8'
    assert present.conflict_choice_label(conflict, 'mal') == 'Use Mal: 7'


# -- plan summaries ---------------------------------------------------

def _plan():
    return SyncPlan(changes=[
        op(uuid='u%d' % i) for i in range(3)
    ] + [op(uuid='c1', old=None, creates_entry=True, selected=False)],
        conflicts=[FieldConflict(
            uuid='x', field='score', values={'local': 1, 'mal': 2},
            policy=FieldPolicy(PolicyKind.RECONCILE, strategy='manual'))])


def test_plan_counts_separates_creates_from_changes():
    assert present.plan_counts(_plan()) == (3, 1, 1)


def test_plan_summary_and_status():
    plan = _plan()
    summary = present.plan_summary(plan)
    assert '3 change(s)' in summary
    assert '1 decision(s) needed' in summary
    assert '1 new entry' in summary
    status = present.plan_status(plan)
    assert status.startswith('Found 3 change(s)')
    assert 'decision' in status
    assert 'new entry' in status
    assert present.plan_summary(SyncPlan()) == 'Everything is in sync.'


def test_plan_status_reports_matching_and_errors():
    plan = SyncPlan(identity=[object()], errors={'mal': 'down'})
    status = present.plan_status(plan)
    assert '1 title(s) needing matching' in status
    assert 'mal (down)' in status


# -- per-field rule choices -------------------------------------------

def test_policy_choices_progress_leads_with_furthest():
    choices = present.policy_choices('progress', ['mal', 'anilist'])
    keys = [k for k, _ in choices]
    assert keys[0] == 'reconcile:progress'
    assert choices[0][1] == 'Keep the furthest progress'
    assert 'provider:mal' in keys and 'provider:anilist' in keys
    assert 'reconcile:manual' in keys
    assert keys[-1] == 'individual'


def test_policy_choices_labels_are_plain_language():
    labels = dict(present.policy_choices('tags', ['mal']))
    assert labels['provider:mal'] == 'Keep from Mal'
    assert labels['reconcile:union'] == 'Combine values from every site'
    assert labels['reconcile:manual'] == 'Ask me when they differ'
    date_labels = dict(present.policy_choices('finish_date', []))
    assert date_labels['reconcile:max'] == 'Keep the latest date'
    assert date_labels['reconcile:min'] == 'Keep the earliest date'


def test_policy_choices_appends_exotic_current_policy():
    # 'union' isn't offered for progress -- but if the advanced matrix
    # set it, the simple view must still show the truth.
    choices = present.policy_choices('progress', ['mal'],
                                     current='reconcile:union')
    assert choices[-1] == ('reconcile:union', 'Reconcile: Union')
    # A current policy already in the list is not duplicated.
    choices = present.policy_choices('progress', ['mal'],
                                     current='provider:mal')
    assert [k for k, _ in choices].count('provider:mal') == 1


def test_policy_choices_marks_unwritable_current_provider():
    choices = dict(present.policy_choices(
        'finish_date', ['anilist', 'mal'], 'provider:kitsu'))
    assert choices['provider:kitsu'].endswith(
        '(unsupported by this tracker)')


def test_policy_choices_cover_the_full_policy_space_when_parsed():
    # Every choice the UI offers round-trips through FieldPolicy.
    for field in ('score', 'progress', 'tags', 'status', 'finish_date'):
        for key, _label in present.policy_choices(field, ['mal', 'kitsu']):
            FieldPolicy.parse(key)   # must not raise
