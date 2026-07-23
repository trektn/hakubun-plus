"""Event log services: recording, undo, statistics (docs/multisync.md).

The event log is the commit history. It is append-only: undo appends
inverse events (git revert), it never rewrites.
"""

import uuid as uuidlib


class History:
    def __init__(self, store):
        self._store = store

    # -- recording -----------------------------------------------------

    @staticmethod
    def new_txn():
        return str(uuidlib.uuid4())

    def record(self, txn, uid, field, old, new, source, op='set'):
        self._store.event_append(txn, uid, field, old, new, source, op)

    # -- undo ----------------------------------------------------------

    def undo(self, txn):
        """Undo a transaction: restore each local 'set' to its old value
        and rewind the affected base_state so already-pushed values are
        re-planned as compensating pushes. Returns the undo txn id.

        Raises ValueError for an unknown or already-undone txn.
        """
        events = self._store.events_of_txn(txn)
        sets = [e for e in events if e['op'] == 'set']
        if not events:
            raise ValueError('unknown transaction: %s' % txn)
        if not sets:
            raise ValueError('transaction has nothing undoable: %s' % txn)
        already = self._store.events_query(op='undo')
        if any(e['source'] == txn for e in already):
            raise ValueError('transaction already undone: %s' % txn)

        undo_txn = self.new_txn()
        with self._store.transaction():
            for e in reversed(sets):
                self._store.local_set(e['uuid'], e['field'],
                                      e['old_value'], source='undo')
                # source of an undo event = the txn it reverts, so
                # double-undo is detectable and history self-describes.
                self._store.event_append(undo_txn, e['uuid'], e['field'],
                                         e['new_value'], e['old_value'],
                                         txn, op='undo')
            for e in events:
                if e['op'] == 'push':
                    # The push happened -- the provider now holds
                    # new_value, so that *is* the correct merge base.
                    # With local restored to the old value the next plan
                    # sees a clean local-side change and proposes the
                    # compensating push of the restored value.
                    self._store.base_set(e['uuid'], e['source'],
                                         e['field'], e['new_value'])
        return undo_txn

    # -- statistics ----------------------------------------------------

    def watch_history(self, uid=None):
        """Progress-advance events (field='progress', op='set'),
        newest first: [{ts, uuid, from, to, source}]."""
        events = self._store.events_query(uid=uid, field='progress',
                                          op='set')
        return [{'ts': e['ts'], 'uuid': e['uuid'],
                 'from': e['old_value'], 'to': e['new_value'],
                 'source': e['source']} for e in events]

    def rating_history(self, uid=None):
        events = self._store.events_query(uid=uid, field='score', op='set')
        return [{'ts': e['ts'], 'uuid': e['uuid'],
                 'from': e['old_value'], 'to': e['new_value'],
                 'source': e['source']} for e in events]

    def field_last_change(self, uid, field):
        events = self._store.events_query(uid=uid, field=field, limit=1)
        return events[0] if events else None
