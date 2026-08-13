"""Identity inspector: given one provider + id, show exactly how
multisync resolved it (or didn't), what it links to on the other
providers, how each link was made, and the raw data behind every
field -- every fact here is a direct readout of the store, not a
re-derivation, so it can be used to audit the resolver's own claims.
"""

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional

from hakubun.sync.models import USER_FIELDS


@dataclass
class MappingRow:
    provider: str
    provider_id: str
    confirmed: bool
    via: Optional[str]


@dataclass
class FieldRow:
    field: str
    local: Any
    per_provider: Dict[str, Dict[str, Any]]   # provider -> {remote, base}
    # The field's synchronization policy (models.FieldPolicy) -- why
    # sync does what it does for this field, kept deliberately separate
    # from the identity explanation (how the entry was linked): those
    # are independent layers.
    policy: Any = None


@dataclass
class InspectionResult:
    provider: str
    provider_id: str
    found: bool
    note: str = ''
    uuid: Optional[str] = None
    title: Optional[str] = None
    aliases: List[str] = dc_field(default_factory=list)
    media_type: Optional[str] = None
    year: Optional[int] = None
    total: Optional[int] = None
    provider_only: Optional[str] = None
    mappings: List[MappingRow] = dc_field(default_factory=list)
    fields: List[FieldRow] = dc_field(default_factory=list)
    atlas_hint: Dict[str, str] = dc_field(default_factory=dict)
    # {provider: which community database said so} for atlas_hint.
    # See sync/relations.py.
    atlas_sources: Dict[str, str] = dc_field(default_factory=dict)
    identity_issue: Optional[dict] = None


def atlas_label(result):
    """What to call the atlas in the UI, for this particular result.

    Naming the database matters: community id databases are not all
    equally authoritative, so a flat "atlas says" would hide the one
    thing the user needs to weigh.
    """
    sources = sorted({result.atlas_sources.get(p)
                      for p in result.atlas_hint
                      if result.atlas_sources.get(p)})
    if len(sources) == 1:
        return '%s atlas' % sources[0]
    if sources:
        return 'id atlas (%s)' % ', '.join(sources)
    return 'id atlas'


def inspect_entry(store, provider, provider_id, atlas=None):
    """Look up one (provider, provider_id) pair. `atlas` (a
    RelationsAtlas, e.g. engine.identity.atlas) is optional and, when
    given, its independent opinion is always reported -- even when the
    entry already resolved -- so a mismatch between what's mapped and
    what the community atlas says is visible, not hidden.
    """
    provider_id = str(provider_id)
    atlas_hint = dict(atlas.lookup(provider, provider_id)) if atlas else {}
    atlas_sources = (dict(atlas.lookup_sources(provider, provider_id))
                     if atlas else {})

    mapping = store.mapping_for(provider, provider_id)
    if mapping is None:
        issue = store.identity_get(provider, provider_id)
        media_type = None
        if issue is not None:
            note = ('Not linked yet -- waiting on a decision in the '
                    "Identity tab (status: %s)." % issue['status'])
            media_type = (issue.get('entry') or {}).get('media_type')
        else:
            note = ('No record of %s id %s. Run Fetch changes first, '
                    'or this id is not in your list.' % (provider,
                                                         provider_id))
        return InspectionResult(provider=provider, provider_id=provider_id,
                                found=False, note=note,
                                media_type=media_type,
                                atlas_hint=atlas_hint,
                                atlas_sources=atlas_sources,
                                identity_issue=issue)

    uid = mapping['uuid']
    ent = store.get_entity(uid) or {}
    mappings = sorted(
        (MappingRow(provider=m['provider'], provider_id=m['provider_id'],
                    confirmed=bool(m['confirmed']), via=m.get('via'))
         for m in store.mappings_of(uid)),
        key=lambda m: m.provider)

    local = store.local_get(uid)
    remote_by_provider = {m.provider: store.remote_get(m.provider,
                                                        m.provider_id)
                          for m in mappings}
    base_by_provider = {m.provider: store.base_get(uid, m.provider)
                        for m in mappings}
    ownership = store.ownership()
    fields = []
    for f in USER_FIELDS:
        per = {}
        for m in mappings:
            remote, base = remote_by_provider[m.provider], \
                base_by_provider[m.provider]
            if f in remote or f in base:
                per[m.provider] = {'remote': remote.get(f, (None, None))[0],
                                   'base': base.get(f)}
        fields.append(FieldRow(field=f, local=local.get(f, (None, None))[0],
                               per_provider=per, policy=ownership.get(f)))

    return InspectionResult(
        provider=provider, provider_id=provider_id, found=True,
        note='Resolved.', uuid=uid, title=ent.get('title'),
        aliases=store.entity_aliases(uid), media_type=ent.get('media_type'),
        year=ent.get('year'), total=ent.get('total'),
        provider_only=ent.get('provider_only'), mappings=mappings,
        fields=fields, atlas_hint=atlas_hint, atlas_sources=atlas_sources)
