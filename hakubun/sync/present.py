"""Toolkit-agnostic presentation of a SyncPlan.

Everything the Qt and GTK sync windows need in order to *describe* a
plan -- format a value on a provider's own scale, explain why something
needs a human, spell out a rounding -- lives here once. It used to live
twice, and the two copies had already drifted, which is exactly the
failure mode this prevents: a fix to one window's explanation silently
not reaching the other's.

Nothing here imports a toolkit. The windows supply the parts that are
genuinely theirs -- arrow glyphs, markup escaping, widget layout -- and
delegate the wording and the arithmetic to these functions.
"""

import re

from hakubun import messenger
from hakubun.sync import normalize, strategies
from hakubun.sync.adapters import adapter_from_account
from hakubun.sync.engine import SyncEngine
from hakubun.sync.models import FieldPolicy, PolicyKind

FIELD_LABELS = {
    'score': 'Score', 'progress': 'Watched Episodes',
    'rewatches': 'Rewatches', 'status': 'Status',
    'notes': 'Notes', 'start_date': 'Start Date',
    'finish_date': 'Finish Date', 'tags': 'Tags', 'favorite': 'Favorites',
}

# Legacy multisync_mode value, kept only so existing configs keep
# working: it meant "always review the plan before applying".
_LEGACY_PLAN_ONLY = 'plan_only'


def settings_plan_only(config):
    """Whether the headless Sync button must always surface the sync
    window for manual review instead of applying clean (non-conflict,
    non-create) changes on the user's behalf."""
    if config.get('multisync_mode') == _LEGACY_PLAN_ONLY:
        return True
    return bool(config.get('multisync_plan_only', True))


def field_label(field):
    return FIELD_LABELS.get(field, field)


def label(name):
    """Provider name -> display name ('mal' -> 'Mal')."""
    return name.capitalize()


def local_label(primary=None):
    """Display name for the 'local' side: hakubun's own reconciled
    working copy -- the state the user actually sees in the app. It is
    named after the APP, never after the signed-in account or any
    provider: field policies, not the active platform, decide what each
    field should be, and calling this side 'Local' proved too easy to
    misread as "the currently loaded tracker". (`primary` is accepted
    for call-site compatibility and ignored.)"""
    return 'Hakubun'


def source_label(source, primary=None):
    """Display name for a SyncOperation/FieldConflict source key."""
    if source == 'local':
        return local_label(primary)
    if source in ('reconcile', 'resolve'):
        return source.capitalize()
    return label(source)


def strategy_label(name):
    return strategies.get_strategy(name).label


def policy_label(policy):
    """Short human name for a FieldPolicy ('Owned by Kitsu',
    'Individual (never syncs)', 'Reconcile: Manual')."""
    if policy.kind is PolicyKind.PROVIDER:
        return 'Owned by %s' % label(policy.provider)
    if policy.kind is PolicyKind.INDIVIDUAL:
        return 'Individual (never syncs)'
    return 'Reconcile: %s' % strategy_label(policy.strategy)


def policy_explanation(policy):
    """Plain-language 'why couldn't this be decided automatically'
    clause for a conflict card -- no policy/strategy jargon."""
    if policy.kind is PolicyKind.PROVIDER:
        return 'this field normally follows %s' % label(policy.provider)
    if policy.kind is PolicyKind.INDIVIDUAL:
        return 'this field is set to never sync'
    if policy.strategy == 'manual':
        return 'this field is set to ask you when values differ'
    return ('the "%s" rule could not pick a value on its own'
            % strategy_label(policy.strategy))


# Which reconcile strategies make sense for which field -- the SIMPLE
# per-field configuration offers only these (plus every provider and
# "don't sync"); the advanced matrix still offers everything. Order
# matters: a leading non-manual entry is that field's recommended rule
# and is listed first in the choices.
_FIELD_STRATEGIES = {
    'score': ['manual', 'max', 'min'],
    'progress': ['progress', 'manual', 'max', 'min'],
    'rewatches': ['max', 'manual', 'min'],
    'status': ['manual'],
    'notes': ['manual'],
    'start_date': ['manual', 'min', 'max'],
    'finish_date': ['manual', 'max', 'min'],
    'tags': ['union', 'manual'],
    'favorite': ['union', 'manual'],
}


def strategy_choice_label(field, name):
    """Consequence-first label for one strategy choice on one field
    ('Keep the furthest progress', 'Keep the latest date')."""
    if field in ('start_date', 'finish_date'):
        if name == 'min':
            return 'Keep the earliest date'
        if name == 'max':
            return 'Keep the latest date'
    return {
        'manual': 'Ask me when they differ',
        'union': 'Combine values from every site',
        'max': 'Keep the highest',
        'min': 'Keep the lowest',
        'progress': 'Keep the furthest progress',
    }.get(name, strategy_label(name))


def policy_choices(field, providers, current=None):
    """[(serialized policy, friendly label)] for the simple per-field
    sync configuration. Recommended rule first (when the field has a
    non-manual one), then one 'Keep from <site>' per provider, then the
    remaining sensible rules, then 'Don't sync'. If `current` (a
    serialized policy, e.g. set through the advanced matrix) isn't in
    the list, it is appended so the widget can always show the truth
    rather than silently misreporting an exotic configuration."""
    strategies = list(_FIELD_STRATEGIES.get(field, ['manual']))
    choices = []
    if strategies and strategies[0] != 'manual':
        lead = strategies.pop(0)
        choices.append(('reconcile:%s' % lead,
                        strategy_choice_label(field, lead)))
    choices += [('provider:%s' % p, 'Keep from %s' % label(p))
                for p in providers]
    choices += [('reconcile:%s' % s, strategy_choice_label(field, s))
                for s in strategies]
    choices.append(('individual', "Don't sync (each site keeps its own)"))
    if current and current not in (key for key, _ in choices):
        choices.append((current, policy_label(FieldPolicy.parse(current))))
    return choices


def fmt_value(field, value):
    """A canonical value as the user should read it."""
    if value is None or value == []:
        return '-'
    if isinstance(value, list):
        return ', '.join(map(str, value))
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if field == 'score' and isinstance(value, float):
        return '%g' % value
    if field == 'status' and isinstance(value, str):
        return value.replace('_', ' ').title()
    return str(value)


def fmt_target_value(adapters, field, value, target):
    """What the destination will actually end up holding.

    A push's canonical value is unprojected -- MAL can never actually
    hold 6.5, so showing that as 'what gets pushed' is a lie about what
    will happen, even though the field is a genuine, correctly-reasoned
    change. Local (pulls, kept values) stays canonical, since that's
    exactly what local state holds.
    """
    if target != 'local' and field == 'score' and target in adapters:
        info = adapters[target].mediainfo
        smax = info.get('score_max', 10)
        value = normalize.provider_score(value, smax,
                                         info.get('score_step', 1))
        if target == 'kitsu' and smax:
            # UI-only re-expression, not a precision change: Kitsu's API
            # stores ratings on its 0-5 native scale, but kitsu.app's own
            # list view shows them out of 10 -- projecting straight
            # through the raw native scale would show a number nobody
            # sees on Kitsu itself. Doubling lands on exactly what the
            # site displays. What actually gets pushed is unchanged.
            value = value * (10.0 / smax)
    return fmt_value(field, value)


def score_round_note(adapters, target, canonical):
    """Suffix spelling out any rounding a score push applies to reach
    `target`'s scale -- '' when it lands exactly, else e.g.
    '  (8.5 rounded up to 9)'. This is the "say what you're pushing and
    why" the coarser sites need: MAL/AniList-integer round to a whole
    number (half up: 8.5 -> 9), Kitsu to a half-star. The target value
    is shown in the same units the push row already uses (Kitsu
    re-expressed on its 0-10 site scale)."""
    if canonical is None or target not in adapters:
        return ''
    info = adapters[target].mediainfo
    smax = info.get('score_max', 10)
    back = normalize.canonical_score(
        normalize.provider_score(canonical, smax,
                                 info.get('score_step', 1)), smax)
    if back is None or abs(back - canonical) < 1e-9:
        return ''       # exact on this scale; nothing was rounded
    return '  (%s rounded %s to %s)' % (
        fmt_value('score', canonical),
        'up' if back > canonical else 'down',
        fmt_target_value(adapters, 'score', canonical, target))


CREATES_ENTRY_HELP = (
    'These would add the title to a tracker that does not have it yet '
    '-- a new entry on that site, not just an update to an existing '
    'one. Each says which sites\' entries establish it. Nothing is '
    'created unless you tick it; right-click a row to never be asked '
    'about that title on that site again.')


def change_line(adapters, change, primary=None):
    """(direction, text) for one SyncOperation. `direction` is 'pull'
    or 'push' so each toolkit can prefix its own arrow glyph and
    colour. The operation's own reason -- which policy or strategy
    produced it -- is spelled out inline: the plan is the explanation.
    """
    name = field_label(change.field)
    if change.target == 'local':
        values = '%s → %s' % (fmt_value(change.field, change.old),
                              fmt_value(change.field, change.new))
        text = 'Update %s from %s, %s: %s' % (
            local_label(primary), source_label(change.source, primary),
            name, values)
        direction = 'pull'
    else:
        values = '%s → %s' % (
            fmt_target_value(adapters, change.field, change.old,
                             change.target),
            fmt_target_value(adapters, change.field, change.new,
                             change.target))
        verb = 'Add to' if change.creates_entry else 'Update'
        text = '%s %s, %s: %s' % (verb, label(change.target), name, values)
        if change.field == 'score':
            text += score_round_note(adapters, change.target, change.new)
        if change.creates_entry:
            # A create must say what establishes that the entry should
            # exist at all: the providers that actually list it (its
            # provenance), never 'Hakubun' -- local state is not a
            # provider and cannot justify creating anything.
            establishes = ' and '.join(
                label(p) for p in (change.provenance
                                   or (change.source,)))
            text += (' (exists on %s; tick to create the entry)'
                     % establishes)
        direction = 'push'
    if change.reason and not change.creates_entry:
        text += '  — %s' % change.reason
    return direction, text


def conflict_why(conflict, primary=None):
    """Explain why this needs a human: what each side holds, then why
    it couldn't be decided automatically -- in plain language, no
    policy jargon. Multi-line plain text."""
    name = field_label(conflict.field)
    others = sorted(s for s in conflict.values if s != 'local')
    lines = ['%s differs:' % name]
    # A MIRROR conflict is between trackers only -- there is no 'local'
    # side, by design (mirror.py), so it must not be invented here.
    if 'local' in conflict.values:
        lines.append('    %s:  %s'
                     % (local_label(primary),
                        fmt_value(conflict.field,
                                  conflict.values['local'])))
    lines += ['    %s:  %s' % (label(s),
                               fmt_value(conflict.field,
                                         conflict.values[s]))
              for s in others]
    why = ('%s could not choose automatically: %s.'
           % (local_label(primary),
              policy_explanation(conflict.policy)))
    if conflict.reason:
        why += ' (%s)' % conflict.reason
    lines.append(why)
    return '\n'.join(lines)


def conflict_choice_label(conflict, source, primary=None):
    """Button text for one side of a conflict. Choosing a side with no
    value is an explicit CLEAR that propagates everywhere -- the button
    must say so, since '-' alone reads like a no-op."""
    value = conflict.values[source]
    shown = fmt_value(conflict.field, value)
    if source == 'local':
        text = 'Keep %s: %s' % (local_label(primary), shown)
    else:
        text = 'Use %s: %s' % (label(source), shown)
    if normalize.emptyish(value):
        text += ' (clears it everywhere)'
    return text


def plan_context():
    """One paragraph saying how the plan is decided. Uses <b>/<i>
    markup, which both Qt rich text and Pango render."""
    return ('<b>Hakubun</b> is this app\'s own copy of your lists -- '
            'what you see in the main window. Each field syncs by the '
            'rule set in <b>Configuration</b>: follow one site, keep '
            'the best value (furthest progress, highest, combined), or '
            'ask you when sites disagree. Every proposed change says '
            'which rule produced it.')


def plan_counts(plan):
    """(ordinary changes, decisions needed, new entries) -- the three
    numbers the sync workflow is organized around."""
    creates = sum(1 for c in plan.changes if c.creates_entry)
    return len(plan.changes) - creates, len(plan.conflicts), creates


def plan_summary(plan):
    """The headline over the sync tab: how much will change, how much
    needs the user, what would create entries."""
    changes, decisions, creates = plan_counts(plan)
    if not (changes or decisions or creates):
        return 'Everything is in sync.'
    parts = ['%d change(s)' % changes]
    if decisions:
        parts.append('%d decision(s) needed' % decisions)
    if creates:
        parts.append('%d new entr%s' % (creates,
                                        'y' if creates == 1 else 'ies'))
    return ' · '.join(parts)


def plan_status(plan):
    """Status-bar line after a fetch/replan: what was found, in the
    user's terms (not 'planned/conflicts/identity issues')."""
    changes, decisions, creates = plan_counts(plan)
    parts = ['%d change(s)' % changes]
    if decisions:
        parts.append('%d decision(s) needed' % decisions)
    if creates:
        parts.append('%d new entr%s to review'
                     % (creates, 'y' if creates == 1 else 'ies'))
    if plan.identity:
        parts.append('%d title(s) needing matching' % len(plan.identity))
    text = 'Found ' + ', '.join(parts) + '.'
    if plan.errors:
        text += ' Errors: %s.' % ', '.join('%s (%s)' % kv
                                           for kv in plan.errors.items())
    return text


# -- Mirror ----------------------------------------------------------
#
# Mirror is tracker-to-tracker convergence (sync/mirror.py). Every
# string below therefore talks about TRACKERS: no Hakubun side, no
# "from Hakubun", no local value presented as a competing opinion.

MIRROR_TAB_HELP = (
    'Make your trackers agree, according to <b>Ownership</b>.\n\n'
    'Ordinary <b>Sync</b> follows changes as they happen. <b>Mirror</b> '
    'ignores history and asks a different question: given your '
    'ownership rules, what should each tracker contain right now? Use '
    'it after changing an ownership rule, or when a tracker has drifted '
    'and normal syncing has nothing left to go on.\n\n'
    'This can change a lot at once, so nothing is applied until you '
    'review it and confirm.')


def membership_note(want, reason=None):
    """Why a tracker row is in the state it is, in words that are
    actually true of how it got there.

    The same `want` arrives by routes that are not the same fact, and
    only some of them are the user's doing:

        'deleted'   a fetch noticed the entry gone from the website.
                    An OBSERVATION. Describing it as something the
                    user chose is simply false -- they deleted an
                    entry on a website, they never made a decision in
                    Hakubun, and being told "you chose" about a
                    setting they have never seen is how a UI teaches
                    someone to distrust it.
        'declined'  the user turned down a proposed creation. A real
                    choice, but a narrow one: "don't add it", not
                    "leave this tracker alone forever".
        None        the user said so in Mirror. The only case where
                    "you chose" is the honest word.
    """
    if want == 'ignore':
        if reason == 'deleted':
            return ('removed on the site — not offered again')
        if reason == 'declined':
            return ('you declined adding it here')
        return 'you chose to leave this tracker as it is'
    if want == 'present':
        return 'you chose: this tracker should have it'
    if want == 'absent':
        return 'you chose: this tracker should not have it'
    return ''


def mirror_membership_lines(issue):
    """The presence matrix for one entry, as tracker rows:

        Anilist  ✓
        Mal      ✓
        Kitsu    ✗

    Returns [(tracker label, present?, note)] so each toolkit can draw
    its own ticks and crosses. `note` explains a row that is not simply
    a yes/no -- a recorded decision, or a tracker identity has never
    matched (which cannot be added to at all).
    """
    rows = []
    for provider in sorted(set(issue.present) | set(issue.missing)
                           | set(issue.unmapped)):
        present = provider in issue.present
        note = ''
        if provider in issue.unmapped:
            note = 'not matched yet -- resolve it under Identity'
        elif provider in issue.removable:
            note = 'marked as not belonging here'
        else:
            note = membership_note(issue.decisions.get(provider),
                                   issue.reasons.get(provider))
        rows.append((label(provider), present, note))
    return rows


def mirror_membership_why(issue):
    """One sentence naming the discrepancy between trackers -- never
    'add to Kitsu from Hakubun', which describes a synchronization the
    user did not ask for."""
    have = ' and '.join(label(p) for p in issue.present)
    if issue.addable:
        lack = ' and '.join(label(p) for p in issue.addable)
        return ('%s %s this entry; %s %s not. Ownership says %s should '
                'contain it.'
                % (have, 'has' if len(issue.present) == 1 else 'have',
                   lack, 'does' if len(issue.addable) == 1 else 'do',
                   lack))
    if issue.removable:
        gone = ' and '.join(label(p) for p in issue.removable)
        return '%s should not contain this entry.' % gone
    return '%s %s this entry.' % (have,
                                  'has' if len(issue.present) == 1
                                  else 'have')


MIRROR_STRUCTURAL_NOTE = (
    'These trackers list this work with different episode counts, so '
    'there is no single progress value to give them all. Mirror cannot '
    'convert between the structures — set this title\'s progress from '
    'the Sync tab instead.')


def mirror_conflict_why(conflict):
    """Explain a MIRROR decision: what each tracker holds, then the
    rule that could not settle it.

    Deliberately not conflict_why(): that one opens with Hakubun's
    value and says Hakubun could not choose, which is the right framing
    for Sync (where local IS the app's working copy being reconciled)
    and the wrong one here. During a mirror the disagreement is between
    trackers, and the app is not one of the parties.
    """
    name = field_label(conflict.field)
    lines = ['%s differs between your trackers:' % name]
    lines += ['    %s:  %s' % (label(s),
                               fmt_value(conflict.field,
                                         conflict.values[s]))
              for s in sorted(conflict.values) if s != 'local']
    why = 'These cannot be settled automatically: %s.' % \
        policy_explanation(conflict.policy)
    if conflict.reason:
        why += ' (%s)' % conflict.reason
    lines.append(why)
    return '\n'.join(lines)


def mirror_add_label(issue, provider):
    return 'Add to %s' % label(provider)


def mirror_remove_label(provider):
    return 'Remove from %s' % label(provider)


def mirror_ignore_label(provider):
    return 'Leave %s as it is' % label(provider)


def mirror_change_line(adapters, change):
    """(direction, text) for one Mirror field operation. Always a push
    between trackers, and always named as one: '<tracker>, <field>:
    old → new — <rule>'."""
    name = field_label(change.field)
    values = '%s → %s' % (
        fmt_target_value(adapters, change.field, change.old,
                         change.target),
        fmt_target_value(adapters, change.field, change.new,
                         change.target))
    text = '%s, %s: %s' % (label(change.target), name, values)
    if change.field == 'score':
        text += score_round_note(adapters, change.target, change.new)
    if change.reason:
        text += '  — %s' % change.reason
    return 'push', text


def mirror_local_line(op):
    """One row in Mirror's Hakubun category. Phrased as the app's own
    copy being brought into line with the trackers -- never as a
    tracker-to-tracker change, which it is not."""
    return '%s, %s: %s → %s  — %s' % (
        op.title, field_label(op.field),
        fmt_value(op.field, op.old), fmt_value(op.field, op.new),
        op.reason or 'matches the trackers')


MIRROR_LOCAL_HELP = (
    "These update Hakubun's own copy of your list so it matches what "
    'the trackers will hold. Hakubun is not a tracker and does not vote '
    'on any of this — but if you changed a value here and have not '
    'synced it yet, mirroring replaces it with the trackers\' value. '
    'Untick anything you want to keep and sync instead.')


def mirror_remove_line(op):
    return 'Remove %s from %s' % (op.title or 'this entry',
                                  label(op.provider))


# -- Mirror, as CARDS -------------------------------------------------
#
# A Mirror plan is a set of operations, and the first version of this
# tab showed exactly that: five tabs of operations, sorted by kind.
# That is the shape the ENGINE needs and the wrong shape for a person.
# A user works one title at a time -- "what happens to Cowboy Bebop?" --
# and the answer was spread across three tabs, with the entry's tracker
# membership in one, its field pushes in another, and the entry it would
# gain in a third, split into one row per field.
#
# So the plan is re-projected here into one card per work: the values
# ownership says it should have, then every tracker underneath it with
# what it holds and what would change. That is the layout MALSync's list
# sync uses (a master entry at the top of each card, slaves below as
# deltas), with the one difference the user named: there is no single
# master list, because ownership assigns each field its own authority.
# The card's top row is therefore SYNTHESIZED across owners -- which is
# precisely what ownership buys over a single master.
#
# Both toolkits render this same model, so the two windows cannot drift
# apart again, and the layout is testable without a widget.

# Card categories, for the preview filter. These are the same divisions
# the old tabs had -- kept, because they are how a user narrows a large
# plan ("just show me what gets deleted"), only no longer imposed as
# five separate places to look.
CARD_CATEGORIES = (
    ('all', 'Everything'),
    ('update', 'Fields to update'),
    ('add', 'Entries to add'),
    ('remove', 'Entries to remove'),
    ('conflict', 'Needs a decision'),
    ('membership', 'Tracker membership'),
    ('local', "Hakubun's copy"),
)


class MirrorTrackerRow:
    """One tracker's line inside a card: what it holds now, and what
    Mirror would do to it."""

    def __init__(self, provider, present_here, action='', note='',
                 owns=(), values=None, changes=(), add_values=None):
        self.provider = provider
        self.label = label(provider)
        self.present = present_here
        # '' | 'add' | 'remove' | 'unmapped' -- the entry-level thing
        # happening to this tracker, distinct from field changes.
        self.action = action
        self.note = note
        # Fields this tracker is the authority for. Shown on the row so
        # "why is Kitsu changing and AniList not?" is answered in place
        # rather than in a legend somewhere else.
        self.owns = list(owns)
        # [(field label, formatted current value)] -- the whole entry,
        # not just what changes.
        self.values = list(values or ())
        # [(field label, formatted old, formatted new, why)]
        self.changes = list(changes)
        # For an 'add' row: [(field label, formatted value)] the created
        # entry starts with.
        self.add_values = list(add_values or ())


class MirrorCard:
    """Everything a Mirror plan does to one work."""

    def __init__(self, uuid, title):
        self.uuid = uuid
        self.title = title
        # [(field label, formatted value, owning tracker label, why)]
        self.desired = []
        self.trackers = []
        self.conflicts = []
        self.local = []         # SyncOperations against Hakubun's copy
        self.ops = []           # every tickable operation on this card
        self.categories = set()

    @property
    def changed(self):
        return bool(self.ops or self.conflicts or self.local)

    def matches(self, category):
        return category == 'all' or category in self.categories


def _fmt_progress(value, total):
    text = fmt_value('progress', value)
    if total:
        text += ' / %s' % total
    return text


def mirror_cards(plan, adapters, category='all'):
    """Re-project a MirrorPlan into one card per work, newest concern
    first: entries that need a decision, then entries being created or
    deleted, then plain field updates.

    Pure data -- no toolkit, no widgets -- so both windows render the
    same thing and the layout can be tested directly.
    """
    cards = {}

    def card_for(uuid, title):
        card = cards.get(uuid)
        if card is None:
            card = cards[uuid] = MirrorCard(uuid, title)
        if title and not card.title:
            card.title = title
        return card

    # Which tracker is a field's AUTHORITY, straight from the
    # configuration. Never inferred from which tracker's value won: a
    # reconcile policy's winner is whichever tracker happened to hold
    # the agreed value, and calling it the owner would put a claim on
    # screen the configuration does not make.
    owned_fields = {}
    for field, policy in (plan.ownership or {}).items():
        if getattr(policy, 'kind', None) is PolicyKind.PROVIDER:
            owned_fields.setdefault(policy.provider, []).append(
                field_label(field))

    # The ownership row, per card: what this work SHOULD look like.
    for uuid, fields in plan.desired.items():
        card = card_for(uuid, '')
        for field in sorted(fields):
            value, source, reason = fields[field]
            card.desired.append((field_label(field),
                                 fmt_value(field, value),
                                 label(source) if source else '',
                                 reason))

    updates_by = {}
    for op in plan.updates:
        card_for(op.uuid, op.title)
        updates_by.setdefault((op.uuid, op.target), []).append(op)
    adds_by = {op.uuid: op for op in plan.adds}
    removes_by = {}
    for op in plan.removes:
        removes_by[(op.uuid, op.provider)] = op
        card_for(op.uuid, op.title)
    issues = {issue.uuid: issue for issue in plan.membership}
    for issue in plan.membership:
        card_for(issue.uuid, issue.title)
    for conflict in plan.conflicts:
        card_for(conflict.uuid, conflict.title).conflicts.append(conflict)
    for op in plan.local:
        card_for(op.uuid, op.title).local.append(op)

    for uuid, card in cards.items():
        issue = issues.get(uuid)
        observed = plan.observed.get(uuid, {})
        add_op = adds_by.get(uuid)
        # Every tracker this work touches, whether or not it changes.
        providers = set(observed)
        if issue is not None:
            providers |= (set(issue.present) | set(issue.missing)
                          | set(issue.unmapped))
        if add_op is not None:
            providers.add(add_op.provider)
        for _u, target in list(updates_by):
            if _u == uuid:
                providers.add(target)

        for provider in sorted(providers):
            values = observed.get(provider, {})
            total = values.get('_total')
            present_here = bool(values) or (
                issue is not None and provider in issue.present)
            row_values = [
                (field_label(f),
                 _fmt_progress(values[f], total) if f == 'progress'
                 else fmt_target_value(adapters, f, values[f], provider))
                for f in sorted(values) if f != '_total']

            changes = []
            for op in updates_by.get((uuid, provider), ()):
                changes.append((
                    field_label(op.field),
                    _fmt_progress(op.old, total) if op.field == 'progress'
                    else fmt_target_value(adapters, op.field, op.old,
                                          provider),
                    _fmt_progress(op.new, total) if op.field == 'progress'
                    else fmt_target_value(adapters, op.field, op.new,
                                          provider),
                    op.reason or ''))
                card.ops.append(op)
                card.categories.add('update')

            action, note, add_values = '', '', ()
            if add_op is not None and add_op.provider == provider:
                action = 'add'
                add_values = [(field_label(f),
                               fmt_target_value(adapters, f,
                                                add_op.values[f], provider))
                              for f in sorted(add_op.values)]
                card.ops.append(add_op)
                card.categories.add('add')
            elif (uuid, provider) in removes_by:
                action = 'remove'
                card.ops.append(removes_by[(uuid, provider)])
                card.categories.add('remove')
            elif issue is not None and provider in issue.unmapped:
                action = 'unmapped'
                note = 'not matched yet — resolve it under Identity'
            elif issue is not None and provider in issue.missing:
                note = membership_note(issue.decisions.get(provider),
                                       issue.reasons.get(provider))

            card.trackers.append(MirrorTrackerRow(
                provider, present_here, action=action, note=note,
                owns=sorted(owned_fields.get(provider, ())),
                values=row_values, changes=changes,
                add_values=add_values))

        if issue is not None:
            card.categories.add('membership')
        if card.conflicts:
            card.categories.add('conflict')
        if card.local:
            card.categories.add('local')

    def rank(card):
        # Decisions first (nothing else can proceed until they are
        # answered), then entry-level changes, then field updates.
        for i, key in enumerate(('conflict', 'remove', 'add', 'update',
                                 'membership', 'local')):
            if key in card.categories:
                return i
        return 9

    ordered = sorted(cards.values(),
                     key=lambda c: (rank(c), c.title.casefold()))
    return [c for c in ordered if c.changed and c.matches(category)]


def mirror_card_headline(card):
    """The one-line summary on a collapsed card: what happens to this
    work, in the fewest words that are still true."""
    parts = []
    adds = [t for t in card.trackers if t.action == 'add']
    removes = [t for t in card.trackers if t.action == 'remove']
    updated = [t for t in card.trackers if t.changes]
    if adds:
        parts.append('add to %s' % ', '.join(t.label for t in adds))
    if removes:
        parts.append('remove from %s' % ', '.join(t.label for t in removes))
    if updated:
        parts.append('%d field(s) on %s'
                     % (sum(len(t.changes) for t in updated),
                        ', '.join(t.label for t in updated)))
    if card.conflicts:
        parts.append('%d decision(s) needed' % len(card.conflicts))
    if card.local and not parts:
        parts.append("%d value(s) in %s's copy"
                     % (len(card.local), local_label()))
    return ' · '.join(parts)


def mirror_plan_summary(plan):
    """The headline over the Mirror tab."""
    counts = plan.counts()
    entries = sum(counts['add'].values())
    removals = sum(counts['remove'].values())
    fields = sum(counts['update'].values())
    if not (entries or removals or fields or plan.conflicts):
        if plan.local:
            # The trackers genuinely agree; it is Hakubun's own copy
            # that drifted. Still worth applying -- left alone, the
            # next ordinary Sync would read Hakubun as the side that
            # moved and push the stale value back out to everyone.
            return ('Your trackers already agree — %d Hakubun value(s) '
                    'to bring into line with them.' % len(plan.local))
        return 'Your trackers already agree.'
    parts = []
    if entries:
        parts.append('%d entr%s to add' % (entries,
                                           'y' if entries == 1 else 'ies'))
    if removals:
        parts.append('%d entr%s to remove'
                     % (removals, 'y' if removals == 1 else 'ies'))
    if fields:
        parts.append('%d field(s) to update' % fields)
    if plan.conflicts:
        parts.append('%d decision(s) needed' % len(plan.conflicts))
    return ' · '.join(parts)


def mirror_confirmation(plan):
    """The text of the bulk confirmation shown BEFORE a Mirror plan is
    applied. Mirror can create and delete entries across real accounts
    in bulk, so the user sees the per-tracker numbers first -- and adds
    and removals are approved separately."""
    counts = plan.counts()
    lines = ['Mirror will make changes across your trackers:', '']
    for heading, key, verb in (('Add', 'add', 'entries'),
                               ('Remove', 'remove', 'entries'),
                               ('Update', 'update', 'fields')):
        group = counts[key]
        if not group:
            continue
        lines.append('%s:' % heading)
        for provider in sorted(group):
            lines.append('    %s: %d %s' % (label(provider),
                                            group[provider], verb))
        lines.append('')
    if counts.get('local'):
        # Listed apart from the trackers, and named as this app's own
        # copy: Hakubun is not a tracker, but converging it IS a change
        # the user is about to make, and one that can discard an edit
        # they made here and had not synced yet.
        lines.append("Update %s's own copy:" % local_label())
        lines.append('    %d value(s), to match the trackers'
                     % counts['local'])
        lines.append('')
    if sum(counts['remove'].values()):
        lines.append('Removing an entry deletes it from that tracker '
                     'account. This cannot be undone from Hakubun.')
        lines.append('')
    lines.append('This may make substantial changes to your tracker '
                 'lists.')
    return '\n'.join(lines)


def mirror_result_status(result):
    """Status line after applying a Mirror plan."""
    parts = []
    if result.get('pushed'):
        parts.append('%d tracker change(s)' % result['pushed'])
    if result.get('removed'):
        parts.append('%d entr%s removed'
                     % (result['removed'],
                        'y' if result['removed'] == 1 else 'ies'))
    text = ('Mirror applied: ' + ', '.join(parts) + '.' if parts
            else 'Mirror made no changes.')
    if result.get('skipped'):
        text += (' %d not applied (you did not approve that category).'
                 % result['skipped'])
    if result.get('cancelled'):
        text += ' Cancelled partway -- the rest re-plans.'
    if result.get('errors'):
        text += ' Errors: %s.' % ', '.join('%s (%s)' % kv for kv
                                           in result['errors'].items())
    return text


def display_title(title, aliases):
    """Native-script titles get a latin alias alongside, so a user whose
    AniList title language is Native can still tell what a row refers
    to."""
    title = title or '?'
    if not re.search('[A-Za-z]', title):
        for alias in aliases or []:
            if alias and re.search('[A-Za-z]', alias):
                return '%s  /  %s' % (title, alias)
    return title


def build_engine(store, accountman, media_type):
    """(SyncEngine, [error strings]) over every configured account.

    Each account is forced to `media_type` (a Kitsu account last used
    for manga still contributes its ANIME list to an anime sync).
    Providers that can't do this media type at all raise here and are
    reported, not silently dropped.
    """
    by_provider, errors = {}, []
    msg = messenger.Messenger(None, 'Sync')
    for _num, account in (accountman.get_accounts() if accountman else []):
        api = account['api']
        if api in by_provider:
            errors.append('%s: only one account per provider is supported '
                          'for now' % api)
            continue
        try:
            by_provider[api] = adapter_from_account(account, msg,
                                                    media_type=media_type)
        except Exception as e:
            errors.append('%s: %s' % (api, e))
    from hakubun.sync.relations import RelationsAtlas
    # Reads local files only -- this runs on the UI thread, and the arm
    # download is done from Engine.start (see sync/arm.py).
    atlas = RelationsAtlas.from_sources() if media_type == 'anime' else None
    return SyncEngine(store, by_provider, relations=atlas), errors
