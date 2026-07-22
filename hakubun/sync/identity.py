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

import json

from hakubun.sync.normalize import normalize_title


class IdentityResolver:
    def __init__(self, store):
        self._store = store

    # -- resolution ----------------------------------------------------

    def resolve_entry(self, entry):
        """Resolve one NormalizedEntry to a uuid, or None if it needs
        the user (identity conflict recorded/kept open)."""
        store = self._store

        # 1. Already mapped.
        mapping = store.mapping_for(entry.provider, entry.provider_id)
        if mapping:
            return mapping['uuid']

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
        candidates = self._candidates(entry)
        exact = [c for c in candidates
                 if c['exact'] and c['year_ok']
                 and entry.provider not in c['providers']]
        if len(candidates) == 1 and len(exact) == 1:
            uid = exact[0]['uuid']
            store.add_mapping(uid, entry.provider, entry.provider_id,
                              confirmed=False)   # auto, not user-confirmed
            store.entity_add_aliases(uid, [entry.title] + entry.aliases)
            if previous:
                store.identity_set_status(previous['id'], 'resolved')
            return uid
        if candidates:
            status = previous['status'] if previous else 'open'
            store.identity_upsert(entry.provider, entry.provider_id,
                                  entry.title, candidates, status=status,
                                  entry=self._entry_payload(entry))
            return None
        return self._create_entity(entry)

    def _exact_link(self, entry):
        """Link via ids the provider itself publishes. A claimed id
        already mapped to a *different* entity than another claim is an
        ambiguity -> identity conflict, never an auto-merge."""
        store = self._store
        targets = set()
        for other_provider, other_id in entry.external_ids.items():
            m = store.mapping_for(other_provider, other_id)
            if m:
                targets.add(m['uuid'])
        if len(targets) > 1:
            candidates = [self._candidate_of(u, via='external-id-clash')
                          for u in sorted(targets)]
            store.identity_upsert(entry.provider, entry.provider_id,
                                  entry.title, candidates)
            return 'ambiguous'
        if targets:
            uid = targets.pop()
            existing = {m['provider'] for m in store.mappings_of(uid)}
            if entry.provider in existing:
                # Entity already has a different entry of this provider:
                # same-provider duplicate is ambiguity, not a merge.
                candidates = [self._candidate_of(u := uid,
                                                 via='provider-duplicate')]
                store.identity_upsert(entry.provider, entry.provider_id,
                                      entry.title, candidates)
                return 'ambiguous'
            store.add_mapping(uid, entry.provider, entry.provider_id,
                              confirmed=True)
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

    def _candidates(self, entry):
        entry_names = {normalize_title(entry.title)} | {
            normalize_title(a) for a in entry.aliases}
        entry_names.discard('')
        if not entry_names:
            return []
        found = []
        for ent in self._store.entities():
            if ent['media_type'] != entry.media_type:
                continue
            aliases = json.loads(ent['aliases']) if ent.get('aliases') \
                else []
            ent_names = {normalize_title(ent['title'])} | {
                normalize_title(a) for a in aliases}
            ent_names.discard('')
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
        self._store.add_mapping(uid, entry.provider, entry.provider_id,
                                confirmed=True)
        self._store.entity_add_aliases(uid, [entry.title] + entry.aliases)
        if provider_only is None:
            # A pinned ("keep provider-only") entity must not record the
            # provider-published external ids: they would auto-link other
            # providers to an entity the user explicitly excluded from
            # cross-provider sync.
            self._record_external_ids(uid, entry)
        return uid

    def _record_external_ids(self, uid, entry):
        """Provider-published ids become mappings themselves (exact),
        unless that slot is taken -- a clash surfaces as ambiguity on
        the other provider's own fetch."""
        for other_provider, other_id in entry.external_ids.items():
            if self._store.mapping_for(other_provider, other_id):
                continue
            if any(m['provider'] == other_provider
                   for m in self._store.mappings_of(uid)):
                continue
            self._store.add_mapping(uid, other_provider, other_id,
                                    confirmed=True)

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
                              row['provider_id'], confirmed=True)
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
        for row in self._store.identity_open():
            if row['id'] == conflict_id:
                return row
        return None
