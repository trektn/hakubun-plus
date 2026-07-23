"""UI-agnostic glue between the multisync store and a tracker front-end.

Both the Qt and GTK UIs need the same two things, so the logic lives
here once (and is testable without any toolkit):

  * build the read-only DISPLAY OVERLAY for the active account's list --
    the reconciled per-field values, including an owned Score shown in
    the OWNER's own rating system (docs/multisync.md, 'local-first');
  * write a score the user entered in an owner's rating system straight
    to multisync local, so the next sync propagates it (the editing
    side of ownership-dependent scoring).

The UI keeps its own gating (is multi-sync enabled? is an account
signed in?) and its own repaint; this module only does the store work.
"""

from hakubun import utils
from hakubun.sync import normalize
from hakubun.sync.adapters import adapter_from_account
from hakubun.sync.overlay import build_overlay


def store_path(media_type):
    """The per-media-type multisync database path. Anime and manga get
    separate files so their id spaces never poison each other."""
    return utils.to_data_path('multisync-%s.db' % (media_type or 'anime'))


# Adapters constructed for the display overlay, keyed by
# (api, media_type, username). Constructing an adapter builds the
# whole lib (config/token loads), and the overlay is rebuilt on EVERY
# list repaint -- caching the adapters keeps that off the hot path.
# Safe to reuse: ProviderAdapter.mediainfo is a live property (it reads
# lib.media_info() on every access, so e.g. AniList's lazily-detected
# score format is never frozen), and the overlay only ever reads.
# Bounded by accounts x media types, i.e. tiny.
_adapter_cache = {}

# One SyncStore per database path, for the same reason: opening a
# store runs the whole schema script plus migration checks, which is
# real work to redo on every repaint. Safe to keep open: the sync
# window uses its OWN store instance and WAL mode lets this reader
# coexist with its writes (each autocommit read sees a fresh
# snapshot), the connection is created with check_same_thread=False
# and locked internally, and even the window's 'reset database' action
# recreates the tables in the same file, which a cached connection
# follows naturally.
_store_cache = {}


def _get_store(db):
    store = _store_cache.get(db)
    if store is None:
        from hakubun.sync.store import SyncStore
        store = _store_cache[db] = SyncStore(db)
    return store


def provider_mediainfo(accounts, media_type, msg):
    """{provider: mediainfo} for every configured account.

    Built locally (no network -- mediainfo is read off the lib), with
    per-account failures skipped rather than breaking the overlay. This
    is what lets an owned Score render, and be edited, in the owner's
    own rating system (AniList's 8.4 while signed into Kitsu).
    """
    info = {}
    for _num, account in accounts:
        api = account['api']
        if api in info:
            continue
        key = (api, media_type or 'anime', account.get('username'))
        adapter = _adapter_cache.get(key)
        if adapter is None:
            try:
                adapter = adapter_from_account(account, msg,
                                               media_type=media_type)
            except Exception:
                continue
            _adapter_cache[key] = adapter
        try:
            info[api] = adapter.mediainfo
        except Exception:
            continue
    return info


def build_list_overlay(accounts, active_api, active_mediainfo, media_type,
                       msg, show_ids=None):
    """Return (overlay, provider_mediainfo) for the active account's list.

    ({}, {}) when there is no multisync database yet -- i.e. nothing has
    ever been synced, so the list shows exactly the account's own values.
    Read-only: it never writes to the store.
    """
    db = store_path(media_type)
    if not utils.file_exists(db):
        return {}, {}
    pmi = provider_mediainfo(accounts, media_type, msg)
    overlay = build_overlay(_get_store(db), active_api, active_mediainfo,
                            provider_mediainfo=pmi, show_ids=show_ids)
    return overlay, pmi


def write_owned_score(media_type, active_api, showid, owner_raw,
                      owner_mediainfo):
    """Persist a score entered in the OWNER's rating system to multisync
    local, as user intent, so the next sync pushes it to the owner and
    every tracker (see engine.set_local_field).

    `owner_raw` is the value the score editor produced while configured
    with `owner_mediainfo` (the owner's scale). Returns True when the
    score was written; False to fall back to the active-account path
    (no database, or this account has no mapping for the show).
    """
    from hakubun.sync.engine import SyncEngine
    db = store_path(media_type)
    if not owner_mediainfo or not utils.file_exists(db):
        return False
    store = _get_store(db)
    mapping = store.mapping_for(active_api, str(showid))
    if not mapping:
        return False
    canonical = normalize.canonical_score(
        owner_raw, owner_mediainfo.get('score_max', 10))
    SyncEngine(store).set_local_field(mapping['uuid'], 'score', canonical)
    return True
