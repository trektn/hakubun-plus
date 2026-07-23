"""Build a read-only display overlay for the main list from multisync's
reconciled local state.

The multisync database keeps one reconciled value per field per entity
(local_state), composed from every provider per the ownership matrix.
This turns that into `{active_provider_show_id: {my_field: value}}`,
expressed in the ACTIVE account's own representation (score on its
scale, status as its key, dates as date objects) so the existing list
model can display it unchanged. Read-only: it never writes anything.

The point (docs/multisync.md, 'local-first'): while signed into Kitsu
you can see AniList's rating and MAL's progress in your list, because
local -- not whichever account you happen to be signed into --
orchestrates who owns what.
"""

import datetime

from hakubun.sync import normalize
from hakubun.sync.models import USER_FIELDS


def _to_provider_value(field, value, mediainfo):
    """Canonical field value -> the ACTIVE provider's my_* value, i.e.
    what the list model expects to render for that column."""
    if value is None:
        return None
    if field == 'score':
        return normalize.provider_score(
            value, mediainfo.get('score_max', 10),
            mediainfo.get('score_step', 1))
    if field == 'status':
        return normalize.provider_status(
            value, mediainfo.get('statuses_dict') or {})
    if field in ('start_date', 'finish_date'):
        return normalize.provider_date(value)
    if field == 'tags':
        return ', '.join(value) if isinstance(value, list) else value
    # progress, rewatches, favorite, notes: used as-is.
    return value


# canonical field -> the show-dict my_* key the list model reads.
_MY_KEY = {
    'score': 'my_score', 'progress': 'my_progress',
    'status': 'my_status', 'start_date': 'my_start_date',
    'finish_date': 'my_finish_date', 'tags': 'my_tags',
    'rewatches': 'my_rewatched_times',
}


def build_overlay(store, active_provider, active_mediainfo, show_ids=None):
    """{active-provider show id: {my_field: reconciled display value}}.

    Only fields whose reconciled value actually DIFFERS from what the
    active provider itself reports are included, so an entity fully in
    sync adds nothing (no needless italic cues, no work). `show_ids`
    optionally restricts to the shows currently displayed.
    """
    wanted = set(str(s) for s in show_ids) if show_ids is not None else None
    overlay = {}
    for mapping in store.mappings_of_provider(active_provider):
        pid = mapping['provider_id']
        if wanted is not None and pid not in wanted:
            continue
        uid = mapping['uuid']
        local = store.local_get(uid)
        remote = store.remote_get(active_provider, pid)
        fields = {}
        for field in USER_FIELDS:
            my_key = _MY_KEY.get(field)
            if my_key is None or field not in local:
                continue
            canonical = local[field][0]
            # Skip fields already matching what this account holds --
            # nothing to override, nothing to flag.
            if field in remote and _eqish(remote[field][0], canonical):
                continue
            value = _to_provider_value(field, canonical, active_mediainfo)
            if value is not None:
                fields[my_key] = value
        if fields:
            # store keys provider_id as text; the list model keys shows
            # by their own id type (usually int). Expose both so the
            # lookup in the model hits regardless.
            overlay[pid] = fields
            if pid.isdigit():
                overlay[int(pid)] = fields
    return overlay


def _eqish(a, b):
    from hakubun.sync.diff import eq
    return eq(a, b)
