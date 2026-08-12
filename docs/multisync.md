# Multisync — local-first, field-strategy multi-provider sync

Status: authoritative spec for the field-strategy architecture (the
rework that replaced the git-like three-way merge engine). Update this
document when the implementation moves.

The one-sentence model:

> **Identity determines what entries represent the same work. Field
> policies determine where each field gets its authoritative value.
> Strategies determine how fields without a single authority behave.
> Membership determines which trackers should hold an entry at all.
> History records what happened.**

Those are five separate responsibilities, and the architecture's core
rule is that they never collapse into one generic merge resolver.

Two things follow that are easy to lose sight of:

- **Hakubun's local state is not a tracker.** It is the app's working,
  reconciled copy — reconciliation state. It never owns a field and,
  in tracker-to-tracker reconciliation (§15), it is not one of the
  sides being compared.
- **Ownership decides values; membership decides existence.** No
  ownership policy can say whether a list entry belongs on a tracker,
  and no observation can say the user wants one deleted.

## 1. Why the provider-centric sync isn't sufficient

Hakubun (inherited from Trackma) is **provider-centric**:

- One *account* = one provider (`accounts.py`, pickled registry). Each
  account gets its own isolated pickle DB (`data.py`) keyed by
  `<username>.<api>.<mediatype>`.
- The show "identity" is the **provider's own id** (`show['id']`). The
  queue system syncs one account against one provider; there is no
  cross-provider notion at all.
- Provider APIs are wrapped by `lib/lib.py` subclasses with a uniform
  surface (`fetch_list`, `update_show(item)`, `add_show(item)`, …).
  These are good enough to build on — **we do not rewrite providers**;
  we adapt them (`sync/adapters.py`).

The sync subsystem therefore keeps its own **SQLite database** (one per
media type, `multisync-<mediatype>.db`, WAL mode), global across
accounts and separate from the per-account pickles.

## 2. The layers

```
identity          "what is this?"        (provider, id) -> entity UUID
policy            "what should this      provider-owned / individual /
                   field be?"            reconcile
strategy          "how do we reconcile   manual / union / max / min /
                   without an owner?"    progress
membership        "should this entry     present / absent / ignore,
                   exist on this          per (entity, provider)
                   tracker at all?"
history           "what happened?"       append-only event log; undo
```

The old architecture answered all of these with one mechanism —
a git-style three-way merge (`merge_base` vs `local` vs `remote`, a
divergence classifier, and mode switches). That resolver, and its
concepts (`NO_BASE`, `IN_SYNC`, `PULL`, `PUSH`, `BOTH`, `three_way()`,
`resolve()`, sync modes), are gone. Nothing replaced them with another
generic "local vs remote" merge: what the planner does for a field
follows from that field's policy alone.

```
identity
    ↓
internal entity UUID
    ↓
field policy
    ↓
authoritative value / reconciliation
    ↓
sync plan (explicit SyncOperations, each carrying its reason)
```

## 3. Identity (unchanged subsystem)

Internal identity is a UUID. Provider IDs are **mappings**, never the
canonical identity:

```
internal UUID
    |
    +-- MAL ID
    +-- AniList ID
    +-- Kitsu ID
```

Resolution pipeline (`sync/identity.py`, per fetched provider entry):

1. **Exact mapping** — provider id already mapped → done.
2. **Exact external-id link** — the entry carries another provider's id
   (AniList/Kitsu-GraphQL expose `mal_id`; legacy Kitsu's library fetch
   includes its `mappings` relationship) that is already mapped, or two
   fetched entries share the same `mal_id` → link automatically
   (`confirmed = 1`). The community **anime-relations** database is
   harvested as an id atlas (every rule carries MAL|Kitsu|AniList
   triples) and its links are trusted the same way. Annict ids come
   from a second database, **arm**, since anime-relations has no
   Annict column — and Annict cannot be linked any other way, because
   its titles are Japanese-only (step 3 below is dead for it) and it
   publishes no AniList id. arm is fetched at runtime and cached, not
   bundled; `arm_time: 0` in `config.json` disables it. The two
   sources are additive: an arm row linking Annict to a MAL id joins
   up with an anime-relations rule for that MAL id, so Annict reaches
   Kitsu through a provider neither database named directly. The atlas
   is an identity aid only — it never participates in field
   synchronization.
3. **Single exact title match** — exactly one candidate whose
   normalized title or alias is equal (same media type, compatible
   year) → auto-link (`confirmed = 0`).
4. **Candidate scoring** — anything short of that (multiple candidates,
   prefix-only similarity, year mismatch, same-provider duplicate) is
   **never auto-merged**: it creates an *identity conflict* for the
   user (`confirm` / `provider_only` / `defer` / `ignore`).
5. Unmatched, no candidates → new entity with a single mapping.

Tiers 1–5 are purely local. `SyncEngine._discover_cross_ids` runs after
every fetch as a separate, deliberately network-calling step: providers
exposing an exact reverse lookup (AniList's `Media(idMal:...)`) are
asked about entities whose MAL id is known but unmapped there. A hit
records an id-only mapping with an **empty remote snapshot** — field
planning ignores it (only providers with some remote row contribute
values); only the planner's create-entry offer acts on it. A miss is
remembered (`resolved_absent`).

Titles are matched after NFKC + casefold normalization that preserves
every script, and entities accumulate aliases from every provider so a
native-title entity still matches a romaji entry.

**Field synchronization logic is never mixed into identity resolution,
and identity explanations stay separate from synchronization
explanations in the Inspector.**

## 4. The internal entity is the synchronization unit

After identity resolution:

```
AniList entry  ─┐
Kitsu entry    ─┼──→ entity UUID
MAL entry      ─┘
```

All synchronization operates on the entity UUID; providers are merely
representations of that entity. The engine never merges provider
entries into another provider-shaped object — it builds a field-level
plan.

`local_state` is the application's **working representation** — the
reconciled values the list overlay displays — not an independent
provider and not a competing permanent authority. For an owned field it
converges to the owner; for a reconcile field it participates like any
other side and receives the resolved value.

## 5. Field policies ("Where should hakubun sync to?")

A field policy answers: *where does the authoritative value for this
field come from?* Three kinds (`models.PolicyKind`):

- `provider:<name>` — that provider is **authoritative** for the field.
  Not merely "who wins a conflict": its current value is the field's
  value, everywhere, every plan.
- `individual` — the field does not synchronize. The engine does not
  compare, merge, push, or conflict it; provider-local values stay
  independent (e.g. notes kept different on each site).
- `reconcile:<strategy>` — no single owner; a reconciliation strategy
  decides (§6).

Stored per field in the `ownership` table; edited from the Sync
window's ownership matrix. Legacy spellings from the retired engine
parse onto the nearest equivalent (`local`/`ask` → `reconcile:manual`,
`merge` → `reconcile:union`), so existing databases keep working.

Defaults (chosen so a first sync mostly resolves itself):

```
score        → reconcile:manual
progress     → reconcile:progress
rewatches    → reconcile:max
status       → reconcile:manual
notes        → individual
start_date   → reconcile:manual
finish_date  → reconcile:manual
tags         → reconcile:union
favorite     → reconcile:union
```

An example personal configuration:

```
score       → provider:anilist
progress    → provider:kitsu
status      → provider:kitsu
rewatches   → reconcile:manual
notes       → individual
start_date  → provider:kitsu
finish_date → provider:kitsu
```

Score scales: the canonical score is a 0–10 float. Adapters convert to
each provider's scale on push (MAL 0–10 int, AniList any of its five
formats, Kitsu half-stars), rounding **half up**; the Preview names
every rounded push explicitly ("8.5 rounded up to 9").

## 6. Reconciliation strategies

A strategy (`sync/strategies.py`) receives every participant's current
value — `local` plus one entry per provider that can represent the
field — and returns exactly one of:

```python
Resolved(value, reason)   # adopt `value` everywhere
Conflict(reason)          # a human decides (FieldConflict)
NoChange(reason)          # nothing to do
```

Built-in strategies (`strategies.STRATEGIES`):

- **`manual`** (ManualReconcile) — a human arbitrates, *except* when
  base history proves only one side changed since the last sync: a
  single-sided edit is not a conflict and propagates cleanly. Sides
  that disagree with no history to attribute the difference (a first
  sync) are a genuine conflict — nothing silently overwrites. A side
  holding an **empty** value (None/0/'') **abstains** rather than
  votes — "MAL has no finish date" is missing data, not a competing
  opinion, so three sides agreeing on a date and one holding nothing
  resolve to the date (which also fills it in on the empty side) —
  *unless* history proves the value was deliberately cleared (it moved
  *to* empty since the last sync), in which case the clear votes like
  any other change and, if single-sided, propagates.
- **`union`** (SetUnion) — union of set-valued fields (tags); boolean
  OR for flags (favorite).
- **`max`** / **`min`** (Maximum / Minimum) — highest / lowest
  non-empty value wins (rewatch counts; earliest/latest dates — ISO
  date strings order chronologically).
- **`progress`** (CustomProgressReconcile) — progress-aware: a
  single-sided change propagates as-is, *including a deliberate
  regression* (a rewatch reset); when several sides moved or there is
  no history, the furthest progress wins (watching is monotone, so the
  highest value never claims episodes as unwatched).

Unknown strategy names degrade to `manual` — the safe strategy, since
it never writes on its own.

### Base state is data, not the algorithm

`base_state` — *what each provider's value was when synchronization
last established a common state* — survives from the old engine as
**historical information available to strategies**, not as a merge
algorithm:

```
base:    Kitsu = 2, AniList = 2
current: Kitsu = 3, AniList = 2
⇒ Kitsu changed; AniList did not
```

The planner precomputes this per participant
(`ReconcileContext.changed`: `True` / `False` / `None` for "no base —
cannot tell") and hands it to the strategy. Local has no base of its
own; whether it moved is judged against the providers' bases, and the
test depends on the local value's **provenance** (`local_state.source`):

- a provider-fed value (an echo) has *not* moved as long as it still
  equals *some* recorded base;
- a deliberate act — a direct edit (`local`) or a conflict resolution
  (`resolve`) — *has* moved as long as it differs from *any* base.

The distinction matters exactly at resolution time: adopting one
provider's value makes local equal that provider's base, and the
some-base-matches rule alone would misread the resolution as "local
never moved" and re-raise the very conflict the user just settled.

Bases advance when: a value is adopted into local from a provider
(apply), a push lands (to the value actually sent), a fetch observes
local and remote already agreeing (`_settle_base`), a direct local edit
snapshots every provider's current value (`set_local_field`), or the
user resolves a conflict (which snapshots **every mapped provider's**
current value — not just the sides listed in the conflict, since the
precision collapse can drop a redundant provider from the options).

## 7. The sync planner (`sync/planner.py`)

`SyncPlanner.plan()` walks every entity and, per synchronizable field,
dispatches on the policy — this loop **is** the whole decision
procedure:

```python
policy = ownership[field]

if policy.kind is INDIVIDUAL:
    continue
if policy.kind is PROVIDER:
    plan_authoritative_field(...)
elif policy.kind is RECONCILE:
    plan_reconciliation_field(...)
```

It returns a `SyncPlan` of explicit `SyncOperation`s:

```python
SyncOperation(
    uuid=uuid, provider='anilist', field='progress',
    old_value=10, new_value=12,
    reason='Kitsu owns progress',
)
```

Every operation (and every conflict) carries a **reason** — which
policy or strategy produced it. The Inspector and the sync windows
never reverse-engineer the engine's decisions; *the plan is the
explanation*.

### 7.1 Step by step: `plan()`

1. Collect open identity issues into the plan (informational).
2. Bulk-load every table the per-entity planner reads (mappings, local
   state + provenance, bases, remote snapshots, absent-marks) — per-uid
   queries here are an N+1 pattern the planner cannot afford.
3. Per entity: `_plan_entity` (cancellable between entities).

### 7.2 `_plan_entity`

1. Skip entities with no mapped, connected provider; respect
   `provider_only` pins.
2. Offer entry creation for mapped providers with an empty remote
   snapshot (§7.6).
3. Per field: skip `individual`; convert progress values into the local
   episode structure (§8) — incomparable partials freeze the field as a
   *structural conflict* for this plan; then dispatch to §7.3 or §7.4.
   Providers whose snapshot lacks the field cannot represent it and
   simply contribute nothing — an unsupported field is never treated
   as an empty value.

### 7.3 Provider-owned fields (`_plan_authoritative_field`)

The owner's current value is the field's value. The engine does not
need a divergence resolver to know this — it already knows who owns the
field:

- **Owner doesn't list the entry** (or can't represent the field):
  there is no authoritative value to assert; the planner emits nothing
  rather than inventing an authority.
- **Local equals the owner**: converge every other provider that
  differs (push, reason "`<Owner>` owns `<field>`").
- **Pending local edit**: the owner provably did *not* move since the
  last sync (its value still equals its base) while local did — the
  edit is routed **to the owner** first, and to everyone else
  (reason "local edit; `<Owner>` owns `<field>` and receives it").
  This is what makes "a local edit to an authoritative field is
  ultimately sent to the owning provider" true: after the push the
  owner holds the edit, and every other side converges to it.
- **Otherwise the owner wins**: pull its value into local, push to
  every other provider that differs. External changes made on the
  owner's website propagate this way with **no manual conflict** —
  the case that made the old generic three-way resolver unnecessary.
  When both the owner and local moved simultaneously, the owner still
  wins: ownership means authoritative source, not conflict priority.

Edits made on a **non-owner** provider's website are not authoritative
and converge back to the owner's value. To move an owned field, edit it
in the app (routed to the owner) or on the owner's site.

### 7.4 Reconciliation fields (`_plan_reconcile_field`)

1. **Precision collapse**: a provider whose score is fully explained —
   consistent, under *that provider's own* precision — with local's
   value or an at-least-as-precise survivor is dropped from the
   participants (MAL's integer 8 next to a finer 8.4 is rounding, not
   disagreement, and must never become a redundant conflict option).
   Local's own vote is likewise dropped when it is merely a coarse
   *echo* of a provider (its stored provenance names that provider, it
   still equals that provider's current value, and under that
   provider's precision it is consistent with a kept finer value).
2. Compute `changed` per participant from base state (§6).
3. Run the strategy over the participants' values.
4. `NoChange` → nothing (if local's echo was dropped and the providers
   agree on a finer value, local still converges to it).
   `Conflict` → a `FieldConflict` carrying the strategy's reason.
   `Resolved(v)` → a local operation if local differs, plus pushes to
   every provider that differs under its own representation
   (`ProviderAdapter.values_equivalent` — quantization residue never
   re-pushes). The operation's source credits the participant whose
   value won, so apply can advance that provider's base.

### 7.5 Conflict resolution (`engine.resolve_conflict`)

The user picks a side (or supplies an explicit value). Local state
takes the choice (`source='resolve'`); every listed provider's base
advances to its current value (the user has acknowledged it), so the
next plan sees a clean local-side resolution to push — the same
conflict never re-raises. Structural conflicts (§8) only accept
"local" or an explicit value in the local structure.

### 7.6 Entry existence and creation (`_plan_missing_entries`)

Whether an entry should **exist** on a provider is a separate question
from where its fields sync from — field ownership answers "where does
this *value* come from", never "should this *entry* be there". That
second question is its own model, **membership** (§14); the planner
only *reads* it, so ordinary Sync and Mirror can never drift into
disagreeing about whether an entry belongs somewhere.

For each provider `Membership.addable()` names, the planner offers a
creation whose operations carry `provenance` — the tuple of providers
that establish existence — and a reason in those terms ("exists on
AniList and Kitsu; Mal has no entry"). Nothing but an actual provider
entry can justify a creation: an entity *no* provider lists
(local-only) is never offered for creation at all, because Hakubun's
own state is not a provider.

`SyncPlanner.entry_values` decides what a new entry starts with
(provider-owned → the owner's value; reconcile → local's working value;
individual → nothing) and is shared with Mirror, so a created entry is
seeded identically either way. apply() batches an entity's creations
into a single `adapter.add` call. Planned **unselected**
(`SyncOperation.creates_entry`): adding a library entry to a real
account is opted into per show, never applied by a headless Sync.

**provider-only** (`entities.provider_only`) is a separate mechanism:
the user pinned the entity to one provider, isolating it from
cross-provider sync entirely (planning and membership see only that
provider), which also means no creations anywhere. It does **not**
change field policies or make that provider authoritative — it only
bounds *which providers participate*.

### 7.7 Equality

The old generic `eq()` merge primitive is gone. The planner compares
**normalized internal values** with field-aware equality
(`normalize.values_equal`): emptyish values (None/0/''/[]) are one
state, tags compare as sets, scores numerically, progress as integers,
dates and statuses as canonical strings. Provider-scale equivalence
(does 8.4 round to this provider's 8?) is the adapter's:
`ProviderAdapter.values_equivalent`. Provider adapters normalize
provider-specific representations *before* the planner sees them.

## 8. Episode-structure translation

The same work can be a 1-episode movie on one provider and a 4-episode
listing on another. Each provider's own total is snapshotted (`_total`)
and progress is planned in the **local** structure: completion is
equivalence (1/1 ≡ 4/4), completion converts on push/pull (each
provider receives its own total, never a raw copy), and *partial*
progress across differing structures is incomparable — it surfaces once
as a **structural** `FieldConflict` and is never guessed. Bases for
progress record each provider's RAW value and are viewed through the
same conversion as the remote (a base that no longer converts is
honestly no base at all).

## 9. Fetch, apply, failure, undo (`sync/engine.py`)

**Fetch** snapshots every provider's list into `remote_state`
(remote-tracking), resolves identity, seeds brand-new entities' local
state, and settles bases where local and remote already agree.
Failure isolation covers processing, not just the network call: each
provider ingests in one transaction, so a bug rolls that provider back
cleanly. Entries a fetch no longer returns are dropped from the
snapshot (with their bases, plus an absent-mark so creation is never
re-offered); an *empty* fetch is left alone — indistinguishable from an
API quietly failing. Fields a provider cannot represent are absent from
its snapshot, never fabricated as empty.

**Apply** commits the plan under one transaction id (`txn`):

1. Local operations + events in one transaction; adopting a provider's
   value advances that provider's base (raw value for converted
   progress).
2. Pushes per provider, batched per entity, paced and rate-limit
   retried. **Per-provider failure isolation**: one provider erroring
   skips only its remaining pushes and its bases never advance, so the
   same operations re-plan next run. A push the adapter could not
   actually deliver is never recorded as delivered (no base/remote
   advance) — a base claiming the remote holds a value it never
   received turns the provider's real value into a phantom edit later.
3. A provider with no remote row at all is **created** (`add_show`)
   instead of updated (§7.6); its new library-entry id is persisted.

**Direction of history**: planner → operations → apply → state changes
→ event recording. The engine never consults `History` to *decide*
synchronization — the event log is an audit trail (and the undo/stats
substrate), not an input.

**Undo** (`history.undo`) appends inverse events (never rewrites),
restores local values, and sets each pushed field's base to the value
the provider actually holds — so the next plan proposes the
compensating pushes of the restored value (a push cannot be "unsent";
it is compensated).

## 10. Storage schema (SQLite, `multisync-<mediatype>.db`)

```sql
entities(uuid TEXT PRIMARY KEY, media_type, title, year, total,
         status, provider_only, aliases, created_at)

mappings(uuid REFERENCES entities, provider, provider_id,
         confirmed,               -- user-confirmed or exact link
         via,                     -- how the link was made (Inspector)
         created_at,
         UNIQUE(provider, provider_id), UNIQUE(uuid, provider))

local_state(uuid, field, value /*JSON*/, updated_at,
            source,   -- provenance: 'local' | provider | 'resolve' ...
            PRIMARY KEY(uuid, field))

remote_state(provider, provider_id, field, value, fetched_at,
             PRIMARY KEY(provider, provider_id, field))
             -- fetched_at advances only when the VALUE changes

base_state(uuid, provider, field, value, synced_at,
           PRIMARY KEY(uuid, provider, field))
           -- per-provider value at the last established common state;
           -- historical data for strategies (§6), not an algorithm

events(id, ts, txn, uuid, field, old_value, new_value, source,
       op /* 'set' | 'undo' | 'push' | 'add' | 'remove' */)
       -- 'remove' (a Mirror deletion, field '_entry') is logged but
       -- deliberately not undoable: see §15.3

identity_conflicts(id, provider, provider_id, title, candidates,
                   status /* open|resolved|ignored|provider_only|
                             deferred|unlisted */, entry, created_at,
                   UNIQUE(provider, provider_id))

ownership(field PRIMARY KEY, policy)   -- §5 serialized policies

mirror_resolution(uuid, field, value, fingerprint, decided_at,
                  PRIMARY KEY(uuid, field))
                  -- a Mirror decision, valid while the tracker values
                  -- it was made against still hold (§15.3)

resolved_absent(uuid, provider, checked_at, reason /* 'lookup_miss' */,
                PRIMARY KEY(uuid, provider))
                -- the cross-id LOOKUP CACHE only (§14.1). The two
                -- reasons that were user decisions ('declined',
                -- 'deleted') migrate to membership as 'ignore'.

membership(uuid, provider,
           want /* 'present' | 'absent' | 'ignore' */,
           reason, decided_at,
           PRIMARY KEY(uuid, provider))
           -- does this entry belong on this tracker (§14)? 'absent'
           -- is the only state that can produce a DELETION, and is
           -- only ever written from an explicit user action.
```

Values are JSON-encoded. The event log is append-only.

## 11. File layout

```
hakubun/sync/__init__.py     public surface (SyncEngine, SyncPlan, …)
hakubun/sync/models.py       PolicyKind, FieldPolicy, SyncOperation,
                             FieldConflict, SyncPlan, NormalizedEntry
hakubun/sync/strategies.py   ReconcileStrategy interface + built-ins,
                             Resolved/Conflict/NoChange
hakubun/sync/planner.py      SyncPlanner: policies -> SyncOperations
hakubun/sync/membership.py   which trackers should hold an entry (§14)
hakubun/sync/mirror.py       MirrorPlanner: converge the trackers (§15)
hakubun/sync/engine.py       SyncEngine: fetch/apply/edits/undo
hakubun/sync/store.py        SQLite store (schema above, transactions)
hakubun/sync/history.py      event log: record, undo, stats
hakubun/sync/normalize.py    canonical values, field-aware equality,
                             episode-structure conversion
hakubun/sync/adapters.py     ProviderAdapter over the existing libs
hakubun/sync/identity.py     IdentityResolver (§3)
hakubun/sync/inspect.py      Inspector readout (identity + per-field
                             policy, two separate layers)
hakubun/sync/present.py      toolkit-agnostic plan wording
hakubun/sync/overlay.py      read-only list overlay from local_state
hakubun/sync/uibridge.py     UI glue (overlay build, owned-score edit)
tests/sync/…                 fake providers, tmp DB
hakubun/ui/qt/syncwindow.py  Qt Sync window (§12)
hakubun/ui/gtk/multisyncwindow.py  GTK twin
```

## 12. UI

One **Sync window** (Tools → "Multi-provider Sync…"). Both toolkits
share one information architecture and one vocabulary, organized
around the user's questions rather than the engine's internals. The
engine's `local` is always presented as **Hakubun** — the app's own
reconciled state — never as a provider or as the signed-in account
(which is shown separately, as an icon, and carries no sync
authority) — except in Mirror, where nothing is presented as
Hakubun at all, because that tab reconciles trackers against each
other and the app is not one of the parties. Five tabs:

1. **Sync** — a headline of the three numbers that matter ("3
   change(s) · 1 decision(s) needed · 2 new entries"), then the plan
   as checkable rows grouped by show, each spelling out its reason
   ("Update Mal, Watched Episodes: 10 → 12 — Kitsu owns progress").
   **New entries** ("Add to Mal…", `creates_entry`) sit in their own
   list beside Changes — creating a title on another site is a
   different act than updating a field — and stay unticked until the
   user opts in; each names its provenance ("exists on AniList and
   Kitsu"), and right-clicking a row declines the creation durably
   (`engine.decline_create`, never re-proposed) as opposed to merely
   unticking it for this plan. **Decisions** (conflicts) are a pane on the right:
   each card lists what Hakubun and each site hold, says in plain
   language why it couldn't be decided automatically, and offers one
   button per adoptable side. The buttons are *Fetch changes* and
   *Sync selected*; the engine keeps calling these plan and apply.
2. **Mirror** — *make your trackers agree, according to Ownership*
   (§15). Four categories — **Tracker membership** (the presence
   matrix per entry: `Anilist ✓ / Mal ✓ / Kitsu ✗`, with the
   discrepancy stated between trackers and a context menu recording
   the membership decision), **Entries to add**, **Entries to remove**
   and **Fields to update** — plus a Decisions pane for
   tracker-vs-tracker disagreements. Its own preview, deliberately not
   overloaded onto Sync's: it is a different and potentially much
   larger operation. Applying always goes through a confirmation
   quoting the per-tracker totals, and the two bulk gates ("Allow
   adding entries", "Allow removing entries") are independent, both
   default off, and are enforced by the engine regardless (§15.3).
3. **Configuration** — one rule picker per field, in consequences
   rather than policy syntax: "Keep the furthest progress", "Keep
   from Mal", "Ask me when they differ", "Don't sync". Serialized
   policies never surface here; `present.policy_choices` maps each
   field to its sensible options and appends the current policy when
   the advanced matrix set something the simple list doesn't offer.
4. **Identity** — "N title(s) need matching", with the resolution
   workflow of §3.
5. **Advanced** — the full policy matrix of §5 (kept in agreement
   with Configuration — both views write the same store rows), the
   identity **Inspector** (one entry by provider id: mappings, via,
   atlas opinion, and separately the per-field data with each field's
   governing policy), and the destructive **Reset sync database**
   action, deliberately away from the Sync button. Identity
   explanations and synchronization explanations remain independent
   layers; the sync side needs no dedicated "ownership details" UI
   because every planned operation already names its policy.

The headless Sync button fetches, plans, and either surfaces the window
(review mode, conflicts, or create offers) or applies clean changes
directly; `multisync_plan_only` forces review.

## 13. Worked example

Configuration: `score → provider:anilist`, `progress/status/dates →
provider:kitsu`, `rewatches → reconcile:manual`, `notes → individual`.

```
AniList: score 8,  progress 10, status watching
Kitsu:   score 7,  progress 12, status completed
MAL:     score 7,  progress 10, status watching
local:   score 8,  progress 12, status completed
```

Plan:

```
score:     AniList authoritative = 8   → Kitsu 7→8, MAL 7→8
progress:  Kitsu authoritative  = 12  → AniList 10→12, MAL 10→12
status:    Kitsu authoritative  = completed
                                  → AniList watching→completed, MAL too
rewatches: manual reconciliation → single-sided change propagates,
                                   genuine disagreement asks
notes:     individual            → no operation
```

No `PULL`/`PUSH`/`BOTH` classification exists anywhere in that plan —
each field's policy directly determined what the planner did.

## 14. Membership (`sync/membership.py`)

Ownership decides where a field's **value** comes from. It can never
decide whether a whole list **entry** belongs on a tracker — that is a
question about a different thing, so it gets its own model, its own
persisted state and its own user decisions.

For one entity, each connected provider is in exactly one **observed**
state:

| state      | meaning                                                     |
|------------|-------------------------------------------------------------|
| `PRESENT`  | the provider's own fetch lists an entry — the only evidence that actually establishes existence |
| `MISSING`  | a mapping exists (identity knows this provider's id) but no fetch has ever listed an entry there — an add is addressable |
| `UNMAPPED` | identity has no id for this provider at all |

`UNMAPPED` is deliberately **not** an add candidate. `adapter.add`
needs an id, and finding one is identity resolution's job (§3), not
membership's — so it is surfaced as an *identity gap* rather than as a
button that would quietly do nothing.

On top of the observation sits at most one **decision**
(`membership` table; `engine.set_membership`):

| want      | meaning                                                       |
|-----------|---------------------------------------------------------------|
| `present` | the entry belongs here → propose creating it                  |
| `absent`  | the entry does **not** belong here → propose **removing** it  |
| `ignore`  | whatever is here is fine → never propose either, stop asking  |

No row means undecided: Mirror surfaces the discrepancy and Sync offers
the (unticked) creation.

### 14.1 Why three states

The engine used to have one flag, which had to stand in for two
genuinely different things:

```
"never add this to Kitsu"      →  Kitsu should remain absent
                               →  Kitsu is wrong and should be removed
```

Those have opposite consequences, and only the second may ever delete
anything. Hence `ignore` (leave it alone) and `absent` (take it away)
are separate, and:

> **`absent` is only ever written from an explicit user action.** No
> observation, ownership policy or heuristic produces one.

`resolved_absent` is now purely the cross-id **lookup cache**
(`lookup_miss`): "we asked this provider and it said no", superseded
automatically the moment a real mapping appears. The two reasons that
were always user decisions — `declined` (the user rejected a proposed
creation) and `deleted` (the user removed the entry on the website) —
migrate to `ignore`, never to `absent`. Promoting either into a
deletion proposal would let a stale row destroy an entry the user has
since re-added on purpose, and a deletion on a real account is the one
thing this subsystem cannot undo.

## 15. Mirror (`sync/mirror.py`)

Sync and Mirror answer different questions.

**Sync** is incremental: *what changed since the last sync?* It uses
base state to attribute each change to the side that made it, and
leaves a tracker alone when nothing about it moved. That is the right
model for day-to-day use.

**Mirror** ignores history: *given the ownership configuration, what
should each tracker contain right now?*

```
AniList ─┐
Kitsu    ├─ ownership policy → desired tracker state
MAL     ─┘
```

Mirror is therefore useful exactly when change history is stale,
missing or wrong — after changing an ownership rule, or when a tracker
has drifted and incremental sync has nothing left to go on.

### 15.1 Trackers only

Hakubun's local state is **not** a participant in the comparison. It
never votes, is never an owner, and never appears as a side in a mirror
row or a mirror conflict. A plan that said "Hakubun → AniList" would be
describing a synchronization the user did not ask for.

Local state is still **written** (`MirrorPlan.local`) — just never
displayed and never consulted as an authority. Skipping it would be a
bug with a delayed fuse: with every provider's base advanced to the new
value and local matching none of them, the *next* ordinary Sync would
read local as the side that moved and push the stale value straight
back out (or raise a conflict over it).

`MirrorPlan.clean` therefore counts `local` even though it is never
displayed: both windows gate their apply button on it, and a plan
whose only work is bringing Hakubun's stale copy back into line with
trackers that already agree is still work. Omitting it left that case
with a dead button under a summary claiming everything was fine.

### 15.2 What the planner does

Per entity, over the providers that actually hold the entry:

- **PROVIDER** — the owner's current value is the value; every other
  tracker converges to it. No base-state check, no pending-local-edit
  routing: the owner's value is authoritative, full stop. If the owner
  doesn't hold the entry, nothing is asserted — synthesizing a value
  from a non-authority is precisely what ownership exists to prevent.
- **RECONCILE** — the field's strategy runs over the **trackers'**
  values alone, with no local participant and no `changed` flags. A
  strategy that needs history to avoid asking (Manual) therefore asks,
  which is correct here: during a mirror, two trackers genuinely
  disagreeing is a decision, not something to settle from a base the
  user may have reached for Mirror to escape.
- **INDIVIDUAL** — skipped, same as Sync. Mirror reads the same
  ownership configuration; there is no second ownership system.

The desired value per field is computed **once** and used for both of
its purposes: pushing existing entries into line, and seeding an entry
created on a tracker that lacks one. Computing it twice is how the two
drift — an earlier revision seeded new entries from local's working
value, so one plan could hand an existing tracker the tracker-derived
value and a newly created one local's. Those differ exactly when local
is stale, i.e. in the situation Mirror exists to fix.

A strategy returning `NoChange` means "nothing to push", not "no
value": the trackers already agree, and their agreed value is still
what a newly created entry must be seeded with.

Membership discrepancies become `MirrorPlan.adds` (proposals),
`MirrorPlan.removes` (only from a recorded `absent` decision) and
`MirrorPlan.membership` — the presence matrix the UI renders.

### 15.3 Resolving a Mirror decision

A Mirror conflict is a disagreement **between trackers**, and it cannot
be recorded the way Sync records one. `engine.resolve_conflict` writes
local state and advances every provider's base — and Mirror reads
neither, by design. A resolution stored that way is invisible to the
next mirror, which re-raises the identical conflict: the user clicks a
side, the preview rebuilds, and the card is still there.

`engine.resolve_mirror_conflict` stores it where Mirror *does* look
(`mirror_resolution`), together with a **fingerprint** of the tracker
values it was decided against. The decision stands while those values
stand. If any tracker's value later changes, the question is a new one
— the old answer was about a state of the world that no longer holds —
so the fingerprint stops matching and Mirror asks again rather than
replaying a stale verdict.

**Structural** conflicts (progress across differing episode structures)
are information only in Mirror: each tracker's number is in its own
structure, so there is no single value to adopt and no honest
conversion between them. `resolve_mirror_conflict` refuses, and the UI
points at the Sync tab, which has the local structure to resolve
against.

### 15.4 Applying: the bulk gates

`engine.apply_mirror(plan, allow_adds=False, allow_removes=False)`.

Both default to False, and they are enforced **in the engine**, not in
either sync window: there are two UIs plus a headless path, and "the
dialog asked" is not a safety property. Together with each operation's
own selection (adds and removes are planned unticked), creating or
deleting entries in bulk takes two independent confirmations.

Removals run **first**, and any field operation aimed at an entry being
removed is dropped — pushing a score to an entry and then deleting it
is wasted API calls and a confusing partial state. A removal that
*failed* does not suppress its field updates: the entry is still there,
and still wrong.

Deletion is logged as `op='remove'`, which `History.undo` ignores by
design. Undo replays local `set`s and rewinds pushed bases; a deleted
list entry is not something this app can honestly restore, and the log
should not pretend otherwise.

Provider support is read from `mediainfo['can_delete']`, defaulting to
**False** when a lib says nothing — an optimistic default would turn a
silently unimplemented delete into a Mirror that reports removals it
never performed.
