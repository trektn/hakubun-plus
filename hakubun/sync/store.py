"""SQLite persistence for multisync (schema: docs/multisync.md §5)."""

import json
import sqlite3
import threading
import time
import uuid as uuidlib

from hakubun.sync.models import DEFAULT_OWNERSHIP, FieldPolicy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities(
    uuid TEXT PRIMARY KEY,
    media_type TEXT NOT NULL DEFAULT 'anime',
    title TEXT,
    year INTEGER,
    total INTEGER,
    status TEXT,
    provider_only TEXT,
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS mappings(
    uuid TEXT NOT NULL REFERENCES entities(uuid),
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(provider, provider_id),
    UNIQUE(uuid, provider));
CREATE TABLE IF NOT EXISTS local_state(
    uuid TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY(uuid, field));
CREATE TABLE IF NOT EXISTS remote_state(
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    fetched_at REAL NOT NULL,
    PRIMARY KEY(provider, provider_id, field));
CREATE TABLE IF NOT EXISTS base_state(
    uuid TEXT NOT NULL,
    provider TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    synced_at REAL NOT NULL,
    PRIMARY KEY(uuid, provider, field));
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    txn TEXT NOT NULL,
    uuid TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    source TEXT NOT NULL,
    op TEXT NOT NULL DEFAULT 'set');
CREATE INDEX IF NOT EXISTS idx_events_txn ON events(txn);
CREATE INDEX IF NOT EXISTS idx_events_uuid ON events(uuid, field);
CREATE TABLE IF NOT EXISTS identity_conflicts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    title TEXT,
    candidates TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    UNIQUE(provider, provider_id));
CREATE TABLE IF NOT EXISTS ownership(
    field TEXT PRIMARY KEY,
    policy TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT);
"""


def _dump(value):
    return json.dumps(value, sort_keys=True)


def _load(text):
    return None if text is None else json.loads(text)


class SyncStore:
    """All multisync persistence. Values are JSON-encoded.

    A single connection guarded by an RLock: the sync engine is the
    only writer, UI reads go through the engine. Never hand out the
    raw connection.
    """

    def __init__(self, path=':memory:'):
        # Autocommit mode: standalone writes are durable immediately;
        # grouped writes use transaction() below (explicit BEGIN).
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._txn_depth = 0
        self._txn_failed = False
        with self._lock:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA foreign_keys=ON')
            self._conn.executescript(_SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    # -- transactions -------------------------------------------------

    class _Txn:
        """Nesting-aware: only the outermost level BEGINs/COMMITs; an
        exception anywhere dooms the whole transaction."""

        def __init__(self, store):
            self._store = store

        def __enter__(self):
            s = self._store
            s._lock.acquire()
            if s._txn_depth == 0:
                s._conn.execute('BEGIN IMMEDIATE')
                s._txn_failed = False
            s._txn_depth += 1
            return s

        def __exit__(self, exc_type, exc, tb):
            s = self._store
            try:
                if exc_type is not None:
                    s._txn_failed = True
                s._txn_depth -= 1
                if s._txn_depth == 0:
                    if s._txn_failed:
                        s._conn.execute('ROLLBACK')
                    else:
                        s._conn.execute('COMMIT')
            finally:
                s._lock.release()
            return False

    def transaction(self):
        return SyncStore._Txn(self)

    def _exec(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    # -- entities & mappings ------------------------------------------

    def create_entity(self, title, media_type='anime', year=None,
                      total=None, status=None, provider_only=None):
        uid = str(uuidlib.uuid4())
        self._exec(
            'INSERT INTO entities(uuid, media_type, title, year, total,'
            ' status, provider_only, created_at) VALUES(?,?,?,?,?,?,?,?)',
            (uid, media_type, title, year, total, status,
             provider_only, time.time()))
        return uid

    def get_entity(self, uid):
        row = self._exec('SELECT * FROM entities WHERE uuid=?',
                         (uid,)).fetchone()
        return dict(row) if row else None

    def entities(self):
        return [dict(r) for r in
                self._exec('SELECT * FROM entities').fetchall()]

    def update_entity_meta(self, uid, **fields):
        allowed = {'title', 'year', 'total', 'status',
                   'media_type', 'provider_only'}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ', '.join('%s=?' % k for k in sets)
        self._exec('UPDATE entities SET %s WHERE uuid=?' % cols,
                   (*sets.values(), uid))

    def add_mapping(self, uid, provider, provider_id, confirmed=False):
        self._exec(
            'INSERT OR REPLACE INTO mappings(uuid, provider, provider_id,'
            ' confirmed, created_at) VALUES(?,?,?,?,?)',
            (uid, provider, str(provider_id), int(confirmed), time.time()))

    def mapping_for(self, provider, provider_id):
        row = self._exec(
            'SELECT * FROM mappings WHERE provider=? AND provider_id=?',
            (provider, str(provider_id))).fetchone()
        return dict(row) if row else None

    def mappings_of(self, uid):
        return [dict(r) for r in self._exec(
            'SELECT * FROM mappings WHERE uuid=?', (uid,)).fetchall()]

    # -- state tables --------------------------------------------------

    def local_get(self, uid):
        rows = self._exec('SELECT field, value, updated_at FROM local_state'
                          ' WHERE uuid=?', (uid,)).fetchall()
        return {r['field']: (_load(r['value']), r['updated_at'])
                for r in rows}

    def local_set(self, uid, field, value, ts=None):
        self._exec(
            'INSERT OR REPLACE INTO local_state(uuid, field, value,'
            ' updated_at) VALUES(?,?,?,?)',
            (uid, field, _dump(value), ts if ts is not None else time.time()))

    def remote_get(self, provider, provider_id):
        rows = self._exec(
            'SELECT field, value, fetched_at FROM remote_state'
            ' WHERE provider=? AND provider_id=?',
            (provider, str(provider_id))).fetchall()
        return {r['field']: (_load(r['value']), r['fetched_at'])
                for r in rows}

    def remote_set_all(self, provider, provider_id, values, ts=None):
        ts = ts if ts is not None else time.time()
        with self.transaction():
            for field, value in values.items():
                self._exec(
                    'INSERT OR REPLACE INTO remote_state(provider,'
                    ' provider_id, field, value, fetched_at)'
                    ' VALUES(?,?,?,?,?)',
                    (provider, str(provider_id), field, _dump(value), ts))

    def base_get(self, uid, provider):
        rows = self._exec(
            'SELECT field, value FROM base_state WHERE uuid=? AND provider=?',
            (uid, provider)).fetchall()
        return {r['field']: _load(r['value']) for r in rows}

    def base_set(self, uid, provider, field, value):
        self._exec(
            'INSERT OR REPLACE INTO base_state(uuid, provider, field,'
            ' value, synced_at) VALUES(?,?,?,?,?)',
            (uid, provider, field, _dump(value), time.time()))

    def base_delete(self, uid, provider, field):
        self._exec('DELETE FROM base_state WHERE uuid=? AND provider=?'
                   ' AND field=?', (uid, provider, field))

    # -- ownership -----------------------------------------------------

    def ownership(self):
        rows = self._exec('SELECT field, policy FROM ownership').fetchall()
        policies = dict(DEFAULT_OWNERSHIP)
        for r in rows:
            policies[r['field']] = FieldPolicy.parse(r['policy'])
        return policies

    def set_ownership(self, field, policy):
        self._exec('INSERT OR REPLACE INTO ownership(field, policy)'
                   ' VALUES(?,?)', (field, policy.serialize()))

    # -- identity conflicts -------------------------------------------

    def identity_upsert(self, provider, provider_id, title, candidates,
                        status='open'):
        self._exec(
            'INSERT INTO identity_conflicts(provider, provider_id, title,'
            ' candidates, status, created_at) VALUES(?,?,?,?,?,?)'
            ' ON CONFLICT(provider, provider_id) DO UPDATE SET'
            ' title=excluded.title, candidates=excluded.candidates',
            (provider, str(provider_id), title, _dump(candidates),
             status, time.time()))

    def identity_get(self, provider, provider_id):
        row = self._exec(
            'SELECT * FROM identity_conflicts WHERE provider=?'
            ' AND provider_id=?', (provider, str(provider_id))).fetchone()
        return self._identity_row(row)

    def identity_set_status(self, conflict_id, status):
        self._exec('UPDATE identity_conflicts SET status=? WHERE id=?',
                   (status, conflict_id))

    def identity_open(self):
        rows = self._exec(
            "SELECT * FROM identity_conflicts WHERE status IN"
            " ('open', 'deferred')").fetchall()
        return [self._identity_row(r) for r in rows]

    @staticmethod
    def _identity_row(row):
        if row is None:
            return None
        d = dict(row)
        d['candidates'] = _load(d['candidates']) or []
        return d

    # -- events --------------------------------------------------------

    def event_append(self, txn, uid, field, old, new, source, op='set',
                     ts=None):
        self._exec(
            'INSERT INTO events(ts, txn, uuid, field, old_value, new_value,'
            ' source, op) VALUES(?,?,?,?,?,?,?,?)',
            (ts if ts is not None else time.time(), txn, uid, field,
             _dump(old), _dump(new), source, op))

    def events_of_txn(self, txn):
        rows = self._exec('SELECT * FROM events WHERE txn=? ORDER BY id',
                          (txn,)).fetchall()
        return [self._event_row(r) for r in rows]

    def events_query(self, uid=None, field=None, op=None, limit=None):
        sql, params = 'SELECT * FROM events', []
        clauses = []
        for col, val in (('uuid', uid), ('field', field), ('op', op)):
            if val is not None:
                clauses.append('%s=?' % col)
                params.append(val)
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY id DESC'
        if limit:
            sql += ' LIMIT %d' % limit
        return [self._event_row(r) for r in self._exec(sql, params)]

    def last_txns(self, limit=20):
        rows = self._exec(
            'SELECT txn, MIN(ts) AS ts, COUNT(*) AS n FROM events'
            " WHERE op IN ('set', 'push') GROUP BY txn ORDER BY ts DESC"
            ' LIMIT ?', (limit,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _event_row(row):
        d = dict(row)
        d['old_value'] = _load(d['old_value'])
        d['new_value'] = _load(d['new_value'])
        return d

    # -- meta ----------------------------------------------------------

    def meta_get(self, key, default=None):
        row = self._exec('SELECT value FROM meta WHERE key=?',
                         (key,)).fetchone()
        return _load(row['value']) if row else default

    def meta_set(self, key, value):
        self._exec('INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)',
                   (key, _dump(value)))
