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

from hakubun import utils
from hakubun.sync import normalize
from hakubun.sync.models import PolicyKind, USER_FIELDS
from hakubun.sync.normalize import values_equal


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


def build_overlay(store, active_provider, active_mediainfo,
                 provider_mediainfo=None, show_ids=None):
    """{active-provider show id: {my_field: reconciled display value}}.

    Only fields whose reconciled value actually DIFFERS from what the
    active provider itself reports are included, so an entity fully in
    sync adds nothing (no needless italic cues, no work). `show_ids`
    optionally restricts to the shows currently displayed.

    `provider_mediainfo` ({provider: mediainfo}) enables the ownership-
    dependent score display: when Score is owned by another provider,
    the cell shows the reconciled score in THAT provider's own rating
    system (AniList's 8.4, MAL's 8, Kitsu's 8.5) rather than the
    signed-in account's -- the '_score_display' / '_score_owner' keys
    -- so the list reflects who owns the rating, not who you're
    signed into.
    """
    wanted = set(str(s) for s in show_ids) if show_ids is not None else None
    ownership = store.ownership()
    score_owner = _score_owner_provider(ownership.get('score'),
                                        active_provider, provider_mediainfo)
    # Bulk-load everything up front: this runs on the UI thread on
    # every list repaint, so per-show queries (an N+1 pattern over the
    # entire visible list) are exactly what it cannot afford.
    mappings = [m for m in store.mappings_of_provider(active_provider)
                if wanted is None or m['provider_id'] in wanted]
    uids = [m['uuid'] for m in mappings]
    locals_many = store.local_get_many(uids)
    remotes_many = store.remote_get_many(
        active_provider, [m['provider_id'] for m in mappings])
    mappings_many = store.mappings_many(uids)
    wanted_uids = set(uids)
    entities = {e['uuid']: e for e, _aliases in
                store.entities_with_aliases() if e['uuid'] in wanted_uids}
    overlay = {}
    for mapping in mappings:
        pid = mapping['provider_id']
        uid = mapping['uuid']
        local = locals_many.get(uid, {})
        remote = remotes_many.get(pid, {})
        fields = {}
        for field in USER_FIELDS:
            my_key = _MY_KEY.get(field)
            if my_key is None or field not in local:
                continue
            canonical = local[field][0]
            # Skip fields already matching what this account holds --
            # nothing to override, nothing to flag.
            if field in remote and _eqish(field, remote[field][0],
                                          canonical):
                continue
            if field == 'progress':
                # The reconciled progress is in the LOCAL episode
                # structure; this account may list the work differently
                # (a 1-episode movie vs the local 4-episode listing).
                # Convert, or the cell renders impossible fractions
                # like '4 / 1'. Incomparable partials show the
                # account's own value (no override).
                r_scale = (remote.get('_total') or (None, 0))[0]
                ent_total = (entities.get(uid) or {}).get('total')
                value = normalize.progress_convert(canonical, ent_total,
                                                   r_scale)
            else:
                value = _to_provider_value(field, canonical,
                                           active_mediainfo)
            # Also skip when the value only differs BELOW this
            # account's own precision -- local 8.4 and the account's 8
            # both display as 8 on a 10/1 scale; an override there is
            # pure noise (an italic cue with nothing behind it).
            if field in remote:
                shown_remote = (remote[field][0] if field == 'progress'
                                else _to_provider_value(
                                    field, remote[field][0],
                                    active_mediainfo))
                if value == shown_remote:
                    continue
            if value is not None:
                fields[my_key] = value

        # Ownership-dependent score, expressed in the OWNER's own rating
        # system, for SHARED entries -- those that also exist on another
        # tracker, so an owner other than the active account legitimately
        # dictates the scale. Emitted even with no score yet, so you can
        # *rate* a shared-but-unrated entry in the owner's system (slide
        # to AniList's 8.4 while signed into Kitsu) rather than being
        # limited to the signed-in account's coarser steps. A platform-
        # specific entry (only the active tracker has it) is deliberately
        # excluded: nothing else owns it, so it keeps the active system.
        #   _score_display  friendly string for the cell (only when rated)
        #   _score_owner    the owning provider (drives editor + tooltip)
        #   _score_owner_raw  reconciled score in the owner's raw scale,
        #                     to seed the score editor (0 when unrated)
        #   _uuid           so an edit can address this entity in local
        if score_owner and _is_shared(mappings_many.get(uid, ()),
                                      active_provider):
            owner_mi = provider_mediainfo[score_owner]
            canonical = local.get('score', (None,))[0]
            owner_raw = (normalize.provider_score(
                canonical, owner_mi.get('score_max', 10),
                owner_mi.get('score_step', 1))
                if canonical is not None else 0)
            fields['_score_owner'] = score_owner
            fields['_score_owner_raw'] = owner_raw
            fields['_uuid'] = uid
            if canonical is not None:
                fields['_score_display'] = utils.score_to_display(
                    owner_raw, owner_mi)

        if fields:
            # store keys provider_id as text; the list model keys shows
            # by their own id type (usually int). Expose both so the
            # lookup in the model hits regardless.
            overlay[pid] = fields
            if pid.isdigit():
                overlay[int(pid)] = fields
    return overlay


def _eqish(field, a, b):
    return values_equal(field, a, b)


def _is_shared(entity_mappings, active_provider):
    """True when this entity is tracked on a provider OTHER than the
    active account -- i.e. genuinely cross-tracker. Only then does an
    owner other than the signed-in account get to dictate the rating
    system; a purely platform-specific entry (only the active account
    has it) keeps the active account's own system."""
    return any(m['provider'] != active_provider
               for m in entity_mappings)


def _score_owner_provider(policy, active_provider, provider_mediainfo):
    """The provider whose rating system the Score column should use, or
    None to keep the active account's. Only when Score is owned by a
    specific OTHER provider whose mediainfo we actually have."""
    if policy is None or provider_mediainfo is None:
        return None
    if policy.kind is not PolicyKind.PROVIDER:
        return None
    if policy.provider == active_provider:
        return None
    if policy.provider not in provider_mediainfo:
        return None
    return policy.provider
