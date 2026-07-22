# Multisync — local-first, reconciliation-based multi-provider sync

Target: Hakubun+ 0.13. Status: design + core implementation (this document
is the authoritative spec; update it when the implementation moves).

## 1. Why the existing sync isn't sufficient (inspection results)

Hakubun (inherited from Trackma) is **provider-centric**:

- One *account* = one provider (`accounts.py`, pickled registry). Each
  account gets its own isolated pickle DB (`data.py`: showlist cache,
  info cache, queue) keyed by `<username>.<api>.<mediatype>`.
- The show "identity" is the **provider's own id** (`show['id']`). The
  queue system syncs one account against one provider; there is no
  cross-provider notion at all.
- `mal_id` exists only as an opportunistic extra (AniList `idMal`,
  Kitsu-GraphQL mappings) used for display features, not identity.
- Provider APIs are wrapped by `lib/lib.py` subclasses with a uniform
  surface: `check_credentials, fetch_list, add_show(item),
  update_show(item), delete_show(item), search, request_info(items),
  media_info()`. These are good enough to build on — **we do not rewrite
  providers**; we adapt them.

Storage limitations found:

- Pickle blobs, whole-file rewrite per save, no queryability, no
  transactionality, no history. Fine as a per-account list cache;
  unusable as an event log or identity map.
- Show dicts are shared mutable objects across threads (engine/UI);
  anything long-lived and queried (events, mappings) must not live in
  them.

Decision: the sync subsystem gets its own **SQLite database**
(`utils.to_data_path('multisync.db')`, stdlib `sqlite3`, WAL mode). It
is global (cross-account), separate from the per-account pickles, and
does not disturb any existing storage.

## 2. Mental model: it's git

| Git            | Multisync equivalent                                  |
| -------------- | ----------------------------------------------------- |
| Repository     | Local media database (`entities` + `local_state`)     |
| Commit history | Event log (`events`, grouped by transaction)          |
| Remote         | MAL / AniList / Kitsu (a configured account)          |
| Fetch          | Download provider state into `remote_state` snapshots |
| Merge base     | `base_state` — state as of the last successful sync   |
| Diff           | 3-way diff: base vs local vs each remote              |
| Merge          | Reconcile per field-ownership policy                  |
| Conflict       | Same field changed in two places, no owner decides    |
| Commit         | Apply: write resolved canonical state + events        |
| Push           | Upload resolved values via provider adapters          |

Local state is canonical ("local-first"): providers are remotes that we
fetch from and push to. Every apply is a transaction; the event log is
the commit history that makes undo, stats and conflict detection
possible.

## 3. Identity

Internal identity is a UUID. Provider IDs are **mappings**, never the
canonical identity:

```
internal UUID
    |
    +-- MAL ID
    +-- AniList ID
    +-- Kitsu ID
    +-- (other providers)
```

Resolution pipeline (per fetched provider entry):

1. **Exact mapping** — provider id already mapped → done.
2. **Exact external-id link** — the entry carries another provider's id
   (AniList/Kitsu-GraphQL expose `mal_id`) that is already mapped, or
   two fetched entries share the same `mal_id` → link automatically.
   This is the only automatic merge; it is exact, not heuristic.
3. **Candidate scoring** — normalized-title (+type, +year) match against
   existing entities produces *candidates*. Ambiguous or fuzzy results
   are **never auto-merged**: they create an *identity conflict* for the
   user, with the candidates attached.
4. Unmatched, no candidates → new entity with a single mapping.

Identity conflict workflow (per unresolved entry — UI mock is the spec):

```
Kitsu: Ghost in the Shell (2026)
Possible matches: MAL ID 12345, AniList ID 67890

( ) Keep Kitsu-only        — do not sync this entry elsewhere
( ) Search manually        — find the MAL/AniList entry by search
( ) Create mappings later  — keep watching for new matches (defer)
( ) Ignore this title      — never ask again
```

Confirming a candidate stores the mapping permanently
(`mappings.confirmed = 1`). "Keep provider-only" pins the entity to that
provider (excluded from cross-provider push/pull). "Ignore" persists as
status `ignored` and is never surfaced again. "Later" stays `open` and
is re-evaluated on every fetch as new metadata arrives (airing shows
with incomplete metadata resolve themselves over time this way).

Handled cases (tested): MAL id on AniList but not Kitsu; differing
titles between providers; airing entries with incomplete metadata;
unknown episode counts; providers representing the same media
differently (movie vs special, season splits) — the last is exactly why
fuzzy matches are conflicts, not merges.

## 4. Field ownership ("Where should hakubun sync to?")

Not a Settings-page option — it lives in the Sync window itself, as a
radio matrix:

```
                    LOCAL   MAL   ANILIST   KITSU   MERGE   INDIV   ASK
-----------------------------------------------------------------------
Score                  ○      ○      ●         ○
Watched Episodes       ○      ○      ○         ○                     ●
Status                 ●      ○      ○         ○
Notes                  ○      ○      ○         ○              ●
Start Date             ●      ○      ○         ○
Finish Date            ●      ○      ○         ○
Tags                   ○      ○      ○         ○       ●
Favorites              ○      ○      ○         ○       ●
```

Policies per field (superset of the mock, covering the examples given):

- `local` — local value is authoritative; pushed outward.
- `provider:<name>` — that provider owns the field (e.g. Score →
  AniList; values pushed to MAL/Kitsu are converted to their scales and
  **rounded**).
- `merge` — union/最新-wins reconciliation for set-like or independent
  fields (tags, favorites): union of sets; for scalars, newest change
  wins, ties → conflict.
- `individual` — per-provider, not synchronized at all (e.g. notes kept
  different on each site). Never diffs, never conflicts.
- `ask` — "reconciled by user": any divergence becomes a conflict in the
  preview (e.g. watched episodes when the user wants manual control).

No provider always wins: ownership is per-field, user-editable, stored
in the `ownership` table.

Score scales: canonical score is a 0–10 float. Adapter conversion:
MAL 0–10 int (round), AniList 0–100 (POINT_100; ×10), Kitsu 0–20
(×2, round). Conversions are lossy by design; the canonical value is
what the owner said, converted copies are best-effort projections.

## 5. Storage schema (SQLite, `multisync.db`)

```sql
entities(uuid TEXT PRIMARY KEY, media_type TEXT NOT NULL DEFAULT 'anime',
         title TEXT, year INTEGER, total INTEGER,          -- best-known metadata
         status TEXT,                                       -- airing status
         provider_only TEXT,                                -- non-NULL: pinned to that provider
         created_at REAL NOT NULL)

mappings(uuid TEXT NOT NULL REFERENCES entities(uuid),
         provider TEXT NOT NULL, provider_id TEXT NOT NULL,
         confirmed INTEGER NOT NULL DEFAULT 0,              -- user-confirmed or exact link
         created_at REAL NOT NULL,
         UNIQUE(provider, provider_id), UNIQUE(uuid, provider))

local_state(uuid TEXT NOT NULL, field TEXT NOT NULL,
            value TEXT,                                     -- JSON
            updated_at REAL NOT NULL,
            PRIMARY KEY(uuid, field))

remote_state(provider TEXT NOT NULL, provider_id TEXT NOT NULL,
             field TEXT NOT NULL, value TEXT, fetched_at REAL NOT NULL,
             PRIMARY KEY(provider, provider_id, field))     -- remote-tracking

base_state(uuid TEXT NOT NULL, provider TEXT NOT NULL, field TEXT NOT NULL,
           value TEXT, synced_at REAL NOT NULL,
           PRIMARY KEY(uuid, provider, field))              -- merge base per provider

events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
       txn TEXT NOT NULL,                                   -- groups an apply/undo
       uuid TEXT NOT NULL, field TEXT NOT NULL,
       old_value TEXT, new_value TEXT,                      -- JSON
       source TEXT NOT NULL,                                -- 'local' | 'mal' | 'anilist' | ... | 'undo'
       op TEXT NOT NULL DEFAULT 'set')                      -- 'set' | 'undo' | 'push'

identity_conflicts(id INTEGER PRIMARY KEY AUTOINCREMENT,
                   provider TEXT NOT NULL, provider_id TEXT NOT NULL,
                   title TEXT, candidates TEXT,             -- JSON list
                   status TEXT NOT NULL DEFAULT 'open',     -- open|resolved|ignored|provider_only|deferred
                   created_at REAL NOT NULL,
                   UNIQUE(provider, provider_id))

ownership(field TEXT PRIMARY KEY, policy TEXT NOT NULL)     -- see §4

meta(key TEXT PRIMARY KEY, value TEXT)
```

Everything valued is JSON-encoded so the schema never chases field
types. The event log is append-only; undo appends inverse events (like
`git revert`, history is never rewritten).

## 6. Pipeline

```
Provider APIs
    ↓  (existing lib/ classes, untouched)
Provider adapters      sync/adapters.py   — uniform fetch()/push(); scale conversion
    ↓
Normalize              sync/normalize.py  — provider entry → NormalizedEntry (canonical fields)
    ↓
Identity resolution    sync/identity.py   — §3; writes mappings / identity_conflicts
    ↓
Diff engine            sync/diff.py       — 3-way per entity×provider×field
    ↓
Conflict resolution    sync/conflicts.py  — ownership policies → decisions or conflicts
    ↓
Sync preview           SyncPlan           — changes + conflicts + identity issues
    ↓
Apply                  sync/engine.py     — transaction: events + local_state + base + pushes
    ↓
Record history         sync/history.py    — event log, undo, stats
```

### Sync modes

- **Mirror** — local pushes outward: plan takes local values for every
  divergent field (remote changes are overwritten on push).
- **Pull** — providers update local: remote values (per ownership when
  multiple providers disagree; owner wins, else conflict) overwrite
  local; nothing is pushed.
- **Merge** — full 3-way reconciliation into local state per §4, then
  push resolved values outward. Default.

### Diff semantics (per entity, per synced field, per mapped provider)

```
b = base_state[uuid, provider, field]    (merge base)
l = local_state[uuid, field]
r = remote_state[provider_id, field]

l==b and r==b  → in sync, nothing
l==b and r!=b  → remote change → pull into local (unless mirror)
l!=b and r==b  → local change  → push to provider (unless pull)
l!=b and r!=b  → both changed:
                   owner decides (local/provider policy) or
                   merge policy (union / newest) or
                   ask → CONFLICT for the user
```

Unknown episode counts: `total` is metadata, not user state — progress
is never clamped against an unknown total, and a provider reporting
`total=None` for an airing show never produces a metadata conflict
(missing values never "win" over known values, and never conflict).

### Apply, failure, rollback

Apply runs as one transaction id (`txn`, a UUID):

1. Write local_state changes + events (source = who caused the value).
2. Advance base_state for every (entity, provider, field) applied.
3. Push provider changes via adapters. **Per-provider failure
   isolation**: one provider erroring skips only its pushes; its
   base_state is *not* advanced (so the same changes re-plan next sync),
   other providers proceed. The plan result records per-provider
   success/failure.
4. `rollback(txn)` (user-facing undo): appends inverse events, restores
   local_state, rewinds base_state for that txn — and re-plans so
   already-pushed values show up as pending pushes of the restored
   value (a push cannot be "unsent", it is compensated).

## 7. File layout

```
hakubun/sync/__init__.py     public surface (SyncEngine, SyncPlan, …)
hakubun/sync/models.py       dataclasses/enums: Entity, Mapping, NormalizedEntry,
                             FieldPolicy, FieldChange, Conflict, SyncMode, SyncPlan
hakubun/sync/store.py        SQLite store (schema above, transactions)
hakubun/sync/history.py      event log: record, undo, stats queries
hakubun/sync/normalize.py    provider dict → NormalizedEntry; score scales
hakubun/sync/adapters.py     ProviderAdapter (wraps a lib instance), builds from accounts
hakubun/sync/identity.py     IdentityResolver (§3)
hakubun/sync/diff.py         three-way differ
hakubun/sync/conflicts.py    ownership policies, resolution
hakubun/sync/engine.py       SyncEngine: fetch/plan/apply/undo, modes
tests/sync/…                 required test matrix (fake providers, tmp DB)
hakubun/ui/qt/syncwindow.py  Sync window: preview, ownership matrix,
                             identity-conflict resolution (§8)
```

## 8. UI (Qt first; GTK follows the same model)

One **Sync window** (menu: Tools → “Multi-provider Sync…”), three
sections:

1. **Preview** —
   ```
   Changes:
   ✓ MAL  episode 14 → 15
   ✓ AniList  status Watching → Completed
   Conflicts:
   ! Rating differs        (Local 8.0 / AniList 8.5)
   ! Kitsu identity unresolved
   Actions:  [Apply]  [Skip]  [Resolve]
   ```
   Changes are checkable (Skip = uncheck). Apply runs the plan;
   Resolve opens the picker for the selected conflict.
2. **Ownership** — “Where should hakubun sync to?” — the radio matrix of
   §4. Lives here, not in Settings.
3. **Identity** — open identity conflicts with the 4-option workflow of
   §3 (candidate list, manual search via the provider adapter's search).

## 9. Implementation plan (incremental, tests with every step)

1. Scaffold: this document, `hakubun/sync/` package, version → 0.13.dev0.
2. `models` + `store` + `history` — schema, transactions, event log, undo. *Tests: store roundtrip, event grouping, undo.*
3. `normalize` + `adapters` (+ `FakeProvider` for tests) — score scales, canonical fields. *Tests: scale conversion/rounding, missing fields.*
4. `identity` — exact links, candidates, conflict workflow, permanence. *Tests: matching, missing external IDs, ambiguity not auto-merged.*
5. `diff` + `conflicts` — 3-way + policies. *Tests: conflicting ratings, notes-individual, unknown episode counts, simultaneous edits.*
6. `engine` — fetch/plan/apply/undo, modes, failure isolation. *Tests: provider API failure, rollback, all three modes.*
7. Qt Sync window + mainwindow menu wiring.
8. CHANGELOG; GTK window tracked as follow-up work.

Required test matrix (from the spec) → `tests/sync/`:
provider identity matching · missing external IDs · conflicting ratings
· conflicting notes · unknown episode counts · simultaneous edits ·
provider API failure · rollback.
