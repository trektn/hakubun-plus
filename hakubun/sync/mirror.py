"""Mirror: make the TRACKERS agree, according to Ownership.

Ordinary Sync is incremental. It asks "what changed since the last
sync?" and propagates that, using base state to attribute each change
to the side that made it. That is the right model for day-to-day use,
and it is why Sync leaves a tracker alone when nothing about it moved.

Mirror asks a different question, and it asks it about a different set
of things:

    Given the ownership configuration, what SHOULD each tracker
    contain right now -- regardless of history?

So Mirror is not "Sync, but harder". It is a convergence operation:

    AniList ─┐
    Kitsu    ├─ ownership policy → desired tracker state
    MAL     ─┘

Three properties follow from that, and they are the whole design:

1. **Trackers only.** Hakubun's local state is NOT a participant in the
   comparison. It is reconciliation state, not a tracker: it never
   votes, it is never an owner, and it never appears as a side in a
   Mirror conflict or a Mirror row. A plan that said "Hakubun →
   AniList" would be describing a synchronization that isn't the one
   the user asked for.

   Local state is still WRITTEN (see MirrorPlan.local below) -- just
   never displayed as a tracker, and never consulted as an authority.

2. **Ownership is the master.** `score = anilist` means AniList's score
   is the value, and Kitsu's and MAL's converge to it. Not "AniList
   wins a conflict", not "AniList → Hakubun → the others". Mirror reads
   exactly the same ownership configuration Sync does; there is no
   second ownership system.

3. **No base-state attribution.** Sync needs history to tell who moved.
   Mirror does not care who moved: the owner's current value is the
   value, full stop. This is what makes Mirror useful precisely when
   history is stale, missing or wrong -- the situation that motivates
   reaching for it in the first place.

RECONCILE fields keep their strategies (a field with no single owner
still has no single owner during a mirror), but the strategy is run
over the TRACKERS' values alone, with no local participant and no
"changed since last sync" flags -- so e.g. Manual asks the user
whenever the trackers genuinely disagree, rather than silently
resolving on history Mirror is deliberately ignoring.

MEMBERSHIP (does the entry exist on this tracker at all?) is the other
half of Mirror and lives in membership.py. Mirror surfaces every
membership discrepancy as a discrepancy -- "Kitsu ✗", with the option
to add, to remove, or to leave it -- rather than as a bare "add to
Kitsu from Hakubun".
"""

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List

from hakubun.sync import membership as membership_mod
from hakubun.sync import strategies as strategies_mod
from hakubun.sync.models import (FieldConflict, PolicyKind, SyncCancelled,
                                 SyncOperation)
from hakubun.sync.normalize import progress_convert, values_equal
from hakubun.sync.planner import SyncPlanner


@dataclass
class RemoveOperation:
    """Delete this entity's entry from one tracker.

    Only ever produced from an explicit, persisted membership decision
    of 'absent' (membership.Membership.removable()). No ownership
    policy, no observation and no heuristic can create one: a deletion
    on a real account is the single irreversible thing this subsystem
    can do, so it requires a user to have said so, in as many words.

    Planned UNSELECTED, and additionally gated at apply time by
    `allow_removes` (see SyncEngine.apply_mirror) -- two independent
    confirmations, because bulk deletion is exactly the operation that
    must never happen by accident.
    """

    uuid: str
    provider: str
    title: str = ''
    reason: str = ''
    selected: bool = False


@dataclass
class MembershipIssue:
    """A tracker-membership discrepancy for the UI to present as one:
    which trackers have this entry, which don't, and what can be done
    about each. Presented as a discrepancy between TRACKERS -- never as
    "add to Kitsu from Hakubun"."""

    uuid: str
    title: str = ''
    present: List[str] = dc_field(default_factory=list)
    # Mapped trackers with no entry -- the full "✗" column, including
    # ones the user has already settled, so the UI can show a decision
    # rather than silently dropping the row.
    missing: List[str] = dc_field(default_factory=list)
    # The subset of `missing` Mirror is actually proposing to create.
    addable: List[str] = dc_field(default_factory=list)
    # Trackers holding an entry the user marked as not belonging there.
    removable: List[str] = dc_field(default_factory=list)
    # Trackers with no id for this work at all: identity has not
    # matched them, so there is nothing to add TO. An identity gap to
    # resolve in the Identity tab, not a button that would no-op.
    unmapped: List[str] = dc_field(default_factory=list)
    # provider -> already-recorded decision, so the UI can show that a
    # discrepancy has been settled rather than re-asking.
    decisions: Dict[str, str] = dc_field(default_factory=dict)
    # What a created entry would start with, for the preview.
    values: Dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class MirrorPlan:
    """A Mirror preview, in the four categories the UI shows.

    `adds`/`updates` are SyncOperations targeting trackers; `local` is
    the local-state convergence, kept SEPARATE rather than mixed into
    `updates` so no tracker-facing view can accidentally render
    Hakubun as a tracker -- while still being applied, because a Mirror
    that converged the trackers and left local stale would be undone by
    the very next ordinary Sync (local would look like the side that
    moved, and get pushed back out or raise a conflict).
    """

    adds: List[SyncOperation] = dc_field(default_factory=list)
    removes: List[RemoveOperation] = dc_field(default_factory=list)
    updates: List[SyncOperation] = dc_field(default_factory=list)
    local: List[SyncOperation] = dc_field(default_factory=list)
    conflicts: List[FieldConflict] = dc_field(default_factory=list)
    membership: List[MembershipIssue] = dc_field(default_factory=list)
    errors: Dict[str, str] = dc_field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.adds or self.removes or self.updates
                    or self.conflicts)

    def counts(self):
        """{'add': {provider: n_entries}, 'remove': {...},
        'update': {provider: n_fields}} -- the numbers the bulk
        confirmation dialog quotes before anything is applied."""
        adds = {}
        for uid, provider in {(o.uuid, o.target) for o in self.adds}:
            adds[provider] = adds.get(provider, 0) + 1
        removes = {}
        for op in self.removes:
            removes[op.provider] = removes.get(op.provider, 0) + 1
        updates = {}
        for op in self.updates:
            updates[op.target] = updates.get(op.target, 0) + 1
        return {'add': adds, 'remove': removes, 'update': updates}

    def selected_adds(self):
        return [o for o in self.adds if o.selected]

    def selected_removes(self):
        return [o for o in self.removes if o.selected]

    def selected_updates(self):
        return [o for o in self.updates if o.selected]


class MirrorPlanner:
    """Builds a MirrorPlan. Read-only: it never writes to the store."""

    def __init__(self, store, adapters, primary=None):
        self.store = store
        self.adapters = dict(adapters or {})
        self.primary = primary

    # -- entry point ---------------------------------------------------

    def plan(self, should_cancel=None):
        ownership = self.store.ownership()
        plan = MirrorPlan()
        ents = self.store.entities()
        uids = [e['uuid'] for e in ents]
        snapshot = {
            'mappings': self.store.mappings_many(uids),
            'local': self.store.local_get_many(uids),
            'sources': self.store.local_sources_many(uids),
        }
        ids_by_provider = {}
        for maps in snapshot['mappings'].values():
            for m in maps:
                if m['provider'] in self.adapters:
                    ids_by_provider.setdefault(m['provider'], []).append(
                        m['provider_id'])
        snapshot['remote'] = {
            provider: self.store.remote_get_many(provider, ids)
            for provider, ids in ids_by_provider.items()}
        snapshot['absent'] = {
            provider: self.store.absent_for_provider(provider)
            for provider in self.adapters}
        snapshot['membership'] = self.store.membership_many(uids)
        memberships = membership_mod.build(
            self.store, self.adapters, uids=uids, snapshot=snapshot)

        for ent in ents:
            if should_cancel is not None and should_cancel():
                raise SyncCancelled()
            self._mirror_entity(plan, ent, ownership, snapshot,
                                memberships.get(ent['uuid']))
        return plan

    # -- per entity ----------------------------------------------------

    def _mirror_entity(self, plan, ent, ownership, snapshot, member):
        uid = ent['uuid']
        title = ent.get('title') or uid[:8]
        if member is None:
            return
        mappings = [m for m in snapshot['mappings'].get(uid, [])
                    if m['provider'] in self.adapters]
        if ent.get('provider_only'):
            mappings = [m for m in mappings
                        if m['provider'] == ent['provider_only']]
        if not mappings:
            return
        local = snapshot['local'].get(uid, {})
        # sides: provider -> its remote snapshot. Empty dict = the
        # provider is mapped but has no entry (a membership question,
        # handled below, not a field question).
        sides = {m['provider']:
                 snapshot['remote'].get(m['provider'], {}).get(
                     m['provider_id'], {})
                 for m in mappings}
        holders = {p: r for p, r in sides.items() if r}

        self._mirror_membership(plan, uid, title, ownership, sides,
                                local, member)
        if len(holders) < 2:
            # Nothing to mirror BETWEEN: with fewer than two trackers
            # actually holding the entry there is no disagreement
            # between trackers to resolve. (A single holder's values
            # still reach a newly created entry through the membership
            # path above.)
            return

        ent_total = ent.get('total')
        for field, policy in ownership.items():
            if policy.kind is PolicyKind.INDIVIDUAL:
                continue
            self._mirror_field(plan, uid, title, field, policy, holders,
                               local, ent_total)

    # -- fields --------------------------------------------------------

    def _mirror_field(self, plan, uid, title, field, policy, holders,
                      local, ent_total):
        """One field across the trackers that hold this entry. Local is
        not a participant; it only receives the result."""
        l_val = local.get(field, (None, 0))[0]
        progress_field = field == 'progress'
        states, raws, mismatched = {}, {}, {}
        for provider, remote in holders.items():
            if field not in remote:
                continue    # this tracker cannot represent the field
            r_val = remote[field][0]
            if progress_field:
                r_scale = (remote.get('_total') or (None, 0))[0]
                raws[provider] = (r_val, r_scale)
                converted = progress_convert(r_val, r_scale, ent_total)
                if converted is None:
                    mismatched[provider] = (r_val, r_scale)
                    continue
                r_val = converted
            states[provider] = r_val
        if mismatched:
            plan.conflicts.append(FieldConflict(
                uid, field, {p: rv for p, (rv, _) in mismatched.items()},
                policy=policy, title=title,
                reason='episode structures differ: %s' % ', '.join(
                    '%s total %s' % (p, sc or '?')
                    for p, (_, sc) in mismatched.items()),
                structural=True))
            return
        if not states:
            return

        if policy.kind is PolicyKind.PROVIDER:
            owner = policy.provider
            if owner not in states:
                # The authoritative tracker doesn't hold this entry (or
                # can't represent the field): there is no authoritative
                # value, and synthesizing one from a non-authority is
                # exactly the thing ownership exists to prevent.
                return
            effective, source = states[owner], owner
            reason = '%s owns %s' % (owner.capitalize(), field)
        else:
            resolved = self._reconcile(field, policy, states, ent_total)
            if resolved is None:
                return
            if isinstance(resolved, FieldConflict):
                resolved.uuid, resolved.title = uid, title
                plan.conflicts.append(resolved)
                return
            effective, source, reason = resolved

        for provider, current in states.items():
            if provider == source:
                continue
            self._push_op(plan, uid, title, field, policy, provider,
                          current, effective, source, reason, raws,
                          ent_total)
        # Local converges too -- silently, as reconciliation state.
        if not values_equal(field, l_val, effective):
            plan.local.append(SyncOperation(
                uid, field, l_val, effective, target='local',
                source=source, reason=reason, title=title,
                remote_raw=(raws[source][0]
                            if progress_field and source in raws
                            else None)))

    def _reconcile(self, field, policy, states, ent_total):
        """Run a RECONCILE field's strategy over the TRACKERS only.

        No 'local' participant and no `changed` flags: Mirror converges
        current tracker state and deliberately ignores the history Sync
        uses to attribute changes. A strategy that needs history to
        avoid asking (Manual) therefore asks -- which is correct here:
        during a mirror, two trackers genuinely disagreeing is a
        decision, not something to settle from a base that may be
        exactly what the user reached for Mirror to escape.

        Returns (value, source, reason), a FieldConflict, or None.
        """
        strategy = strategies_mod.get_strategy(policy.strategy)
        context = strategies_mod.ReconcileContext(
            field=field, changed={},
            equal=lambda a, b: values_equal(field, a, b),
            entity_total=ent_total)
        result = strategy.reconcile(field, dict(states), context)
        if isinstance(result, strategies_mod.NoChange):
            return None
        if isinstance(result, strategies_mod.Conflict):
            return FieldConflict(
                '', field, dict(states), policy=policy,
                reason='%s reconciliation: %s' % (strategy.label,
                                                  result.reason))
        value = result.value
        source = next((p for p in sorted(states)
                       if values_equal(field, states[p], value)),
                      'reconcile')
        return (value, source,
                '%s reconciliation: %s' % (strategy.label, result.reason))

    def _push_op(self, plan, uid, title, field, policy, provider, current,
                 effective, source, reason, raws, ent_total):
        """Append one tracker-to-tracker field push, if it changes
        anything. Progress converts into the destination's own episode
        structure, exactly as in ordinary Sync."""
        if field == 'progress' and provider in raws:
            raw_r, r_scale = raws[provider]
            if values_equal(field, current, effective):
                return
            target_val = progress_convert(effective, ent_total, r_scale)
            if target_val is None:
                plan.conflicts.append(FieldConflict(
                    uid, field, {provider: raw_r}, policy=policy,
                    title=title, structural=True,
                    reason='episode structures differ: %s total %s'
                           % (provider, r_scale or '?')))
                return
            plan.updates.append(SyncOperation(
                uid, field, raw_r, target_val, target=provider,
                source=source, reason=reason, title=title))
            return
        if not self.adapters[provider].values_equivalent(
                field, current, effective):
            plan.updates.append(SyncOperation(
                uid, field, current, effective, target=provider,
                source=source, reason=reason, title=title))

    # -- membership ----------------------------------------------------

    def _mirror_membership(self, plan, uid, title, ownership, sides,
                           local, member):
        """Turn this entity's tracker-membership discrepancies into
        proposals, and record the discrepancy itself for the UI.

        Adds are proposals (unselected). Removes come only from a
        recorded 'absent' decision. Both are reported as a difference
        between TRACKERS -- which have it, which don't -- so the user
        can answer the real question ("should Kitsu have this?")
        instead of the misleading one ("add to Kitsu from Hakubun?").
        """
        present = member.present()
        addable = [p for p in member.addable()
                   if self.adapters[p].mediainfo.get('can_add', True)]
        removable = member.removable()
        unmapped = member.unmapped()

        values = {}
        if present and addable:
            values = SyncPlanner.entry_values(
                ownership, {p: (r, {}) for p, r in sides.items()}, local)

        missing = sorted(p for p, s in member.state.items()
                         if s == membership_mod.MISSING)
        if present and (missing or removable or unmapped):
            plan.membership.append(MembershipIssue(
                uuid=uid, title=title, present=present, missing=missing,
                addable=addable, removable=removable, unmapped=unmapped,
                decisions=dict(member.decisions), values=dict(values)))

        if present and values:
            provenance = tuple(present)
            reason = 'exists on %s; %%s has no entry' % ' and '.join(
                p.capitalize() for p in present)
            for provider in addable:
                for field, val in values.items():
                    plan.adds.append(SyncOperation(
                        uid, field, None, val, target=provider,
                        source=present[0], title=title,
                        reason=reason % provider.capitalize(),
                        selected=False, creates_entry=True,
                        provenance=provenance))

        for provider in removable:
            plan.removes.append(RemoveOperation(
                uid, provider, title=title,
                reason='marked as not belonging on %s'
                       % provider.capitalize()))
