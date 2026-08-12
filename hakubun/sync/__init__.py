"""Multisync: local-first, field-strategy multi-provider sync.

See docs/multisync.md for the authoritative design. Mental model:
identity answers "what is this?" (entries map to internal entity
UUIDs); field policies answer "what should this field be?" (a provider
owns it, it stays individual, or a reconciliation strategy decides);
strategies answer "how do we reconcile fields without a single
authority?"; history answers "what happened?" (append-only event log).
The planner turns policies into explicit SyncOperations; applying
commits them locally and pushes to the providers.
"""

from hakubun.sync.models import (FieldConflict, FieldPolicy,
                                 NormalizedEntry, PolicyKind,
                                 SyncOperation, SyncPlan, USER_FIELDS)
from hakubun.sync.store import SyncStore
from hakubun.sync.engine import SyncEngine

__all__ = ['FieldConflict', 'FieldPolicy', 'NormalizedEntry',
           'PolicyKind', 'SyncOperation', 'SyncPlan', 'SyncStore',
           'SyncEngine', 'USER_FIELDS']
