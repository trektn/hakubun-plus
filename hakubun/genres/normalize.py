"""Provider-union normalization and UI grouping for anime genres."""

from hakubun.genres.labels import (
    genre_locale, get_category_label, get_genre_label,
)
from hakubun.genres.mappings import (
    CANONICAL_ORDER, CANONICAL_TAGS, canonical_id_for,
)
from hakubun.genres.models import CATEGORIES, GenreNormalization


TRACKER_ORDER = ('mal', 'anilist', 'kitsu')


def normalize_genres(mal=None, anilist=None, kitsu=None, **sources):
    """Return the union of recognized tags with source provenance.

    Unknown values are deliberately excluded from ``tags`` and retained in
    ``unknown_tags`` for diagnostics and future declarative mapping updates.
    """
    supplied = {'mal': mal, 'anilist': anilist, 'kitsu': kitsu}
    supplied.update(sources)
    recognized = {}
    unknown = {}

    ordered_sources = list(TRACKER_ORDER)
    ordered_sources.extend(source for source in supplied
                           if source not in ordered_sources)
    for source in ordered_sources:
        values = supplied.get(source) or ()
        if isinstance(values, str):
            values = (values,)
        for raw in values:
            if not raw:
                continue
            label = str(raw).strip()
            canonical_id = canonical_id_for(source, label)
            if canonical_id:
                recognized.setdefault(canonical_id, set()).add(source)
            else:
                bucket = unknown.setdefault(source, [])
                if label not in bucket:
                    bucket.append(label)

    tags = []
    for canonical_id in CANONICAL_ORDER:
        source_set = recognized.get(canonical_id)
        if not source_set:
            continue
        sources_for_tag = [source for source in ordered_sources
                           if source in source_set]
        tags.append({
            'id': canonical_id,
            'category': CANONICAL_TAGS[canonical_id].category,
            'sources': sources_for_tag,
        })
    return GenreNormalization(tags=tags, unknown_tags=unknown)


def group_genres(normalized, locale='auto'):
    """Localized category rows suitable for the generic details views."""
    language = genre_locale(locale)
    separator = '・' if language == 'ja' else ' · '
    rows = []
    for category in CATEGORIES:
        labels = [get_genre_label(tag['id'], locale)
                  for tag in normalized.tags
                  if tag['category'] == category]
        if labels:
            rows.append((get_category_label(category, locale),
                         separator.join(labels)))
    return rows
