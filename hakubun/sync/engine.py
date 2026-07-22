"""SyncEngine: fetch -> identity -> plan (diff+policies) -> apply.

Git shape: fetch snapshots remotes, plan is the merge preview, apply is
the commit (event log) plus pushes. Local state is canonical; a
provider failing isolates to that provider (its merge base does not
advance, so the same changes re-plan next run).
"""

import json

from hakubun.sync import conflicts as conflicts_mod
from hakubun.sync.adapters import AdapterError
from hakubun.sync.diff import BOTH, NO_BASE, PULL, eq, three_way
from hakubun.sync.history import History
from hakubun.sync.identity import IdentityResolver
from hakubun.sync.models import (FieldChange, FieldConflict, IdentityIssue,
                                 PolicyKind, SyncMode, SyncPlan)

_UNSET = object()


class SyncEngine:
    def __init__(self, store, adapters=None, msg=None, primary=None,
                 relations=None):
        self.store = store
        self.adapters = dict(adapters or {})
        self.history = History(store)
        self.identity = IdentityResolver(store, atlas=relations)
        # The primary provider is the account the app is signed into --
        # the working tree, in git terms. The main window (and the
        # tracker) edit *that account's* list, so its fetched changes
        # ARE the user's local intent: they fold into local state
        # without policy friction, and ownership then arbitrates
        # between that intent and the other providers.
        self.primary = primary
        self._msg = msg

    def _debug(self, text):
        if self._msg:
            self._msg.debug(text)

    # -- fetch ---------------------------------------------------------

    def fetch(self, providers=None):
        """Download every provider's state (remote-tracking update).
        Returns {provider: error message} for providers that failed;
        the others complete regardless."""
        errors = {}
        seed_txn = None
        for name, adapter in self.adapters.items():
            if providers and name not in providers:
                continue
            try:
                entries = adapter.fetch()
            except Exception as e:  # failure isolation, incl. bugs
                errors[name] = str(e)
                continue
            for entry in entries:
                # '_total' is metadata, not a synced field (never in
                # ownership, so it never diffs): each provider's own
                # episode count, needed to translate progress between
                # differing structures (a 1-episode movie vs a
                # 4-episode listing of the same work).
                self.store.remote_set_all(
                    name, entry.provider_id,
                    {**entry.user, '_total': entry.total})
                uid = self.identity.resolve_entry(entry)
                if uid is None:
                    continue
                self._update_entity_meta(uid, entry)
                if not self.store.local_get(uid):
                    if seed_txn is None:
                        seed_txn = self.history.new_txn()
                    self._seed_entity(seed_txn, uid, entry)
                else:
                    self._settle_base(uid, name, entry)
        return errors

    def _seed_entity(self, txn, uid, entry):
        """First sight of an entity: import this provider's state as the
        local state and set the merge base -- in sync at birth."""
        with self.store.transaction():
            for field, value in entry.user.items():
                self.store.local_set(uid, field, value)
                self.history.record(txn, uid, field, None, value,
                                    entry.provider)
                self.store.base_set(uid, entry.provider, field, value)

    @staticmethod
    def _complete(value, scale):
        return bool(scale) and value is not None and value >= scale

    @classmethod
    def _progress_convert(cls, value, from_scale, to_scale):
        """View a progress value through another episode structure.
        Completion maps to the target's own total (1/1 movie == 4/4
        listing); zero is zero everywhere; equal or unknown structures
        pass through. Returns None when genuinely incomparable --
        partial progress across differing structures."""
        if not value:
            return value or 0
        if from_scale == to_scale or not from_scale or not to_scale:
            return value
        if cls._complete(value, from_scale):
            return to_scale
        return None

    def _settle_base(self, uid, provider, entry):
        """No merge base recorded but local and remote already agree:
        record the agreement. Without this, a later remote-only edit
        reads as divergence instead of a clean pull -- and a 'local'
        policy would overwrite a legitimate website edit."""
        local = self.store.local_get(uid)
        base = self.store.base_get(uid, provider)
        ent_total = (self.store.get_entity(uid) or {}).get('total')
        for field, value in entry.user.items():
            if field in base or field not in local:
                continue
            agrees = eq(local[field][0], value)
            if not agrees and field == 'progress':
                # Complete is complete, whatever the episode structure.
                agrees = (self._complete(local[field][0], ent_total)
                          and self._complete(value, entry.total))
            if agrees:
                self.store.base_set(uid, provider, field, value)

    def _update_entity_meta(self, uid, entry):
        """Known metadata never loses to unknown; airing status tracks
        the latest fetch. (Cross-provider metadata conflicts render per
        provider in remote_state; a dedicated UI is follow-up work.)"""
        ent = self.store.get_entity(uid)
        updates = {}
        if entry.title and not ent.get('title'):
            updates['title'] = entry.title
        if entry.year and not ent.get('year'):
            updates['year'] = entry.year
        if entry.total and not ent.get('total'):
            updates['total'] = entry.total
        if entry.airing_status:
            updates['status'] = entry.airing_status
        if updates:
            self.store.update_entity_meta(uid, **updates)
        # Accumulate every title this provider knows -- cross-language
        # matching (e.g. AniList Native vs Kitsu romaji) depends on it.
        self.store.entity_add_aliases(uid, [entry.title] + entry.aliases)

    # -- plan ----------------------------------------------------------

    def plan(self, mode=SyncMode.MERGE):
        ownership = self.store.ownership()
        plan = SyncPlan(mode)
        plan.identity = [IdentityIssue(id=r['id'], provider=r['provider'],
                                       provider_id=r['provider_id'],
                                       title=r['title'],
                                       candidates=r['candidates'],
                                       status=r['status'])
                         for r in self.store.identity_open()]
        for ent in self.store.entities():
            self._plan_entity(plan, ent, ownership, mode)
        return plan

    def _plan_entity(self, plan, ent, ownership, mode):
        uid = ent['uuid']
        title = ent.get('title') or uid[:8]
        mappings = [m for m in self.store.mappings_of(uid)
                    if m['provider'] in self.adapters]
        if ent.get('provider_only'):
            mappings = [m for m in mappings
                        if m['provider'] == ent['provider_only']]
        if not mappings:
            return
        local = self.store.local_get(uid)
        sides = {}
        for m in mappings:
            provider = m['provider']
            sides[provider] = (
                self.store.remote_get(provider, m['provider_id']),
                self.store.base_get(uid, provider))

        ent_total = ent.get('total')
        for field, policy in ownership.items():
            if policy.kind is PolicyKind.INDIVIDUAL:
                continue
            l_val, l_ts = local.get(field, (None, 0))
            progress_field = field == 'progress'
            states, pulls, divergent = {}, {}, {}
            p_scales, mismatched = {}, {}
            for provider, (remote, base) in sides.items():
                if field not in remote:
                    continue  # provider can't represent this field
                r_val, r_ts = remote[field]
                if progress_field:
                    # Classify in the LOCAL episode structure: raw
                    # numbers from a differing structure (1/1 movie vs
                    # 4-episode listing) are not comparable -- exactly
                    # the case that proposed 'update AniList to 1'.
                    r_scale = (remote.get('_total') or (None, 0))[0]
                    p_scales[provider] = (r_val, r_scale)
                    converted = self._progress_convert(r_val, r_scale,
                                                       ent_total)
                    if converted is None:
                        mismatched[provider] = (r_val, r_scale)
                        continue
                    r_val = converted
                state = three_way(base.get(field, NO_BASE), l_val, r_val)
                states[provider] = (state, r_val, r_ts)
                if state == PULL:
                    pulls[provider] = (r_val, r_ts)
                elif state == BOTH:
                    divergent[provider] = (r_val, r_ts)
            if mismatched:
                # Partial progress across differing structures: honest
                # one-line conflict, field frozen this plan.
                values = {'local': l_val}
                values.update({p: rv for p, (rv, _) in mismatched.items()})
                note = 'episode structures differ: local total %s vs %s' % (
                    ent_total or '?', ', '.join(
                        '%s total %s' % (p, sc or '?')
                        for p, (_, sc) in mismatched.items()))
                plan.conflicts.append(FieldConflict(
                    uid, field, values, base=None, policy=policy,
                    title=title, note=note))
                continue
            if not states:
                continue

            # Fold the primary provider's changes in as local intent
            # (see __init__): they replace the local value up front and
            # are never a conflict against the reconciled DB itself.
            orig_l = l_val
            intent = None
            if self.primary and self.primary in states:
                p_state, p_val, p_ts = states[self.primary]
                if p_state in (PULL, BOTH):
                    intent = (p_val, p_ts)
                    l_val, l_ts = p_val, p_ts
                    merged = {**pulls, **divergent}
                    merged.pop(self.primary, None)
                    # Reclassify the others against the new local value:
                    # equal means converged; different means both sides
                    # moved -> divergence for the policy to arbitrate.
                    pulls = {}
                    divergent = {q: qv for q, qv in merged.items()
                                 if not eq(qv[0], p_val)}

            if mode is SyncMode.MIRROR:
                # Local pushes outward; remote changes are overwritten
                # (the primary's, folded above, being the exception).
                if intent is not None and not eq(l_val, orig_l):
                    plan.changes.append(FieldChange(
                        uid, field, orig_l, l_val, target='local',
                        source=self.primary, title=title,
                        remote_raw=(p_scales[self.primary][0]
                                    if progress_field
                                    and self.primary in p_scales
                                    else None)))
                for provider, (state, r_val, _) in states.items():
                    self._plan_push(plan, uid, title, field, policy,
                                    provider, r_val, l_val, 'local',
                                    progress_field, p_scales, ent_total)
                continue

            resolution = self._resolve_field(
                plan, uid, title, field, policy, l_val, l_ts,
                pulls, divergent, mode)
            if resolution is _UNSET:      # conflict recorded; freeze
                # Still record the working tree's own edit: the
                # conflict is between that intent and the others, not
                # against the reconciled DB's stale value.
                if intent is not None and not eq(intent[0], orig_l):
                    plan.changes.append(FieldChange(
                        uid, field, orig_l, intent[0], target='local',
                        source=self.primary, title=title,
                        remote_raw=(p_scales[self.primary][0]
                                    if progress_field
                                    and self.primary in p_scales
                                    else None)))
                continue
            if resolution is not None:
                effective, eff_source = resolution
            elif intent is not None:
                effective, eff_source = intent[0], self.primary
            else:
                effective, eff_source = orig_l, 'local'
            if not eq(effective, orig_l):
                plan.changes.append(FieldChange(
                    uid, field, orig_l, effective, target='local',
                    source=eff_source, title=title,
                    remote_raw=(p_scales[eff_source][0]
                                if progress_field
                                and eff_source in p_scales else None)))

            if mode is SyncMode.PULL:
                continue  # providers update local; nothing pushes
            for provider, (state, r_val, _) in states.items():
                self._plan_push(plan, uid, title, field, policy,
                                provider, r_val, effective, eff_source,
                                progress_field, p_scales, ent_total)

    def _plan_push(self, plan, uid, title, field, policy, provider,
                   state_val, effective, source, progress_field,
                   p_scales, ent_total):
        """Append the push of `effective` to one provider if needed.
        Progress pushes convert into the provider's own episode
        structure (completing a 4-episode listing pushes 1 to the
        1-episode movie entry, and vice versa)."""
        if progress_field and provider in p_scales:
            raw_r, r_scale = p_scales[provider]
            if eq(state_val, effective):
                return
            target_val = self._progress_convert(effective, ent_total,
                                                r_scale)
            if target_val is None:
                plan.conflicts.append(FieldConflict(
                    uid, field, {'local': effective, provider: raw_r},
                    base=None, policy=policy, title=title,
                    note='episode structures differ: local total %s vs'
                         ' %s total %s' % (ent_total or '?', provider,
                                           r_scale or '?')))
                return
            plan.changes.append(FieldChange(
                uid, field, raw_r, target_val, target=provider,
                source=source, title=title))
            return
        if not self.adapters[provider].values_equivalent(
                field, state_val, effective):
            plan.changes.append(FieldChange(
                uid, field, state_val, effective, target=provider,
                source=source, title=title))

    def _resolve_field(self, plan, uid, title, field, policy,
                       l_val, l_ts, pulls, divergent, mode):
        """Returns None (keep local), (value, source), or _UNSET when a
        FieldConflict was recorded (field frozen until resolved)."""
        def conflict():
            values = {'local': l_val}
            values.update({p: v for p, (v, _) in divergent.items()})
            values.update({p: v for p, (v, _) in pulls.items()})
            plan.conflicts.append(FieldConflict(
                uid, field, values, base=None, policy=policy, title=title))
            return _UNSET

        candidates = {}
        if divergent:
            if mode is SyncMode.PULL:
                # Providers update local: remote side wins divergence.
                candidates.update(divergent)
            else:
                for provider, (r_val, r_ts) in divergent.items():
                    kind, value = conflicts_mod.resolve(
                        policy, field, l_val, r_val, provider, l_ts, r_ts)
                    if kind == 'conflict':
                        return conflict()
                    if kind in ('remote', 'merged'):
                        candidates[provider] = (value, r_ts)
                    # kind 'local': local wins -> push happens in caller
        candidates.update(pulls)
        if not candidates:
            return None
        distinct = {}
        for provider, (value, ts) in candidates.items():
            distinct.setdefault(self._value_key(value),
                                (value, provider, ts))
        if len(distinct) == 1:
            value, provider, _ = next(iter(distinct.values()))
            return (value, provider)
        # Multiple providers propose different values.
        if policy.kind is PolicyKind.PROVIDER and policy.provider in candidates:
            value, ts = candidates[policy.provider]
            return (value, policy.provider)
        if policy.kind is PolicyKind.MERGE:
            values = [v for v, _, _ in distinct.values()]
            if all(isinstance(v, list) for v in values):
                union = sorted({str(x) for v in values for x in v})
                return (union, 'merge')
            value, provider, _ = max(distinct.values(), key=lambda t: t[2])
            return (value, provider)
        return conflict()

    @staticmethod
    def _value_key(value):
        if isinstance(value, list):
            value = sorted(map(str, value))
        return json.dumps(value, sort_keys=True)

    # -- apply ---------------------------------------------------------

    def apply(self, plan):
        """Commit the plan: local changes + events in one transaction,
        then pushes per provider with failure isolation. Conflicts are
        never applied -- resolve them first."""
        txn = self.history.new_txn()
        selected = [c for c in plan.changes if c.selected]
        local_changes = [c for c in selected if c.target == 'local']
        pushes = {}
        for c in selected:
            if c.target != 'local':
                pushes.setdefault(c.target, []).append(c)

        with self.store.transaction():
            for c in local_changes:
                self.store.local_set(c.uuid, c.field, c.new)
                self.history.record(txn, c.uuid, c.field, c.old, c.new,
                                    c.source)
                if c.source in self.adapters:
                    # Pulled from that provider: we now agree with it.
                    # The base records the provider's RAW value -- for
                    # structure-converted progress that differs from
                    # the local-scale value we stored.
                    self.store.base_set(
                        c.uuid, c.source, c.field,
                        c.remote_raw if c.remote_raw is not None
                        else c.new)

        errors = {}
        pushed = 0
        for provider, changes in pushes.items():
            adapter = self.adapters[provider]
            by_entity = {}
            for c in changes:
                by_entity.setdefault(c.uuid, []).append(c)
            failed = False
            for uid, chs in by_entity.items():
                mapping = next((m for m in self.store.mappings_of(uid)
                                if m['provider'] == provider), None)
                if mapping is None:
                    continue
                try:
                    sent = adapter.push(mapping['provider_id'],
                                        {c.field: c.new for c in chs})
                except AdapterError as e:
                    errors[provider] = str(e)
                    failed = True
                    break  # isolate: skip the rest of this provider
                with self.store.transaction():
                    for c in chs:
                        value = sent.get(c.field, c.new)
                        self.history.record(txn, uid, c.field, c.old,
                                            value, provider, op='push')
                        self.store.base_set(uid, provider, c.field, value)
                        self.store.remote_set_all(
                            provider, mapping['provider_id'],
                            {c.field: value})
                        pushed += 1
            if failed:
                continue
        return {'txn': txn, 'local': len(local_changes),
                'pushed': pushed, 'errors': errors}

    # -- local edits ---------------------------------------------------

    def edit_local(self, uid, field, value, source='local'):
        """Record a local edit (git: a commit). All app-originated
        changes should come through here so the event log stays the
        complete history -- direct store writes have no events and
        therefore can't be undone."""
        old = self.store.local_get(uid).get(field, (None, 0))[0]
        txn = self.history.new_txn()
        with self.store.transaction():
            self.store.local_set(uid, field, value)
            self.history.record(txn, uid, field, old, value, source)
        return txn

    # -- conflicts & undo ---------------------------------------------

    def resolve_conflict(self, conflict, choice, value=_UNSET):
        """Resolve a FieldConflict: choice is a source key from
        conflict.values ('local', a provider) or 'value' with an
        explicit value. Writes local state; divergent providers re-plan
        as pushes of the resolved value."""
        if choice == 'value':
            if value is _UNSET:
                raise ValueError("choice 'value' needs value=")
            chosen = value
        else:
            if choice not in conflict.values:
                raise ValueError('unknown side: %s' % choice)
            chosen = conflict.values[choice]
        txn = self.history.new_txn()
        old = conflict.values.get('local')
        with self.store.transaction():
            self.store.local_set(conflict.uuid, conflict.field, chosen)
            self.history.record(txn, conflict.uuid, conflict.field,
                                old, chosen, 'resolve')
            for provider in conflict.values:
                if provider == 'local' or provider not in self.adapters:
                    continue
                # The provider's current value becomes the merge base:
                # the user has acknowledged it. If it differs from the
                # chosen value, the next plan sees a clean local-side
                # change and pushes the resolution -- it never re-raises
                # the same conflict.
                self.store.base_set(conflict.uuid, provider,
                                    conflict.field,
                                    conflict.values[provider])
        return txn

    def undo(self, txn):
        return self.history.undo(txn)
