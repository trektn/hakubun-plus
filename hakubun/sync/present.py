"""Toolkit-agnostic presentation of a SyncPlan.

Everything the Qt and GTK sync windows need in order to *describe* a
plan -- format a value on a provider's own scale, explain why something
needs a human, name the signed-in platform, spell out a rounding --
lives here once. It used to live twice, and the two copies had already
drifted (differing conflict wording, differing rounding comments), which
is exactly the failure mode this prevents: a fix to one window's
explanation silently not reaching the other's.

Nothing here imports a toolkit. The windows supply the parts that are
genuinely theirs -- arrow glyphs, markup escaping, widget layout -- and
delegate the wording and the arithmetic to these functions.
"""

from hakubun import messenger
from hakubun.sync import normalize
from hakubun.sync.adapters import adapter_from_account
from hakubun.sync.engine import SyncEngine
from hakubun.sync.models import PolicyKind, SyncMode

FIELD_LABELS = {
    'score': 'Score', 'progress': 'Watched Episodes',
    'rewatches': 'Rewatches', 'status': 'Status',
    'notes': 'Notes', 'start_date': 'Start Date',
    'finish_date': 'Finish Date', 'tags': 'Tags', 'favorite': 'Favorites',
}

# Order matters: both windows index their mode dropdown by position.
MODES = ((SyncMode.MERGE, 'Merge (reconcile all)'),
         (SyncMode.MIRROR, 'Mirror (local pushes out)'),
         (SyncMode.PULL, 'Pull (providers update local)'),
         (SyncMode.REBASE, 'Rebase (owners overwrite all)'))

# Settings' Mode dropdown (Behavior page) -> engine mode. Rebase is
# deliberately absent: it is a deliberate one-shot from the sync window,
# never something a headless Sync button should be configured to do.
SETTINGS_MODES = {'merge': SyncMode.MERGE, 'pull': SyncMode.PULL,
                  'push': SyncMode.MIRROR}

# A fourth Settings-page choice, deliberately NOT a SyncMode: fetch+plan
# still runs (under Merge, the most conservative reconciliation), but
# the Sync button/action always surfaces the window for manual review
# and never calls s_apply() on the user's behalf -- unlike Merge/Pull/
# Push, which auto-apply clean (non-conflicting, non-first-sync)
# changes headlessly. Multisync is beta; this is the safe posture for
# anyone who hasn't audited what those three actually do to their real
# accounts yet, hence the default (see utils.config_defaults).
SETTINGS_PLAN_ONLY = 'plan_only'


def field_label(field):
    return FIELD_LABELS.get(field, field)


def label(name):
    """Provider name -> display name ('mal' -> 'Mal')."""
    return name.capitalize()


def local_label(primary):
    """'local' is really just whatever the signed-in account fed it (see
    the primary fold in SyncEngine._plan_entity -- local's value always
    equals the primary's own value when a primary is set). Naming it
    'Local' as if it were some third, separate thing is needless jargon;
    call it by the platform it actually is. Falls back to 'Local' only
    when no account is signed in."""
    return label(primary) if primary else 'Local'


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


# Appended to a change's text when it would overwrite a side we have no
# shared history with (FieldChange.first_sync). Such rows are planned
# unchecked; this says why, so an unticked box doesn't read as a glitch.
FIRST_SYNC_NOTE = (' (first sync: no shared history with this tracker; '
                   'tick to overwrite it)')

FIRST_SYNC_HELP = (
    'Some changes are unticked because this is the first sync for those '
    'fields: with no shared history, there is no way to tell which side '
    'is newer, so the value that would win is just whichever tracker '
    'happened to be read first. Review them and tick the ones you '
    'actually want, or use Rebase or Mirror to declare a winner '
    'deliberately.')

CREATES_ENTRY_HELP = (
    'Some changes are unticked because they would add this show to a '
    'tracker that does not have it yet, not just update an existing '
    'entry. Review them and tick the ones you actually want.')


def change_line(adapters, change, primary=None):
    """(direction, text) for one FieldChange. `direction` is 'pull' or
    'push' so each toolkit can prefix its own arrow glyph and colour."""
    name = field_label(change.field)
    if change.target == 'local':
        values = '%s → %s' % (fmt_value(change.field, change.old),
                              fmt_value(change.field, change.new))
        source = (local_label(primary) if change.source == 'local'
                  else label(change.source))
        text = 'Pull from %s, %s: %s' % (source, name, values)
        direction = 'pull'
    else:
        values = '%s → %s' % (
            fmt_target_value(adapters, change.field, change.old,
                             change.target),
            fmt_target_value(adapters, change.field, change.new,
                             change.target))
        verb = 'Add to' if change.creates_entry else 'Push to'
        text = '%s %s, %s: %s' % (verb, label(change.target), name, values)
        if change.field == 'score':
            text += score_round_note(adapters, change.target, change.new)
        if change.creates_entry:
            # old is always None for a create (nothing existed to diff
            # against), so the value's ORIGIN isn't inferable the way a
            # push's old->new transition normally hints at it -- say it
            # plainly, in the same parenthetical that invites the tick.
            source_label = (local_label(primary) if change.source == 'local'
                            else label(change.source))
            text += ' (from %s; tick to create the entry)' % source_label
        direction = 'push'
    if change.first_sync:
        text += FIRST_SYNC_NOTE
    return direction, text


def conflict_why(conflict, primary=None):
    """Explain why this needs a human, not just what differs."""
    name = field_label(conflict.field)
    others = sorted(s for s in conflict.values if s != 'local')
    sides = ' and '.join(
        ['%s (%s)' % (local_label(primary),
                      fmt_value(conflict.field, conflict.values['local']))]
        + ['%s (%s)' % (label(s), fmt_value(conflict.field,
                                            conflict.values[s]))
           for s in others])
    why = ('%s changed in more than one place since the last sync (%s), '
           'so syncing either way would overwrite someone. ' % (name, sides))
    kind = conflict.policy.kind
    if kind is PolicyKind.ASK:
        why += ("The '%s' policy is set to Ask, which leaves every such "
                'tie to you.' % name)
    elif kind is PolicyKind.MERGE:
        why += ('Both sides changed at effectively the same time, so '
                "'newest wins' cannot break the tie.")
    else:
        why += ("The providers disagree with each other and the '%s' "
                'policy does not name a winner among them.'
                % conflict.policy)
    if conflict.note:
        why += ' Note: %s.' % conflict.note
    return why


def conflict_choice_label(conflict, source, primary=None):
    """Button text for one side of a conflict."""
    shown = fmt_value(conflict.field, conflict.values[source])
    if source == 'local':
        return ('Use %s: %s' % (local_label(primary), shown) if primary
                else 'Keep yours: %s' % shown)
    return 'Use %s: %s' % (label(source), shown)


def mode_context(mode, primary=None):
    """One paragraph saying what the selected mode will actually do,
    naming the signed-in account (mirror without that context is a
    footgun). Uses <b>/<i> markup, which both Qt rich text and Pango
    render."""
    signed = (' You are signed into <b>%s</b>; changes you make in the '
              'app count as local.' % label(primary) if primary else '')
    if mode is SyncMode.MIRROR:
        return ('<b>Mirror:</b> pushes local state out to every provider. '
                'Remote-only changes are overwritten.%s' % signed)
    if mode is SyncMode.PULL:
        return ('<b>Pull:</b> providers update local state; nothing is '
                'pushed.%s' % signed)
    if mode is SyncMode.REBASE:
        return ('<b>Rebase:</b> forces each field\'s declared owner (set '
                'in the Ownership tab) onto local <i>and</i> every other '
                'tracker, even for entries that already look in sync. Use '
                'it right after changing who owns a field, to make that '
                'change take effect immediately. Fields set to Merge, '
                'Ask, or Individual have no single owner and are left '
                'alone. Rebase also adds the show to any connected '
                'tracker that does not have it yet, using the owned '
                "fields' current values; those additions are planned "
                'unticked, so you opt in per show.')
    return ('<b>Merge:</b> reconciles every provider into local state, '
            'then pushes the result.%s' % signed)


def display_title(title, aliases):
    """Native-script titles get a latin alias alongside, so a user whose
    AniList title language is Native can still tell what a row refers
    to."""
    import re
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
    atlas = RelationsAtlas.from_file() if media_type == 'anime' else None
    return SyncEngine(store, by_provider, relations=atlas), errors
