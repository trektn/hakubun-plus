"""Multisync: local-first, reconciliation-based multi-provider sync.

See docs/multisync.md for the authoritative design. Mental model: git.
The local database is the repository, providers are remotes, fetch
snapshots remote state, a 3-way diff against the last-synced base feeds
per-field ownership policies, conflicts go to the user, applying is a
commit (event log) followed by pushes to the providers.
"""

from hakubun.sync.models import (FieldChange, FieldConflict, FieldPolicy,
                                 NormalizedEntry, SyncMode, SyncPlan,
                                 USER_FIELDS)
from hakubun.sync.store import SyncStore
from hakubun.sync.engine import SyncEngine

__all__ = ['FieldChange', 'FieldConflict', 'FieldPolicy', 'NormalizedEntry',
           'SyncMode', 'SyncPlan', 'SyncStore', 'SyncEngine', 'USER_FIELDS']
