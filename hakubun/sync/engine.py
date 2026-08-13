"""SyncEngine: fetch -> identity -> plan (field policies) -> apply.

fetch snapshots every provider's list into remote_state; identity
resolution maps entries onto internal entity UUIDs; the SyncPlanner
turns field policies (provider-owned / individual / reconcile) into
explicit SyncOperations; apply commits local changes plus events in one
transaction, then pushes per provider with failure isolation (a
provider failing isolates to that provider -- its base does not
advance, so the same changes re-plan next run).
"""

from hakubun.sync import membership as membership_mod
from hakubun.sync import normalize
from hakubun.sync.adapters import AdapterError
from hakubun.sync.history import History
from hakubun.sync.identity import IdentityResolver
from hakubun.sync.models import SyncCancelled, SyncOperation
from hakubun.sync.normalize import values_equal
from hakubun.sync.planner import SyncPlanner

_UNSET = object()


class SyncEngine:
    def __init__(self, store, adapters=None, msg=None, primary=None,
                 relations=None):
        self.store = store
        self.adapters = dict(adapters or {})
        self.history = History(store)
        self.identity = IdentityResolver(store, atlas=relations)
        # The primary provider is the account the app is signed into.
        # Purely informational for the UIs (icons, labels): it carries
        # no synchronization authority -- field policies do.
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
        own pre-emptive linking, so field planning ignores it entirely
        (only providers with SOME remote row contribute values); only
        the planner's create-entry step (_plan_missing_entries) ever
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
                    self.store.mark_absent(uid, name,
                                           reason='lookup_miss')
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
            # synced (never in ownership, so they never plan):
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
        dropped, and their recorded bases with it: the snapshot must
        mirror what the provider actually holds, or the planner keeps
        comparing -- and pushing -- against a phantom. With no remote
        row the provider simply contributes nothing for that entity; if
        the entry ever reappears it re-plans as a first sync (no base:
        the field's policy or strategy decides afresh) instead of being
        judged against history from before the deletion. Local state is
        untouched -- a remote delete is never propagated as a local one.

        A fetch that returns an EMPTY list while we track entries for
        this provider is left alone: indistinguishable from an API
        quietly returning nothing, and wiping every base over a hiccup
        is far worse than keeping a stale snapshot one run longer.

        The same reasoning does not stop at zero. A fetch that drops a
        LARGE SHARE of a provider's entries at once is the same class
        of event: people delete entries a few at a time, so 250
        vanishing in one response is far more likely to be a partial
        API result, a changed account, or a bad token than 250
        individual decisions. The snapshots are still dropped (they
        genuinely are not in the provider's answer, and pushing
        against a phantom is worse), but no 'ignore' decision is
        recorded -- so Mirror keeps offering to restore them instead of
        silently agreeing they should be gone. That silent agreement is
        what turned one bad AniList response into 250 entries the UI
        would never offer to re-add."""
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
        tracked = self.store.remote_provider_ids(name)
        stale = tracked - fetched
        # See the docstring: a mass disappearance is evidence about the
        # RESPONSE, not about the user's intent. Above this share, drop
        # the snapshots but record no decision -- the entries stay
        # proposable, which is the recoverable failure.
        mass_loss = (len(stale) >= 25
                     and len(stale) > len(tracked) * 0.20)
        if mass_loss:
            self._debug('%s dropped %d of %d tracked entries in one '
                        'fetch; treating as a suspect response rather '
                        'than %d deletions'
                        % (name, len(stale), len(tracked), len(stale)))
        for pid in stale:
            self._debug('%s no longer lists entry %s; dropping its '
                        'remote snapshot' % (name, pid))
            self.store.remote_delete(name, pid)
            mapping = self.store.mapping_for(name, pid)
            if mapping:
                self.store.base_delete(mapping['uuid'], name)
                # The user deleted this entry there on purpose: the
                # planner must not turn around and offer to re-create
                # it. Recorded as membership 'ignore' -- "leave this
                # provider as it is" -- and deliberately NOT 'absent':
                # this is an observation of what the user did once, not
                # a standing instruction to delete the entry again if
                # it ever comes back. clear_membership() (the UI's
                # "ask me about this again") undoes it.
                if not mass_loss:
                    self.store.set_membership(mapping['uuid'], name,
                                              'ignore', reason='deleted')
            row = self.store.identity_get(name, pid)
            if row and row['status'] in ('open', 'deferred'):
                # An unresolved entry the user deleted on the website
                # answers its own question -- don't keep asking about
                # an entry no provider lists. 'unlisted' (not
                # 'ignored') so a future re-add reopens it normally.
                self.store.identity_set_status(row['id'], 'unlisted')

    def _seed_entity(self, txn, uid, entry):
        """First sight of an entity: import this provider's state as the
        local working value and record the base -- in sync at birth."""
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
        """No base recorded but local and remote already agree: record
        the agreement. The base is what reconciliation strategies (and
        the pending-local-edit check for owned fields) measure change
        against -- without it, a later single-sided edit reads as an
        unattributable disagreement instead of a clean propagation."""
        local = self.store.local_get(uid)
        base = self.store.base_get(uid, provider)
        ent_total = (self.store.get_entity(uid) or {}).get('total')
        for field, value in entry.user.items():
            if field in base or field not in local:
                continue
            agrees = values_equal(field, local[field][0], value)
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
        if entry.image and not ent.get('image'):
            # First provider to ship cover art wins, and keeps winning:
            # these URLs are only ever a picture in a preview, so
            # re-pointing them on every fetch would churn the entity
            # table (and everyone's poster cache) to no visible end.
            updates['image'] = entry.image
        if updates:
            self.store.update_entity_meta(uid, **updates)
        # Accumulate every title this provider knows -- cross-language
        # matching (e.g. AniList Native vs Kitsu romaji) depends on it.
        self.store.entity_add_aliases(uid, [entry.title] + entry.aliases)

    # -- plan ----------------------------------------------------------

    def plan(self, should_cancel=None):
        """Build the SyncPlan (see planner.SyncPlanner). Read-only."""
        # Every scale/precision decision in planning reads an adapter's
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
            return SyncPlanner(self.store, self.adapters,
                               primary=self.primary).plan(should_cancel)
        finally:
            for adapter in frozen:
                adapter.thaw_mediainfo()

    # -- mirror --------------------------------------------------------

    def mirror_plan(self, should_cancel=None):
        """Build a MirrorPlan: what the TRACKERS should contain per the
        ownership configuration, ignoring change history (mirror.py).
        Read-only."""
        from hakubun.sync.mirror import MirrorPlanner
        frozen = [a for a in self.adapters.values()
                  if hasattr(a, 'freeze_mediainfo')]
        for adapter in frozen:
            adapter.freeze_mediainfo()
        try:
            return MirrorPlanner(
                self.store, self.adapters,
                primary=self.primary).plan(should_cancel)
        finally:
            for adapter in frozen:
                adapter.thaw_mediainfo()

    def resolve_mirror_conflict(self, conflict, choice, value=_UNSET):
        """Resolve a MIRROR conflict: a disagreement between trackers
        that no policy could settle.

        Deliberately NOT resolve_conflict(). That one records the
        decision by writing local state and advancing every provider's
        base -- and Mirror reads neither, by design, since ignoring
        history is the whole point of it. A resolution recorded that
        way is invisible to the next mirror, which re-raises the
        identical conflict: the user clicks a side, the preview
        rebuilds, and the card is still there.

        So the decision is stored where Mirror does look
        (store.set_field_resolution), fingerprinted against the tracker
        values it was made against: it stands while those stand, and
        lapses if any tracker moves, because then it is a new question.

        `choice` is a tracker name from conflict.values, or 'value'
        with an explicit value=.
        """
        if getattr(conflict, 'structural', False):
            # Progress across differing episode structures: the tracker
            # numbers are each in their OWN structure, so there is no
            # single value to adopt across them and no conversion that
            # would be honest. Nothing Mirror can do -- it is reported
            # as information, and the per-entry fix belongs in Sync,
            # which has the local structure to resolve against.
            raise ValueError(
                'episode structures differ between these trackers; '
                'Mirror cannot convert between them -- resolve this '
                "entry's progress from the Sync tab")
        if choice == 'value':
            if value is _UNSET:
                raise ValueError("choice 'value' needs value=")
            chosen = value
        else:
            if choice not in conflict.values:
                raise ValueError('unknown tracker: %s' % choice)
            chosen = conflict.values[choice]
        from hakubun.sync.mirror import MirrorPlanner
        self.store.set_field_resolution(
            conflict.uuid, conflict.field, chosen,
            MirrorPlanner.fingerprint(conflict.values))
        return chosen

    def clear_mirror_resolution(self, uid, field):
        """Forget a mirror resolution so the disagreement is asked
        about again."""
        self.store.clear_field_resolution(uid, field)

    def apply_mirror(self, plan, allow_adds=False, allow_removes=False,
                     progress=None, should_cancel=None):
        """Commit a MirrorPlan.

        `allow_adds` / `allow_removes` are the BULK GATES, and they are
        enforced here rather than in either sync window: there are two
        UIs plus a headless path, and "the dialog asked" is not a
        safety property. Both default to False, so the only way to
        create or delete entries in bulk is for a caller to have
        explicitly said so -- on top of each operation's own selection,
        which for adds and removes starts unticked.

        Removals run FIRST, and any field operation aimed at an entry
        being removed is dropped: pushing a score to an entry and then
        deleting it is wasted API calls at best and a confusing partial
        state at worst.

        Returns the same shape as apply(), plus 'removed' and
        'skipped' (operations the gates withheld).
        """
        removes = ([r for r in plan.removes if r.selected]
                   if allow_removes else [])
        adds = plan.selected_adds() if allow_adds else []
        skipped = ((len(plan.selected_removes()) if not allow_removes
                    else 0)
                   + (len(plan.selected_adds()) if not allow_adds else 0))

        gone, errors, cancelled = self._apply_removes(
            removes, progress, should_cancel)
        removed = len(gone)

        updates = [o for o in plan.selected_updates()
                   if (o.uuid, o.target) not in gone]
        # An AddOperation is one decision about a whole entry (see
        # mirror.AddOperation); apply() speaks in per-field operations
        # and batches them back together by (provider, entity), so the
        # expansion happens HERE, after the user's single yes/no, and
        # never becomes a set of independently tickable fields.
        adds = [op for op in adds if (op.uuid, op.provider) not in gone]
        add_changes = [
            SyncOperation(op.uuid, field, None, value,
                          target=op.provider,
                          source=(op.provenance[0] if op.provenance
                                  else None),
                          title=op.title, reason=op.reason,
                          selected=True, creates_entry=True,
                          provenance=op.provenance)
            for op in adds
            for field, value in op.values.items()]
        # Local convergence follows the trackers only where the user
        # actually let the trackers converge: a field whose tracker
        # pushes were unticked is left entirely alone, rather than
        # having local quietly adopt a value no tracker was told about
        # (which the next ordinary Sync would then push straight back
        # out). A field with no tracker push at all -- the trackers
        # already agree and only local is stale -- still converges.
        held_back = {(o.uuid, o.field)
                     for o in plan.updates if not o.selected}
        local = [o for o in plan.selected_local()
                 if (o.uuid, o.field) not in held_back]

        result = {'txn': None, 'local': 0, 'pushed': 0, 'errors': {},
                  'cancelled': cancelled}
        if not cancelled:
            from hakubun.sync.models import SyncPlan
            result = self.apply(SyncPlan(changes=local + updates
                                         + add_changes),
                                progress=progress,
                                should_cancel=should_cancel)
        result['errors'] = {**errors, **result.get('errors', {})}
        result['cancelled'] = cancelled or result.get('cancelled', False)
        result['removed'] = removed
        result['skipped'] = skipped
        return result

    def _apply_removes(self, removes, progress=None, should_cancel=None):
        """Delete entries from providers, one at a time, with the same
        per-provider failure isolation as pushes.

        On success the provider's remote snapshot and recorded base are
        cleared so the next plan sees the truth, and the membership
        decision is LEFT in place ('absent'): it is what authorized the
        deletion, and keeping it stops the next fetch from immediately
        proposing to add the entry straight back.

        Returns ({(uuid, provider) actually deleted}, errors,
        cancelled) -- the caller drops field operations aimed at those
        entries, and must not drop them for a delete that FAILED.
        """
        txn = self.history.new_txn()
        gone, errors = set(), {}
        cancelled = False
        for i, op in enumerate(removes):
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            adapter = self.adapters.get(op.provider)
            if adapter is None or not adapter.can_remove():
                errors[op.provider] = ('%s cannot delete list entries'
                                       % op.provider.capitalize())
                continue
            mapping = next((m for m in self.store.mappings_of(op.uuid)
                            if m['provider'] == op.provider), None)
            if mapping is None:
                continue
            remote = self.store.remote_get(op.provider,
                                           mapping['provider_id'])
            if not remote:
                continue    # already gone; nothing to delete
            my_id = (remote.get('_my_id') or (None, None))[0]
            if progress is not None:
                progress(i, len(removes), 'Removing from %s: %s...'
                         % (op.provider.capitalize(), op.title))
            try:
                sent = adapter.remove(mapping['provider_id'],
                                      title=op.title, my_id=my_id,
                                      cancel=should_cancel)
            except SyncCancelled:
                cancelled = True
                break
            except AdapterError as e:
                errors[op.provider] = str(e)
                continue
            if not sent:
                continue
            with self.store.transaction():
                self.store.remote_delete(op.provider,
                                         mapping['provider_id'])
                self.store.base_delete(op.uuid, op.provider)
                # Logged as op='remove', which History.undo ignores by
                # design: undo replays local 'set's and rewinds pushed
                # bases, and a deleted list entry is not something this
                # app can honestly restore. The log stays complete
                # (the Advanced tab and the Inspector read it) without
                # pretending the deletion is reversible.
                self.history.record(txn, op.uuid, '_entry', True, None,
                                    op.provider, op='remove')
            gone.add((op.uuid, op.provider))
        return gone, errors, cancelled

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
                    # Adopted from that provider: we now agree with it.
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
                # actually listed the entry (see planner's
                # _plan_missing_entries): the mapping only records an
                # id someone else claimed for it. Create it there
                # instead of trying to update an entry that doesn't
                # exist.
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
                            # would poison the base with a value the
                            # remote never held -- and the next fetch
                            # would read the provider's real value as
                            # a fresh remote edit and adopt it back
                            # OVER local. Record nothing.
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
        """Record a local edit (a commit). All app-originated changes
        should come through here so the event log stays the complete
        history -- direct store writes have no events and therefore
        can't be undone."""
        old = self.store.local_get(uid).get(field, (None, 0))[0]
        txn = self.history.new_txn()
        with self.store.transaction():
            self.store.local_set(uid, field, value, source=source)
            self.history.record(txn, uid, field, old, value, source)
        return txn

    def set_local_field(self, uid, field, value, source='local'):
        """A user edit made straight against LOCAL (e.g. rating an
        owned-elsewhere score in the owner's system from the main list).

        Like edit_local it commits the value and records the event, but
        it also advances every mapped provider's base to that
        provider's CURRENT remote value. That is what makes the edit
        legible to the planner as a pending LOCAL change: each
        provider's value now equals its own base ("that side did not
        move"), while local differs -- so an owned field routes the
        edit to its owner, and a reconcile strategy attributes the
        change to local instead of seeing an unattributable
        disagreement."""
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

    # -- membership (does this entry belong on this tracker?) ----------
    #
    # See membership.py. Three decisions, persisted per (entity,
    # provider), so the next sync never re-discovers a discrepancy the
    # user has already settled.

    def set_membership(self, uid, provider, want, reason=None):
        """Record what should be true of this entity on `provider`:
        'present' (create it there), 'absent' (remove it from there),
        or 'ignore' (leave whatever is there alone, stop asking).

        'absent' is the only decision that can produce a DELETION, and
        only ever through this call -- i.e. only from a user saying so.
        """
        self.store.set_membership(uid, provider, want, reason=reason)

    def clear_membership(self, uid, provider):
        """Forget a membership decision: the discrepancy becomes an
        open question again and Mirror/Sync may propose an action."""
        self.store.set_membership(uid, provider, None)

    def membership(self, uid=None):
        """Membership for one entity, or {uuid: Membership} for all."""
        built = membership_mod.build(self.store, self.adapters,
                                     uids=None if uid is None else [uid])
        return built if uid is None else built.get(uid)

    def decline_create(self, uid, provider):
        """Durably record "do not create this entity on `provider`":
        the planner stops proposing the creation on every subsequent
        fetch/replan. Distinct from unticking the proposal (transient,
        this-plan-only) and from provider-only (which isolates the
        entity from cross-provider sync entirely).

        Recorded as membership 'ignore', NOT 'absent': declining a
        creation says "don't put it there", which is not the same as
        "take it away if it turns up there". Reversible via
        allow_create()."""
        self.store.set_membership(uid, provider, 'ignore',
                                  reason='declined')

    def allow_create(self, uid, provider):
        """Forget a decline_create (and any cached lookup miss) so the
        next plan may propose the creation again."""
        self.store.set_membership(uid, provider, None)
        self.store.clear_absent(uid, provider)

    # -- conflicts & undo ----------------------------------------------

    def resolve_conflict(self, conflict, choice, value=_UNSET):
        """Resolve a FieldConflict: choice is a source key from
        conflict.values ('local', a provider) or 'value' with an
        explicit value. Writes local state; every listed provider's
        base advances to its current value, so the next plan sees a
        clean local-side resolution to propagate -- it never re-raises
        the same conflict.

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
            # EVERY mapped provider's current value becomes its base:
            # the user has acknowledged the state of the world as of
            # this decision. Providers whose value differs from the
            # chosen one then plan as a clean local-side change (the
            # resolution, provenance 'resolve') to push -- and none of
            # them can re-raise the same conflict. Deliberately not
            # just the providers named in conflict.values: the
            # precision-redundancy collapse can drop a provider from a
            # conflict's options, and skipping its base here would
            # leave that provider re-conflicting forever.
            for m in self.store.mappings_of(conflict.uuid):
                provider = m['provider']
                if provider not in self.adapters:
                    continue
                remote = self.store.remote_get(provider,
                                               m['provider_id'])
                if conflict.field in remote:
                    self.store.base_set(conflict.uuid, provider,
                                        conflict.field,
                                        remote[conflict.field][0])
        return txn

    def undo(self, txn):
        return self.history.undo(txn)
