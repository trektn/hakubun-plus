"""Reconciliation strategies for RECONCILE-policy fields.

A strategy receives every participant's current value ('local' plus one
entry per provider that can represent the field) and decides what the
field should be. It returns one of:

    Resolved(value, reason)   adopt `value` everywhere
    Conflict(reason)          a human decides (FieldConflict)
    NoChange(reason)          nothing to do

Strategies may consult BASE STATE -- what each provider's value was
when synchronization last established a common state -- through the
context to determine which side actually changed. Base state is
historical data available to strategies, not the synchronization
algorithm itself: an ownership-policy field never touches it beyond
the same "did the owner move" question, and no generic three-way merge
exists anymore.
"""

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, Optional

from hakubun.sync.normalize import emptyish


@dataclass(frozen=True)
class Resolved:
    value: Any
    reason: str = ''


@dataclass(frozen=True)
class Conflict:
    reason: str = ''


@dataclass(frozen=True)
class NoChange:
    reason: str = ''


@dataclass
class ReconcileContext:
    """Everything a strategy may consult beyond the values themselves.

    `changed[participant]` is True when that side moved since the last
    sync, False when it provably did not, and None (or absent) when
    there is no base to tell -- a first sync for that side. `equal` is
    the field-aware value equality the planner itself uses.
    """
    field: str
    changed: Dict[str, Optional[bool]] = dc_field(default_factory=dict)
    equal: Callable[[Any, Any], bool] = lambda a, b: a == b
    entity_total: Optional[int] = None


def _groups(values, equal):
    """Group participants by value equivalence: [(value, [participant])]
    with a stable order (providers sorted, 'local' last -- so a value
    that exists on a provider is attributed to the provider)."""
    order = sorted(values, key=lambda p: (p == 'local', p))
    grouped = []
    for participant in order:
        value = values[participant]
        for group in grouped:
            if equal(group[0], value):
                group[1].append(participant)
                break
        else:
            grouped.append((value, [participant]))
    return grouped


def _names(participants):
    return ', '.join(p.capitalize() if p != 'local' else 'local'
                     for p in participants)


class ReconcileStrategy:
    """Interface: reconcile one field across every participant."""

    name = ''
    label = ''

    def reconcile(self, field, values, context):
        raise NotImplementedError


class ManualReconcile(ReconcileStrategy):
    """A human arbitrates -- except when base history proves only one
    side changed, in which case that change propagates cleanly (a
    single-sided edit is not a conflict).

    A side holding an EMPTY value (None/0/'' -- see normalize.emptyish)
    abstains rather than votes, unless its history proves the value was
    deliberately cleared (it moved TO empty since the last sync): "MAL
    has no finish date" is missing data, not a competing opinion, and
    must never turn an otherwise unanimous value into a conflict. When
    every voting side agrees, that value is adopted -- which also fills
    it in on the abstaining sides. A proven deliberate clear votes like
    any other change and propagates the clear."""

    name = 'manual'
    label = 'Manual'

    def reconcile(self, field, values, context):
        groups = _groups(values, context.equal)
        if len(groups) == 1:
            return NoChange('all sides agree')
        voting, abstained = [], []
        for value, participants in groups:
            flags = [context.changed.get(p) for p in participants]
            if emptyish(value) and not any(f is True for f in flags):
                abstained.append((value, participants))
            else:
                voting.append((value, participants))
        if not voting:
            return NoChange('no side has a value')
        if len(voting) == 1:
            value, participants = voting[0]
            return Resolved(value, 'the only known value (%s); the '
                            'other sides have none' % _names(participants))
        moved, still, unknown = [], [], []
        for value, participants in voting:
            flags = [context.changed.get(p) for p in participants]
            if any(f is True for f in flags):
                moved.append((value, participants))
            elif all(f is False for f in flags):
                still.append((value, participants))
            else:
                unknown.append((value, participants))
        if len(moved) == 1 and not unknown:
            value, participants = moved[0]
            return Resolved(value, 'only %s changed since the last sync'
                            % _names(participants))
        if not moved and not unknown and len(still) > 1:
            # Nobody moved yet the sides disagree: a pre-existing
            # disagreement the last sync never settled.
            return Conflict('sides disagree and none of them changed '
                            'since the last sync')
        return Conflict('changed in more than one place'
                        if len(moved) > 1 else
                        'sides disagree with no shared history to tell '
                        'which one changed')


class SetUnion(ReconcileStrategy):
    """Union of set-valued fields (tags); boolean OR for flags."""

    name = 'union'
    label = 'Union'

    def reconcile(self, field, values, context):
        present = [v for v in values.values() if v not in (None, '', ())]
        if all(isinstance(v, list) for v in present):
            union = sorted({str(x) for v in present for x in v})
            if all(context.equal(v, union) for v in values.values()):
                return NoChange('all sides agree')
            return Resolved(union, 'union of every side')
        if all(isinstance(v, bool) for v in present):
            combined = any(present)
            if all(bool(v) == combined for v in values.values()):
                return NoChange('all sides agree')
            return Resolved(combined, 'set anywhere means set everywhere')
        return Conflict('union cannot combine these values')


class _Extremum(ReconcileStrategy):
    _pick = None
    _wins = ''

    def reconcile(self, field, values, context):
        counted = {p: v for p, v in values.items() if not emptyish(v)}
        if not counted:
            return NoChange('no side has a value')
        try:
            best = self._pick(counted.values())
        except TypeError:
            return Conflict('values are not comparable')
        if all(context.equal(v, best) for v in counted.values()):
            if len(counted) == len(values):
                return NoChange('all sides agree')
        winners = [p for p, v in counted.items() if context.equal(v, best)]
        return Resolved(best, '%s value wins (%s)'
                        % (self._wins, _names(winners)))


class Maximum(_Extremum):
    name = 'max'
    label = 'Highest'
    _pick = staticmethod(max)
    _wins = 'highest'


class Minimum(_Extremum):
    name = 'min'
    label = 'Lowest'
    _pick = staticmethod(min)
    _wins = 'lowest'


class CustomProgressReconcile(ReconcileStrategy):
    """Progress-aware reconciliation. A single-sided change propagates
    as-is -- including a deliberate regression (a rewatch reset).
    When more than one side moved, or there is no history to tell,
    the furthest progress wins: watching is monotone, and adopting the
    highest value never claims episodes as unwatched.

    Values arrive already converted into the LOCAL episode structure
    (the planner freezes incomparable structures as a structural
    conflict before any strategy runs)."""

    name = 'progress'
    label = 'Progress'

    def reconcile(self, field, values, context):
        groups = _groups(values, context.equal)
        if len(groups) == 1:
            return NoChange('all sides agree')
        moved = [(value, participants) for value, participants in groups
                 if any(context.changed.get(p) is True
                        for p in participants)]
        unknown = any(context.changed.get(p) is None
                      for _v, ps in groups for p in ps)
        if len(moved) == 1 and not unknown:
            value, participants = moved[0]
            return Resolved(value, 'only %s changed since the last sync'
                            % _names(participants))
        counted = {p: v for p, v in values.items() if v is not None}
        if not counted:
            return NoChange('no side has a value')
        best = max(counted.values())
        winners = [p for p, v in counted.items() if v == best]
        return Resolved(best, 'furthest progress wins (%s)'
                        % _names(winners))


STRATEGIES = {s.name: s for s in (ManualReconcile(), SetUnion(),
                                  Maximum(), Minimum(),
                                  CustomProgressReconcile())}


def get_strategy(name):
    """Strategy by registry name; unknown names degrade to manual
    reconciliation (the safe strategy: it never writes on its own)."""
    return STRATEGIES.get(name) or STRATEGIES['manual']
