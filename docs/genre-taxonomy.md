# Canonical genre taxonomy

Hakubun+ does not render MyAnimeList, AniList, or Kitsu category strings
directly. Provider labels are mapped to stable IDs, merged as a union, and
then localized from those IDs for English, Japanese, Simplified Chinese,
Traditional Chinese, and Spanish.

The implementation is split by responsibility:

- `hakubun/genres/models.py` defines `GenreTag` and normalization results.
- `hakubun/genres/mappings.py` owns explicit provider-label mappings and
  canonical categories.
- `hakubun/genres/labels.py` owns localized labels keyed by canonical ID.
- `hakubun/genres/normalize.py` merges duplicates, records provenance, keeps
  unknown raw labels for diagnostics, and groups display rows.

The supported categories are `genre`, `theme`, and `demographic`. Display
order is stable and grouped, for example:

```text
Genres       Comedy · Girls' Love
Themes       School · Video Games
Demographic  Seinen
```

Japanese uses `ジャンル`, `テーマ`, and `対象`, with `・` between values.
Unknown provider strings are saved under `unknown_genre_tags` but are never
shown as if they were canonical metadata.

AniList detail/search queries combine its broad `genres` with recognized,
non-spoiler Media Tags. MAL supplies its genre/theme/demographic labels from
the v2 `genres` field. Kitsu GraphQL and legacy REST details use categories.
All three enter the same `normalize_genres()` path; tracker choice therefore
does not determine the terminology shown by the UI.
