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
import functools
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


def provider_date(value):
    """Canonical 'YYYY-MM-DD' -> datetime.date | None -- the inverse
    of canonical_date, for the PUSH direction. The libs were built for
    trackma's native show dicts, where date fields are datetime.date
    objects: libanilist's _date2dict reads .year/.month/.day off the
    value directly (and only guards TypeError/ValueError, so a str
    escapes as AttributeError -- \"'str' object has no attribute
    'year'\"). Handing them real date objects is correct for every
    lib: AniList gets its attributes, MAL's urlencode renders a date
    object as ISO anyway. Unparseable input degrades to None (= clear
    the date) rather than crashing the whole push."""
    if value is None or isinstance(value, datetime.date):
        # note: datetime.datetime is a date subclass; both pass through
        return value
    try:
        return datetime.date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def progress_complete(value, scale):
    """True when `value` completes a `scale`-episode structure."""
    return bool(scale) and value is not None and value >= scale


def progress_convert(value, from_scale, to_scale):
    """View a progress value through another episode structure.
    Completion maps to the target's own total (1/1 movie == 4/4
    listing); zero is zero everywhere; equal or unknown structures
    pass through. Returns None when genuinely incomparable --
    partial progress across differing structures."""
    if not value:
        return value or 0
    if from_scale == to_scale or not from_scale or not to_scale:
        return value
    if progress_complete(value, from_scale):
        return to_scale
    return None


def normalize_show(provider, show, mediainfo, external_ids=None):
    """Build a NormalizedEntry from a trackma show dict.

    `mediainfo` is the lib's mediatype dict (score_max, statuses_dict).
    Missing fields normalize to None/empty -- unknown, never zero-ish:
    an unknown episode count must not read as "0 episodes".
    """
    mi = mediainfo or {}
    statuses_dict = mi.get('statuses_dict') or {}
    score_max = mi.get('score_max')
    start = show.get('start_date')
    year = None
    if isinstance(start, (datetime.datetime, datetime.date)):
        year = start.year
    user = {
        'score': canonical_score(show.get('my_score'), score_max),
        'progress': show.get('my_progress') or 0,
        'rewatches': show.get('my_rewatched_times') or 0,
        'status': canonical_status(show.get('my_status'), statuses_dict),
        'notes': show.get('my_notes') or None,
        'start_date': canonical_date(show.get('my_start_date')),
        'finish_date': canonical_date(show.get('my_finish_date')),
        'tags': sorted(t.strip() for t in (show.get('my_tags') or '').split(',')
                       if t.strip()) if isinstance(show.get('my_tags'), str)
                else sorted(show.get('my_tags') or []),
        'favorite': bool(show.get('my_favorite', False)),
    }
    # A field the provider cannot represent is OMITTED, never
    # fabricated as empty. A fetched entry claiming tags=[] from a
    # provider with no tags feature would enter remote_state as a real
    # (empty) remote value and diff against local -- and since a push
    # of the field can never actually be sent there, the provider's
    # nothingness would eventually read back as a phantom 'remote
    # edit' and pull local's real value away. Absence means absence
    # (the engine's 'provider can't represent this field' skip).
    # Capability defaults mirror ProviderAdapter.push's exactly; notes
    # and favorites additionally require the lib to actually report
    # them (none does yet -- the model keeps the fields for future
    # adapters).
    representable = {
        'score': mi.get('can_score', True),
        'status': mi.get('can_status', True),
        'progress': mi.get('can_update', True),
        'rewatches': mi.get('can_update', True),
        'start_date': mi.get('can_date', True),
        'finish_date': mi.get('can_date', True),
        'tags': mi.get('can_tag', False),
        'notes': 'my_notes' in show,
        'favorite': 'my_favorite' in show,
    }
    user = {f: v for f, v in user.items() if representable.get(f, True)}
    return NormalizedEntry(
        provider=provider,
        provider_id=str(show['id']),
        title=show.get('title') or '',
        media_type=(mediainfo or {}).get('mediatype', 'anime'),
        my_id=(str(show['my_id']) if show.get('my_id') is not None
               else None),
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


@functools.lru_cache(maxsize=16384)
def normalize_title(title):
    """NFKC + casefold + strip punctuation, keeping word characters of
    every script -- a native title like 葬送のフリーレン must survive
    normalization (an earlier latin-only regex reduced CJK titles to
    empty strings, breaking matching entirely for users whose AniList
    title language is Native). NFKC also unifies full-width forms.

    Memoized: identity resolution normalizes every entity's title and
    aliases again for EVERY unmatched fetched entry (candidate scan),
    which is quadratic in list size -- a first sync of a large list
    re-normalizes the same few thousand strings millions of times.
    Titles recur constantly and the cache is bounded, so this turns
    the whole scan into dict lookups."""
    text = unicodedata.normalize('NFKC', title or '')
    return _TITLE_JUNK.sub(' ', text.casefold()).strip()
