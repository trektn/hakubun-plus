"""Provider adapters: uniform fetch()/push() over the existing lib/
classes (which are used as-is, per the design: providers are not
rewritten).

An adapter owns one provider account. fetch() returns NormalizedEntry
objects; push() converts canonical values back to the provider's scales
and calls lib.update_show() with a minimal trackma-style item dict.
"""

from hakubun import utils
from hakubun.sync import normalize
from hakubun.sync.models import NormalizedEntry


class AdapterError(Exception):
    """Provider communication failed (network, auth, API)."""


class ProviderAdapter:
    """Wraps a constructed lib instance (duck-typed)."""

    # show-dict keys that may carry another provider's id.
    _EXTERNAL_ID_KEYS = {'mal_id': 'mal'}

    def __init__(self, name, lib):
        self.name = name
        self.lib = lib
        self.mediainfo = dict(lib.media_info())

    # -- inbound -------------------------------------------------------

    def fetch(self):
        """Download the account's list -> [NormalizedEntry]."""
        try:
            self.lib.check_credentials()
            showlist = self.lib.fetch_list()
        except utils.APIError as e:
            raise AdapterError('%s: %s' % (self.name, e)) from e
        entries = []
        for show in showlist.values() if isinstance(showlist, dict) \
                else showlist:
            external = {canon: show.get(key)
                        for key, canon in self._EXTERNAL_ID_KEYS.items()
                        if show.get(key) and canon != self.name}
            entries.append(normalize.normalize_show(
                self.name, show, self.mediainfo, external))
        return entries

    def search(self, criteria):
        try:
            results = self.lib.search(criteria, utils.SearchMethod.KW)
        except utils.APIError as e:
            raise AdapterError('%s: %s' % (self.name, e)) from e
        return [normalize.normalize_show(self.name, show, self.mediainfo)
                for show in results]

    # -- outbound ------------------------------------------------------

    def push(self, provider_id, changes):
        """Push canonical {field: value} changes to the provider.

        Returns the provider-scale values actually sent (what the
        remote is expected to hold afterwards, e.g. the rounded score).
        Unsupported fields are silently projected out.
        """
        item = {'id': self._coerce_id(provider_id)}
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
            elif field == 'status' and self.mediainfo.get('can_status',
                                                          True):
                converted = normalize.provider_status(value, statuses_dict)
                if converted is None:
                    continue
                item['my_status'] = converted
                sent['status'] = value
            elif field in ('start_date', 'finish_date') and \
                    self.mediainfo.get('can_date', True):
                item['my_' + field] = value
                sent[field] = value
            elif field == 'tags' and self.mediainfo.get('can_tag', False):
                item['my_tags'] = ', '.join(value or [])
                sent['tags'] = sorted(value or [])
            elif field == 'notes':
                # No lib exposes notes yet; kept in the model for the
                # 'individual' policy and future adapters.
                continue
        if len(item) == 1:
            return {}
        try:
            self.lib.update_show(item)
        except utils.APIError as e:
            raise AdapterError('%s: %s' % (self.name, e)) from e
        return sent

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
        from hakubun.sync.diff import eq
        return eq(a, b)

    @staticmethod
    def _coerce_id(provider_id):
        text = str(provider_id)
        return int(text) if text.isdigit() else text

    def logout(self):
        try:
            self.lib.logout()
        except Exception:
            pass


def adapter_from_account(account, messenger, userconfig=None):
    """Construct an adapter for an accounts.py account dict, mirroring
    data.py's lib construction.

    The account's persisted userconfig (user.json) is essential: it
    holds the stored OAuth access/refresh tokens and the mediatype. A
    throwaway userconfig makes OAuth backends re-redeem the original,
    already-consumed authorization code (MAL answers HTTP 400
    invalid_grant). Token refreshes the lib performs are written back.
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

    lib = libclass(messenger, account, userconfig)

    # lib.signals is a *class* attribute: a freshly constructed lib
    # shares whatever callbacks the running app's Data instance
    # connected, so emitting from this instance would invoke the app's
    # cache handlers against the wrong lib (surfacing as "Call to
    # undefined signal" when they blow up) and could corrupt its info
    # cache. Shadow it with an instance dict: info-cache pings are
    # dropped, token refreshes persist to this account's user.json.
    lib.signals = {
        'show_info_changed': None,
        'userconfig_changed': lambda: utils.save_config(userconfig,
                                                        userconfig_file),
    }
    return ProviderAdapter(account['api'], lib)
