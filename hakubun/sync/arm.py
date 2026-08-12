"""Annict cross-provider ids, from the arm database.

https://github.com/SlashNephy/arm-supplementary (MIT) -- a maintained
superset of https://github.com/kawaiioverflow/arm, a JSON table stating
which MyAnimeList / AniList / Annict / Syoboi ids denote the same anime.
It is fetched at runtime and cached (see Engine.start), never bundled:
arm-supplementary is MIT but derives from an upstream that carries no
license at all, so this app doesn't redistribute it.

This exists because Annict cannot be linked any other way. It publishes
a MAL id for about two thirds of its works and no AniList id at all, and
its titles are Japanese-only -- `searchWorks` will not match a romaji or
English name -- so the title-similarity path identity resolution falls
back on is effectively dead for Annict. arm turns that into an exact
lookup.

Like the anime-relations atlas this feeds (sync/relations.py), these are
*anime* ids and must never be consulted for a manga entry: the id spaces
overlap, so a manga id would silently collide with an unrelated anime's
row. IdentityResolver._external_ids already enforces that.

Only rows carrying an annict_id are ingested. arm also relates works
this app has no backend for (AniDB, Anime-Planet, AniSearch, LiveChart)
and covers plenty of MAL/AniList pairs anime-relations doesn't -- but
widening it to those would change how *existing* mal/kitsu/anilist
accounts resolve identity, which is a much bigger blast radius than
adding a provider that currently has none. Easy to widen later.
"""

import json
import os
import shutil
import urllib.error
import urllib.request

from hakubun import utils

# arm column -> this app's provider name. arm has no Kitsu column.
PROVIDER_KEYS = (
    ('mal_id', 'mal'),
    ('anilist_id', 'anilist'),
    ('annict_id', 'annict'),
)

FILENAME = 'arm.json'

# Seconds to wait on the download. utils.sync_file passes no timeout at
# all, which is survivable for anime-relations' 31KB but not for a
# multi-megabyte fetch on the startup path: a connection that is
# accepted and then never answered would hang the app launch outright,
# with nothing raised for the caller's except to catch.
DOWNLOAD_TIMEOUT = 20

# path -> (mtime, size, rows). build_engine() runs on every sync window
# open and the file is several megabytes; re-reading and re-parsing it
# each time is a visible stall for no benefit.
_cache = {}


def default_path():
    """User-provided copy first, then the runtime-synced one. There is
    no bundled fallback -- see the module docstring.

    Same contract as anime-relations.txt: a file placed in the config
    directory is the user's to manage and is never refreshed or
    overwritten (sync() bails out when it sees one).
    """
    candidates = (
        utils.to_config_path(FILENAME),
        utils.to_data_path(FILENAME),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[-1]


def sync(config, msg=None):
    """Download the database if the cached copy is missing or stale.

    Network work, so this belongs on startup (Engine.start) or a worker
    -- never in load(), which the sync window calls on the UI thread.
    Returns True when a fresh copy was written.
    """
    interval_days = config.get('arm_time')
    if not interval_days or not config.get('arm_url'):
        return False   # explicitly disabled

    # A user-provided copy is theirs to manage; never overwrite it.
    if os.path.isfile(utils.to_config_path(FILENAME)):
        return False

    path = utils.to_data_path(FILENAME)
    if (utils.file_exists(path)
            and not utils.file_older_than(path, interval_days * 86400)):
        return False

    if msg:
        msg.info("Syncing Annict id database...")
    return _download(config['arm_url'], path, msg)


def _download(url, path, msg=None):
    """Fetch to a temp file and rename into place.

    Not utils.sync_file: that copies straight onto the destination with
    no timeout, so an interrupted transfer leaves a truncated file
    behind -- and a truncated JSON array that still happens to parse is
    worse than a corrupt one, since it would quietly become a partial
    atlas rather than no atlas.
    """
    utils.make_dir(os.path.dirname(path))
    tmp_path = path + '.tmp'
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as r, \
                open(tmp_path, 'wb') as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp_path, path)
    except Exception as e:
        if msg:
            msg.debug("Couldn't fetch %s: %s" % (url, e))
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False
    return True


def load(path=None):
    """Rows as {provider: id}, ids as strings.

    Missing or unreadable file yields nothing: the atlas is an optional
    enrichment and everything still works without it.
    """
    path = path or default_path()
    try:
        stat = os.stat(path)
    except OSError:
        return []

    signature = (stat.st_mtime, stat.st_size)
    cached = _cache.get(path)
    if cached and cached[0] == signature:
        return cached[1]

    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []

    rows = []
    for entry in data if isinstance(data, list) else ():
        if not entry.get('annict_id'):
            continue
        ids = {}
        for arm_key, provider in PROVIDER_KEYS:
            value = entry.get(arm_key)
            if value in (None, '', 0):
                continue
            # arm's ids are JSON integers; the atlas keys on strings
            # (RelationsAtlas.lookup stringifies what it is asked
            # about), so a raw int here would simply never match.
            ids[provider] = str(value)
        # A row naming only Annict relates nothing to anything.
        if len(ids) >= 2:
            rows.append(ids)

    # Keyed by path, not by (path, mtime): a second path must not evict
    # the first, or two loads in one process re-parse each other's file
    # on every call.
    _cache[path] = (signature, rows)
    return rows
