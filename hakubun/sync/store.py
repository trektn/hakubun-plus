"""SQLite persistence for multisync (schema: docs/multisync.md §10)."""

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
    via TEXT,
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
CREATE INDEX IF NOT EXISTS idx_events_op_source ON events(op, source);
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
CREATE TABLE IF NOT EXISTS resolved_absent(
    uuid TEXT NOT NULL,
    provider TEXT NOT NULL,
    checked_at REAL NOT NULL,
    PRIMARY KEY(uuid, provider));
CREATE TABLE IF NOT EXISTS membership(
    uuid TEXT NOT NULL,
    provider TEXT NOT NULL,
    want TEXT NOT NULL,
    reason TEXT,
    decided_at REAL NOT NULL,
    PRIMARY KEY(uuid, provider));
"""


def _dump(value):
    return json.dumps(value, sort_keys=True)


def _load(text):
    return None if text is None else json.loads(text)


def _chunked(seq, size=500):
    """Split a sequence for SQL IN() lists (SQLite caps bound
    parameters per statement; 500 stays far under every build's
    limit)."""
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


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
        # entities_with_aliases() snapshot; None = rebuild on demand.
        self._entities_cache = None
        with self._lock:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA foreign_keys=ON')
            self._conn.executescript(_SCHEMA)
            # Migrations for databases created by earlier revisions.
            self._migrate()

    def _migrate(self):
        self._ensure_column('entities', 'aliases', 'TEXT')
        self._ensure_column('identity_conflicts', 'entry', 'TEXT')
        self._ensure_column('mappings', 'via', 'TEXT')
        self._ensure_column('local_state', 'source', 'TEXT')
        self._ensure_column('resolved_absent', 'reason', 'TEXT')
        self._migrate_membership()

    def _migrate_membership(self):
        """Move the two resolved_absent reasons that were always USER
        DECISIONS into the membership table, leaving resolved_absent as
        what it should have been all along: the network lookup cache.

        Both migrate to 'ignore' ("leave this provider as it is"), never
        to 'absent'. 'absent' additionally means "remove the entry if
        the provider has one", and neither historical reason carries
        that intent: 'declined' meant only "don't create it here", and
        'deleted' was an OBSERVATION that the user removed it on the
        website. Promoting either into a deletion proposal would let a
        stale row silently destroy a list entry the user has since
        re-added on purpose -- and deletions on real accounts are the
        one thing here that cannot be undone.
        """
        rows = self._conn.execute(
            "SELECT uuid, provider, reason FROM resolved_absent"
            " WHERE reason IN ('declined', 'deleted')").fetchall()
        if not rows:
            return
        now = time.time()
        for row in rows:
            self._conn.execute(
                'INSERT OR IGNORE INTO membership(uuid, provider, want,'
                ' reason, decided_at) VALUES(?,?,?,?,?)',
                (row['uuid'], row['provider'], 'ignore', row['reason'],
                 now))
        self._conn.execute("DELETE FROM resolved_absent WHERE reason IN"
                           " ('declined', 'deleted')")

    def _ensure_column(self, table, column, decl):
        cols = {r['name'] for r in
                self._conn.execute('PRAGMA table_info(%s)' % table)}
        if column not in cols:
            self._conn.execute('ALTER TABLE %s ADD COLUMN %s %s'
                               % (table, column, decl))

    def close(self):
        with self._lock:
            self._conn.close()

    def reset(self):
        """Drop everything and re-create the schema. The database is
        derived state (fetch re-populates it) plus user decisions;
        after a bad run -- e.g. one polluted by broken title matching
        creating duplicate entities -- a reset re-derives cleanly."""
        with self._lock:
            # FK enforcement must be off for the drops, and the pragma
            # is a no-op inside a transaction -- toggle it outside.
            self._conn.execute('PRAGMA foreign_keys=OFF')
            try:
                with self.transaction():
                    rows = self._exec("SELECT name FROM sqlite_master"
                                      " WHERE type='table'").fetchall()
                    for row in rows:
                        if not row['name'].startswith('sqlite_'):
                            self._exec('DROP TABLE %s' % row['name'])
                self._conn.executescript(_SCHEMA)
                self._migrate()
                self._entities_cache = None
            finally:
                self._conn.execute('PRAGMA foreign_keys=ON')

    # -- transactions -------------------------------------------------

    class _Txn:
        """Nesting-aware: only the outermost level BEGINs/COMMITs; an
        exception anywhere dooms the whole transaction."""

        def __init__(self, store):
            self._store = store

        def __enter__(self):
            s = self._store
            s._lock.acquire()
            try:
                if s._txn_depth == 0:
                    # Can raise: two connections share this file by
                    # design (the sync window's store vs the list
                    # overlay's cached one), so a write that lands
                    # while the other holds the write lock fails with
                    # 'database is locked' after the busy timeout. If
                    # __enter__ raises, __exit__ never runs -- release
                    # here or the lock is held for the process's life
                    # and every other thread's read blocks forever.
                    s._conn.execute('BEGIN IMMEDIATE')
                    s._txn_failed = False
            except BaseException:
                s._lock.release()
                raise
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
                        # A snapshot built inside this transaction saw
                        # now-rolled-back rows; never serve it.
                        s._entities_cache = None
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
        self._entities_cache = None
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

    def _data_version(self):
        """SQLite's cross-connection write counter. It changes whenever
        ANOTHER connection commits to this database, and deliberately
        does NOT move for this connection's own writes -- so it is
        exactly the signal the in-process caches below cannot derive
        themselves. Two connections share this file by design (the sync
        window opens its own store; hakubun.sync.uibridge keeps a
        long-lived one for the list overlay), so without this a cache
        built once by the overlay would never again see anything the
        sync window wrote."""
        row = self._exec('PRAGMA data_version').fetchone()
        return row[0] if row else None

    def entities_with_aliases(self):
        """[(entity_row, parsed_alias_list)], cached until any entity
        write -- by this connection or another one -- invalidates it.
        Identity resolution scans every entity for EVERY unmatched
        fetched entry; without the cache a first sync of a large second
        provider re-reads and re-json-parses the whole table
        quadratically (inside the fetch transaction, holding the write
        lock the whole time). Treat rows as read-only."""
        version = self._data_version()
        if self._entities_cache is None \
                or self._entities_cache[0] != version:
            self._entities_cache = (version, [
                (dict(r), _load(r['aliases']) or [])
                for r in self._exec('SELECT * FROM entities').fetchall()])
        return self._entities_cache[1]

    def update_entity_meta(self, uid, **fields):
        allowed = {'title', 'year', 'total', 'status',
                   'media_type', 'provider_only'}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        self._entities_cache = None
        cols = ', '.join('%s=?' % k for k in sets)
        self._exec('UPDATE entities SET %s WHERE uuid=?' % cols,
                   (*sets.values(), uid))

    def entity_aliases(self, uid):
        row = self._exec('SELECT aliases FROM entities WHERE uuid=?',
                         (uid,)).fetchone()
        return (_load(row['aliases']) or []) if row else []

    def entity_add_aliases(self, uid, titles):
        """Accumulate known titles (any provider, any language) so
        title matching works across title-language settings -- e.g. an
        entity created from AniList with a native title still matches a
        romaji Kitsu entry via the shared alias."""
        current = self.entity_aliases(uid)
        seen = set(current)
        added = False
        for title in titles:
            if title and title not in seen:
                current.append(title)
                seen.add(title)
                added = True
        if not added:
            return
        self._entities_cache = None
        self._exec('UPDATE entities SET aliases=? WHERE uuid=?',
                   (_dump(current), uid))

    def add_mapping(self, uid, provider, provider_id, confirmed=False,
                    via=None):
        self._exec(
            'INSERT OR REPLACE INTO mappings(uuid, provider, provider_id,'
            ' confirmed, via, created_at) VALUES(?,?,?,?,?,?)',
            (uid, provider, str(provider_id), int(confirmed), via,
             time.time()))

    def remove_mapping(self, provider, provider_id):
        """Quarantine a bad mapping (e.g. one that turned out to point
        at the wrong media type -- see identity.py's step-1 guard).
        Without this, a poisoned mapping would be trusted forever: it
        is the FIRST thing every future resolve_entry() checks."""
        self._exec('DELETE FROM mappings WHERE provider=? AND'
                   ' provider_id=?', (provider, str(provider_id)))

    def mapping_for(self, provider, provider_id):
        row = self._exec(
            'SELECT * FROM mappings WHERE provider=? AND provider_id=?',
            (provider, str(provider_id))).fetchone()
        return dict(row) if row else None

    def mappings_of(self, uid):
        return [dict(r) for r in self._exec(
            'SELECT * FROM mappings WHERE uuid=?', (uid,)).fetchall()]

    def mappings_of_provider(self, provider):
        return [dict(r) for r in self._exec(
            'SELECT * FROM mappings WHERE provider=?',
            (provider,)).fetchall()]

    def mark_absent(self, uid, provider, reason='lookup_miss'):
        """Record that a cross-provider id LOOKUP came back empty: this
        entity is (as of that answer) not on `provider`, so engine.py's
        _discover_cross_ids doesn't repeat the same network call every
        fetch forever.

        This table is a network cache, not a decision. User decisions
        about whether an entry belongs on a provider live in
        `membership` (set_membership) -- keeping them apart is what
        stops a stale, purely observational miss from ever being read
        as "the user wants this removed from Kitsu".

        The row is superseded automatically the moment a REAL mapping
        appears for (uid, provider) through any path
        (mappings_of_provider is checked first), so it can never block
        a later legitimate add or identity link; a re-added entry's
        fetch likewise gives it a remote snapshot again, which trumps
        the absence for field planning. clear_absent() forgets it
        deliberately."""
        self._exec(
            'INSERT OR REPLACE INTO resolved_absent(uuid, provider,'
            ' checked_at, reason) VALUES(?,?,?,?)',
            (uid, provider, time.time(), reason))

    def clear_absent(self, uid, provider):
        """Forget a recorded absence/exclusion: the next plan may
        propose creation again."""
        self._exec('DELETE FROM resolved_absent WHERE uuid=? AND'
                   ' provider=?', (uid, provider))

    def absent_for_provider(self, provider):
        return {r['uuid'] for r in self._exec(
            'SELECT uuid FROM resolved_absent WHERE provider=?',
            (provider,)).fetchall()}

    # -- membership decisions ------------------------------------------
    #
    # Whether an entry should EXIST on a provider is its own question,
    # with its own persisted state -- deliberately NOT folded into
    # field ownership (ownership says where a field's value comes from;
    # it can never say whether a list entry belongs somewhere). Three
    # distinct wants, because "never add this to Kitsu" and "Kitsu
    # should not have this" are different states with different
    # consequences:
    #
    #   'present'  the entry belongs on this provider: propose creating
    #              it there whenever the provider doesn't have it
    #   'absent'   the entry does NOT belong here: propose REMOVING it
    #              whenever the provider does have it. Only ever written
    #              from an explicit user action -- nothing infers it
    #   'ignore'   whatever this provider currently holds is fine:
    #              never propose an add or a remove, and stop asking
    #
    # No row at all means "undecided": Mirror surfaces the discrepancy
    # and ordinary Sync offers the (unticked) creation.

    WANTS = ('present', 'absent', 'ignore')

    def set_membership(self, uid, provider, want, reason=None):
        """Persist a membership decision (see WANTS). Passing None
        clears it back to undecided."""
        if want is None:
            self._exec('DELETE FROM membership WHERE uuid=? AND'
                       ' provider=?', (uid, provider))
            return
        if want not in self.WANTS:
            raise ValueError('unknown membership want: %s' % want)
        self._exec(
            'INSERT OR REPLACE INTO membership(uuid, provider, want,'
            ' reason, decided_at) VALUES(?,?,?,?,?)',
            (uid, provider, want, reason, time.time()))

    def membership_of(self, uid):
        """{provider: want} for one entity (only decided providers)."""
        return {r['provider']: r['want'] for r in self._exec(
            'SELECT provider, want FROM membership WHERE uuid=?',
            (uid,)).fetchall()}

    def membership_many(self, uids):
        """{uid: {provider: want}} in a few queries -- the planner and
        the mirror planner both need this for every entity at once."""
        out = {}
        for chunk in _chunked(uids):
            marks = ','.join('?' * len(chunk))
            for r in self._exec(
                    'SELECT uuid, provider, want FROM membership WHERE'
                    ' uuid IN (%s)' % marks, chunk).fetchall():
                out.setdefault(r['uuid'], {})[r['provider']] = r['want']
        return out

    def membership_reason(self, uid, provider):
        row = self._exec(
            'SELECT reason FROM membership WHERE uuid=? AND provider=?',
            (uid, provider)).fetchone()
        return row['reason'] if row else None

    def absent_reason(self, uid, provider):
        """The stored reason for an absence row, or None when there is
        no row (rows from before reasons existed read as the historical
        default, 'lookup_miss')."""
        row = self._exec(
            'SELECT reason FROM resolved_absent WHERE uuid=? AND'
            ' provider=?', (uid, provider)).fetchone()
        if row is None:
            return None
        return row['reason'] or 'lookup_miss'

    def mappings_many(self, uids):
        """{uid: [mapping, ...]} for many entities in a few queries.
        The per-uid mappings_of() in a loop is an N+1 pattern neither
        the planner nor the display overlay can afford."""
        out = {}
        for chunk in _chunked(uids):
            marks = ','.join('?' * len(chunk))
            for r in self._exec('SELECT * FROM mappings WHERE uuid IN'
                                ' (%s)' % marks, chunk).fetchall():
                out.setdefault(r['uuid'], []).append(dict(r))
        return out

    # -- state tables --------------------------------------------------

    def local_get(self, uid):
        rows = self._exec('SELECT field, value, updated_at FROM local_state'
                          ' WHERE uuid=?', (uid,)).fetchall()
        return {r['field']: (_load(r['value']), r['updated_at'])
                for r in rows}

    def local_set(self, uid, field, value, ts=None, source=None):
        """`source` is the value's provenance ('local', a provider
        name, 'resolve', ...), mirroring the event that records the
        change. It is persisted so a later plan can still tell a
        provider-fed value from a direct local edit -- the PROVIDER
        ownership guard needs exactly that (engine._plan_push)."""
        self._exec(
            'INSERT OR REPLACE INTO local_state(uuid, field, value,'
            ' updated_at, source) VALUES(?,?,?,?,?)',
            (uid, field, _dump(value),
             ts if ts is not None else time.time(), source))

    def local_sources_many(self, uids):
        """{uid: {field: source}} -- the stored provenance of every
        local value (None for rows from before provenance existed,
        treated as direct local edits)."""
        out = {}
        for chunk in _chunked(uids):
            marks = ','.join('?' * len(chunk))
            for r in self._exec(
                    'SELECT uuid, field, source FROM local_state'
                    ' WHERE uuid IN (%s)' % marks, chunk).fetchall():
                out.setdefault(r['uuid'], {})[r['field']] = r['source']
        return out

    def local_get_many(self, uids):
        """{uid: {field: (value, updated_at)}} for many entities in a
        few queries (see mappings_many on why)."""
        out = {}
        for chunk in _chunked(uids):
            marks = ','.join('?' * len(chunk))
            for r in self._exec(
                    'SELECT uuid, field, value, updated_at FROM'
                    ' local_state WHERE uuid IN (%s)' % marks,
                    chunk).fetchall():
                out.setdefault(r['uuid'], {})[r['field']] = (
                    _load(r['value']), r['updated_at'])
        return out

    def remote_get(self, provider, provider_id):
        rows = self._exec(
            'SELECT field, value, fetched_at FROM remote_state'
            ' WHERE provider=? AND provider_id=?',
            (provider, str(provider_id))).fetchall()
        return {r['field']: (_load(r['value']), r['fetched_at'])
                for r in rows}

    def remote_get_many(self, provider, provider_ids):
        """{provider_id: {field: (value, fetched_at)}} for many entries
        of one provider in a few queries (see mappings_many on why)."""
        out = {}
        for chunk in _chunked(str(p) for p in provider_ids):
            marks = ','.join('?' * len(chunk))
            for r in self._exec(
                    'SELECT provider_id, field, value, fetched_at FROM'
                    ' remote_state WHERE provider=? AND provider_id IN'
                    ' (%s)' % marks, [provider, *chunk]).fetchall():
                out.setdefault(r['provider_id'], {})[r['field']] = (
                    _load(r['value']), r['fetched_at'])
        return out

    def remote_set_all(self, provider, provider_id, values, ts=None):
        """Write a remote-tracking snapshot. fetched_at advances ONLY
        when the stored value actually changes, so it means 'when this
        value was first seen' -- the closest available approximation of
        when the remote really changed, which is what newest-wins merge
        arbitration compares against local edit times. (Advancing it on
        every fetch would make the remote side of every divergence look
        freshly changed and always win the tie.) Unchanged values are
        not rewritten at all, which also keeps a full-list fetch from
        churning thousands of no-op row writes."""
        ts = ts if ts is not None else time.time()
        provider_id = str(provider_id)
        with self.transaction():
            self._conn.executemany(
                'INSERT INTO remote_state(provider, provider_id,'
                ' field, value, fetched_at) VALUES(?,?,?,?,?)'
                ' ON CONFLICT(provider, provider_id, field)'
                ' DO UPDATE SET value=excluded.value,'
                ' fetched_at=excluded.fetched_at'
                ' WHERE excluded.value IS NOT remote_state.value',
                [(provider, provider_id, field, _dump(value), ts)
                 for field, value in values.items()])

    def remote_provider_ids(self, provider):
        """Every provider_id currently remote-tracked for a provider."""
        return {r['provider_id'] for r in self._exec(
            'SELECT DISTINCT provider_id FROM remote_state'
            ' WHERE provider=?', (provider,)).fetchall()}

    def remote_delete(self, provider, provider_id):
        """Drop an entry's remote-tracking rows -- the provider no
        longer lists it, so there is no remote snapshot to diff
        against."""
        self._exec('DELETE FROM remote_state WHERE provider=? AND'
                   ' provider_id=?', (provider, str(provider_id)))

    def remote_prune_fields(self, provider, keep_fields):
        """Drop a provider's remote-state rows for fields a fetch no
        longer reports -- e.g. fields earlier revisions fabricated as
        empty for providers that cannot represent them at all. Absent
        must mean absent, or the stale row keeps diffing forever."""
        keep = list(keep_fields)
        if not keep:
            return
        marks = ','.join('?' * len(keep))
        self._exec('DELETE FROM remote_state WHERE provider=? AND field'
                   ' NOT IN (%s)' % marks, [provider, *keep])

    def base_get(self, uid, provider):
        rows = self._exec(
            'SELECT field, value FROM base_state WHERE uuid=? AND provider=?',
            (uid, provider)).fetchall()
        return {r['field']: _load(r['value']) for r in rows}

    def base_get_many(self, uids):
        """{(uid, provider): {field: value}} for many entities in a few
        queries (see mappings_many on why)."""
        out = {}
        for chunk in _chunked(uids):
            marks = ','.join('?' * len(chunk))
            for r in self._exec(
                    'SELECT uuid, provider, field, value FROM base_state'
                    ' WHERE uuid IN (%s)' % marks, chunk).fetchall():
                out.setdefault((r['uuid'], r['provider']), {})[
                    r['field']] = _load(r['value'])
        return out

    def base_set(self, uid, provider, field, value):
        self._exec(
            'INSERT OR REPLACE INTO base_state(uuid, provider, field,'
            ' value, synced_at) VALUES(?,?,?,?,?)',
            (uid, provider, field, _dump(value), time.time()))

    def base_delete(self, uid, provider, field=None):
        """Forget the merge base -- for one field, or (field=None) for
        every field of the (entity, provider) pair, e.g. when the
        provider no longer lists the entry and any future reappearance
        must be treated as a first sync."""
        if field is None:
            self._exec('DELETE FROM base_state WHERE uuid=? AND'
                       ' provider=?', (uid, provider))
        else:
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
                        status='open', entry=None):
        """Record (or refresh) an identity conflict. The status is
        written on update too: a row that was once 'resolved' can
        genuinely become unresolved again (its mapping quarantined by
        identity.py's type-mismatch guard) and must reappear in
        identity_open() rather than staying invisibly 'resolved'
        forever. Callers that want to preserve a previous status
        (e.g. keep a 'deferred' row deferred) pass it explicitly."""
        self._exec(
            'INSERT INTO identity_conflicts(provider, provider_id, title,'
            ' candidates, status, created_at, entry) VALUES(?,?,?,?,?,?,?)'
            ' ON CONFLICT(provider, provider_id) DO UPDATE SET'
            ' title=excluded.title, candidates=excluded.candidates,'
            ' status=excluded.status, entry=excluded.entry',
            (provider, str(provider_id), title, _dump(candidates),
             status, time.time(), _dump(entry)))

    def identity_get(self, provider, provider_id):
        row = self._exec(
            'SELECT * FROM identity_conflicts WHERE provider=?'
            ' AND provider_id=?', (provider, str(provider_id))).fetchone()
        return self._identity_row(row)

    def identity_get_by_id(self, conflict_id):
        row = self._exec(
            'SELECT * FROM identity_conflicts WHERE id=?',
            (conflict_id,)).fetchone()
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
        d['entry'] = _load(d.get('entry')) or {}
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

    def event_undo_exists(self, txn):
        """Whether `txn` already has an 'undo' event recording it as
        the source -- an indexed point lookup instead of scanning
        every undo event ever recorded (see idx_events_op_source)."""
        return self._exec(
            "SELECT 1 FROM events WHERE op='undo' AND source=? LIMIT 1",
            (txn,)).fetchone() is not None

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

    @staticmethod
    def _event_row(row):
        d = dict(row)
        d['old_value'] = _load(d['old_value'])
        d['new_value'] = _load(d['new_value'])
        return d
