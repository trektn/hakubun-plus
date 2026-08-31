# Multisync Rework: Field-Strategy Synchronization

## Goal

Replace the old Git-like three-way merge/divergence resolver with a simpler model:

> **Identity determines what entries represent the same work. Field policies determine where each field gets its authoritative value. Strategies determine how non-authoritative/reconciliation fields behave.**

Identity resolution is independent and must not be rewritten.

The Inspector is also independent and must continue to expose *why* an identity or sync decision was made.

---

# 1. Delete the old merge abstraction

Remove the generic divergence resolver and its dependency on `diff.py`.

The following concepts are no longer part of the core synchronization API:

```python
NO_BASE
IN_SYNC
PULL
PUSH
BOTH
three_way()
resolve()
```

Do not replace them with another generic "local vs remote merge" function.

The old assumption was:

```text
merge_base
    ├── local
    └── remote
        ↓
    divergence resolver
        ↓
    winner
```

The new assumption is:

```text
identity
    ↓
internal entity UUID
    ↓
field strategy
    ↓
authoritative value / reconciliation
    ↓
sync plan
```

---

# 2. Identity resolution remains unchanged

`IdentityResolver` is a separate subsystem.

It continues to:

* map `(provider, provider_id)` → internal UUID
* trust provider-published cross IDs
* use the anime-relations atlas for anime
* perform exact normalized-title matching
* create identity conflicts for genuine ambiguity
* support `confirm`, `provider_only`, `defer`, and `ignore`
* enforce media-type compatibility
* prevent same-provider duplicates from being silently merged

Do not mix field synchronization logic into identity resolution.

The atlas is an identity aid only.

---

# 3. Internal entity is the synchronization unit

After identity resolution:

```text
AniList entry  ─┐
Kitsu entry    ─┼──→ entity UUID
MAL entry      ─┘
```

All synchronization operates on the entity UUID.

Providers are merely representations of that entity.

Example:

```text
UUID: abc123

AniList:
    id = 123
    score = 8

Kitsu:
    id = 456
    progress = 12

MAL:
    id = 789
    progress = 10
```

The engine does not try to merge the provider entries into another provider-shaped object.

It builds a field-level synchronization plan.

---

# 4. Field policies

A field policy answers:

> "Where does the authoritative value for this field come from?"

Recommended policy kinds:

```python
class PolicyKind(Enum):
    PROVIDER = "provider"
    INDIVIDUAL = "individual"
    RECONCILE = "reconcile"
```

A provider policy identifies the authoritative provider:

```python
FieldPolicy(
    kind=PolicyKind.PROVIDER,
    provider="kitsu",
)
```

Example configuration:

```text
score        → provider: anilist
progress     → provider: kitsu
status       → provider: kitsu
rewatches    → reconcile
notes        → individual
start_date   → provider: kitsu
finish_date  → provider: kitsu
```

Do not interpret provider ownership as merely "who wins a conflict."

It means:

> **This provider is authoritative for this field.**

---

# 5. Individual fields

`individual` means:

> This field is not synchronized between providers.

Example:

```text
notes → individual
```

If:

```text
AniList notes = "watch dub"
Kitsu notes   = "great ending"
MAL notes     = "rewatch later"
```

the engine does nothing.

It must not:

* compare them
* merge them
* push one over another
* create a conflict

The provider-local values remain independent.

---

# 6. Provider-owned fields

For a provider-owned field:

```text
progress → Kitsu
```

Kitsu is authoritative.

Suppose:

```text
Kitsu   = 12
AniList = 10
MAL     = 10
```

The plan is:

```text
Kitsu → authoritative value = 12

push AniList progress: 10 → 12
push MAL progress:     10 → 12
```

If the local representation contains a Kitsu-fed value, it should converge to Kitsu as well.

The important point is:

> The engine does not need a generic local-vs-remote conflict resolver to determine this.

It already knows who owns the field.

---

# 7. External changes

Provider-owned fields must detect provider changes.

Example:

Initial state:

```text
Kitsu progress = 10
AniList progress = 10
MAL progress = 10
```

User changes Kitsu externally:

```text
Kitsu progress = 12
```

Next fetch:

```text
Kitsu = 12
AniList = 10
MAL = 10
```

Because Kitsu owns `progress`:

```text
authoritative progress = 12
```

Plan:

```text
AniList: 10 → 12
MAL:     10 → 12
```

No manual conflict should be generated.

This is one of the major reasons the old generic `three_way()` resolver is unnecessary.

---

# 8. Local edits

Local state is still useful as the application's working representation.

A local edit to an authoritative field should ultimately be sent to the owning provider.

Example:

```text
progress owner = Kitsu

local progress: 10 → 15
Kitsu progress: 10
```

The planner sees that the authoritative provider is Kitsu and produces:

```text
push Kitsu progress: 10 → 15
```

After successful push:

```text
Kitsu = 15
local = 15
```

Then other providers converge to 15.

The local value is therefore not a competing permanent authority.

It is the application's working value.

---

# 9. Reconciliation fields

Some fields cannot sensibly have one owner.

Example:

```text
rewatches → reconcile
```

A reconciliation strategy receives the provider values and determines what to do.

Do not implement reconciliation inside the generic ownership policy.

Instead use a strategy interface:

```python
class ReconcileStrategy:
    def reconcile(self, field, values, context):
        ...
```

Possible strategies include:

```text
ManualReconcile
SetUnion
Maximum
Minimum
CustomProgressReconcile
```

The strategy returns one of:

```python
Resolved(value)
Conflict(reason)
NoChange()
```

For example:

```text
AniList rewatches = 2
Kitsu rewatches   = 3
MAL rewatches     = 2
```

A manual reconciliation strategy might return:

```text
Conflict:
    AniList = 2
    Kitsu   = 3
    MAL     = 2
```

The Inspector can then ask the user.

---

# 10. Base state

Do not automatically delete `base_state`.

The old `three_way()` algorithm can be removed while the stored merge base remains available for reconciliation strategies.

Base state is historical information:

```text
what this provider's value was when synchronization last established a common state
```

It is useful when a reconciliation strategy needs to determine which provider actually changed.

Example:

```text
base:
    Kitsu = 2
    AniList = 2

current:
    Kitsu = 3
    AniList = 2
```

A reconciliation strategy can infer:

```text
Kitsu changed
AniList did not
```

This is different from the old architecture where every field automatically required three-way merging.

Therefore:

> `base_state` is data available to strategies, not the synchronization algorithm itself.

---

# 11. Sync planner

Implement a planner roughly with this conceptual API:

```python
plan_entity(uuid, provider_entries)
```

For every synchronizable field:

```python
policy = ownership[field]

if policy.kind == INDIVIDUAL:
    continue

if policy.kind == PROVIDER:
    plan_authoritative_field(...)

elif policy.kind == RECONCILE:
    plan_reconciliation_field(...)
```

The planner returns operations.

For example:

```python
SyncOperation(
    uuid=uuid,
    provider="anilist",
    field="progress",
    old_value=10,
    new_value=12,
    reason="kitsu is authoritative",
)
```

The operation should contain enough information for the Inspector to explain it.

---

# 12. Authoritative-field planning

Conceptually:

```python
def plan_authoritative_field(uuid, field, owner, states):
    authoritative = states[owner][field]

    for provider, state in states.items():
        if provider == owner:
            continue

        current = state.get(field)

        if values_equal(current, authoritative):
            continue

        yield SyncOperation(
            uuid=uuid,
            provider=provider,
            field=field,
            old_value=current,
            new_value=authoritative,
            reason=f"{owner} owns {field}",
        )
```

The exact implementation can account for:

* provider capability
* read-only fields
* provider-specific representations
* missing values
* unsupported fields
* provider-specific normalization

Do not silently treat unsupported fields as empty values.

---

# 13. Local state

`local_state` represents the application's current working representation.

It should not be interpreted as an independent provider.

For an authoritative field:

```text
owner provider → local state → other providers
```

The engine should keep the local representation consistent with the authoritative source after synchronization.

For example:

```text
Kitsu progress = 12
local progress = 10
MAL progress = 10

→ pull authoritative Kitsu value into local state
→ push 12 to MAL
```

The exact ordering may be implementation-specific, but the resulting state should converge.

---

# 14. Provider capabilities

Do not assume every provider supports every field.

The planner must distinguish:

```text
field exists
field is supported
field is writable
field is readable
```

Example:

```text
notes:
    AniList: writable
    Kitsu: unsupported
    MAL: writable
```

If `notes` is `individual`, none of this matters.

If a provider is authoritative for a field but cannot write it to another provider, the planner should report:

```text
unsupported / not writable
```

rather than pretending synchronization succeeded.

---

# 15. Equality

The old `eq()` helper should not remain as a generic merge primitive.

Instead, equality should be field/provider aware where necessary.

For example:

```text
score:
    numeric equality

tags:
    set equality

dates:
    normalized date equality

status:
    provider status normalized to internal status

progress:
    integer equality
```

Provider adapters should normalize provider-specific representations before the planner sees them whenever possible.

The planner should operate on normalized internal values.

---

# 16. Sync operation reasons

Every planned operation should carry an explanation.

Examples:

```text
" Kitsu owns progress"
" AniList owns score"
" value already synchronized"
" reconciliation strategy selected Kitsu value"
" user resolved conflict"
" provider does not support field"
```

This is important because the Inspector should not have to reverse-engineer why the engine made a decision.

The plan itself is the explanation.

---

# 17. Inspector integration

Do not modify identity resolution to accommodate the new sync architecture.

The Inspector should expose two independent explanations:

### Identity

```text
Kitsu 12345
    ↓
mapped to UUID abc123
    ↓
via anime-relations atlas
```

or:

```text
Kitsu 12345
    ↓
exact normalized title match
    ↓
auto-linked, unconfirmed
```

### Synchronization

```text
progress

Owner: Kitsu

Kitsu:   12
AniList: 10
MAL:     10

Plan:
    AniList 10 → 12
    MAL 10 → 12

Reason:
    Kitsu owns progress
```

These are separate layers.

---

# 18. What happens to History

History is optional infrastructure.

Keep the event log if undo, audit history, or progress history is desired.

The sync engine should not consult `History` to determine synchronization.

The direction is:

```text
planner
   ↓
operations
   ↓
apply
   ↓
state changes
   ↓
event recording
```

Not:

```text
history
   ↓
decide what sync should do
```

History is an audit trail.

---

# 19. Recommended architecture

Final architecture:

```text
                    Provider APIs
                         │
                         ▼
                 Normalized Entries
                         │
                         ▼
                  IdentityResolver
                         │
              ┌──────────┴──────────┐
              │                     │
          mappings              conflicts
              │
              ▼
             UUID
              │
              ▼
        ┌───────────────┐
        │   SyncPlanner │
        └───────┬───────┘
                │
       ┌────────┼─────────┐
       ▼        ▼         ▼
 authoritative individual reconcile
       │        │         │
       │        │    strategy
       │        │         │
       └────────┴────┬────┘
                     ▼
               SyncOperations
                     │
                     ▼
                Apply Engine
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
      local_state remote_state base_state
                     │
                     ▼
                   events
```

The critical architectural rule is:

> **Identity answers "what is this?" Synchronization policy answers "what should this field be?" Strategies answer "how do we reconcile fields without a single authority?" History answers "what happened?"**

Do not let those four responsibilities collapse into one generic merge resolver.

---

# 20. Example using the intended personal configuration

```text
score       → AniList
progress    → Kitsu
status      → Kitsu
rewatches   → reconcile/manual
notes       → individual
start_date  → Kitsu
finish_date → Kitsu
```

Given:

```text
AniList:
    score = 8
    progress = 10
    status = watching

Kitsu:
    score = 7
    progress = 12
    status = completed

MAL:
    score = 7
    progress = 10
    status = watching

local:
    score = 8
    progress = 12
    status = completed
```

The planner produces approximately:

```text
score:
    AniList authoritative = 8
    → Kitsu 7 → 8
    → MAL 7 → 8

progress:
    Kitsu authoritative = 12
    → AniList 10 → 12
    → MAL 10 → 12

status:
    Kitsu authoritative = completed
    → AniList watching → completed
    → MAL watching → completed

rewatches:
    reconciliation strategy
    → inspect/reconcile

notes:
    individual
    → no operation

start_date:
    Kitsu authoritative
    → propagate Kitsu value

finish_date:
    Kitsu authoritative
    → propagate Kitsu value
```

There is no generic:

```text
PULL
PUSH
BOTH
```

classification required.

The field's policy directly determines what the planner should do.
