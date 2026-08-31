"""Provider adapters: uniform fetch()/push() over the existing lib/
classes (which are used as-is, per the design: providers are not
rewritten).

An adapter owns one provider account. fetch() returns NormalizedEntry
objects; push() converts canonical values back to the provider's scales
and calls lib.update_show() with a minimal trackma-style item dict.
"""

import time

from hakubun import utils
from hakubun.sync import normalize
from hakubun.sync.models import NormalizedEntry, SyncCancelled


class AdapterError(Exception):
    """Provider communication failed (network, auth, API)."""


# Conservative minimum seconds between consecutive pushes to one
# provider, to keep a bulk sync under each API's request-rate limit.
# AniList publishes 90 req/min (and has run a degraded 30/min mode);
# 1.5s (~40/min) stays well clear of the former, and the retry loop
# below absorbs the occasional 429 from the latter. MAL/Kitsu are more
# lenient. (Values are conservative and easily tuned.)
_PUSH_INTERVAL = {'anilist': 1.5, 'mal': 0.5, 'kitsu': 1.0}
_DEFAULT_PUSH_INTERVAL = 1.0
_RATE_LIMIT_RETRIES = 5
_RATE_LIMIT_DEFAULT_WAIT = 60   # AniList's rate window is 60s


def is_rate_limited(error):
    """True for a provider rate-limit error, whether flagged by the lib
    (libanilist sets error.rate_limited) or only recognizable by its
    message (429 / 'Too Many Requests')."""
    if getattr(error, 'rate_limited', False):
        return True
    text = str(error)
    return '429' in text or 'Too Many Requests' in text


# Web URL templates, %-formatted with the 'mt' and 'id' keys. MAL and
# AniList route canonically by numeric id -- matches the exact patterns
# hakubun/lib/libmal.py and libanilist.py already build for their own
# 'url' fields. Kitsu's *canonical* URL uses a title slug (which the
# lib classes fetch as part of full show details, not something a bare
# identity mapping carries), but its frontend also resolves the bare
# numeric id directly, so that's used here as a pragmatic fallback
# rather than requiring a slug just to open a page in a browser.
_WEB_URL_TEMPLATES = {
    'mal': 'https://myanimelist.net/%(mt)s/%(id)s',
    'anilist': 'https://anilist.co/%(mt)s/%(id)s',
    'kitsu': 'https://kitsu.app/%(mt)s/%(id)s',
}


def web_url(provider, media_type, provider_id):
    """Best-effort link to the provider's own page for one entry, or
    None when the provider isn't recognized. `media_type` defaults to
    'anime' for anything other than literally 'manga' -- callers that
    only have a provider_id and no confirmed type yet (e.g. opening an
    unresolved Identity row) still get a usable link."""
    template = _WEB_URL_TEMPLATES.get(provider)
    if not template or not provider_id:
        return None
    mt = 'manga' if media_type == 'manga' else 'anime'
    return template % {'mt': mt, 'id': provider_id}


class ProviderAdapter:
    """Wraps a constructed lib instance (duck-typed)."""

    # show-dict keys that may carry another provider's id.
    _EXTERNAL_ID_KEYS = {'mal_id': 'mal'}

    def __init__(self, name, lib, on_userconfig=None):
        self.name = name
        self.lib = lib
        self._infolist = []
        self._last_push = 0.0
        # Set by freeze_mediainfo() for the duration of one plan; None
        # everywhere else so the live-read guarantee below still holds.
        self._mi_frozen = None
        # Scaled up after a rate-limit hit so the rest of the run
        # paces itself slower and stops tripping the limit repeatedly.
        self._interval_scale = 1.0
        # lib.signals is a *class* attribute: a freshly constructed lib
        # shares whatever callbacks the running app's Data instance
        # connected, so emitting from this instance would invoke the
        # app's cache handlers against the wrong lib. Shadow it with an
        # instance dict: info payloads are captured here (legacy Kitsu
        # delivers titles/aliases/mal_id only via this signal), token
        # refreshes go to on_userconfig.
        lib.signals = {
            'show_info_changed': self._capture_info,
            'userconfig_changed': on_userconfig or (lambda: None),
        }

    def _capture_info(self, infolist):
        self._infolist.extend(infolist or [])

    @property
    def mediainfo(self):
        """Always read live, never cached. AniList accounts pick their
        own score format -- 0-3 smileys, 1-5 stars, 1-10, 1-10 with .1
        decimals, or 1-100 -- and libanilist only detects which one a
        given account actually uses *inside* fetch_list(), the first
        time it talks to the server (and the format can change on the
        website at any time after that). A snapshot taken once at
        adapter construction, before any fetch ever ran, would freeze
        in whatever was true then -- usually just the class's
        hardcoded 100/1 default -- and never notice the correction.
        media_info() is a cheap dict lookup, but "cheap" is relative:
        planning reads it O(entities x fields x providers) times, so a
        plan explicitly freezes it for its own duration (see
        freeze_mediainfo) -- the format cannot change mid-plan anyway,
        since only a fetch can learn a new one."""
        if self._mi_frozen is not None:
            return self._mi_frozen
        return self._read_mediainfo()

    def _read_mediainfo(self):
        info = dict(self.lib.media_info())
        # The real libs' media_info() (mediatypes[type]) carries no
        # 'mediatype' key -- only api_info does -- so without this,
        # normalize.normalize_show()'s `.get('mediatype', 'anime')`
        # would type EVERY entry as anime, even in a manga sync.
        if 'mediatype' not in info:
            info['mediatype'] = getattr(self.lib, 'mediatype', 'anime')
        return info

    def freeze_mediainfo(self):
        """Pin mediainfo to one read for a bounded, read-only stretch
        (SyncEngine.plan). Always paired with thaw_mediainfo() in a
        finally, so nothing outside that stretch can ever observe a
        stale score format -- the bug the live property exists to
        prevent."""
        self._mi_frozen = self._read_mediainfo()

    def thaw_mediainfo(self):
        self._mi_frozen = None

    # -- inbound -------------------------------------------------------

    def fetch(self):
        """Download the account's list -> [NormalizedEntry]."""
        self._infolist = []
        try:
            self.lib.check_credentials()
            showlist = self.lib.fetch_list()
        except utils.APIError as e:
            raise AdapterError('%s: %s' % (self.name, e)) from e
        shows = list(showlist.values() if isinstance(showlist, dict)
                     else showlist)
        self._merge_infolist(shows)
        entries = []
        for show in shows:
            external = {canon: show.get(key)
                        for key, canon in self._EXTERNAL_ID_KEYS.items()
                        if show.get(key) and canon != self.name}
            entries.append(normalize.normalize_show(
                self.name, show, self.mediainfo, external))
        return entries

    def _merge_infolist(self, shows):
        """Legacy Kitsu's library entries carry no titles at all -- the
        media details (title, aliases, mal_id) arrive as a separate
        infolist via show_info_changed, and data.py normally merges
        them. Do the same here or its entries normalize as empty-titled
        husks that can never be identified."""
        if not self._infolist or not hasattr(self.lib, 'merge'):
            return
        by_id = {info['id']: info for info in self._infolist}
        for show in shows:
            info = by_id.get(show.get('id'))
            if info is not None and not show.get('title'):
                self.lib.merge(show, info)

    def search(self, criteria):
        try:
            results = self.lib.search(criteria, utils.SearchMethod.KW)
        except utils.APIError as e:
            raise AdapterError('%s: %s' % (self.name, e)) from e
        return [normalize.normalize_show(self.name, show, self.mediainfo)
                for show in results]

    def supports_mal_id_lookup(self):
        return hasattr(self.lib, 'find_by_mal_id')

    def resolve_by_mal_id(self, mal_id, cancel=None):
        """Exact reverse lookup (SyncEngine._discover_cross_ids): does
        THIS provider have an entry for a MAL id already known from
        elsewhere? Returns the provider's own media id (str), or None
        when the provider genuinely doesn't have it -- distinct from
        `supports_mal_id_lookup()` being False, which means "can't even
        ask". Paced/rate-limited like push()/add(); a real failure
        (auth, network, rate limit) raises AdapterError, same as those."""
        if not self.supports_mal_id_lookup():
            return None
        found = self._send(self.lib.find_by_mal_id, mal_id, cancel, None)
        return str(found) if found is not None else None

    # -- outbound ------------------------------------------------------

    def _sleep(self, seconds, cancel):
        """Interruptible sleep: check cancellation ~5x/sec so a long
        rate-limit wait doesn't pin the whole sync (or the window's
        close) for a full minute."""
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            if cancel is not None and cancel():
                raise SyncCancelled()
            time.sleep(min(0.2, remaining))

    def _pace(self, cancel):
        interval = _PUSH_INTERVAL.get(self.name, _DEFAULT_PUSH_INTERVAL) \
            * self._interval_scale
        gap = interval - (time.time() - self._last_push)
        if gap > 0:
            self._sleep(gap, cancel)
        self._last_push = time.time()

    def _build_item(self, provider_id, changes, title=None, my_id=None):
        """{field: value} -> (lib item dict, {field: provider-scale value
        actually included}). Shared by push() and add(): both send the
        same per-field conversions/capability gating to the lib, just
        through a different lib call (update vs create)."""
        item = {'id': self._coerce_id(provider_id),
                'my_id': my_id}
        sent = {}
        statuses_dict = self.mediainfo.get('statuses_dict') or {}
        for field, value in changes.items():
            if field == 'score' and self.mediainfo.get('can_score', True):
                converted = normalize.provider_score(
                    value, self.mediainfo.get('score_max', 10),
                    self.mediainfo.get('score_step', 1))
                item['my_score'] = converted
                sent['score'] = normalize.canonical_score(
                    converted, self.mediainfo.get('score_max', 10))
            elif field == 'progress' and self.mediainfo.get('can_update',
                                                            True):
                item['my_progress'] = int(value or 0)
                sent['progress'] = int(value or 0)
            elif field == 'rewatches' and self.mediainfo.get('can_update',
                                                            True):
                item['my_rewatched_times'] = int(value or 0)
                sent['rewatches'] = int(value or 0)
            elif field == 'status' and self.mediainfo.get('can_status',
                                                          True):
                converted = normalize.provider_status(value, statuses_dict)
                if converted is None:
                    continue
                item['my_status'] = converted
                sent['status'] = value
            elif field in ('start_date', 'finish_date') and \
                    self.mediainfo.get('can_date', True):
                # Libs expect datetime.date here, not the canonical
                # ISO string (see normalize.provider_date).
                item['my_' + field] = normalize.provider_date(value)
                sent[field] = value
            elif field == 'tags' and self.mediainfo.get('can_tag', False):
                item['my_tags'] = ', '.join(value or [])
                sent['tags'] = sorted(value or [])
            elif field == 'notes':
                # No lib exposes notes yet; kept in the model for the
                # 'individual' policy and future adapters.
                continue
        # Every lib's update_show()/add_show()/delete_show() logs
        # item['title'] (purely informational -- the actual API payload
        # is built from id/my_* only) but accesses it unconditionally,
        # so a missing key crashes the whole call with a bare
        # KeyError('title') ("Apply failed: 'title'"). This adapter's
        # item is a minimal {id, my_*} patch with no title of its own;
        # the caller (SyncEngine.apply, from FieldChange.title) is
        # expected to supply the show's title for logging.
        item['title'] = title or ''
        return item, sent

    def creation_values(self, changes):
        """Canonical values a newly-created provider entry must carry.

        Field policy may intentionally omit a field, but some provider
        create schemas still require it.  Such requirements belong to the
        adapter capability data, not to identity or reconciliation.  The
        planner calls this too, so a required default is disclosed in the
        preview instead of being a hidden API-side mutation.
        """
        values = dict(changes)
        default_status = self.mediainfo.get('add_default_status')
        if default_status and not values.get('status'):
            values['status'] = default_status
        return values

    def can_write(self, field):
        """Whether this provider can actually accept ``field``.

        Planning and payload construction must share this answer.  Relying
        only on ``_build_item`` to drop unsupported fields leaves a previewed
        operation that Apply silently turns into an empty payload -- exactly
        the recurring no-op a stale Kitsu date snapshot exposed.
        """
        info = self.mediainfo
        return {
            'score': info.get('can_score', True),
            'status': info.get('can_status', True),
            'progress': info.get('can_update', True),
            'rewatches': info.get('can_update', True),
            'start_date': info.get('can_date', True),
            'finish_date': info.get('can_date', True),
            'tags': info.get('can_tag', False),
            'notes': False,
            'favorite': False,
        }.get(field, True)

    def _send(self, lib_call, item, cancel, on_wait):
        """Retry wrapper shared by push()/add(): paces per provider and
        retries on a rate-limit (429) response, waiting the server's
        Retry-After when given. `on_wait(provider, seconds, attempt)`
        reports a wait; `cancel` (a `() -> bool`) aborts (raising
        SyncCancelled) between/within waits. Returns lib_call's result."""
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            self._pace(cancel)   # may raise SyncCancelled
            try:
                return lib_call(item)
            except utils.APIError as e:
                if is_rate_limited(e) and attempt < _RATE_LIMIT_RETRIES:
                    wait = getattr(e, 'retry_after', None) \
                        or _RATE_LIMIT_DEFAULT_WAIT
                    # Slow the rest of this run so we stop tripping it.
                    self._interval_scale = min(self._interval_scale * 1.5, 6.0)
                    if on_wait is not None:
                        on_wait(self.name, wait, attempt + 1)
                    self._sleep(wait, cancel)   # may raise SyncCancelled
                    continue
                raise AdapterError('%s: %s' % (self.name, e)) from e
            except Exception as e:
                # The libs were written for trackma's native show dicts
                # and read keys/types this adapter has to reconstruct;
                # several field-reported crashes came from exactly this
                # boundary (KeyError 'title', str-vs-date, KeyError
                # 'my_id'). An unexpected exception here must degrade to
                # a per-provider failure (engine.apply isolates
                # AdapterError, other providers proceed, changes
                # re-plan) -- never kill the entire apply.
                raise AdapterError('%s: %s: %s'
                                  % (self.name, type(e).__name__, e)) from e
        raise AdapterError('%s: still rate limited after %d retries'
                          % (self.name, _RATE_LIMIT_RETRIES))

    def push(self, provider_id, changes, title=None, my_id=None,
            on_wait=None, cancel=None):
        """Push canonical {field: value} changes to an EXISTING entry.

        Returns the provider-scale values actually sent (what the
        remote is expected to hold afterwards, e.g. the rounded score).
        Unsupported fields are silently projected out.

        `my_id` is the provider's library-entry id (persisted from the
        last fetch as remote-state '_my_id'): Kitsu, both backends,
        addresses updates by it -- item['my_id'], read unconditionally
        -- while MAL/AniList only use the media id.
        """
        item, sent = self._build_item(provider_id, changes, title, my_id)
        if not sent:
            return {}
        self._send(self.lib.update_show, item, cancel, on_wait)
        return sent

    def add(self, provider_id, changes, title=None, on_wait=None,
           cancel=None):
        """Create a NEW library entry on a provider that has none (see
        membership.py: whether an entry belongs on a tracker is its own
        question, answered there). Same shape as push(), but calls the
        lib's add_show(); no my_id since there's no existing entry to
        address.

        Returns (sent, my_id): `my_id` is the provider's new
        library-entry id when it reports one (Kitsu, both backends),
        None otherwise (MAL/AniList key updates on the media id alone --
        there's no separate library-entry id to learn)."""
        if not self.mediainfo.get('can_add', True):
            return {}, None
        item, sent = self._build_item(
            provider_id, self.creation_values(changes), title)
        if not sent:
            return {}, None
        new_id = self._send(self.lib.add_show, item, cancel, on_wait)
        return sent, (str(new_id) if new_id is not None else None)

    def can_remove(self):
        """Whether this provider supports deleting a library entry.

        Defaults to FALSE when the lib says nothing: every lib that can
        really delete declares `can_delete` in its mediatypes dict, and
        the base lib.delete_show only raises NotImplementedError. An
        optimistic default would turn a silently unimplemented delete
        into a Mirror that reports removals it never performed."""
        return bool(self.mediainfo.get('can_delete', False))

    def remove(self, provider_id, title=None, my_id=None, on_wait=None,
               cancel=None):
        """Delete an EXISTING library entry from this provider.

        `my_id` is the provider's library-entry id, persisted from the
        last fetch as remote-state '_my_id': AniList and both Kitsu
        backends address a delete by it, MAL by the media id. Paced and
        rate-limited exactly like push()/add(); a real failure raises
        AdapterError so the engine can isolate it to this provider.

        Returns True when the delete was sent, False when the provider
        cannot delete at all (see can_remove) -- never a silent no-op
        reported as success.
        """
        if not self.can_remove():
            return False
        item = {'id': self._coerce_id(provider_id), 'my_id': my_id,
                'title': title or ''}
        self._send(self.lib.delete_show, item, cancel, on_wait)
        return True

    def values_equivalent(self, field, a, b):
        """True when two canonical values project to the same value on
        this provider's scale. Keeps quantization residue from looping:
        after pushing a canonical 8.4 to a 10/1 provider, the remote
        echoes 8.0 -- equivalent here, so nothing re-pushes and the
        canonical 8.4 survives locally."""
        if field == 'score':
            smax = self.mediainfo.get('score_max', 10)
            step = self.mediainfo.get('score_step', 1)
            return (normalize.provider_score(a, smax, step)
                    == normalize.provider_score(b, smax, step))
        return normalize.values_equal(field, a, b)

    @staticmethod
    def _coerce_id(provider_id):
        text = str(provider_id)
        return int(text) if text.isdigit() else text

    def logout(self):
        try:
            self.lib.logout()
        except Exception:
            pass


def adapter_from_account(account, messenger, userconfig=None,
                        media_type=None):
    """Construct an adapter for an accounts.py account dict, mirroring
    data.py's lib construction.

    The account's persisted userconfig (user.json) is essential: it
    holds the stored OAuth access/refresh tokens and the mediatype. A
    throwaway userconfig makes OAuth backends re-redeem the original,
    already-consumed authorization code (MAL answers HTTP 400
    invalid_grant). Token refreshes the lib performs are written back.

    `media_type`, when given, is the mediatype this adapter is built
    for regardless of what the account was last used as -- a multisync
    of anime fetches every provider's ANIME list, even a Kitsu account
    whose stored mediatype happens to be manga (all three providers
    keep both lists under one login). The account's OWN stored
    mediatype is never overwritten by this: token refreshes are saved
    back with the account's original mediatype preserved, so the main
    app's view of the account is untouched.
    """
    libbase = account['api']
    libname = 'lib' + libbase
    if libbase == 'kitsu':
        # Same backend selection as data.py: the 'kitsu_api' setting
        # picks legacy REST or GraphQL for the same kitsu account.
        config = utils.parse_config(utils.to_config_path('config.json'),
                                    utils.config_defaults)
        if config.get('kitsu_api') == 'graphql':
            libname = 'libkitsu_graphql'
    modulename = 'hakubun.lib.%s' % libname
    __import__(modulename)
    import sys as _sys
    libclass = getattr(_sys.modules[modulename], libname)

    userfolder = '%s.%s' % (account['username'], account['api'])
    userconfig_file = utils.to_data_path(userfolder, 'user.json')
    if userconfig is None:
        userconfig = utils.parse_config(userconfig_file,
                                        utils.userconfig_defaults)

    stored_mediatype = userconfig.get('mediatype')
    # The lib gets a COPY with the target mediatype forced, so token
    # refreshes (written into this copy) can be persisted without
    # clobbering the account's own stored mediatype.
    lib_userconfig = dict(userconfig)
    if media_type is not None:
        lib_userconfig['mediatype'] = media_type

    lib = libclass(messenger, account, lib_userconfig)

    def save():
        merged = dict(lib_userconfig)
        if stored_mediatype is not None:
            merged['mediatype'] = stored_mediatype
        else:
            merged.pop('mediatype', None)
        utils.save_config(merged, userconfig_file)

    return ProviderAdapter(account['api'], lib, on_userconfig=save)
