# Localized synopses

Hakubun+ localizes anime synopses in Taiga mode's Seasons page and Search
page, the Qt add/search views, GTK search, and Details in both Qt and GTK.
English keeps the synopsis supplied by the active tracker. Other languages
use this order:

| App language | Synopsis priority |
| --- | --- |
| English | Active tracker |
| 日本語 | Animumemo MADB → TMDB `ja-JP` → tracker English |
| 简体中文 | Bangumi → TMDB `zh-CN` → tracker English |
| 繁體中文 | TMDB `zh-TW` → Bangumi converted with OpenCC → tracker English |
| Español | TMDB `es-MX` → TMDB `es-ES` → tracker English |

MADB matching uses the native title resolved from AniDB/AniList. Bangumi
matching prefers the already-resolved Simplified Chinese title and then the
native title. TMDB searches with the active tracker's title and filters by
the show's year when available.

TMDB lookups require a v3 API key. Enter it under **Settings → Interface →
Language → TMDB API key**, or set `HAKUBUN_TMDB_API_KEY` in the environment.
The key is kept in the user's local configuration and is not bundled in the
source tree.

Results are stored in `localized-synopses-v1.json` in Hakubun+'s cache
directory. Positive matches are refreshed after 30 days and misses after
seven days. Network or matching failures silently retain the tracker's
English synopsis.

This product uses the TMDB API but is not endorsed or certified by TMDB.
