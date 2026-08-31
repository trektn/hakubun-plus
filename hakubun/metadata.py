"""Locale-aware presentation of canonical anime metadata values."""

import re

from hakubun import i18n, utils
from hakubun.genres import group_genres, normalize_genres
from hakubun.genres.labels import CATEGORY_LABELS


_SEASONS = {
    'Winter': {'en': 'Winter', 'ja': '冬', 'zh_CN': '冬季', 'zh_TW': '冬季', 'es': 'Invierno'},
    'Spring': {'en': 'Spring', 'ja': '春', 'zh_CN': '春季', 'zh_TW': '春季', 'es': 'Primavera'},
    'Summer': {'en': 'Summer', 'ja': '夏', 'zh_CN': '夏季', 'zh_TW': '夏季', 'es': 'Verano'},
    'Fall': {'en': 'Fall', 'ja': '秋', 'zh_CN': '秋季', 'zh_TW': '秋季', 'es': 'Otoño'},
}

_TYPES = {
    'TV': {'en': 'TV', 'ja': 'TVアニメ', 'zh_CN': '电视动画', 'zh_TW': '電視動畫', 'es': 'Serie de TV'},
    'TV Short': {'en': 'TV Short', 'ja': '短編TVアニメ', 'zh_CN': '电视短篇', 'zh_TW': '電視短篇', 'es': 'Corto de TV'},
    'Movie': {'en': 'Movie', 'ja': '映画', 'zh_CN': '电影', 'zh_TW': '電影', 'es': 'Película'},
    'OVA': {'en': 'OVA', 'ja': 'OVA', 'zh_CN': 'OVA', 'zh_TW': 'OVA', 'es': 'OVA'},
    'ONA': {'en': 'ONA', 'ja': 'ONA', 'zh_CN': 'ONA', 'zh_TW': 'ONA', 'es': 'ONA'},
    'Special': {'en': 'Special', 'ja': 'スペシャル', 'zh_CN': '特别篇', 'zh_TW': '特別篇', 'es': 'Especial'},
    'Music Video': {'en': 'Music Video', 'ja': 'MV', 'zh_CN': 'MV', 'zh_TW': 'MV', 'es': 'Videoclip'},
    'Unknown': {'en': 'Unknown', 'ja': '不明', 'zh_CN': '未知', 'zh_TW': '未知', 'es': 'Desconocido'},
    'Other': {'en': 'Other', 'ja': 'その他', 'zh_CN': '其他', 'zh_TW': '其他', 'es': 'Otro'},
}

_STATUSES = {
    utils.Status.ONGOING: {'en': 'Airing', 'ja': '放送中', 'zh_CN': '播出中', 'zh_TW': '播出中', 'es': 'En emisión'},
    utils.Status.FINISHED: {'en': 'Completed', 'ja': '放送終了', 'zh_CN': '已完结', 'zh_TW': '已完結', 'es': 'Finalizado'},
    utils.Status.NOTYET: {'en': 'Upcoming', 'ja': '放送予定', 'zh_CN': '即将播出', 'zh_TW': '即將播出', 'es': 'Próximamente'},
    utils.Status.CANCELLED: {'en': 'Cancelled', 'ja': '中止', 'zh_CN': '已取消', 'zh_TW': '已取消', 'es': 'Cancelado'},
    utils.Status.UNKNOWN: {'en': 'Unknown', 'ja': '不明', 'zh_CN': '未知', 'zh_TW': '未知', 'es': 'Desconocido'},
    utils.Status.OTHER: {'en': 'Other', 'ja': 'その他', 'zh_CN': '其他', 'zh_TW': '其他', 'es': 'Otro'},
}


def _language(locale):
    return (i18n.active_language() if locale in (None, 'auto')
            else i18n.effective_language(locale))


def localize_season(value, locale='auto'):
    """Turn canonical ``Summer 2026`` into native locale grammar."""
    if value is None:
        return value
    match = re.fullmatch(r'\s*(Winter|Spring|Summer|Fall)\s+(\d{4})\s*',
                         str(value), re.IGNORECASE)
    if not match:
        return value
    season = match.group(1).capitalize()
    year = match.group(2)
    language = _language(locale)
    label = _SEASONS[season].get(language, _SEASONS[season]['en'])
    if language in ('ja', 'zh_CN', 'zh_TW'):
        return '%s年%s' % (year, label)
    if language == 'es':
        return '%s de %s' % (label, year)
    return '%s %s' % (label, year)


def canonical_season(value):
    """Recover canonical English season/year from any supported locale."""
    if value is None:
        return None
    text = str(value).strip()
    year = re.search(r'(?<!\d)(\d{4})(?!\d)', text)
    if not year:
        return None
    for canonical, labels in _SEASONS.items():
        if any(label and label.casefold() in text.casefold()
               for label in labels.values()):
            return '%s %s' % (canonical, year.group(1))
    return None


def localize_type(value, locale='auto'):
    if value is None:
        return value
    raw = str(value)
    canonical = {
        'tv': 'TV', 'tv_short': 'TV Short', 'tv short': 'TV Short',
        'movie': 'Movie', 'ova': 'OVA', 'ona': 'ONA',
        'special': 'Special', 'tv special': 'Special',
        'music': 'Music Video', 'music video': 'Music Video', 'mv': 'Music Video',
        'unknown': 'Unknown', 'other': 'Other',
    }.get(raw.strip().lower(), raw)
    labels = _TYPES.get(canonical)
    if not labels:
        return raw
    language = _language(locale)
    return labels.get(language, labels['en'])


def localize_status(value, locale='auto'):
    if value is None:
        return value
    try:
        status = value if isinstance(value, utils.Status) else utils.Status.find(value)
    except (AttributeError, TypeError, ValueError):
        return str(value)
    labels = _STATUSES.get(status)
    if not labels:
        return str(value)
    language = _language(locale)
    return labels.get(language, labels['en'])


def localize_details(details, provider, locale='auto'):
    """Canonicalize and localize one provider's generic details rows."""
    localized = dict(details)
    rows = []
    genre_position = None
    genre_sources = dict(details.get('genre_sources') or {})
    category_labels = {
        label for labels in CATEGORY_LABELS.values()
        for label in labels.values()
    }

    for key, value, *rest in details.get('extra') or ():
        if key in category_labels:
            if genre_position is None:
                genre_position = len(rows)
            if key == 'Genres' and provider not in genre_sources:
                genre_sources[provider] = value or ()
            continue  # raw tracker labels are never display rows
        if key == 'Season':
            season = (localized.get('season_canonical')
                      or canonical_season(value))
            if season:
                localized['season_canonical'] = season
                value = localize_season(season, locale)
        elif key == 'Type':
            value = localize_type(localized.get('type', value), locale)
        elif key == 'Status':
            value = localize_status(localized.get('status', value), locale)
        rows.append((key, value, *rest))

    normalized = normalize_genres(**genre_sources)
    grouped = group_genres(normalized, locale)
    if grouped:
        position = genre_position if genre_position is not None else len(rows)
        rows[position:position] = grouped

    localized['extra'] = rows
    localized['genre_tags'] = normalized.tags
    localized['unknown_genre_tags'] = normalized.unknown_tags
    localized['genre_sources'] = genre_sources
    return localized
