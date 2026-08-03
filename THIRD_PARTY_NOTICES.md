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
