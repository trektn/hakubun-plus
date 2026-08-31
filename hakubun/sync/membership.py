"""Tracker MEMBERSHIP: which trackers should hold an entry at all.

Field ownership answers "where does this field's value come from?".
It cannot answer "should this entry exist on Kitsu?" -- that is a
different question about a different thing (a whole list entry, not a
field), with its own state, its own history and its own user decisions.
Conflating the two is what produced the old special-cased
`creates_entry` path, where "never add this to Kitsu" was being made to
stand in for two genuinely different states:

    Kitsu should remain absent      (leave it alone)
    Kitsu is wrong and should go    (remove the entry)

So membership is modelled explicitly. For one entity, each connected
provider is in exactly one OBSERVED state:

    PRESENT    the provider's own fetch lists an entry (a remote
               snapshot exists) -- the only evidence that actually
               establishes existence
    MISSING    a mapping exists (identity knows this provider's id for
               the work) but no fetch has ever listed an entry there --
               so an add is addressable and can really be performed
    UNMAPPED   identity has no id for this provider at all. NOT an add
               candidate: adapter.add needs an id, and inventing one
               here would be identity resolution done in the wrong
               layer. Surfaced as an identity gap instead, so the UI
               never offers a button that would quietly do nothing.

and carries at most one persisted DECISION (store.WANTS: 'present',
'absent', 'ignore'; see SyncStore.set_membership for what each means).
Observation and decision are kept apart on purpose: the observation is
re-derived from every fetch, the decision outlives it.

Nothing in this module treats Hakubun's own local state as a provider.
Local state is reconciliation state; it never establishes that a work
exists anywhere, and it is never a membership side.
"""

from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Tuple

PRESENT = 'present'
MISSING = 'missing'
UNMAPPED = 'unmapped'

# Decisions (store.WANTS), re-exported so callers need one import.
WANT_PRESENT = 'present'
WANT_ABSENT = 'absent'
WANT_IGNORE = 'ignore'


@dataclass
class Membership:
    """One entity's tracker membership: observed state per provider,
    plus whatever the user has decided about it."""

    uuid: str
    title: str = ''
    # provider -> PRESENT / MISSING / UNMAPPED
    state: Dict[str, str] = dc_field(default_factory=dict)
    # provider -> 'present' / 'absent' / 'ignore' (only decided ones)
    decisions: Dict[str, str] = dc_field(default_factory=dict)
    # provider -> WHY that decision is on record. Not decoration: the
    # same want arrives by routes that are not the same fact. 'ignore'
    # can mean the user declined a creation ('declined'), or that a
    # fetch noticed they deleted the entry on the website ('deleted')
    # -- an observation nobody chose -- or that they said so in Mirror
    # (None). Telling a user they "chose" the second is simply false.
    reasons: Dict[str, str] = dc_field(default_factory=dict)
    # Providers a cross-id lookup already reported empty
    # (store.resolved_absent): a cache, never a decision -- it only
    # suppresses re-proposing a creation, and never justifies removal.
    lookup_missed: Tuple[str, ...] = ()

    # -- observed ------------------------------------------------------

    def present(self) -> List[str]:
        """Providers whose fetch actually lists this work. This is the
        entry's PROVENANCE: the only justification for creating it
        somewhere else."""
        return sorted(p for p, s in self.state.items() if s == PRESENT)

    def unmapped(self) -> List[str]:
        return sorted(p for p, s in self.state.items() if s == UNMAPPED)

    # -- proposals -----------------------------------------------------

    def addable(self, master=None) -> List[str]:
        """Providers that could receive a creation: mapped, without an
        entry, and not excluded by a decision or an answered lookup.

        A 'present' decision does NOT force an add on a provider with
        no mapping -- see UNMAPPED above.

        With a MASTER designated, the master's list defines the set, so
        a work the master does not hold is not propagated outward: it
        is a candidate for removal instead (see removable). Without
        one, any tracker holding the entry justifies creating it
        elsewhere.
        """
        if master and self.state.get(master) != PRESENT:
            return []
        return sorted(
            p for p, s in self.state.items()
            if s == MISSING
            and self.decisions.get(p) not in (WANT_ABSENT, WANT_IGNORE)
            and p not in self.lookup_missed)

    def removable(self, master=None) -> List[str]:
        """Providers holding an entry that should not be there.

        Two sources, and only two:

        1. An explicit WANT_ABSENT decision. Always honoured.
        2. The MASTER's list, when one is designated: the master is the
           entry manager, so a work it does not list is one the other
           trackers should not list either.

        Rule 2 is deliberately narrow, because a deletion on a real
        account is the one thing here that cannot be taken back. It
        applies only when this work is genuinely RESOLVED against the
        master -- identity has matched it there (the master is not
        UNMAPPED) and the master's own fetch simply does not list it.

        A tracker's unique entries, and anything identity has not
        matched to the master, are LEFT ALONE. "The master has no id
        for this" means we do not know whether it belongs there; it
        does not mean the entry is unwanted, and treating those two as
        the same is exactly how a converge turns into deleting things
        at random. An unresolved entry is an identity gap to fix, never
        a deletion to propose.
        """
        out = {p for p, s in self.state.items()
               if s == PRESENT and self.decisions.get(p) == WANT_ABSENT}
        if master and self.state.get(master) == MISSING:
            # MISSING, not UNMAPPED: the master is mapped (identity
            # resolved this work there) and its fetch still has no
            # entry -- so it really is absent from the managed list.
            out |= {p for p, s in self.state.items()
                    if s == PRESENT and p != master
                    # A decision the user made outranks the master:
                    # 'present'/'ignore' means hands off this tracker.
                    and self.decisions.get(p) is None}
        return sorted(out)

    def undecided_gaps(self) -> List[str]:
        """Providers missing the entry with no decision recorded yet --
        the discrepancies Mirror asks the user about."""
        return sorted(p for p in self.addable()
                      if p not in self.decisions)

    def discrepant(self) -> bool:
        """True when the trackers do not agree about this entry's
        existence in a way the user has not already settled."""
        return bool(self.addable() or self.removable())


def build(store, providers, uids=None, snapshot=None):
    """{uuid: Membership} for every entity, in a handful of queries.

    `providers` is the set of CONNECTED provider names (an account the
    user has not configured is not a membership side -- it is simply
    not part of the picture). `snapshot`, when given, reuses the bulk
    loads the caller already did (see SyncPlanner.plan) rather than
    repeating them.
    """
    providers = list(providers)
    ents = store.entities()
    if uids is not None:
        wanted = set(uids)
        ents = [e for e in ents if e['uuid'] in wanted]
    ids = [e['uuid'] for e in ents]

    snapshot = snapshot or {}
    mappings = snapshot.get('mappings')
    if mappings is None:
        mappings = store.mappings_many(ids)
    remote = snapshot.get('remote')
    if remote is None:
        by_provider = {}
        for maps in mappings.values():
            for m in maps:
                if m['provider'] in providers:
                    by_provider.setdefault(m['provider'], []).append(
                        m['provider_id'])
        remote = {p: store.remote_get_many(p, pids)
                  for p, pids in by_provider.items()}
    absent = snapshot.get('absent')
    if absent is None:
        absent = {p: store.absent_for_provider(p) for p in providers}
    decisions = snapshot.get('membership')
    if decisions is None:
        decisions = store.membership_many(ids)

    out = {}
    for ent in ents:
        uid = ent['uuid']
        maps = {m['provider']: m['provider_id']
                for m in mappings.get(uid, ())
                if m['provider'] in providers}
        # A provider-only entity is deliberately quarantined from
        # cross-provider sync; it has no membership question to answer.
        only = ent.get('provider_only')
        state = {}
        for provider in providers:
            if only and provider != only:
                continue
            pid = maps.get(provider)
            if pid is None:
                state[provider] = UNMAPPED
            elif remote.get(provider, {}).get(pid):
                state[provider] = PRESENT
            else:
                state[provider] = MISSING
        out[uid] = Membership(
            uuid=uid,
            title=ent.get('title') or uid[:8],
            state=state,
            decisions={p: w for p, (w, _r) in decisions.get(uid, {}).items()
                       if p in state},
            reasons={p: r for p, (_w, r) in decisions.get(uid, {}).items()
                     if p in state and r},
            lookup_missed=tuple(p for p in state
                                if uid in absent.get(p, ())))
    return out
