# Primary title language

Settings > Interface provides primary-title choices based on the UI language:

| UI language | Default | Choices |
| --- | --- | --- |
| English | English official, then romaji | English / Romaji / Native |
| 日本語 | Native | Native |
| 简体中文 | Simplified Chinese | 简体中文 / 繁體中文 / Native |
| 繁體中文 | Traditional Chinese | 繁體中文 / 简体中文 / Native |
| Español | Spanish official, then romaji | Español / Romaji / Native |

The feature needs a local AOD data file:

1. `anime-offline-database.json`, `anime-offline-database-minified.json`, or
   `anime-offline-database.jsonl` from
   [anime-offline-database](https://github.com/manami-project/anime-offline-database).
Put it in Hakubun+'s configuration or data directory. Hakubun+ downloads
AniDB's public title dump in the background on first use, stores it as
`anime-titles-auto.xml.gz` in the data directory, and refreshes it after seven
days. A manually supplied `anime-titles.xml.gz` or `anime-titles.xml` takes
precedence and is never overwritten. If a show, tracker ID, AniDB ID, or
requested language is missing, Hakubun+ falls back to romaji and ultimately
to the tracker's original title. Native mode has one additional fallback:
when AniDB has no native title, Hakubun+ uses AOD's AniList mapping and asks
AniList for its native title in background batches before falling back to
romaji. AniList accounts normally already include that value in their list
data and need no additional title request.

Simplified and Traditional Chinese modes also provide a **Use native fallback
for Chinese titles** toggle. It is on by default. When the requested Chinese
title is unavailable, the display uses AniDB's native title or AniList's
native title before romaji and the tracker title.

This is a display layer: tracker records, queued changes, and provider payloads
keep their original titles. A manually configured alternate title remains
separate and is still shown in brackets after the selected primary title.
Details use the selected primary title as their heading and include the
original title in a **Tracker title** row whenever the two differ.
