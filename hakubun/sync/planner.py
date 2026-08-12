"""SyncPlanner: field policies -> explicit SyncOperations.

For every entity (identified by its internal UUID -- identity
resolution happens elsewhere and earlier), the planner walks every
synchronizable field and lets that field's policy decide directly:

    INDIVIDUAL  the field never synchronizes; skip it
    PROVIDER    the named provider is authoritative; everyone else
                (local state included) converges to its value
    RECONCILE   a reconciliation strategy (strategies.py) decides

There is no generic local-vs-remote merge, no divergence
classification, and no mode switch: what the planner does for a field
follows from the field's policy alone. Every operation carries a
`reason` so the Inspector and the sync windows can explain the plan
without re-deriving it.

Local state is the application's working representation, not a
competing authority: for a provider-owned field it converges to the
owner, and a pending local edit (detected against the owner's base --
the owner did not move, local did) is routed TO the owner first.
"""

from hakubun.sync import membership as membership_mod
from hakubun.sync import strategies as strategies_mod
from hakubun.sync.models import (FieldConflict, IdentityIssue, PolicyKind,
                                 SyncCancelled, SyncOperation, SyncPlan)
from hakubun.sync.normalize import (emptyish, progress_convert,
                                    values_equal)

# "No base recorded" sentinel -- distinct from None, a real value.
_NO_BASE = object()


class SyncPlanner:
    def __init__(self, store, adapters, primary=None):
        self.store = store
        self.adapters = dict(adapters or {})
        self.primary = primary

    # -- entry point ---------------------------------------------------

    def plan(self, should_cancel=None):
        ownership = self.store.ownership()
        plan = SyncPlan()
        plan.identity = [IdentityIssue(id=r['id'], provider=r['provider'],
                                       provider_id=r['provider_id'],
                                       title=r['title'],
                                       candidates=r['candidates'],
                                       status=r['status'])
                         for r in self.store.identity_open()]
        # Bulk-load every table the per-entity planner reads: per-uid
        # queries in this loop are an N+1 pattern that scales as
        # entities x providers x 3 round-trips.
        ents = self.store.entities()
        uids = [e['uuid'] for e in ents]
        snapshot = {
            'mappings': self.store.mappings_many(uids),
            'local': self.store.local_get_many(uids),
            'base': self.store.base_get_many(uids),
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
        # Tracker membership (does an entry exist there at all?) is its
        # own model with its own persisted decisions -- see
        # membership.py. The planner only READS it here, to decide
        # which creations to offer.
        snapshot['memberships'] = membership_mod.build(
            self.store, self.adapters, uids=uids, snapshot=snapshot)
        for ent in ents:
            if should_cancel is not None and should_cancel():
                raise SyncCancelled()
            self._plan_entity(plan, ent, ownership, snapshot)
        return plan

    # -- per entity ----------------------------------------------------

    def _plan_entity(self, plan, ent, ownership, snapshot):
        uid = ent['uuid']
        title = ent.get('title') or uid[:8]
        mappings = [m for m in snapshot['mappings'].get(uid, [])
                    if m['provider'] in self.adapters]
        if ent.get('provider_only'):
            mappings = [m for m in mappings
                        if m['provider'] == ent['provider_only']]
        if not mappings:
            return
        local = snapshot['local'].get(uid, {})
        sources = snapshot['sources'].get(uid, {})
        sides = {}
        for m in mappings:
            provider = m['provider']
            sides[provider] = (
                snapshot['remote'].get(provider, {}).get(
                    m['provider_id'], {}),
                snapshot['base'].get((uid, provider), {}))

        self._plan_missing_entries(
            plan, uid, title, ownership, sides, local,
            snapshot['memberships'].get(uid))

        ent_total = ent.get('total')
        for field, policy in ownership.items():
            if policy.kind is PolicyKind.INDIVIDUAL:
                continue
            l_val = local.get(field, (None, 0))[0]
            progress_field = field == 'progress'
            # states: provider -> current value in the LOCAL episode
            # structure; bases likewise (a base that no longer converts
            # is honestly no base at all); raws: the provider's own raw
            # progress + scale, for push conversion and base recording.
            states, bases, raws, mismatched = {}, {}, {}, {}
            for provider, (remote, base) in sides.items():
                if field not in remote:
                    continue  # provider can't represent this field
                r_val = remote[field][0]
                b_val = base.get(field, _NO_BASE)
                if progress_field:
                    r_scale = (remote.get('_total') or (None, 0))[0]
                    raws[provider] = (r_val, r_scale)
                    converted = progress_convert(r_val, r_scale,
                                                 ent_total)
                    if converted is None:
                        # Raw numbers from a differing structure (1/1
                        # movie vs 4-episode listing) are incomparable.
                        mismatched[provider] = (r_val, r_scale)
                        continue
                    r_val = converted
                    if b_val is not _NO_BASE:
                        b_conv = progress_convert(b_val, r_scale,
                                                  ent_total)
                        b_val = _NO_BASE if b_conv is None else b_conv
                states[provider] = r_val
                if b_val is not _NO_BASE:
                    bases[provider] = b_val
            if mismatched:
                # Partial progress across differing structures: honest
                # one-line conflict, field frozen this plan.
                values = {'local': l_val}
                values.update({p: rv for p, (rv, _) in mismatched.items()})
                plan.conflicts.append(FieldConflict(
                    uid, field, values, policy=policy, title=title,
                    reason='episode structures differ: local total %s '
                           'vs %s' % (ent_total or '?', ', '.join(
                               '%s total %s' % (p, sc or '?')
                               for p, (_, sc) in mismatched.items())),
                    structural=True))
                continue
            if not states:
                continue
            if policy.kind is PolicyKind.PROVIDER:
                self._plan_authoritative_field(
                    plan, uid, title, field, policy, l_val, states,
                    bases, raws, ent_total)
            else:
                self._plan_reconcile_field(
                    plan, uid, title, field, policy, l_val, states,
                    bases, raws, ent_total, sources.get(field))

    # -- provider-owned fields ----------------------------------------

    def _plan_authoritative_field(self, plan, uid, title, field, policy,
                                  l_val, states, bases, raws, ent_total):
        """One field with a declared authoritative provider. The owner's
        current value is the field's value; local state and every other
        provider converge to it. The single exception is a pending
        LOCAL edit: the owner provably did not move since the last sync
        (its value still equals its base) while local did -- the edit is
        routed to the owner (and everyone else) instead, per "a local
        edit to an authoritative field is sent to the owning provider".
        """
        owner = policy.provider
        if owner not in states:
            # The owner doesn't list this entry (or can't represent the
            # field): there is no authoritative value to assert, and
            # inventing one from a non-authority would be exactly the
            # generic merge this planner no longer performs.
            return
        owner_val = states[owner]
        owner_base = bases.get(owner, _NO_BASE)
        owner_label = owner.capitalize()
        if values_equal(field, l_val, owner_val):
            effective, source = owner_val, owner
            reason = '%s owns %s' % (owner_label, field)
        elif (owner_base is not _NO_BASE
              and values_equal(field, owner_val, owner_base)):
            # Pending local edit: push it to the owner first.
            effective, source = l_val, 'local'
            reason = ('local edit; %s owns %s and receives it'
                      % (owner_label, field))
        else:
            effective, source = owner_val, owner
            reason = '%s owns %s' % (owner_label, field)
            plan.changes.append(SyncOperation(
                uid, field, l_val, owner_val, target='local',
                source=owner, reason=reason, title=title,
                remote_raw=(raws[owner][0]
                            if field == 'progress' and owner in raws
                            else None)))
        for provider, current in states.items():
            if provider == source:
                continue
            self._push_op(plan, uid, title, field, policy, provider,
                          current, effective, source, reason, raws,
                          ent_total)

    # -- reconciliation fields ----------------------------------------

    def _plan_reconcile_field(self, plan, uid, title, field, policy,
                              l_val, states, bases, raws, ent_total,
                              l_source=None):
        strategy = strategies_mod.get_strategy(policy.strategy)
        participants = self._collapse_precision_redundant(
            field, l_val, states)
        # Local's vote is dropped when it is merely a coarse ECHO of a
        # provider: its stored provenance names a provider (never a
        # direct edit or user resolution), it still equals what that
        # provider currently holds, and -- judged under THAT provider's
        # own precision -- it is consistent with a kept, finer
        # participant. E.g. local 8.0 seeded from MAL alongside
        # AniList's 8.4: MAL's 8 is exactly what it would show for 8.4,
        # so neither MAL nor its echo in local is independent
        # information, and the finer value must win without a conflict.
        # A genuinely disagreeing echo (MAL 8 vs AniList 3) is NOT
        # consistent under MAL's precision and keeps its vote.
        feeder = self.adapters.get(l_source)
        local_echo = (
            feeder is not None and l_source in states
            and values_equal(field, l_val, states[l_source])
            and any(feeder.values_equivalent(field, l_val,
                                             participants[p])
                    for p in participants if p != l_source))
        values = {} if local_echo else {'local': l_val}
        values.update(participants)
        # Which sides provably moved since the last sync -- base state
        # as historical data for the strategy, not as an algorithm.
        # Local has no base of its own: at the last sync it converged
        # with every synced provider, so a local value still equal to
        # SOME recorded base has not moved.
        changed = {}
        for provider in participants:
            base = bases.get(provider, _NO_BASE)
            changed[provider] = (
                None if base is _NO_BASE
                else not values_equal(field, participants[provider], base))
        known_bases = [bases[p] for p in participants if p in bases]
        # Local has no base of its own; whether it "moved" is judged
        # against the providers' bases -- but the test depends on the
        # value's PROVENANCE. A provider-fed value (an echo) has not
        # moved as long as it still equals SOME recorded base. A
        # deliberate act -- a direct edit ('local') or a conflict
        # resolution ('resolve') -- has moved as long as it differs
        # from ANY base: adopting one provider's value in a resolution
        # makes local equal that provider's base, and the old
        # some-base-matches rule then misread the resolution as "local
        # never moved", re-raising the very conflict the user had just
        # resolved (or silently planning nothing).
        deliberate = l_source not in self.adapters
        if not known_bases:
            changed['local'] = None
        elif deliberate:
            changed['local'] = not all(values_equal(field, l_val, b)
                                       for b in known_bases)
        else:
            changed['local'] = not any(values_equal(field, l_val, b)
                                       for b in known_bases)
        context = strategies_mod.ReconcileContext(
            field=field, changed=changed,
            equal=lambda a, b: values_equal(field, a, b),
            entity_total=ent_total)
        result = strategy.reconcile(field, dict(values), context)
        if isinstance(result, strategies_mod.NoChange):
            if not local_echo or not participants:
                return
            # The participants agree but local's dropped echo may still
            # be coarser than their agreed value: converge it.
            result = strategies_mod.Resolved(
                next(iter(participants.values())),
                'finer value from the providers')
        if isinstance(result, strategies_mod.Conflict):
            plan.conflicts.append(FieldConflict(
                uid, field, {'local': l_val, **participants},
                policy=policy, title=title,
                reason='%s reconciliation: %s' % (strategy.label,
                                                  result.reason)))
            return
        resolved = result.value
        # Credit the participant whose value won (a provider name lets
        # apply() advance that provider's base); a synthesized value
        # (e.g. a union) is credited to the strategy itself.
        source = next((p for p in sorted(participants)
                       if values_equal(field, participants[p], resolved)),
                      'local' if values_equal(field, l_val, resolved)
                      else 'reconcile')
        reason = '%s reconciliation: %s' % (strategy.label, result.reason)
        if not values_equal(field, l_val, resolved):
            plan.changes.append(SyncOperation(
                uid, field, l_val, resolved, target='local',
                source=source, reason=reason, title=title,
                remote_raw=(raws[source][0]
                            if field == 'progress' and source in raws
                            else None)))
        for provider, current in states.items():
            if provider == source:
                continue
            self._push_op(plan, uid, title, field, policy, provider,
                          current, resolved, source, reason, raws,
                          ent_total)

    def _collapse_precision_redundant(self, field, l_val, group):
        """Drop a provider's value from the reconciliation participants
        when it is fully explained by -- consistent with, under THAT
        provider's own precision -- local's current value or another,
        at-least-as-precise provider's value already kept.

        Concretely: MAL only stores an integer 0-10 score. If local
        reads 9.5 and MAL reads 10, that is not MAL disagreeing with
        anyone -- 10 is exactly what MAL would show for 9.5. A provider
        whose number is fully explained this way carries no information
        beyond what a more precise source already states, and must
        never turn into a redundant conflict option.

        Providers are checked from most to least precise, each only
        against survivors confirmed so far -- so two providers that
        happen to agree exactly can't each look "explained by" the
        other and both vanish. Never drops local itself, and never
        drops a value that isn't actually redundant."""
        if not group or field != 'score':
            return dict(group)

        def precision(provider):
            info = getattr(self.adapters.get(provider), 'mediainfo',
                           None) or {}
            smax = info.get('score_max') or 10
            step = info.get('score_step') or 1
            return smax / step if step else float('inf')

        kept = {}
        survivors = [l_val]
        for provider in sorted(group, key=precision, reverse=True):
            adapter = self.adapters.get(provider)
            if adapter is not None and any(
                    adapter.values_equivalent(field, group[provider],
                                              other)
                    for other in survivors):
                continue    # redundant -- add nothing to survivors
            kept[provider] = group[provider]
            survivors.append(group[provider])
        return kept

    # -- pushes --------------------------------------------------------

    def _push_op(self, plan, uid, title, field, policy, provider,
                 current, effective, source, reason, raws, ent_total):
        """Append the push of `effective` to one provider if needed.
        Progress pushes convert into the provider's own episode
        structure (completing a 4-episode listing pushes 1 to the
        1-episode movie entry, and vice versa)."""
        if field == 'progress' and provider in raws:
            raw_r, r_scale = raws[provider]
            if values_equal(field, current, effective):
                return
            target_val = progress_convert(effective, ent_total, r_scale)
            if target_val is None:
                plan.conflicts.append(FieldConflict(
                    uid, field, {'local': effective, provider: raw_r},
                    policy=policy, title=title,
                    reason='episode structures differ: local total %s '
                           'vs %s total %s' % (ent_total or '?',
                                               provider, r_scale or '?'),
                    structural=True))
                return
            plan.changes.append(SyncOperation(
                uid, field, raw_r, target_val, target=provider,
                source=source, reason=reason, title=title))
            return
        if not self.adapters[provider].values_equivalent(
                field, current, effective):
            plan.changes.append(SyncOperation(
                uid, field, current, effective, target=provider,
                source=source, reason=reason, title=title))

    # -- entry creation ------------------------------------------------

    def _plan_missing_entries(self, plan, uid, title, ownership, sides,
                              local, member):
        """Offer to create the show on every provider MEMBERSHIP says
        can receive it (membership.Membership.addable(): mapped, no
        entry there yet, and not excluded by a decision or an answered
        cross-id lookup).

        The judgement of which providers those are does NOT live here
        any more -- it is the membership model's, so ordinary Sync and
        Mirror ask exactly one implementation the same question and can
        never drift into disagreeing about whether an entry belongs
        somewhere. What stays here is what is genuinely the planner's:
        turning that answer into field operations.

        Entry EXISTENCE is a separate question from field ownership: a
        creation is justified only by actual provider entries -- the
        providers whose fetches list this work are its provenance, and
        every create operation names them ("exists on AniList and Mal").
        An entity NO provider lists (local-only: e.g. an identity
        created by a manual search that was never fetched) is never
        offered for creation -- there is no provider entry establishing
        that it should exist elsewhere, and Hakubun's own state is not
        a provider.

        Field values initialize the new entry: a PROVIDER-owned field
        contributes its owner's current value, a RECONCILE field
        local's working value; INDIVIDUAL fields contribute nothing.
        apply() batches all of one entity's create operations into a
        single adapter.add call. Planned UNSELECTED, like any action
        bigger than a field push -- see SyncOperation.creates_entry."""
        if member is None:
            return
        existing = member.present()
        if not existing:
            return   # local-only entity: no provider establishes it
        missing = member.addable()
        if not missing:
            return
        values = self.entry_values(ownership, sides, local)
        if not values:
            return
        provenance = tuple(existing)
        reason = 'exists on %s; %%s has no entry' % ' and '.join(
            p.capitalize() for p in existing)
        for provider in missing:
            if not self.adapters[provider].mediainfo.get('can_add', True):
                continue
            for field, val in values.items():
                plan.changes.append(SyncOperation(
                    uid, field, None, val, target=provider,
                    source=existing[0], title=title,
                    reason=reason % provider.capitalize(),
                    selected=False, creates_entry=True,
                    provenance=provenance))

    @staticmethod
    def entry_values(ownership, sides, local):
        """{field: value} a newly created entry should start with.

        A PROVIDER-owned field contributes its OWNER's current value --
        the whole point of ownership is that the owner says what the
        field is, including on an entry that doesn't exist yet. Only
        when the owner has nothing does local's working value fill in.
        RECONCILE fields take local's working value, which is already
        the reconciled one. INDIVIDUAL fields contribute nothing: a
        field that never synchronizes must not be seeded across either.

        Shared with the mirror planner so a Mirror-created entry and a
        Sync-created one are seeded identically."""
        values = {}
        for field, policy in ownership.items():
            if policy.kind is PolicyKind.INDIVIDUAL:
                continue
            if policy.kind is PolicyKind.PROVIDER:
                remote, _base = sides.get(policy.provider, ({}, {}))
                val = remote.get(field, (None, 0))[0]
                if emptyish(val):
                    val = local.get(field, (None, 0))[0]
            else:
                val = local.get(field, (None, 0))[0]
            if not emptyish(val):
                values[field] = val
        return values
