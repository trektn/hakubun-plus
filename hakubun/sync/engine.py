"""SyncEngine: fetch -> identity -> plan (diff+policies) -> apply.

Git shape: fetch snapshots remotes, plan is the merge preview, apply is
the commit (event log) plus pushes. Local state is canonical; a
provider failing isolates to that provider (its merge base does not
advance, so the same changes re-plan next run).
"""

import json

from hakubun.sync import conflicts as conflicts_mod
from hakubun.sync import normalize
from hakubun.sync.adapters import AdapterError
from hakubun.sync.diff import (BOTH, NO_BASE, PULL, emptyish, eq,
                               three_way)
from hakubun.sync.history import History
from hakubun.sync.identity import IdentityResolver
from hakubun.sync.models import (FieldChange, FieldConflict, IdentityIssue,
                                 PolicyKind, SyncCancelled, SyncMode,
                                 SyncPlan)

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

    def fetch(self, providers=None, should_cancel=None):
        """Download every provider's state (remote-tracking update).
        Returns {provider: error message} for providers that failed;
        the others complete regardless.

        Failure isolation covers PROCESSING too, not just the network
        call: a bug normalizing or identity-resolving one provider's
        entries rolls back that provider's writes (one transaction per
        provider -- which also turns thousands of per-entry commits
        into one) and records the error, instead of killing the whole
        fetch with half a provider's rows committed.

        `should_cancel` (a `() -> bool`) stops the run between
        providers and between entries -- a closing sync window must
        not stay parked behind three providers' full list downloads.
        The provider being ingested when the cancel lands is rolled
        back whole (its next fetch re-derives it); completed providers
        stay. The in-flight network call itself cannot be interrupted,
        so the worst-case wait is one provider's download."""
        errors = {}
        seed_txn = None
        for name, adapter in self.adapters.items():
            if providers and name not in providers:
                continue
            if should_cancel is not None and should_cancel():
                break
            try:
                entries = adapter.fetch()
            except Exception as e:  # failure isolation, incl. bugs
                errors[name] = str(e)
                continue
            try:
                with self.store.transaction():
                    if seed_txn is None:
                        seed_txn = self.history.new_txn()
                    self._ingest(name, entries, seed_txn, should_cancel)
            except SyncCancelled:
                break   # this provider rolled back; stop cleanly
            except Exception as e:
                errors[name] = '%s: %s' % (type(e).__name__, e)
        self._discover_cross_ids(providers, should_cancel)
        return errors

    def _discover_cross_ids(self, providers, should_cancel):
        """After every connected provider's own list is ingested, ask
        any provider whose lib exposes an exact reverse lookup
        (currently AniList's Media(idMal:...); duck-typed via
        ProviderAdapter.supports_mal_id_lookup) whether it has each
        entity whose MAL id is already known from elsewhere (Kitsu's
        published mal_id, the community atlas, ...) but that provider
        has no mapping for yet -- e.g. a show only ever added on Kitsu,
        whose MAL cross-id is known, but that was never independently
        matched by title on AniList because it was never fetched from
        there at all.

        DISCOVERY only: a hit writes a mapping with an EMPTY remote
        snapshot, identical in shape to identity._record_external_ids's
        own pre-emptive linking, so Merge/Pull/Push ignore it entirely
        (they only touch providers with SOME remote row to diff
        against); only Rebase's create step (_plan_rebase_add) ever
        acts on it. A miss is remembered (resolved_absent) so it isn't
        re-queried every fetch forever -- superseded automatically the
        moment a real mapping appears for that (entity, provider) pair
        through any path, since that's checked first.

        Genuine network calls, one per (missing entity, capable
        provider) not already resolved_absent -- paced/rate-limited
        like a push via ProviderAdapter.resolve_by_mal_id. A provider
        failure isolates (stops that provider's discovery only) rather
        than raising into the main fetch above."""
        mal_ids = {m['uuid']: m['provider_id']
                  for m in self.store.mappings_of_provider('mal')}
        if not mal_ids:
            return
        for name, adapter in self.adapters.items():
            if providers and name not in providers:
                continue
            if name == 'mal' or not adapter.supports_mal_id_lookup():
                continue
            if should_cancel is not None and should_cancel():
                return
            mapped = {m['uuid'] for m in
                     self.store.mappings_of_provider(name)}
            absent = self.store.absent_for_provider(name)
            for uid, mal_id in mal_ids.items():
                if uid in mapped or uid in absent:
                    continue
                if should_cancel is not None and should_cancel():
                    return
                try:
                    found = adapter.resolve_by_mal_id(
                        mal_id, cancel=should_cancel)
                except SyncCancelled:
                    return
                except AdapterError:
                    break   # isolate: stop this provider, others proceed
                if found is None:
                    self.store.mark_absent(uid, name)
                    continue
                if self.store.mapping_for(name, found) is not None:
                    continue   # already claimed -- not ours to add
                self.store.add_mapping(uid, name, found, confirmed=False,
                                       via='resolved via MAL id lookup')

    def _ingest(self, name, entries, seed_txn, should_cancel=None):
        """Store one provider's fetched entries (inside a transaction)."""
        for entry in entries:
            if should_cancel is not None and should_cancel():
                raise SyncCancelled()
            # Reserved '_'-prefixed fields are metadata, never
            # synced (never in ownership, so they never diff):
            # '_total' is the provider's own episode count (needed
            # to translate progress between differing structures);
            # '_my_id' is the provider's library-entry id, which
            # Kitsu (both backends) requires to address an update
            # -- distinct from the media id and only learnable
            # from a fetch, so it must be persisted for pushes.
            self.store.remote_set_all(
                name, entry.provider_id,
                {**entry.user, '_total': entry.total,
                 '_my_id': entry.my_id})
            uid = self.identity.resolve_entry(entry)
            if uid is None:
                continue
            self._update_entity_meta(uid, entry)
            if not self.store.local_get(uid):
                self._seed_entity(seed_txn, uid, entry)
            else:
                self._settle_base(uid, name, entry)
        self._forget_unlisted(name, entries)

    def _forget_unlisted(self, name, entries):
        """Remote-tracking rows for entries the provider no longer
        returned (deleted on the website since the last fetch) are
        dropped, and their merge bases with it: the snapshot must
        mirror what the provider actually holds, or the planner keeps
        diffing -- and pushing -- against a phantom. With no remote row
        the provider simply contributes nothing for that entity; if
        the entry ever reappears it re-plans as a first sync (NO_BASE:
        equal values settle, differing ones surface to policy/user)
        instead of being diffed against a base from before the
        deletion. Local state is untouched -- a remote delete is never
        propagated as a local one.

        A fetch that returns an EMPTY list while we track entries for
        this provider is left alone: indistinguishable from an API
        quietly returning nothing, and wiping every base over a hiccup
        is far worse than keeping a stale snapshot one run longer."""
        fetched = {str(e.provider_id) for e in entries}
        if not fetched:
            return
        # Fields no fetched entry reports (normalize omits the ones
        # this provider cannot represent) have no business staying in
        # its snapshot -- earlier revisions fabricated e.g. tags=[]
        # for tagless providers, and those rows would diff forever.
        reported = {f for e in entries for f in e.user}
        self.store.remote_prune_fields(
            name, reported | {'_total', '_my_id'})
        stale = self.store.remote_provider_ids(name) - fetched
        for pid in stale:
            self._debug('%s no longer lists entry %s; dropping its '
                        'remote snapshot' % (name, pid))
            self.store.remote_delete(name, pid)
            mapping = self.store.mapping_for(name, pid)
            if mapping:
                self.store.base_delete(mapping['uuid'], name)
            row = self.store.identity_get(name, pid)
            if row and row['status'] in ('open', 'deferred'):
                # An unresolved entry the user deleted on the website
                # answers its own question -- don't keep asking about
                # an entry no provider lists. 'unlisted' (not
                # 'ignored') so a future re-add reopens it normally.
                self.store.identity_set_status(row['id'], 'unlisted')

    def _seed_entity(self, txn, uid, entry):
        """First sight of an entity: import this provider's state as the
        local state and set the merge base -- in sync at birth."""
        with self.store.transaction():
            for field, value in entry.user.items():
                self.store.local_set(uid, field, value,
                                     source=entry.provider)
                self.history.record(txn, uid, field, None, value,
                                    entry.provider)
                self.store.base_set(uid, entry.provider, field, value)

    # Episode-structure translation lives in normalize (the overlay
    # needs it too); kept as engine aliases for the call sites here.
    _complete = staticmethod(normalize.progress_complete)
    _progress_convert = staticmethod(normalize.progress_convert)

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

    def plan(self, mode=SyncMode.MERGE, should_cancel=None):
        # Every scale/precision decision below reads an adapter's
        # mediainfo, which is a live property (deliberately -- AniList's
        # score format is only learned during a fetch). Planning reads
        # it O(entities x fields x providers) times, so pin it for the
        # duration: a format cannot change mid-plan, and thawing in a
        # finally keeps the live guarantee everywhere else.
        frozen = [a for a in self.adapters.values()
                  if hasattr(a, 'freeze_mediainfo')]
        for adapter in frozen:
            adapter.freeze_mediainfo()
        try:
            return self._plan(mode, should_cancel)
        finally:
            for adapter in frozen:
                adapter.thaw_mediainfo()

    def _plan(self, mode, should_cancel):
        ownership = self.store.ownership()
        plan = SyncPlan(mode)
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
        for ent in ents:
            if should_cancel is not None and should_cancel():
                raise SyncCancelled()
            self._plan_entity(plan, ent, ownership, mode, snapshot)
        return plan

    def _plan_entity(self, plan, ent, ownership, mode, snapshot):
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
        sides = {}
        for m in mappings:
            provider = m['provider']
            sides[provider] = (
                snapshot['remote'].get(provider, {}).get(
                    m['provider_id'], {}),
                snapshot['base'].get((uid, provider), {}))

        if mode is SyncMode.REBASE:
            self._plan_rebase_add(plan, uid, title, ownership, sides, local)

        ent_total = ent.get('total')
        for field, policy in ownership.items():
            if policy.kind is PolicyKind.INDIVIDUAL:
                continue
            l_val, l_ts = local.get(field, (None, 0))
            progress_field = field == 'progress'
            states, pulls, divergent = {}, {}, {}
            p_scales, mismatched = {}, {}
            # Providers we have no shared history with for this field
            # (no merge base). Overwriting one of these is a first-sync
            # coin flip, not a decision -- see FieldChange.first_sync.
            unbased = set()
            for provider, (remote, base) in sides.items():
                if field not in remote:
                    continue  # provider can't represent this field
                r_val, r_ts = remote[field]
                b_val = base.get(field, NO_BASE)
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
                    # The merge base records the provider's RAW value
                    # (its own structure); it must be viewed through
                    # the same conversion as the remote or three_way
                    # compares values in different units -- a movie
                    # base of 1 against a local 4 would read as 'both
                    # changed' when nothing moved. A base that no
                    # longer converts (partial, structures changed) is
                    # honestly no base at all.
                    if b_val is not NO_BASE:
                        b_conv = self._progress_convert(b_val, r_scale,
                                                        ent_total)
                        b_val = NO_BASE if b_conv is None else b_conv
                if b_val is NO_BASE:
                    unbased.add(provider)
                state = three_way(b_val, l_val, r_val)
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
                    title=title, note=note, structural=True))
                continue
            if not states:
                continue

            # Fold the primary provider's changes in as local intent
            # (see __init__): they replace the local value up front and
            # are never a conflict against the reconciled DB itself.
            #
            # But ONLY a genuine change: if the primary's value is just
            # what local already is at the primary's own precision
            # (switching to MAL, whose integer 8 is local's 8.4 rounded
            # -- not an edit), folding it would degrade local to the
            # coarser value and then push that over every finer
            # provider. So skip the fold when the primary's value is
            # equivalent to local under the primary's precision; a real
            # in-app edit (8.4 -> 9) is not equivalent and still folds.
            orig_l = l_val
            intent = None
            primary_adapter = self.adapters.get(self.primary)
            if self.primary and self.primary in states:
                p_state, p_val, p_ts = states[self.primary]
                if p_state in (PULL, BOTH) and not (
                        primary_adapter is not None
                        and primary_adapter.values_equivalent(
                            field, p_val, l_val)):
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

            if mode is SyncMode.REBASE:
                # Retroactively re-establish each field's declared OWNER
                # as the truth everywhere, ignoring the merge base: the
                # owner's current value is forced into local and pushed
                # over every other tracker, whether or not anything
                # "changed" since the last sync. This is the deliberate
                # "I just set MAL to own scores -- now go make that real
                # on Kitsu and AniList" action; nothing here consults
                # three_way's PULL/PUSH/BOTH verdict (which only sees
                # divergence). Previewed and checkbox-selectable like any
                # plan, and it still converges: pushes advance each
                # provider's base as usual. Fields with no single owner
                # (merge/ask/individual) have nothing to rebase to and
                # are left alone.
                #
                # Deliberately placed AFTER the primary fold: 'local' for
                # a `local`-owned field means the working tree, which
                # includes the edit you just made in the app. Rebasing
                # off the pre-fold database value instead would push the
                # STALE number back over the account you are signed into
                # -- silently reverting your own rating.
                #
                # Only a fold backed by a real merge base counts: with
                # no shared history the primary's value is simply what
                # that site happens to hold, not something the user did
                # in the app, and treating it as intent would quietly
                # make "rebase to local" mean "rebase to the signed-in
                # account" on every first sync.
                rebase_intent = (intent if intent is not None
                                 and self.primary not in unbased else None)
                self._plan_rebase(plan, uid, title, field, policy,
                                  l_val if rebase_intent is not None
                                  else orig_l, orig_l, rebase_intent,
                                  states, progress_field, p_scales,
                                  ent_total)
                continue

            # Drop a provider's voice from divergent/pulls when it's
            # fully explained by local's current value or another
            # provider's value once THAT provider's own precision is
            # applied (see _collapse_precision_redundant): a coarser
            # provider that merely reflects the rounding of a value
            # already present isn't independent information and must
            # never turn into a redundant conflict option.
            if divergent:
                divergent = self._collapse_precision_redundant(
                    field, l_val, divergent)
            if pulls:
                pulls = self._collapse_precision_redundant(
                    field, l_val, pulls)

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
                # Adopting a provider's value into local across a first
                # sync overwrites whatever local held (i.e. the FIRST
                # provider ingested) with no shared history to justify
                # it -- same coin flip as a push, flagged the same way.
                # Filling in a blank is never an overwrite: nobody's
                # data is lost, so those still apply normally.
                local_first = (eff_source in unbased
                               and not emptyish(orig_l))
                plan.changes.append(FieldChange(
                    uid, field, orig_l, effective, target='local',
                    source=eff_source, title=title,
                    selected=not local_first, first_sync=local_first,
                    remote_raw=(p_scales[eff_source][0]
                                if progress_field
                                and eff_source in p_scales else None)))

            if mode is SyncMode.PULL:
                continue  # providers update local; nothing pushes
            # The stored provenance of the CURRENT local value: who a
            # value that is 'just local now' originally came from --
            # this plan's own resolution supersedes it when one exists.
            provenance = (eff_source if resolution is not None
                          or intent is not None
                          else snapshot['sources'].get(uid, {}).get(field))
            for provider, (state, r_val, _) in states.items():
                self._plan_push(plan, uid, title, field, policy,
                                provider, r_val, effective, eff_source,
                                progress_field, p_scales, ent_total,
                                provenance=provenance, unbased=unbased)

    def _collapse_precision_redundant(self, field, l_val, group):
        """Drop a provider's value from a same-field group (divergent
        or pull candidates) when it is fully explained by -- consistent
        with, under THAT provider's own precision -- local's current
        value or another, at-least-as-precise provider's value already
        kept from this same group.

        Concretely: MAL only stores an integer 0-10 score. If local
        reads 9.5 and MAL reads 10, that is not MAL disagreeing with
        anyone -- 10 is exactly what MAL would show for 9.5 (or for
        AniList's more precise 9.9). A provider whose number is fully
        explained this way is never asked about; it carries no
        information beyond what a more precise source already states.

        Processing order matters: providers are checked from most to
        least precise, each only against SURVIVORS confirmed so far
        (never the full original group). Checking symmetrically against
        every other raw value would make two providers that happen to
        agree EXACTLY (e.g. AniList 3.0 and MAL 3) each look "explained
        by" the other and delete both, losing the value entirely: MAL
        gets to be redundant against AniList, but never the reverse.

        Never drops local itself, and never drops a value that isn't
        actually redundant -- a MAL score that does NOT match anyone
        else's rounding is a real edit and stays.
        """
        if not group:
            return group

        def precision(provider):
            if field != 'score':
                return 0
            info = getattr(self.adapters.get(provider), 'mediainfo',
                          None) or {}
            smax = info.get('score_max') or 10
            step = info.get('score_step') or 1
            return smax / step if step else float('inf')

        kept = {}
        survivors = [l_val]
        for provider, (r_val, r_ts) in sorted(
                group.items(), key=lambda kv: precision(kv[0]),
                reverse=True):
            adapter = self.adapters.get(provider)
            if adapter is not None and any(
                    adapter.values_equivalent(field, r_val, other)
                    for other in survivors):
                continue    # redundant -- add nothing to survivors
            kept[provider] = (r_val, r_ts)
            survivors.append(r_val)
        return kept

    def _plan_rebase_add(self, plan, uid, title, ownership, sides, local):
        """REBASE-only: create the show on every connected, MAPPED
        provider that doesn't have it yet -- an empty remote snapshot
        for that provider (`sides[provider][0]`) despite a mapping
        existing means identity resolution knows the id (e.g. a
        provider-published cross-id another provider's fetch recorded
        pre-emptively, see identity._record_external_ids) but that
        provider's own fetch has never actually listed an entry there.
        Ordinary rebase pushes (_plan_rebase/_plan_push below) can never
        reach these: they only iterate `states`, which by construction
        only contains providers with SOME remote row to diff against.

        Bundles every owned field's CURRENT authoritative value (the
        same value an ordinary rebase would push to an existing entry)
        into one create per missing provider -- apply() tells create
        from update purely by remote-snapshot presence at apply time,
        so no separate plumbing is needed there. Fields with no single
        owner (merge/ask/individual) contribute nothing, same as an
        ordinary rebase push. A provider-owned field whose declared
        owner has nothing to give (typically because the owner IS one
        of the providers being created -- it can't be its own source)
        falls back to local's current value instead of leaving a brand
        new entry with that field blank; ordinary rebase reconciles it
        properly against the real owner once that owner actually
        exists. Planned unselected, like first_sync -- see
        FieldChange.creates_entry."""
        missing = [p for p, (remote, _base) in sides.items() if not remote]
        if not missing:
            return
        values = {}   # field -> (value, source) -- source shown in the
                      # UI so "where did this come from" is never a
                      # guess: 'local' for a local-owned field, the
                      # owning provider's name for a provider-owned one.
        for field, policy in ownership.items():
            if policy.kind is PolicyKind.LOCAL:
                val = local.get(field, (None, 0))[0]
                source = 'local'
            elif policy.kind is PolicyKind.PROVIDER:
                remote, _base = sides.get(policy.provider, ({}, {}))
                val = remote.get(field, (None, 0))[0]
                source = policy.provider
                if val in (None, '', []):
                    val = local.get(field, (None, 0))[0]
                    source = 'local'
            else:
                continue
            if val not in (None, '', []):
                values[field] = (val, source)
        if not values:
            return
        for provider in missing:
            if not self.adapters[provider].mediainfo.get('can_add', True):
                continue
            for field, (val, source) in values.items():
                plan.changes.append(FieldChange(
                    uid, field, None, val, target=provider,
                    source=source, title=title,
                    selected=False, creates_entry=True))

    def _plan_rebase(self, plan, uid, title, field, policy, l_val,
                     orig_l, intent, states, progress_field, p_scales,
                     ent_total):
        """One field, REBASE mode: force its declared owner everywhere.

        The authoritative value is the owner's CURRENT value -- a named
        provider's live value for a `provider:` policy, or the local
        value for a `local` policy. It is written into local state and
        pushed to every provider whose value differs, with no reference
        to the merge base (that is the whole point of rebase: re-assert
        ownership over values that already "agree" with a stale base).
        Fields owned by no one in particular (merge/ask/individual) are
        skipped -- there is nothing to rebase them to.

        `l_val` is the working tree: the stored local value with the
        signed-in account's own fetched edit already folded in (caller).
        `orig_l` is the pre-fold database value, which is what a local
        change is recorded as replacing."""
        if policy.kind is PolicyKind.PROVIDER:
            if policy.provider not in states:
                return                       # owner lists nothing here
            effective = states[policy.provider][1]
            source = policy.provider
        elif policy.kind is PolicyKind.LOCAL:
            effective = l_val
            # When the fold supplied this value, credit the account it
            # came from so apply() advances THAT provider's merge base
            # (git: the commit records its real parent).
            source = self.primary if intent is not None else 'local'
        else:
            return
        if not eq(effective, orig_l):
            plan.changes.append(FieldChange(
                uid, field, orig_l, effective, target='local',
                source=source, title=title,
                remote_raw=(p_scales[source][0] if progress_field
                            and source in p_scales else None)))
        for provider, (_state, r_val, _ts) in states.items():
            self._plan_push(plan, uid, title, field, policy, provider,
                            r_val, effective, source, progress_field,
                            p_scales, ent_total)

    def _plan_push(self, plan, uid, title, field, policy, provider,
                   state_val, effective, source, progress_field,
                   p_scales, ent_total, provenance=None, unbased=None):
        """Append the push of `effective` to one provider if needed.
        Progress pushes convert into the provider's own episode
        structure (completing a 4-episode listing pushes 1 to the
        1-episode movie entry, and vice versa).

        A field's declared PROVIDER owner is never overwritten on the
        strength of some OTHER provider's authority: `source` is
        'local' for a direct/canonical edit (e.g. set_local_field, or
        a local value simply pending push) and the owner's own name
        when the owner's remote value itself won arbitration -- both
        legitimately reach the owner. But when `source` is a
        DIFFERENT provider (typically the signed-in primary's fetched
        change folded in as local intent, engine.py `_plan_entity`),
        that provider is not this field's authority and must not push
        over -- silently overwriting -- the actual owner.

        `provenance` makes the guard DURABLE: `source` only knows this
        plan's arbitration, but once a provider-fed value has been
        COMMITTED to local state it reappears one plan later as a
        plain local-side change (source 'local') and would sail
        through. The stored provenance (local_state.source) still says
        which provider fed it, so the owner stays protected until an
        actually-authoritative write (a direct edit, a user
        resolution, or the owner itself) supersedes the value. Mirror
        mode passes provenance=None on purpose: mirror is the explicit
        'local overwrites everyone' mode.

        `unbased` names the providers we have no merge base with for
        this field. A push to one of those overwrites a side we share
        no history with, so it is flagged first_sync and planned
        UNSELECTED (see FieldChange.first_sync). Mirror and rebase pass
        unbased=None on purpose -- both are explicit 'overwrite them'
        instructions the user picked from the mode dropdown."""
        if policy.kind is PolicyKind.PROVIDER and policy.provider == provider:
            if source not in ('local', provider):
                return
            if provenance in self.adapters and provenance != provider:
                return
        # A value the target itself supplied is never "overwriting" it,
        # and neither is filling in a blank -- only a real value being
        # replaced by another real value is the coin flip we guard.
        first = (bool(unbased) and provider in unbased
                 and source != provider)
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
                                           r_scale or '?'),
                    structural=True))
                return
            push_first = first and not emptyish(raw_r)
            plan.changes.append(FieldChange(
                uid, field, raw_r, target_val, target=provider,
                source=source, title=title,
                selected=not push_first, first_sync=push_first))
            return
        if not self.adapters[provider].values_equivalent(
                field, state_val, effective):
            push_first = first and not emptyish(state_val)
            plan.changes.append(FieldChange(
                uid, field, state_val, effective, target=provider,
                source=source, title=title,
                selected=not push_first, first_sync=push_first))

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

    def apply(self, plan, progress=None, should_cancel=None):
        """Commit the plan: local changes + events in one transaction,
        then pushes per provider with failure isolation. Conflicts are
        never applied -- resolve them first.

        `progress`, when given, is called as progress(done, total,
        message) from THIS thread (the UI marshals it): once for the
        local commit and once per (provider, show) push batch --
        network pushes are what actually take time, so that's the
        granularity a progress bar can honestly report.

        `should_cancel` (a `() -> bool`) stops a long run cleanly
        between push batches (and inside a rate-limit wait): whatever
        was already committed stays, the rest re-plans. The result's
        'cancelled' flag says whether it stopped early.
        """
        txn = self.history.new_txn()
        selected = [c for c in plan.changes if c.selected]
        local_changes = [c for c in selected if c.target == 'local']
        pushes = {}
        for c in selected:
            if c.target != 'local':
                pushes.setdefault(c.target, []).append(c)

        total_steps = (1 if local_changes else 0) + sum(
            len({c.uuid for c in changes})
            for changes in pushes.values())
        done = 0

        def report(message):
            if progress is not None:
                progress(done, total_steps, message)

        def on_wait(provider, seconds, attempt):
            report('Rate limited by %s; waiting %ss before retry %d...'
                   % (provider.capitalize(), seconds, attempt))

        with self.store.transaction():
            for c in local_changes:
                self.store.local_set(c.uuid, c.field, c.new,
                                     source=c.source)
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
        if local_changes:
            done += 1
            report('Committed %d local change(s).' % len(local_changes))

        errors = {}
        pushed = 0
        cancelled = False
        for provider, changes in pushes.items():
            if cancelled:
                break
            adapter = self.adapters[provider]
            by_entity = {}
            for c in changes:
                by_entity.setdefault(c.uuid, []).append(c)
            entities = list(by_entity.items())
            # Batch-load once per provider instead of one mappings_of()
            # + remote_get() round-trip per entity: apply() never adds
            # or changes a mapping mid-call (that only happens in
            # identity.py, before plan()/apply() run), so nothing here
            # can go stale between the load and the loop below.
            mappings_by_uid = self.store.mappings_many(
                [uid for uid, _ in entities])
            provider_id_of = {}
            for uid, _ in entities:
                m = next((mm for mm in mappings_by_uid.get(uid, ())
                          if mm['provider'] == provider), None)
                if m is not None:
                    provider_id_of[uid] = m['provider_id']
            remote_by_provider_id = self.store.remote_get_many(
                provider, provider_id_of.values())
            for i, (uid, chs) in enumerate(entities):
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                provider_id = provider_id_of.get(uid)
                if provider_id is None:
                    done += 1   # counted in total_steps; keep it honest
                    continue
                remote = remote_by_provider_id.get(provider_id, {})
                # No remote row at all means this provider has never
                # actually listed the entry (see _plan_rebase_add): the
                # mapping only records an id someone else claimed for
                # it. Create it there instead of trying to update an
                # entry that doesn't exist.
                creating = not remote
                my_id = (remote.get('_my_id') or (None, None))[0]
                report('%s to %s: %s (%s)...' % (
                    'Adding' if creating else 'Pushing',
                    provider.capitalize(), chs[0].title,
                    ', '.join(c.field for c in chs)))
                try:
                    if creating:
                        sent, new_my_id = adapter.add(
                            provider_id,
                            {c.field: c.new for c in chs},
                            title=chs[0].title, on_wait=on_wait,
                            cancel=should_cancel)
                    else:
                        sent = adapter.push(provider_id,
                                            {c.field: c.new for c in chs},
                                            title=chs[0].title, my_id=my_id,
                                            on_wait=on_wait,
                                            cancel=should_cancel)
                except SyncCancelled:
                    cancelled = True
                    break
                except AdapterError as e:
                    errors[provider] = str(e)
                    # This entity plus the rest of the provider's batch
                    # are skipped; count them all or the progress bar
                    # stalls short for every later provider.
                    done += len(entities) - i
                    report('FAILED %s: %s' % (provider.capitalize(), e))
                    break  # isolate: skip the rest of this provider
                with self.store.transaction():
                    remote_values = {}
                    for c in chs:
                        if c.field not in sent:
                            # The adapter could not represent this
                            # field (capability gap, or a value that
                            # failed conversion): nothing reached the
                            # provider, so recording it as pushed
                            # would poison the merge base with a value
                            # the remote never held -- and the next
                            # fetch would read the provider's real
                            # value as a fresh remote edit and pull it
                            # back OVER local. Record nothing.
                            continue
                        value = sent[c.field]
                        op = 'add' if creating else 'push'
                        self.history.record(txn, uid, c.field, c.old,
                                            value, provider, op=op)
                        self.store.base_set(uid, provider, c.field, value)
                        remote_values[c.field] = value
                        pushed += 1
                    if creating and sent and new_my_id is not None:
                        remote_values['_my_id'] = new_my_id
                    if remote_values:
                        self.store.remote_set_all(
                            provider, provider_id, remote_values)
                done += 1
                report('%s to %s: %s.' % ('Added' if creating else 'Pushed',
                                          provider.capitalize(),
                                          chs[0].title))
        if cancelled:
            report('Sync cancelled -- %d push(es) done, the rest '
                   're-plan.' % pushed)
        return {'txn': txn, 'local': len(local_changes),
                'pushed': pushed, 'errors': errors, 'cancelled': cancelled}

    # -- local edits ---------------------------------------------------

    def edit_local(self, uid, field, value, source='local'):
        """Record a local edit (git: a commit). All app-originated
        changes should come through here so the event log stays the
        complete history -- direct store writes have no events and
        therefore can't be undone."""
        old = self.store.local_get(uid).get(field, (None, 0))[0]
        txn = self.history.new_txn()
        with self.store.transaction():
            self.store.local_set(uid, field, value, source=source)
            self.history.record(txn, uid, field, old, value, source)
        return txn

    def set_local_field(self, uid, field, value, source='local'):
        """A user edit made straight against LOCAL (e.g. rating an
        owned-elsewhere score in the owner's system from the main list),
        as opposed to one folded in from a provider's fetch.

        Like edit_local it commits the value and records the event, but
        it also advances every mapped provider's merge base to that
        provider's CURRENT remote value. Without that, a provider with
        no recorded base (NO_BASE) reads the edit as divergence rather
        than a clean local change -- and under a 'provider owns this
        field' policy the owner's own (stale) value would win the
        arbitration and silently discard the edit (conflicts.resolve
        returns the provider side). Seeding base = remote makes it a
        clean local-only change: the next plan simply PUSHES the edit to
        the owner, and thence to everyone. Git: an explicit local commit
        whose parent is each remote's current tip."""
        old = self.store.local_get(uid).get(field, (None, 0))[0]
        txn = self.history.new_txn()
        with self.store.transaction():
            self.store.local_set(uid, field, value, source=source)
            self.history.record(txn, uid, field, old, value, source)
            for m in self.store.mappings_of(uid):
                remote = self.store.remote_get(m['provider'],
                                               m['provider_id'])
                self.store.base_set(uid, m['provider'], field,
                                    remote.get(field, (None,))[0])
        return txn

    # -- conflicts & undo ---------------------------------------------

    def resolve_conflict(self, conflict, choice, value=_UNSET):
        """Resolve a FieldConflict: choice is a source key from
        conflict.values ('local', a provider) or 'value' with an
        explicit value. Writes local state; divergent providers re-plan
        as pushes of the resolved value.

        A STRUCTURAL conflict (progress across differing episode
        structures) only accepts 'local' or 'value': the provider-side
        numbers are in each provider's OWN structure, and writing one
        raw into local state would record a different amount of the
        work as watched (Kitsu's 1 of a 1-episode movie is not 1 of
        the local 4-episode listing)."""
        if getattr(conflict, 'structural', False) \
                and choice not in ('local', 'value'):
            raise ValueError(
                "episode structures differ: %s's value is in its own "
                'structure and cannot be adopted as-is -- choose '
                "'local' or supply an explicit value in the local "
                'structure' % choice)
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
            self.store.local_set(conflict.uuid, conflict.field, chosen,
                                 source='resolve')
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
