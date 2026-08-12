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
            decision = issue.decisions.get(provider)
            if decision == 'ignore':
                note = "you chose to leave this tracker as it is"
            elif decision == 'present':
                note = 'should have this entry'
            elif decision == 'absent':
                note = 'should not have this entry'
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


def mirror_remove_line(op):
    return 'Remove %s from %s' % (op.title or 'this entry',
                                  label(op.provider))


def mirror_plan_summary(plan):
    """The headline over the Mirror tab."""
    counts = plan.counts()
    entries = sum(counts['add'].values())
    removals = sum(counts['remove'].values())
    fields = sum(counts['update'].values())
    if not (entries or removals or fields or plan.conflicts):
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
