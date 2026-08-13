"""Identity resolution: provider entries -> internal UUIDs.

Three tiers (docs/multisync.md §3):
1. Exact, provider-published id links merge automatically (confirmed).
2. A single title candidate that matches *exactly* (normalized title or
   alias equality, compatible type and year) links automatically as an
   unconfirmed "auto" mapping -- without this, every legacy-Kitsu entry
   in a large list becomes a manual confirmation.
3. Anything genuinely ambiguous -- multiple candidates, similarity
   short of equality, a year mismatch, a same-provider duplicate --
   becomes an identity conflict for the user. Ambiguity is never
   auto-merged.
"""


from hakubun.sync.normalize import normalize_title


class IdentityResolver:
    def __init__(self, store, atlas=None):
        self._store = store
        # Optional anime-relations id atlas (sync/relations.py):
        # community-verified cross-provider ids, trusted like
        # provider-published ones.
        self._atlas = atlas
        # uuid -> ((title, alias_count), normalized-name set): the
        # candidate scan compares every fetched entry against every
        # entity's names; rebuilding each entity's set per entry is
        # the quadratic half normalize_title's memoization doesn't
        # cover. Aliases only ever append and a title is only set
        # once, so (title, len(aliases)) is a sufficient freshness
        # signature.
        self._names_cache = {}

    @property
    def atlas(self):
        """Exposed for the Inspector: it needs to show what the atlas
        independently says about an id, regardless of resolution
        state, so the user can cross-check trust."""
        return self._atlas

    def _external_ids(self, entry):
        """{provider: (id, source)}. Provider-published ids ('published'
        -- e.g. AniList/Kitsu-GraphQL's own idMal) take priority; the
        community id atlas fills in anything the provider itself doesn't
        publish. The source is kept (not just the id) so a mapping
        created from this can record exactly how it was linked, for the
        Inspector -- and it names the specific database
        ('anime-relations'), since community databases are not all
        equally authoritative.

        The atlas is anime-only -- every anime-relations rule is a
        MAL|Kitsu|AniList *anime* id triple -- and must never be
        consulted for a manga entry: the same numeric id space means a
        manga id would silently collide with an unrelated anime's rule
        and produce a nonsense cross-reference.
        """
        ids = {p: (pid, 'published') for p, pid in entry.external_ids.items()}
        if self._atlas is not None and entry.media_type == 'anime':
            sources = self._atlas.lookup_sources(
                entry.provider, entry.provider_id)
            for provider, pid in self._atlas.lookup(
                    entry.provider, entry.provider_id).items():
                ids.setdefault(provider, (pid, sources.get(provider, 'atlas')))
        ids.pop(entry.provider, None)
        return ids

    # -- resolution ----------------------------------------------------

    def resolve_entry(self, entry):
        """Resolve one NormalizedEntry to a uuid, or None if it needs
        the user (identity conflict recorded/kept open)."""
        store = self._store

        # 1. Already mapped -- but never trust a mapping whose entity
        #    is the wrong media type. A normal fetch can never produce
        #    one (see _exact_link's own type guard), but
        #    _record_external_ids plants mappings PREEMPTIVELY for
        #    providers that haven't been fetched yet, purely from
        #    another provider's claimed cross-id (e.g. AniList's
        #    mal_id) -- if that claim is wrong (bad source data, or it
        #    actually names a different media type on MAL's own side),
        #    the mapping sits there and would be trusted by every
        #    future fetch of that id forever, since this is the FIRST
        #    thing checked. Quarantine it and re-resolve fresh instead.
        quarantined = False
        mapping = store.mapping_for(entry.provider, entry.provider_id)
        if mapping:
            ent = store.get_entity(mapping['uuid']) or {}
            if ent.get('media_type') == entry.media_type:
                return mapping['uuid']
            quarantined = True
            store.remove_mapping(entry.provider, entry.provider_id)
            store.identity_upsert(
                entry.provider, entry.provider_id, entry.title,
                [self._candidate_of(mapping['uuid'], via=(
                    '⚠ TYPE MISMATCH -- this id was already mapped to a '
                    '%s entity, but this %s entry is %s. The mapping '
                    'has been removed and this entry is being '
                    're-resolved from scratch. This means some source '
                    'data was wrong (a published id or an '
                    'anime-relations rule) -- worth checking.'
                    % ((ent.get('media_type') or '?').capitalize(),
                       entry.provider, entry.media_type)))],
                entry=self._entry_payload(entry))
            # Fall through to steps 2-5 below: resolve this entry as
            # if it had never been mapped at all.

        # 2. Respect previous user decisions.
        previous = store.identity_get(entry.provider, entry.provider_id)
        if previous and previous['status'] == 'ignored':
            return None

        # 3. Exact external-id links (provider-published mappings, e.g.
        #    AniList/Kitsu publishing the MAL id).
        linked = self._exact_link(entry)
        if linked == 'ambiguous':
            return None            # conflict recorded by _exact_link
        if linked:
            if previous:
                store.identity_set_status(previous['id'], 'resolved')
            return linked

        # 4. Title candidates: auto-link the unambiguous, ask about the
        #    rest, create a new entity when nothing matches at all.
        #    ONLY exact candidates gate the auto-link: similarity-grade
        #    candidates must never block it, or every franchise entry
        #    ("Frieren" vs "Frieren Season 2" in the same list) turns
        #    into a manual confirmation.
        candidates = self._candidates(entry)
        exact_all = [c for c in candidates if c['exact']]
        exact = [c for c in exact_all
                 if c['year_ok']
                 and entry.provider not in c['providers']]
        if len(exact_all) == 1 and len(exact) == 1:
            uid = exact[0]['uuid']
            store.add_mapping(uid, entry.provider, entry.provider_id,
                              confirmed=False,   # auto, not user-confirmed
                              via='exact title match (auto-linked)')
            store.entity_add_aliases(uid, [entry.title] + entry.aliases)
            if previous:
                store.identity_set_status(previous['id'], 'resolved')
            return uid
        # Only candidates NOT already mapped on this provider are worth
        # asking about: a similar-titled entity that already has its own
        # entry of this provider is a franchise sibling ("Frieren" next
        # to "Frieren Season 2"), not a potential duplicate.
        askable = [c for c in candidates
                   if entry.provider not in c['providers']]
        if askable:
            # Preserve ONLY a live 'deferred' (the user asked to keep
            # watching); any other previous status ('resolved' whose
            # mapping has since been quarantined away, ...) must reopen
            # -- carrying it over would leave a real unresolved entry
            # invisibly marked resolved forever.
            store.identity_upsert(
                entry.provider, entry.provider_id, entry.title, askable,
                status=self._reopen_status(entry.provider,
                                           entry.provider_id),
                entry=self._entry_payload(entry))
            return None
        # Nothing left to ask about: this entry gets its own entity.
        # Any question we had recorded about it is now answered, so
        # close it -- otherwise the row stays 'open' forever (step 1
        # returns early on every later fetch and never revisits it),
        # the Identity tab lists an entry that IS linked, and
        # "resolving" it again would repoint or duplicate the mapping.
        # EXCEPT a row this very call raised by quarantining a
        # type-mismatched mapping: that is a "your source data is
        # wrong" warning, and re-homing the entry does not make it
        # untrue. It stays open until the user acknowledges it.
        uid = self._create_entity(entry)
        if previous and not quarantined:
            store.identity_set_status(previous['id'], 'resolved')
        return uid

    def _reopen_status(self, provider, provider_id):
        """Status for re-recording a conflict: keep a live 'deferred'
        (a user choice), reopen anything else (see resolve_entry)."""
        row = self._store.identity_get(provider, provider_id)
        return ('deferred' if row and row['status'] == 'deferred'
                else 'open')

    def _exact_link(self, entry):
        """Link via ids the provider itself publishes (or the
        anime-relations atlas). A claimed id already mapped to a
        *different* entity than another claim is an ambiguity ->
        identity conflict, never an auto-merge.

        A type mismatch (the matched entity is anime while this entry
        is manga, or vice versa) is blocked outright, even when it's
        the only candidate: media type can never change once an
        entity exists (docs/multisync.md), so a cross-type match here
        is not an ordinary ambiguity to weigh -- it is proof some id
        was wrong (a corrupt/stale published id, or an atlas rule that
        doesn't apply). It is surfaced, loudly, rather than silently
        dropped, since that's exactly the kind of bad data a user
        needs to notice."""
        store = self._store
        targets = {}   # uuid -> via label naming the exact id that matched
        mismatched = []
        for other_provider, (other_id, source) in \
                self._external_ids(entry).items():
            m = store.mapping_for(other_provider, other_id)
            if not m:
                continue
            ent = store.get_entity(m['uuid']) or {}
            source_desc = 'published' if source == 'published' else source
            if ent.get('media_type') != entry.media_type:
                mismatched.append((m['uuid'], source_desc, other_provider,
                                   other_id, ent.get('media_type')))
                continue
            label = '%s %s id %s' % (source_desc, other_provider, other_id)
            targets.setdefault(m['uuid'], label)
        if mismatched or len(targets) > 1:
            candidates = [self._candidate_of(u, via='external-id-clash')
                         for u in sorted(targets)]
            candidates += [
                self._candidate_of(
                    uid, via=('⚠ TYPE MISMATCH -- %s %s id %s points '
                             'to a %s entity, but this %s entry is %s. '
                             'This should never happen; the id is '
                             'probably wrong. Do not confirm this -- '
                             'search manually or keep provider-only.'
                             % (src, prov, pid, (mt or '?').capitalize(),
                                entry.provider, entry.media_type)))
                for uid, src, prov, pid, mt in mismatched]
            store.identity_upsert(entry.provider, entry.provider_id,
                                  entry.title, candidates,
                                  status=self._reopen_status(
                                      entry.provider, entry.provider_id),
                                  entry=self._entry_payload(entry))
            return 'ambiguous'
        if targets:
            uid, via_label = next(iter(targets.items()))
            existing = {m['provider'] for m in store.mappings_of(uid)}
            if entry.provider in existing:
                # Entity already has a different entry of this provider:
                # same-provider duplicate is ambiguity, not a merge.
                candidates = [self._candidate_of(u := uid,
                                                 via='provider-duplicate')]
                store.identity_upsert(entry.provider, entry.provider_id,
                                      entry.title, candidates,
                                      status=self._reopen_status(
                                          entry.provider,
                                          entry.provider_id),
                                      entry=self._entry_payload(entry))
                return 'ambiguous'
            store.add_mapping(uid, entry.provider, entry.provider_id,
                              confirmed=True, via=via_label)
            store.entity_add_aliases(uid, [entry.title] + entry.aliases)
            self._record_external_ids(uid, entry)
            return uid
        return None

    @staticmethod
    def _titles_match(a, b):
        """Equal after normalization, or one is a word-boundary prefix
        of the other -- catches 'Ghost in the Shell (2026)' against
        'Ghost in the Shell'. Deliberately loose-ish: matches only feed
        the candidate list the user confirms, never automatic merges."""
        if not a or not b:
            return False
        return a == b or a.startswith(b + ' ') or b.startswith(a + ' ')

    def _entity_names(self, ent, aliases):
        sig = (ent.get('title'), len(aliases))
        cached = self._names_cache.get(ent['uuid'])
        if cached is None or cached[0] != sig:
            names = {normalize_title(ent.get('title'))} | {
                normalize_title(a) for a in aliases}
            names.discard('')
            cached = (sig, names)
            self._names_cache[ent['uuid']] = cached
        return cached[1]

    def _candidates(self, entry):
        entry_names = {normalize_title(entry.title)} | {
            normalize_title(a) for a in entry.aliases}
        entry_names.discard('')
        if not entry_names:
            return []
        found = []
        for ent, aliases in self._store.entities_with_aliases():
            if ent['media_type'] != entry.media_type:
                continue
            ent_names = self._entity_names(ent, aliases)
            exact = bool(entry_names & ent_names)
            if not exact and not any(self._titles_match(a, b)
                                     for a in ent_names
                                     for b in entry_names):
                continue
            year_ok = not (entry.year and ent['year']
                           and entry.year != ent['year'])
            via = 'title (exact)' if exact else 'title (similar)'
            if not year_ok:
                via += '; year differs: %s vs %s' % (ent['year'],
                                                     entry.year)
            cand = self._candidate_of(ent['uuid'], via=via)
            cand['exact'] = exact
            cand['year_ok'] = year_ok
            found.append(cand)
        return found

    @staticmethod
    def _entry_payload(entry):
        """What the UI needs to render an unresolved entry usefully."""
        return {'title': entry.title, 'aliases': entry.aliases,
                'year': entry.year, 'media_type': entry.media_type,
                'total': entry.total}

    def _candidate_of(self, uid, via):
        ent = self._store.get_entity(uid) or {}
        return {'uuid': uid,
                'title': ent.get('title'),
                'aliases': self._store.entity_aliases(uid),
                'year': ent.get('year'),
                'providers': {m['provider']: m['provider_id']
                              for m in self._store.mappings_of(uid)},
                'via': via}

    def _create_entity(self, entry, provider_only=None):
        uid = self._store.create_entity(
            entry.title, media_type=entry.media_type, year=entry.year,
            total=entry.total, status=entry.airing_status,
            provider_only=provider_only)
        via = ('kept %s-only by user choice' % provider_only
               if provider_only else
               'first seen on %s (no match found)' % entry.provider)
        self._store.add_mapping(uid, entry.provider, entry.provider_id,
                                confirmed=True, via=via)
        self._store.entity_add_aliases(uid, [entry.title] + entry.aliases)
        if provider_only is None:
            # A pinned ("keep provider-only") entity must not record the
            # provider-published external ids: they would auto-link other
            # providers to an entity the user explicitly excluded from
            # cross-provider sync.
            self._record_external_ids(uid, entry)
        return uid

    def _record_external_ids(self, uid, entry):
        """Provider-published (or atlas) ids become mappings themselves
        (exact), unless that slot is taken -- a clash surfaces as
        ambiguity on the other provider's own fetch.

        Cross-ids CHAIN. The atlas is keyed by (provider, id), so
        asking it only about the fetched entry's own id answers only
        the questions that entry can pose. AniList publishing a MAL id
        would plant a MAL mapping and stop -- even when the atlas knows
        that same MAL id's Kitsu counterpart, and even though a MAL
        mapping now exists to ask about. The work ended up linked on
        two sites and unmapped on the third, so Mirror could not offer
        to add it there: an entry "atlased on all three sites" that
        only ever showed two.

        So each id this plants is fed back through the atlas, until
        nothing new appears. Bounded by the provider count, and every
        link is still exact -- a chained id is an atlas triple entry,
        not a title guess.
        """
        pending = dict(self._external_ids(entry))
        seen = {entry.provider}
        while pending:
            other_provider, (other_id, source) = pending.popitem()
            if other_provider in seen:
                continue
            seen.add(other_provider)
            if not self._store.mapping_for(other_provider, other_id) \
                    and not any(m['provider'] == other_provider
                                for m in self._store.mappings_of(uid)):
                via = ('published by %s' % entry.provider
                       if source == 'published'
                       else '%s atlas (seen via %s)' % (source,
                                                        entry.provider))
                self._store.add_mapping(uid, other_provider, other_id,
                                        confirmed=True, via=via)
            # Now ask the atlas what THIS id knows, which is how a
            # third provider is reached at all.
            if self._atlas is None or entry.media_type != 'anime':
                continue
            sources = self._atlas.lookup_sources(other_provider, other_id)
            for chained, pid in self._atlas.lookup(other_provider,
                                                   other_id).items():
                if chained not in seen:
                    pending.setdefault(
                        chained, (pid, sources.get(chained, 'atlas')))

    # -- the user's four-option workflow ------------------------------

    def resolve_conflict(self, conflict_id, action, entry=None,
                         target_uuid=None):
        """action: 'confirm' (target_uuid) | 'provider_only' |
        'defer' | 'ignore'. `entry` (NormalizedEntry) is required for
        'confirm'/'provider_only' so the mapping/entity can be created.
        """
        store = self._store
        row = self._conflict_by_id(conflict_id)
        if row is None:
            raise ValueError('unknown identity conflict: %s' % conflict_id)

        if action == 'confirm':
            if not target_uuid:
                raise ValueError("'confirm' needs target_uuid")
            store.add_mapping(target_uuid, row['provider'],
                              row['provider_id'], confirmed=True,
                              via='confirmed by user')
            info = row.get('entry') or {}
            titles = [row.get('title'), info.get('title'),
                      *(info.get('aliases') or [])]
            store.entity_add_aliases(target_uuid,
                                     [t for t in titles if t])
            if entry is not None:
                store.entity_add_aliases(target_uuid,
                                         [entry.title] + entry.aliases)
            store.identity_set_status(conflict_id, 'resolved')
            return target_uuid
        if action == 'provider_only':
            if entry is None:
                raise ValueError("'provider_only' needs the entry")
            uid = self._create_entity(entry, provider_only=row['provider'])
            store.identity_set_status(conflict_id, 'provider_only')
            return uid
        if action == 'defer':
            store.identity_set_status(conflict_id, 'deferred')
            return None
        if action == 'ignore':
            store.identity_set_status(conflict_id, 'ignored')
            return None
        raise ValueError('unknown action: %s' % action)

    def _conflict_by_id(self, conflict_id):
        """The conflict row, but only if it's still actionable (open or
        deferred) -- resolved/ignored/provider_only rows are treated as
        gone, matching identity_open()'s filter without re-scanning
        every open conflict for one id (the row's PK)."""
        row = self._store.identity_get_by_id(conflict_id)
        if row is None or row['status'] not in ('open', 'deferred'):
            return None
        return row
