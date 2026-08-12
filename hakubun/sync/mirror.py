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
   never displayed as a TRACKER, and never consulted as an authority.

   That has a consequence worth stating plainly, because it follows
   from "Mirror ignores history" and is easy to meet by surprise: a
   PENDING LOCAL EDIT is overwritten. Sync would route it to the
   field's owner (set_local_field advances the bases precisely so it
   reads as "local moved"); Mirror does not read those bases, sees
   only that local disagrees with the trackers, and converges it. So
   local operations are shown in their own category and are
   individually untickable -- disclosed as what they are, rather than
   applied invisibly. They are deliberately NOT a tracker row: the
   category is labelled as this app's own copy, not as a peer of
   AniList and Kitsu.

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
from hakubun.sync.normalize import (emptyish, progress_convert,
                                    values_equal)


@dataclass
class AddOperation:
    """Create this entity's entry on one tracker, WHOLE.

    Deliberately one operation per (entity, tracker) carrying every
    field's value, rather than one per field. Both reasons matter:

    * Correctness. SyncEngine.apply batches a creation by (provider,
      entity) and calls adapter.add once with whatever fields it was
      given, so per-field operations with per-field `selected` flags
      let a user tick `score` and not `status` and get an entry
      created with a score and no status -- a half-formed row on a
      real account, from a UI that only offered tickboxes. An entry is
      created whole or not at all, which is also what MALSync's
      syncMissing does (set every field, then one sync()).
    * Legibility. 200 entries missing from Kitsu across six fields is
      1200 checkboxes describing 200 decisions. The user has one
      question per entry -- "should Kitsu have this?" -- so there is
      one row per entry to answer it with.

    Planned UNSELECTED and gated at apply time by `allow_adds`.
    """

    uuid: str
    provider: str
    title: str = ''
    # field -> the value the created entry starts with. Exactly the
    # values Mirror is asserting for the trackers that already hold the
    # entry -- same computation, same numbers.
    values: Dict[str, Any] = dc_field(default_factory=dict)
    reason: str = ''
    # The trackers whose entries justify creating this one.
    provenance: tuple = ()
    selected: bool = False


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
    # provider -> why that decision is on record (membership.Membership
    # .reasons). Carried so the UI can say what actually happened
    # instead of crediting the user with every recorded state.
    reasons: Dict[str, str] = dc_field(default_factory=dict)
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

    adds: List[AddOperation] = dc_field(default_factory=list)
    removes: List[RemoveOperation] = dc_field(default_factory=list)
    updates: List[SyncOperation] = dc_field(default_factory=list)
    local: List[SyncOperation] = dc_field(default_factory=list)
    conflicts: List[FieldConflict] = dc_field(default_factory=list)
    membership: List[MembershipIssue] = dc_field(default_factory=list)
    errors: Dict[str, str] = dc_field(default_factory=dict)
    # uuid -> {field: (value, owning tracker, why)} -- what ownership
    # says this work SHOULD look like, independent of any one tracker.
    #
    # This is the plan's authoritative row, and it is kept because the
    # preview is unreadable without it. Every update is a delta, and a
    # list of deltas with nothing to stand against ("Kitsu, Score: 7 ->
    # 8") makes the reader reconstruct the target value in their head,
    # per row. MALSync solves this by putting the master list's entry
    # at the top of each card; ownership's equivalent is this row --
    # synthesized per field from each field's own owner, which is
    # exactly the generalization ownership makes over a single master.
    desired: Dict[str, Dict[str, Any]] = dc_field(default_factory=dict)
    # uuid -> {provider: {field: value}} -- what each tracker holds
    # RIGHT NOW, straight from its own snapshot: canonical values (the
    # form every comparison here uses, projected back onto the
    # provider's scale for display) plus `_total`, that tracker's own
    # episode count, so progress can be shown as "12 / 26" against the
    # structure it belongs to.
    #
    # Carried so the preview can show a tracker's whole entry and not
    # only the fields about to change. Unchanged values are what the
    # arrows are read against: "Score 7 -> 8" alone tells you nothing
    # about whether the rest of the entry is about to be disturbed,
    # and that uncertainty is most of what makes a bulk preview
    # frightening to apply.
    observed: Dict[str, Dict[str, Dict[str, Any]]] = dc_field(
        default_factory=dict)
    # The ownership configuration this plan was built from, so the
    # preview can say which tracker is a field's AUTHORITY.
    #
    # Deliberately not inferred from which tracker's value won: under a
    # reconcile policy the winner is whichever tracker happened to hold
    # the agreed value, and labelling that tracker the owner would
    # credit it with an authority the configuration never gave it --
    # the same class of untruth as telling a user they chose something
    # they never chose.
    ownership: Dict[str, Any] = dc_field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """Nothing left to do -- INCLUDING local convergence.

        `local` counts even though it is never displayed: both windows
        gate their apply button on this, and a plan whose only work is
        bringing Hakubun's stale copy back into line with trackers that
        already agree is still work. Omitting it left that case with a
        dead button and a summary claiming everything was fine, which
        is precisely the drift `local` exists to repair."""
        return not (self.adds or self.removes or self.updates
                    or self.local or self.conflicts)

    def counts(self):
        """{'add': {provider: n_entries}, 'remove': {...},
        'update': {provider: n_fields}} -- the numbers the bulk
        confirmation dialog quotes before anything is applied."""
        adds = {}
        for op in self.adds:
            adds[op.provider] = adds.get(op.provider, 0) + 1
        removes = {}
        for op in self.removes:
            removes[op.provider] = removes.get(op.provider, 0) + 1
        updates = {}
        for op in self.updates:
            updates[op.target] = updates.get(op.target, 0) + 1
        return {'add': adds, 'remove': removes, 'update': updates,
                'local': len(self.local)}

    def selected_adds(self):
        return [o for o in self.adds if o.selected]

    def selected_removes(self):
        return [o for o in self.removes if o.selected]

    def selected_updates(self):
        return [o for o in self.updates if o.selected]

    def selected_local(self):
        return [o for o in self.local if o.selected]


class MirrorPlanner:
    """Builds a MirrorPlan. Read-only: it never writes to the store."""

    def __init__(self, store, adapters, primary=None):
        self.store = store
        self.adapters = dict(adapters or {})
        self.primary = primary

    # -- entry point ---------------------------------------------------

    def plan(self, should_cancel=None):
        ownership = self.store.ownership()
        plan = MirrorPlan(ownership=dict(ownership))
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
        snapshot['resolutions'] = self.store.field_resolutions_many(uids)
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
        ent_total = ent.get('total')

        # The DESIRED tracker state is computed once, from the trackers
        # alone, and then used for both purposes it serves: pushing
        # existing entries into line, and seeding an entry created on a
        # tracker that lacks one. Computing it twice is how the two
        # would drift -- and they did: seeding used to fall back to
        # local's working value, so one plan could hand an existing
        # tracker the tracker-reconciled value and a newly created one
        # local's, which differ precisely when local is stale, i.e. in
        # the situation Mirror exists to fix.
        # `status` is resolved FIRST because progress depends on it:
        # an entry the owner calls completed has a defined progress on
        # every tracker (that tracker's own total) even when the
        # episode structures are otherwise incomparable. See `finished`
        # below and _desired_value's `mismatched` handling.
        desired = {}
        for field in sorted(ownership, key=lambda f: f != 'status'):
            policy = ownership[field]
            if policy.kind is PolicyKind.INDIVIDUAL:
                continue
            outcome = self._desired_value(
                plan, uid, title, field, policy, holders, ent_total,
                snapshot['resolutions'],
                finished=self._finished(desired))
            if outcome is not None:
                desired[field] = outcome
        finished = self._finished(desired)
        # Each tracker's entry as IT holds it -- raw, unconverted, so a
        # row reads as that tracker's own numbers rather than as
        # something already translated on its behalf.
        observed = {}
        for provider, remote in holders.items():
            vals = {}
            for field, policy in ownership.items():
                if policy.kind is PolicyKind.INDIVIDUAL or field not in remote:
                    continue
                vals[field] = remote[field][0]
            total = (remote.get('_total') or (None, 0))[0]
            if total:
                vals['_total'] = total
            observed[provider] = vals
        if observed:
            plan.observed[uid] = observed
        if desired:
            plan.desired[uid] = {field: (outcome[0], outcome[1], outcome[2])
                                 for field, outcome in desired.items()}

        for field, (effective, source, reason, states, raws, mismatched) in \
                desired.items():
            for provider, current in states.items():
                if provider == source:
                    continue
                self._push_op(plan, uid, title, field, ownership[field],
                              provider, current, effective, source,
                              reason, raws, ent_total, finished)
            if field == 'progress' and finished:
                # Trackers whose episode structure cannot be compared
                # with this work's. Normally that is a structural
                # conflict -- there is no honest way to translate 5/13
                # into a 26-episode listing. "Completed" is the one
                # case where there is: the answer is that tracker's own
                # total, whatever it happens to be. This is the split-
                # season problem (MAL lists two 13-episode seasons where
                # AniList lists one run of 26), and it is the most
                # common structural difference there is -- so leaving it
                # as an unanswerable conflict on a finished show made
                # Mirror look broken on exactly the entries it should
                # have handled best.
                for provider, (raw_r, r_scale) in mismatched.items():
                    if not r_scale or raw_r == r_scale:
                        continue
                    plan.updates.append(SyncOperation(
                        uid, field, raw_r, r_scale, target=provider,
                        source=source, title=title,
                        reason='%s says completed, so %s goes to its own '
                               'total' % (source.capitalize(),
                                          provider.capitalize())))
            # Local converges too -- silently, as reconciliation state.
            l_val = local.get(field, (None, 0))[0]
            if not values_equal(field, l_val, effective):
                plan.local.append(SyncOperation(
                    uid, field, l_val, effective, target='local',
                    source=source, reason=reason, title=title,
                    remote_raw=(raws[source][0]
                                if field == 'progress' and source in raws
                                else None)))

        self._mirror_membership(plan, uid, title, sides, member, desired)

    # -- fields --------------------------------------------------------

    @staticmethod
    def _finished(desired):
        """Does ownership say this work is completed? Read from the
        already-resolved `status` outcome, so it is the OWNER's verdict
        -- not a vote among trackers, and not local's opinion."""
        outcome = desired.get('status')
        return bool(outcome) and outcome[0] == 'completed'

    def _desired_value(self, plan, uid, title, field, policy, holders,
                       ent_total, resolutions, finished=False):
        """What this field SHOULD be across the trackers.

        Returns (effective, source, reason, states, raws, mismatched),
        or None when
        there is nothing to assert (no tracker can represent the field,
        the owner doesn't hold the entry, a strategy said NoChange, or
        the question needs a human -- in which case a conflict has been
        appended to the plan).

        Local is not a participant anywhere in here. It only receives
        the result.
        """
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
        # A tracker whose structure cannot be translated is a conflict
        # ONLY while the answer is genuinely unknown. Once ownership
        # says the work is completed, each such tracker has a defined
        # target -- its own total -- and the caller pushes it there
        # instead. Raising a conflict anyway would be asking the user a
        # question that has already been answered.
        if mismatched and not (finished and progress_field):
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
                # exactly the thing ownership exists to prevent. That
                # applies to seeding a new entry too -- better a field
                # left unset than one filled in from a tracker the
                # configuration says does not decide it.
                return
            effective, source = states[owner], owner
            reason = '%s owns %s' % (owner.capitalize(), field)
        else:
            # A resolution the user already gave for exactly this
            # disagreement outranks the strategy: they answered the
            # question the strategy could not.
            settled = self._settled(uid, field, states, resolutions)
            if settled is not None:
                effective, source, reason = settled
            else:
                resolved = self._reconcile(field, policy, states,
                                           ent_total)
                if resolved is None:
                    return
                if isinstance(resolved, FieldConflict):
                    resolved.uuid, resolved.title = uid, title
                    plan.conflicts.append(resolved)
                    return
                effective, source, reason = resolved
        return effective, source, reason, states, raws, mismatched

    @staticmethod
    def fingerprint(states):
        """The tracker values a resolution was decided against, in a
        stable form. A decision is an answer about a state of the
        world; when that state changes the answer no longer applies,
        and Mirror must ask again rather than replay a stale verdict."""
        return sorted((p, v) for p, v in states.items())

    def _settled(self, uid, field, states, resolutions):
        """A stored resolution for this exact disagreement, or None."""
        stored = resolutions.get((uid, field))
        if stored is None:
            return None
        value, fingerprint = stored
        # JSON round-trips tuples as lists; compare in one shape.
        if [list(pair) for pair in fingerprint] != \
                [list(pair) for pair in self.fingerprint(states)]:
            return None     # the trackers moved: it is a new question
        source = next((p for p in sorted(states)
                       if values_equal(field, states[p], value)),
                      'resolve')
        return value, source, 'you chose this value for these trackers'

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
            # "Nothing to PUSH", not "no value". The trackers already
            # agree, so their agreed value IS this field's desired
            # value -- and a created entry must be seeded with it. The
            # pushes it generates are all no-ops by construction.
            winner = sorted(states)[0]
            return (states[winner], winner,
                    '%s reconciliation: %s' % (strategy.label,
                                               result.reason))
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
                 effective, source, reason, raws, ent_total,
                 finished=False):
        """Append one tracker-to-tracker field push, if it changes
        anything. Progress converts into the destination's own episode
        structure, exactly as in ordinary Sync."""
        if field == 'progress' and provider in raws:
            raw_r, r_scale = raws[provider]
            if values_equal(field, current, effective):
                return
            target_val = progress_convert(effective, ent_total, r_scale)
            if finished and r_scale:
                # Completed means completed on that tracker's own
                # terms. Its total is the answer whether or not the
                # arithmetic happens to line up.
                target_val = r_scale
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

    def _mirror_membership(self, plan, uid, title, sides, member,
                           desired):
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

        # A created entry starts with exactly the values Mirror is
        # asserting for the trackers that already have it -- the same
        # numbers, from the same computation. Never local's working
        # value: local is not a tracker, and seeding from it is how a
        # single plan ended up carrying two different answers for one
        # field.
        values = {field: outcome[0] for field, outcome in desired.items()
                  if not emptyish(outcome[0])}

        missing = sorted(p for p, s in member.state.items()
                         if s == membership_mod.MISSING)
        if present and (missing or removable or unmapped):
            plan.membership.append(MembershipIssue(
                uuid=uid, title=title, present=present, missing=missing,
                addable=addable, removable=removable, unmapped=unmapped,
                decisions=dict(member.decisions),
                reasons=dict(member.reasons), values=dict(values)))

        if present and values:
            provenance = tuple(present)
            reason = 'exists on %s; %%s has no entry' % ' and '.join(
                p.capitalize() for p in present)
            for provider in addable:
                plan.adds.append(AddOperation(
                    uid, provider, title=title, values=dict(values),
                    reason=reason % provider.capitalize(),
                    provenance=provenance, selected=False))

        for provider in removable:
            plan.removes.append(RemoveOperation(
                uid, provider, title=title,
                reason='marked as not belonging on %s'
                       % provider.capitalize()))
