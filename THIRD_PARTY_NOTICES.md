# Third-party notices

Hakubun+ bundles (as a required runtime dependency) or optionally depends on
the following filename-parsing libraries, both licensed under the
[Mozilla Public License 2.0](https://www.mozilla.org/en-US/MPL/2.0/).

MPL 2.0 is file-level copyleft and compatible with this project's own
GPL-3.0-or-later license (see `COPYING`); using these as separate,
unmodified dependencies does not require relicensing any of Hakubun+'s own
code.

## anitomy-ng (required)

- Project: https://github.com/tylergibbs2/anitomy-ng
- License: Mozilla Public License 2.0
- A pure-Rust filename parser based on the grammar of:
  - **Anitomy** — Copyright (c) 2014-2017, Eren Okka.
    https://github.com/erengy/anitomy (MPL 2.0)
  - **anitomy-rs** — https://github.com/Rapptz/anitomy-rs (MPL 2.0)
  - **anitopy** (see below), whose Python-side test fixtures anitomy-ng's
    own test suite is also validated against.

A copy of the MPL 2.0 license text is included in the installed package's
`dist-info/licenses/LICENSE` (via the standard wheel metadata); the full
text is also reproduced at the URL above.

## anitopy (optional, `title_parser: anitopy`)

- Project: https://github.com/igorcmoura/anitopy
- License: Mozilla Public License 2.0
- A Python port of Anitomy (see above).

## Taiga mode icons

- Most of `hakubun/data/qtui/*.svg` are vendored, unmodified, from
  https://github.com/erengy/taiga (`src/resources/icons/`), Copyright (c)
  2010-2024, Eren Okka, licensed GPL-3.0-or-later -- the same license as
  this project (see `COPYING`), so no relicensing is needed.
- `pause.svg` isn't part of that set (Taiga's own UI has no use for one)
  and comes directly from Google's
  [Material Symbols](https://github.com/google/material-design-icons)
  instead (`symbols/web/pause/materialsymbolsoutlined/pause_24px.svg`),
  licensed Apache License 2.0 -- the same icon family/style Taiga's own
  set is drawn from, so it matches visually.
- The rest of `hakubun/data/qtui/*.svg` are themselves Material Symbols,
  licensed Apache License 2.0.
- Used by `hakubun/ui/qt/util.py`'s `getIcon()` to give Taiga mode Taiga's
  actual iconography (tinted at runtime to the active Qt palette) instead
  of relying on the user's desktop icon theme.
