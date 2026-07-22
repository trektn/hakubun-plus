"""Normalization: provider values <-> the internal canonical model.

Canonical user state (models.USER_FIELDS):
  score:  float 0-10 (0/None = unrated)
  progress: int >= 0
  status: models.STATUSES string
  notes: str | None
  start_date / finish_date: 'YYYY-MM-DD' | None
  tags: sorted list of strings
  favorite: bool

Providers keep their own scales; conversion is generic over the lib's
mediatype info (score_max / score_step), so pushing a canonical score
to MAL (10/1) rounds to an int and to Kitsu (5/0.25) quantizes to a
quarter-star -- "MAL and Kitsu will be rounded".
"""

import datetime
import re
import unicodedata

from hakubun.sync.models import NormalizedEntry, STATUSES

# statuses_dict display names / keys -> canonical status.
_STATUS_PATTERNS = (
    (re.compile(r'watch\w*ing|currents?|reading', re.I), 'watching'),
    (re.compile(r'complet', re.I), 'completed'),
    (re.compile(r'hold|paus', re.I), 'on_hold'),
    (re.compile(r'drop', re.I), 'dropped'),
    (re.compile(r'plan|ptw|ptr', re.I), 'plan'),
)


def canonical_status(value, statuses_dict=None):
    """Map a provider status (key or display name) to canonical."""
    if value is None:
        return None
    candidates = [str(value)]
    if statuses_dict and value in statuses_dict:
        candidates.append(str(statuses_dict[value]))
    for text in candidates:
        for pattern, canon in _STATUS_PATTERNS:
            if pattern.search(text):
                return canon
    return None


def provider_status(canonical, statuses_dict):
    """Map a canonical status back to the provider's own key."""
    if canonical is None or not statuses_dict:
        return None
    for key in statuses_dict:
        if canonical_status(key, statuses_dict) == canonical:
            return key
    return None


def canonical_score(value, score_max):
    """Provider score -> canonical 0-10 float (None/0 -> None)."""
    if not value or not score_max:
        return None
    return round(float(value) * 10.0 / score_max, 2)


def provider_score(value, score_max, score_step):
    """Canonical 0-10 -> provider scale, quantized to score_step."""
    if value is None:
        return 0
    raw = float(value) * score_max / 10.0
    step = score_step or 1
    quantized = round(raw / step) * step
    quantized = min(max(quantized, 0), score_max)
    if float(step).is_integer():
        return int(round(quantized))
    return round(quantized, 2)


def canonical_date(value):
    """datetime/date/str -> 'YYYY-MM-DD' | None."""
    if value is None:
        return None
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.strftime('%Y-%m-%d')
    text = str(value).strip()
    return text or None


def normalize_show(provider, show, mediainfo, external_ids=None):
    """Build a NormalizedEntry from a trackma show dict.

    `mediainfo` is the lib's mediatype dict (score_max, statuses_dict).
    Missing fields normalize to None/empty -- unknown, never zero-ish:
    an unknown episode count must not read as "0 episodes".
    """
    statuses_dict = (mediainfo or {}).get('statuses_dict') or {}
    score_max = (mediainfo or {}).get('score_max')
    start = show.get('start_date')
    year = None
    if isinstance(start, (datetime.datetime, datetime.date)):
        year = start.year
    user = {
        'score': canonical_score(show.get('my_score'), score_max),
        'progress': show.get('my_progress') or 0,
        'status': canonical_status(show.get('my_status'), statuses_dict),
        'notes': show.get('my_notes') or None,
        'start_date': canonical_date(show.get('my_start_date')),
        'finish_date': canonical_date(show.get('my_finish_date')),
        'tags': sorted(t.strip() for t in (show.get('my_tags') or '').split(',')
                       if t.strip()) if isinstance(show.get('my_tags'), str)
                else sorted(show.get('my_tags') or []),
        'favorite': bool(show.get('my_favorite', False)),
    }
    return NormalizedEntry(
        provider=provider,
        provider_id=str(show['id']),
        title=show.get('title') or '',
        media_type=(mediainfo or {}).get('mediatype', 'anime'),
        aliases=list(show.get('aliases') or []),
        year=year,
        total=show.get('total') or None,
        airing_status=str(show['status']) if show.get('status') is not None
                      else None,
        external_ids={k: str(v) for k, v in (external_ids or {}).items()
                      if v},
        user=user,
    )


_TITLE_JUNK = re.compile(r'[\W_]+', re.UNICODE)


def normalize_title(title):
    """NFKC + casefold + strip punctuation, keeping word characters of
    every script -- a native title like 葬送のフリーレン must survive
    normalization (an earlier latin-only regex reduced CJK titles to
    empty strings, breaking matching entirely for users whose AniList
    title language is Native). NFKC also unifies full-width forms."""
    text = unicodedata.normalize('NFKC', title or '')
    return _TITLE_JUNK.sub(' ', text.casefold()).strip()
