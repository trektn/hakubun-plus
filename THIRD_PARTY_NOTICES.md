# Third-party notices

Hakubun+ bundles (as a required runtime dependency) or optionally depends on
the following filename-parsing libraries, both licensed under the
[Mozilla Public License 2.0](https://www.mozilla.org/en-US/MPL/2.0/).

MPL 2.0 is file-level copyleft and compatible with this project's own
GPL-3.0-or-later license (see `COPYING`); using these as separate,
unmodified dependencies does not require relicensing any of Hakubun+'s own
code.

It also optionally depends on RapidFuzz (see below), under the permissive
MIT license -- trivially compatible with GPL-3.0-or-later.

## anime-offline-database (optional local data)

- Project: https://github.com/manami-project/anime-offline-database
- Used for MultiSync identity enrichment and language-aware primary titles
  when an AOD JSON/JSONL export is placed in Hakubun+'s configuration or data
  directory.
- License: Open Database License 1.0 (ODbL) for the database, with Database
  Contents License 1.0 (DbCL) for individual contents.
- The database is not bundled. If it is redistributed or a substantial
  derived database is distributed, retain its attribution/license notices
  and comply with the ODbL share-alike and machine-readable access
  requirements.
- License text: https://github.com/manami-project/anime-offline-database/blob/master/LICENSE

## AniDB title dump (optional local data)

- Project: https://anidb.net/
- Hakubun+ downloads and locally caches AniDB's `anime-titles.xml.gz`, or can
  read a user-supplied `anime-titles.xml(.gz)`, to select language-tagged
  primary titles after AOD resolves a tracker entry to its AniDB anime ID.
- The title dump is not bundled or redistributed by Hakubun+.

## Localized synopsis services

- [Animumemo MADB API](https://animedb.moe/doc/api/madb) supplies Japanese
  synopsis fallbacks derived from Japan's Media Arts Database.
- [Bangumi](https://bgm.tv/) supplies Simplified Chinese synopsis fallbacks.
- [TMDB](https://www.themoviedb.org/) supplies Japanese, Simplified Chinese,
  Traditional Chinese, and Spanish synopsis fallbacks when the user supplies
  their own API key. TMDB data is not bundled.

This product uses the TMDB API but is not endorsed or certified by TMDB.

## OpenCC (required)

- Project: https://github.com/BYVoid/OpenCC
- License: Apache License 2.0
- Used to convert a Simplified Chinese Bangumi synopsis to Traditional
  Chinese when TMDB has no `zh-TW` synopsis.

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

## RapidFuzz (optional, `fuzzy` extra)

- Project: https://github.com/rapidfuzz/RapidFuzz
- License: MIT
- Used for fuzzy title matching when guessing which show a played file
  belongs to.

Copyright © 2020-present Max Bachmann
Copyright © 2011 Adam Cohen

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
