"""Core datatypes for the multisync subsystem (docs/multisync.md)."""

import enum
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional

# User-state fields that participate in synchronization. Metadata
# (title, total, airing status, dates of the *show*) is tracked per
# provider in remote_state and reconciled into the entity separately.
USER_FIELDS = ('score', 'progress', 'status', 'notes',
               'start_date', 'finish_date', 'tags', 'favorite')

# Fields whose values are sets (merge policy = union).
SET_FIELDS = ('tags',)

# Canonical user statuses. Adapters map provider-specific values here.
STATUSES = ('watching', 'completed', 'on_hold', 'dropped', 'plan')


class SyncMode(enum.Enum):
    MIRROR = 'mirror'   # local state pushes outward
    PULL = 'pull'       # providers update local state
    MERGE = 'merge'     # 3-way reconciliation into local, then push


class PolicyKind(enum.Enum):
    LOCAL = 'local'            # local value is authoritative
    PROVIDER = 'provider'      # a named provider owns the field
    MERGE = 'merge'            # union (sets) / newest-wins (scalars)
    INDIVIDUAL = 'individual'  # per-provider, never synchronized
    ASK = 'ask'                # every divergence is a user conflict


@dataclass(frozen=True)
class FieldPolicy:
    """Ownership policy for one field ('local', 'provider:mal', ...)."""
    kind: PolicyKind
    provider: Optional[str] = None  # for kind == PROVIDER

    @classmethod
    def parse(cls, text: str) -> 'FieldPolicy':
        if text.startswith('provider:'):
            return cls(PolicyKind.PROVIDER, text.split(':', 1)[1])
        return cls(PolicyKind(text))

    def serialize(self) -> str:
        if self.kind is PolicyKind.PROVIDER:
            return 'provider:%s' % self.provider
        return self.kind.value

    def __str__(self):
        return self.serialize()


# Local-first defaults; tags merge as sets. The user edits these from
# the Sync window's ownership matrix ("Where should hakubun sync to?").
DEFAULT_OWNERSHIP = {
    'score': FieldPolicy(PolicyKind.LOCAL),
    'progress': FieldPolicy(PolicyKind.LOCAL),
    'status': FieldPolicy(PolicyKind.LOCAL),
    'notes': FieldPolicy(PolicyKind.INDIVIDUAL),
    'start_date': FieldPolicy(PolicyKind.LOCAL),
    'finish_date': FieldPolicy(PolicyKind.LOCAL),
    'tags': FieldPolicy(PolicyKind.MERGE),
    'favorite': FieldPolicy(PolicyKind.MERGE),
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
    aliases: List[str] = dc_field(default_factory=list)
    year: Optional[int] = None
    total: Optional[int] = None            # None: unknown episode count
    airing_status: Optional[str] = None
    external_ids: Dict[str, str] = dc_field(default_factory=dict)
    user: Dict[str, Any] = dc_field(default_factory=dict)  # USER_FIELDS


@dataclass
class FieldChange:
    """One planned modification."""
    uuid: str
    field: str
    old: Any
    new: Any
    target: str          # 'local' or a provider name (push destination)
    source: str          # who caused the value ('local', provider, 'resolve')
    title: str = ''
    selected: bool = True

    def describe(self) -> str:
        return '%s  %s %s -> %s' % (self.target, self.field,
                                    _short(self.old), _short(self.new))


@dataclass
class FieldConflict:
    """Same field changed in two places and no policy decides."""
    uuid: str
    field: str
    values: Dict[str, Any]   # source -> value ('local', provider names)
    base: Any
    policy: FieldPolicy
    title: str = ''

    def describe(self) -> str:
        vals = ', '.join('%s %s' % (s, _short(v))
                         for s, v in sorted(self.values.items()))
        return '%s differs (%s)' % (self.field, vals)


@dataclass
class IdentityIssue:
    """An unresolved provider entry (see identity workflow, §3)."""
    id: int
    provider: str
    provider_id: str
    title: str
    candidates: List[dict]
    status: str = 'open'


@dataclass
class SyncPlan:
    """Preview of a sync run: what would change, what needs the user."""
    mode: SyncMode
    changes: List[FieldChange] = dc_field(default_factory=list)
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
