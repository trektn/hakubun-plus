# Hakubun TODO

## High Priority — Rapid-fire

### UI / UX

* [ ] **Qt:** Add artwork/photo to Now Playing

  * Place above the progress bar and Details button
  * Scope: `hakubun`, `hakubun+`

* [ ] **Qt/GTK:** Fix Now Playing progress bar

  * Scope: `hakubun`

* [ ] **Qt/GTK:** Backport percentage-update customization from `hakubun+`

  * Scope: `hakubun`

* [ ] **GTK:** Rename `Filter` menu item to `Filter list`

  * Avoid ambiguity with search
  * Scope: `hakubun`, `hakubun+`

* [ ] **GTK:** Add category sorting to MultiSync menu

  * Match Qt feature parity
  * Scope: `hakubun+`

* [ ] **Qt/GTK:** Improve MultiSync rebase messages

  * Show which provider owned the rebased changes
  * Example: `Snow Halation (6 changes) (MAL) → Add to AniList, Add to Kitsu`
  * Scope: `hakubun+`

* [ ] **Qt/GTK:** Replace ambiguous `-` values in MultiSync

  * Use `N/A` or `0` where appropriate
  * Scope: `hakubun+`

* [ ] **GTK:** Make SubMiner top-bar switch toggleable

  * Add setting under User Interface
  * Scope: `hakubun+`

* [ ] **GTK:** Set search shortcut to `Ctrl+F`

  * Scope: `hakubun`, `hakubun+`

* [ ] **GTK:** Fix unpredictable filtering behavior in Now Playing

  * Scope: `hakubun`, `hakubun+`

* [ ] **GTK:** Add ID to Details menu

  * Scope: `hakubun+`

* [ ] **GTK:** Add anime status column

  * Statuses: Completing / Airing / Upcoming
  * Scope: `hakubun`, `hakubun+`

### Provider / Sync

* [ ] **Kitsu:** Fix missing `.5` rating increments with Advanced Rating enabled

  * Scope: `hakubun`

* [ ] **MultiSync:** Refresh list after sync completes

  * Run a download request after MultiSync has applied changes
  * Scope: `hakubun+`

* [ ] **MultiSync:** Improve mirroring/ownership behavior

  * Check whether a push would overwrite provider-owned data
  * Treat local/provider ownership as the source of truth when pushing scores, progress, etc.
  * Scope: `hakubun+`

* [ ] **Identity Inspector:** Add `Ownership Details` under Field Data

  * Show which provider owns each stat
  * Scope: `hakubun+`

### Parsing

* [ ] **AIE:** Improve parsing quality

  * Target at least `anitopy` parity
  * Ideally reach `anitomy-ng` parity
  * Keep parsing speed fast
  * If successful, deprecate `anitopy`
  * If unsuccessful, deprecate AIE
  * Scope: `hakubun`, `hakubun+`

* [ ] **anitomy-ng / PR29:** Evaluate wrapper improvements

  * Implement fixes #2 and #3 as a lightweight wrapper
  * Check performance impact
  * Handle a grandparent folder becoming the title
  * Ensure `NCED` in the path is not interpreted as a type
  * Prevent the `3` in `EAC-3` from being interpreted as the episode number
  * Scope: `hakubun`, `hakubun+`

### Internal / Cleanup

* [ ] **Internal:** Trim possible bloat

  * Scope: `hakubun+`

---

## MultiSync Documentation

* [ ] **Internal:** Document MultiSync behavior

  * Document each MultiSync function step-by-step
  * Document how sync decisions are made
  * Document ownership/source-of-truth philosophy
  * Document how local and provider ownership interact

---

## Packaging

* [ ] **Packaging:** Fix AUR packages

  * Push when ready
  * **ON HOLD:** AUR attacks / current security situation
  * Scope: `hakubun`, `hakubun+`

---

# Last / Supervised

These should be handled after the rapid-fire work and tested carefully.

## Annict

* [ ] **Internal / Engine:** Add Annict GraphQL API

  * Scope: `hakubun`, `hakubun+`
  * **Priority:** Last
  * Implementation should be supervised

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

* [ ] Qt/GTK progress bar fix
* [ ] Kitsu `.5` rating fix
* [ ] Percentage-update customization
* [ ] GTK `Ctrl+F` search shortcut
* [ ] AIE parsing improvements
* [ ] anitomy-ng / PR29 wrapper improvements
* [ ] Filter → Filter list

## `hakubun+`

* [ ] Now Playing artwork
* [ ] MultiSync category sorting
* [ ] MultiSync rebase owner display
* [ ] MultiSync `-` value cleanup
* [ ] SubMiner toggle
* [ ] GTK filtering fix
* [ ] GTK Details ID
* [ ] Anime status column
* [ ] Post-MultiSync list refresh
* [ ] MultiSync ownership/mirroring improvements
* [ ] Identity Inspector ownership details
* [ ] Internal MultiSync documentation
* [ ] AIE parsing improvements
* [ ] anitomy-ng / PR29 wrapper improvements
* [ ] Internal bloat cleanup
* [ ] Annict GraphQL
* [ ] UI language support
* [ ] Title/synonym localization behavior
* [ ] Alternative-title search

## On Hold

* [ ] AUR packaging fixes

## Do Last

* [ ] Annict GraphQL API
* [ ] UI language support
* [ ] Title/synonym localization behavior
* [ ] Alternative-title search

