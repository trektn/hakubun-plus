# Hakubun TODO

## Highest Priority

### MultiSync Rework — Field-Strategy Synchronization

* [ ] **Internal:** Rework MultiSync around field strategies

  * **In progress on `feat/multisync-mirror`** (unmerged): Mirror tab (Qt +
    GTK), tracker membership model, Mirror engine layer, entry-owner
    concept. `master` already has `FieldPolicy`/`PolicyKind` in
    `sync/models.py`, but `sync/diff.py`/`engine.py`/`conflicts.py` still
    carry the old `NO_BASE`/`IN_SYNC`/`three_way()` resolver, so the
    replacement isn't complete on `master` yet — check `feat/multisync-mirror`
    before redoing this work.

  * Replace the old Git-like three-way merge/divergence resolver
  * Remove the generic divergence resolver and its `diff.py` dependency
  * Remove the old `NO_BASE`, `IN_SYNC`, `PULL`, `PUSH`, `BOTH`, `three_way()`, and `resolve()` synchronization concepts
  * Synchronization should operate on the internal entity UUID
  * Identity resolution remains a separate subsystem
  * Field policies determine authoritative values
  * Strategies determine how reconciliation fields behave
  * Produce explicit `SyncOperation`s from a sync planner
  * Keep enough information in each operation for the Inspector to explain the decision
  * Preserve `base_state` as historical data available to reconciliation strategies, not as the synchronization algorithm
  * Keep provider capability/read/write checks in the planner
  * Use normalized internal values for comparisons
  * Preserve sync operation reasons for explainability
  * Scope: `hakubun`, `hakubun+`

* [ ] **Internal:** Implement field policy model

  * `provider`: one provider is authoritative for the field
  * `individual`: field does not synchronize
  * `reconcile`: field uses a reconciliation strategy
  * Provider ownership means authoritative source, not merely conflict priority
  * Scope: `hakubun`, `hakubun+`

* [ ] **Internal:** Implement reconciliation strategy interface

  * Support strategies such as:

    * Manual reconciliation
    * Set union
    * Maximum
    * Minimum
    * Custom progress reconciliation
  * Strategies return:

    * `Resolved(value)`
    * `Conflict(reason)`
    * `NoChange()`
  * Scope: `hakubun`, `hakubun+`

* [ ] **Internal:** Update Inspector integration for new MultiSync architecture

  * Keep identity explanations separate from synchronization explanations
  * Explain which field policy/strategy caused each sync operation
  * **Do not add separate Ownership Details UI; the new sync system should make this unnecessary**
  * Scope: `hakubun`, `hakubun+`

* [ ] **Internal:** Write MultiSync documentation

  * Document each MultiSync function step-by-step
  * Document how sync decisions are made
  * Document field ownership philosophy
  * Document reconciliation strategies
  * Document the distinction between:

    * Identity
    * Synchronization policy
    * Reconciliation strategy
    * History
  * Document the new planner and operation flow
  * Scope: `hakubun`, `hakubun+`

---

# High Priority — Rapid-fire

## UI / UX
- [x] **Taiga Mode:** Fix synopsis being cut off when searching seasons
  - Increase the season/search result box size as needed so the synopsis can be displayed properly
  - Scope: `hakubun+`
  - Done: `8d3b61b`, `813de7e`


* [x] **Qt:** Add artwork/photo to Now Playing

  * Place above the progress bar and Details button
  * Scope: `hakubun`, `hakubun+`
  * Done: poster already shown above title/progress row in `hakubun/ui/qt/nowplaying.py`

* [ ] **Qt/GTK:** Fix Now Playing progress bar

  * Scope: `hakubun`

* [ ] **Qt/GTK:** Backport percentage-update customization from `hakubun+`

  * Scope: `hakubun`

* [x] **GTK:** Rename `Filter` menu item to `Filter list`

  * Avoid ambiguity with search
  * Scope: `hakubun`, `hakubun+`
  * Done: `8400e74`

* [ ] **GTK:** Add category sorting to MultiSync menu

  * Match Qt feature parity
  * Scope: `hakubun+`
  * Still open: Qt's `syncwindow.py` has per-category tabs; GTK's
    `multisyncwindow.py` is still a single flat sorted tree. May land as
    part of the `feat/multisync-mirror` rework — check there first.

* [ ] **Qt/GTK:** Replace ambiguous `-` values in MultiSync

  * Use `N/A` or `0` where appropriate
  * Scope: `hakubun+`
  * Still open: still present at `syncwindow.py:1249` and `sync/present.py:90`

* [x] **GTK:** Make SubMiner top-bar switch toggleable

  * Add setting under User Interface
  * Scope: `hakubun+`
  * Done: `b8fba6b` (plus a right-click toggle refinement on `feat/multisync-mirror`'s `6ae0848`)

* [x] **GTK:** Set search shortcut to `Ctrl+F`

  * Scope: `hakubun`, `hakubun+`
  * Done: `825c4e8`

* [x] **GTK:** Fix unpredictable filtering behavior in Now Playing

  * Scope: `hakubun`, `hakubun+`
  * Done: `e38af6c`, plus follow-up `b1c70b6` (details visibility)

* [x] **GTK:** Add ID to Details menu

  * Scope: `hakubun+`
  * Done: `df70b1a`

* [x] **GTK:** Add anime status column

  * Statuses: Completing / Airing / Upcoming
  * Scope: `hakubun`, `hakubun+`
  * Done: `edcc16c`

## Provider / Sync

* [ ] **Kitsu:** Fix missing `.5` rating increments with Advanced Rating enabled

  * Scope: `hakubun`

* [ ] **MultiSync:** Refresh list after sync completes

  * Run a download request after MultiSync has applied changes
  * Scope: `hakubun+`

## Parsing

* [ ] **AIE:** Improve parsing quality

  * Target at least `anitopy` parity
  * Ideally reach `anitomy-ng` parity
  * Keep parsing speed fast
  * If successful, deprecate `anitopy`
  * If unsuccessful, deprecate AIE
  * Scope: `hakubun`, `hakubun+`

* [x] **anitomy-ng / PR29:** Evaluate wrapper improvements

  * Implement fixes #2 and #3 as a lightweight wrapper
  * Check performance impact
  * Handle a grandparent folder becoming the title
  * Ensure `NCED` in the path is not interpreted as a type
  * Prevent the `3` in `EAC-3` from being interpreted as the episode number
  * Scope: `hakubun`, `hakubun+`
  * Done: merged via `25b720d`/`d3aecff` (anitomy-ng >=1.0.9): directory-prefix
    stripping, season-in-parent-folder, `op.` no longer mistaken for a type,
    with regression tests. `NCED` already excluded via `ANITYPE_INVALID`.

## Internal / Cleanup

* [ ] **Internal:** Trim possible bloat

  * Scope: `hakubun+`

---

# Packaging

* [x] **Packaging:** Fix AUR packages

  * Published 2026-08-31: `python-anitomy-ng-bin` `c8e79c2`,
    `hakubun-git` `48ae715`, `hakubun-plus-git` `3fd7035`
  * Scope: `hakubun`, `hakubun+`

---

# Last / Supervised

These should be handled after the MultiSync rework and rapid-fire work, with careful testing.

## UI Languages / Localization

* [ ] **Qt/GTK:** Add UI language support

  * Add language dropdown under Interface
  * Languages:

    * English
    * Japanese
    * Chinese (Simplified)
    * Chinese (Traditional)
    * Spanish
  * Implement initially in `hakubun+`
  * Backport after testing
  * Requires testing of language detection and tracker behavior
  * **In progress on `worktree-ui-i18n`** (unmerged): gettext plumbing +
    language dropdown, GTK UI and Qt main-window chrome translated,
    Settings/Details/context-menu fields, list category tabs/Status
    column/column headers. Not on `master` yet — check that branch
    before redoing this work.

* [ ] **Qt/GTK:** Add title/synonym display behavior setting

  * Add Behavior tab
  * Options:

    * `Try to match UI (Localized)`

      * Falls back to Romaji if unavailable
    * `Native`
    * `Romaji`
    * `English (Localized)`
  * Implement initially in `hakubun+`
  * Backport after testing

* [ ] **Search:** Add alternative-title search options

  * Beta behavior setting
  * Checkboxes:

    * Alternative titles
    * English titles
    * Native titles
    * Romaji titles
  * Implement alongside the language/title system
  * Scope: `hakubun+` → backport after testing

---

# Scope Summary

## `hakubun`

* [ ] MultiSync field-strategy rework
* [ ] MultiSync documentation
* [ ] Inspector integration for new MultiSync system
* [ ] Qt/GTK progress bar fix
* [ ] Kitsu `.5` rating fix
* [ ] Percentage-update customization
* [ ] GTK `Ctrl+F` search shortcut
* [ ] AIE parsing improvements
* [x] anitomy-ng / PR29 wrapper improvements
* [x] Filter → Filter list

## `hakubun+`

* [ ] MultiSync field-strategy rework
* [ ] MultiSync documentation
* [ ] Inspector integration for new MultiSync system
* [x] Now Playing artwork
* [ ] MultiSync category sorting
* [ ] MultiSync `-` value cleanup
* [x] SubMiner toggle
* [x] GTK filtering fix
* [x] GTK Details ID
* [x] Anime status column
* [ ] Post-MultiSync list refresh
* [ ] AIE parsing improvements
* [x] anitomy-ng / PR29 wrapper improvements
* [ ] Internal bloat cleanup
* [ ] UI language support (in progress: `worktree-ui-i18n`)
* [ ] Title/synonym localization behavior
* [ ] Alternative-title search

## On Hold

* [x] AUR packaging fixes

## Do Last

* [ ] UI language support
* [ ] Title/synonym localization behavior
* [ ] Alternative-title search
