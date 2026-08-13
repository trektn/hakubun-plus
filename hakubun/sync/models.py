"""Core datatypes for the multisync subsystem (docs/multisync.md).

The synchronization model is field-strategy based:

    identity -> entity UUID -> field policy -> authoritative value /
    reconciliation -> sync plan

A field POLICY answers "where does this field's authoritative value
come from?": a named provider owns it (PROVIDER), it never syncs
(INDIVIDUAL), or a reconciliation STRATEGY decides (RECONCILE).
There is no generic local-vs-remote merge resolver.
"""

import enum
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional

# User-state fields that participate in synchronization. Metadata
# (title, total, airing status, dates of the *show*) is tracked per
# provider in remote_state and reconciled into the entity separately.
USER_FIELDS = ('score', 'progress', 'rewatches', 'status', 'notes',
               'start_date', 'finish_date', 'tags', 'favorite')

# Fields whose values are sets (union-style reconciliation).
SET_FIELDS = ('tags',)

# Canonical user statuses. Adapters map provider-specific values here.
STATUSES = ('watching', 'completed', 'on_hold', 'dropped', 'plan')


class SyncCancelled(Exception):
    """Raised when the user cancels an in-progress fetch/plan/apply
    (e.g. during a rate-limit wait). Propagates out uncaught so the
    engine can stop cleanly, keeping whatever was already committed."""


class PolicyKind(enum.Enum):
    PROVIDER = 'provider'      # a named provider is authoritative
    INDIVIDUAL = 'individual'  # per-provider, never synchronized
    RECONCILE = 'reconcile'    # a reconciliation strategy decides


# Legacy policy spellings from the removed three-way-merge engine,
# mapped onto the nearest field-strategy equivalent so stored ownership
# rows keep working: 'local' and 'ask' both meant "a human arbitrates
# divergence" (manual reconciliation); 'merge' meant union/newest.
_LEGACY_POLICIES = {
    'local': ('reconcile', 'manual'),
    'ask': ('reconcile', 'manual'),
    'merge': ('reconcile', 'union'),
}


@dataclass(frozen=True)
class FieldPolicy:
    """Synchronization policy for one field.

    Serialized forms: 'provider:mal', 'individual',
    'reconcile:<strategy>' (see strategies.STRATEGIES).
    """
    kind: PolicyKind
    provider: Optional[str] = None   # for kind == PROVIDER
    strategy: Optional[str] = None   # for kind == RECONCILE

    @classmethod
    def parse(cls, text: str) -> 'FieldPolicy':
        if text in _LEGACY_POLICIES:
            kind, arg = _LEGACY_POLICIES[text]
            return cls(PolicyKind.RECONCILE, strategy=arg)
        if ':' in text:
            kind, arg = text.split(':', 1)
            if kind == 'provider':
                return cls(PolicyKind.PROVIDER, provider=arg)
            if kind == 'reconcile':
                return cls(PolicyKind.RECONCILE, strategy=arg)
            raise ValueError('unknown policy: %s' % text)
        return cls(PolicyKind(text))

    def serialize(self) -> str:
        if self.kind is PolicyKind.PROVIDER:
            return 'provider:%s' % self.provider
        if self.kind is PolicyKind.RECONCILE:
            return 'reconcile:%s' % (self.strategy or 'manual')
        return self.kind.value

    def __str__(self):
        return self.serialize()


# Defaults chosen so a first sync mostly resolves itself: monotone
# fields (progress, rewatches, finish date, sets) reconcile
# deterministically; judgement fields (score, status, start date) go to
# manual reconciliation, which auto-resolves single-sided changes via
# base history and only asks when sides genuinely disagree. The user
# edits these from the Sync window's ownership matrix.
DEFAULT_OWNERSHIP = {
    'score': FieldPolicy(PolicyKind.RECONCILE, strategy='manual'),
    'progress': FieldPolicy(PolicyKind.RECONCILE, strategy='progress'),
    'rewatches': FieldPolicy(PolicyKind.RECONCILE, strategy='max'),
    'status': FieldPolicy(PolicyKind.RECONCILE, strategy='manual'),
    'notes': FieldPolicy(PolicyKind.INDIVIDUAL),
    'start_date': FieldPolicy(PolicyKind.RECONCILE, strategy='manual'),
    'finish_date': FieldPolicy(PolicyKind.RECONCILE, strategy='manual'),
    'tags': FieldPolicy(PolicyKind.RECONCILE, strategy='union'),
    'favorite': FieldPolicy(PolicyKind.RECONCILE, strategy='union'),
}


@dataclass
class NormalizedEntry:
    """One provider list entry, normalized into the internal model.

    Scores are canonical 0-10 floats; statuses canonical strings;
    tags a sorted list; dates ISO 'YYYY-MM-DD' strings or None.
    external_ids maps other providers' names to their ids when the
    provider exposes them (e.g. AniList/Kitsu carry a MAL id).
    """
    provider: str
    provider_id: str
    title: str
    media_type: str = 'anime'
    # The provider's *library-entry* id, distinct from the media id --
    # Kitsu (both backends) keys updates on this, not on provider_id.
    my_id: Optional[str] = None
    aliases: List[str] = dc_field(default_factory=list)
    year: Optional[int] = None
    total: Optional[int] = None            # None: unknown episode count
    airing_status: Optional[str] = None
    external_ids: Dict[str, str] = dc_field(default_factory=dict)
    user: Dict[str, Any] = dc_field(default_factory=dict)  # USER_FIELDS
    # Cover art URL, when the provider ships one with its list. Not
    # user data and never synced anywhere -- it exists so a preview can
    # show the work rather than only name it.
    image: Optional[str] = None


@dataclass
class SyncOperation:
    """One planned modification, produced by the SyncPlanner.

    Carries enough context (`source`, `reason`) for the Inspector and
    the sync windows to explain the decision without re-deriving it:
    the plan itself is the explanation.
    """
    uuid: str
    field: str
    old: Any
    new: Any
    target: str          # 'local' or a provider name (push destination)
    source: str          # where the value came from ('local', provider,
                         # 'reconcile', 'resolve')
    reason: str = ''     # human-readable why (policy/strategy verdict)
    title: str = ''
    selected: bool = True
    # For local pulls whose value was converted between episode
    # structures (e.g. a 1/1 movie completing a 4-episode listing): the
    # provider's own raw value, which is what the merge base records.
    remote_raw: Any = None
    # True when this push has no existing entry to update -- the plan
    # additionally offers to create the show on a connected, mapped
    # provider that doesn't have it yet. A bigger action than a field
    # push (a new library entry on a real account), so it is planned
    # UNSELECTED by default: opt-in only, never applied by a headless
    # Sync click.
    creates_entry: bool = False
    # For creates_entry operations: which providers' actual entries
    # establish that this work exists and should be created on the
    # target ("exists on AniList and MAL"). Entry existence provenance
    # is a separate concept from field ownership -- local state is not
    # a provider and never appears here; an entity no provider lists
    # (local-only) is never offered for creation at all.
    provenance: tuple = ()

    def describe(self) -> str:
        text = '%s  %s %s -> %s' % (self.target, self.field,
                                    _short(self.old), _short(self.new))
        return '%s  (%s)' % (text, self.reason) if self.reason else text


@dataclass
class FieldConflict:
    """A reconciliation strategy returned Conflict: the providers
    disagree and no policy/strategy can decide -- a human does."""
    uuid: str
    field: str
    values: Dict[str, Any]   # source -> value ('local', provider names)
    policy: FieldPolicy
    title: str = ''
    reason: str = ''         # the strategy's own explanation
    # Progress across DIFFERING episode structures (1/1 movie vs a
    # 4-episode listing): the provider values in `values` are raw
    # numbers in each provider's own structure and canNOT be adopted
    # into local state as-is -- resolution only accepts 'local' or an
    # explicit 'value' in the local structure (engine.resolve_conflict
    # enforces it; the UIs render the provider sides as information,
    # not buttons).
    structural: bool = False

    def describe(self) -> str:
        vals = ', '.join('%s %s' % (s, _short(v))
                         for s, v in sorted(self.values.items()))
        text = '%s differs (%s)' % (self.field, vals)
        return '%s -- %s' % (text, self.reason) if self.reason else text


@dataclass
class IdentityIssue:
    """An unresolved provider entry (see identity workflow)."""
    id: int
    provider: str
    provider_id: str
    title: str
    candidates: List[dict]
    status: str = 'open'


@dataclass
class SyncPlan:
    """Preview of a sync run: what would change, what needs the user."""
    changes: List[SyncOperation] = dc_field(default_factory=list)
    conflicts: List[FieldConflict] = dc_field(default_factory=list)
    identity: List[IdentityIssue] = dc_field(default_factory=list)
    errors: Dict[str, str] = dc_field(default_factory=dict)  # provider -> msg

    @property
    def clean(self) -> bool:
        return not (self.changes or self.conflicts
                    or self.identity or self.errors)


def _short(value, maxlen=32):
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= maxlen else text[:maxlen - 1] + '…'
