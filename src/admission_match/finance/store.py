"""SQLite ledger for the independent admission finance library.

Storage model
--------------
* ``bank_facts`` holds the *immutable* version history: every accepted CSV
  row is appended as ``(bank_id, version)`` and is never updated or deleted.
* ``bank_current`` holds one row per ``bank_id`` with the current version.
  History and the current pointer advance together in one transaction.
* ``entries`` is the immutable allocation journal: every claim, refund and
  reversal operation gets one row, forever.  A claim/refund is *effective*
  exactly while no ``reverse`` entry targets it; reversing a reversal is
  rejected, so the reversal chain is one level deep.
* ``accounts`` stores ``due_minor`` per ``(applicant, currency)``; the
  effective ``net_paid`` is derived from the journal, never denormalized.
* ``requests`` gives the indexed idempotent replay of request results.

Staging and transaction boundaries
----------------------------------
The bank CSV is parsed into a private staging SQLite file *before* the main
database takes any write lock (see :mod:`admission_match.finance.bankcsv`).
The apply then opens one ``BEGIN IMMEDIATE`` transaction, attaches the
staging file, and performs the whole revision transition inside it:

1. replay/conflict lookup by indexed ``request_id`` and the expected-revision
   check are re-done under the reserved write lock, so a batch validated
   against stale state can never commit;
2. version chains and immutable bank fields are verified with SQL joins over
   the attached staging rows and ``bank_current``;
3. bank history is appended and the current pointer replaced;
4. only the ledger entries touched by this batch (plus their indexed
   dependencies) are loaded, the simultaneous final state is built in
   memory and every conservation rule is checked with Python integers;
5. entries, the revision counter and the immutable request result are written
   together and committed atomically.

A crash inside the transaction is undone by SQLite's journal: a process
killed after the bank writes (step 3) but before step 5 leaves only the
complete old revision -- the new bank amount can never appear next to the old
balances.  The staging directory is outside the database, never auto-discovered
on open, and is discarded on every failure.

Why no SQL money aggregation
-----------------------------
Individual amounts fit in SQLite INTEGER (at most 10**12), but accumulated
totals may exceed the signed 64-bit range.  All summation therefore runs on
Python's arbitrary-precision integers; the database stores only the integer
parts and never a REAL.
"""
import json
import os
import sqlite3
import time

from . import SCHEMA_VERSION
from .bankcsv import StagedBatch, stage_bank_file
from .charges import parse_charges
from .request import parse_request, request_fingerprint
from .roster import FinanceError, parse_roster

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE applicants (id TEXT PRIMARY KEY);
CREATE TABLE accounts (
    applicant TEXT NOT NULL,
    currency TEXT NOT NULL,
    due_minor INTEGER NOT NULL,
    PRIMARY KEY (applicant, currency)
);
CREATE TABLE bank_facts (
    bank_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('credit','refund')),
    status TEXT NOT NULL CHECK (status IN ('posted','void')),
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    origin_id TEXT NOT NULL,
    PRIMARY KEY (bank_id, version)
);
CREATE TABLE bank_current (
    bank_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    origin_id TEXT NOT NULL
);
CREATE TABLE entries (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('claim','refund','reverse')),
    bank_id TEXT,
    applicant TEXT,
    currency TEXT,
    amount_minor INTEGER,
    claim_id TEXT,
    target_id TEXT,
    revision INTEGER NOT NULL
);
CREATE INDEX idx_entries_bank ON entries(bank_id);
CREATE INDEX idx_entries_claim ON entries(claim_id);
CREATE INDEX idx_entries_account ON entries(applicant, currency);
CREATE INDEX idx_entries_target ON entries(target_id);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    canonical TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


def _fault_point(name):
    """Test-only crash injection via ``ADMISSION_MATCH_FINANCE_FAULT``.

    Fires inside the write transaction after the bank facts are written but
    before the ledger and revision update.  Unset in normal operation.
    """
    if os.environ.get('ADMISSION_MATCH_FINANCE_FAULT') == name:
        os._exit(91)


def _install_trace(conn):
    """Optionally log every SQL statement for index-usage evidence."""
    path = os.environ.get('ADMISSION_MATCH_FINANCE_SQL_TRACE')
    if not path:
        return
    def trace(statement):
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(statement.rstrip() + '\n')
    conn.set_trace_callback(trace)


def connect(db_path):
    if not os.path.exists(db_path):
        raise FinanceError('finance database does not exist: ' + str(db_path))
    conn = sqlite3.connect(str(db_path), timeout=10)
    # Bound the page cache so even a long-lived ledger connection stays well
    # inside the 96 MiB RSS envelope; durability comes from the explicit
    # single revision transaction, not from a large cache.
    conn.execute('PRAGMA cache_size=-4096')     # ~4 MiB page cache
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('PRAGMA foreign_keys=ON')
    marker = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    if marker is None:
        conn.close()
        raise FinanceError(
            'not an admission finance database: ' + str(db_path))
    _install_trace(conn)
    return conn


def initialize(db_path, roster_data, roster_sha, charges_data, charges_sha):
    """Create a brand-new finance database; refuses any existing path."""
    if os.path.lexists(db_path):
        raise FinanceError('database already exists: ' + str(db_path))
    roster = parse_roster(roster_data)
    charges = parse_charges(charges_data, roster['applicants'])

    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute('PRAGMA cache_size=-8192')
    try:
        conn.executescript(SCHEMA)
        conn.executemany('INSERT INTO meta(key,value) VALUES(?,?)', [
            ('schema_version', str(SCHEMA_VERSION)),
            ('roster_sha256', roster_sha),
            ('charges_sha256', charges_sha),
            ('revision', '0'),
        ])
        conn.executemany('INSERT INTO applicants(id) VALUES(?)',
                         [(aid,) for aid in roster['applicants']])
        conn.executemany(
            'INSERT INTO accounts(applicant,currency,due_minor) '
            'VALUES(?,?,?)',
            [(c['applicant'], c['currency'], c['due_minor'])
             for c in charges])
        conn.commit()
    except BaseException:
        conn.rollback()
        conn.close()
        if os.path.exists(db_path):
            os.unlink(db_path)
        raise
    conn.close()
    return {
        'initialized': True,
        'db': os.fspath(db_path),
        'schema_version': SCHEMA_VERSION,
        'roster_sha256': roster_sha,
        'charges_sha256': charges_sha,
        'applicants': len(roster['applicants']),
        'accounts': len(charges),
    }


def _lookup_replay(conn, request_id, canonical):
    row = conn.execute(
        'SELECT canonical,result FROM requests WHERE request_id=?',
        (request_id,)).fetchone()
    if row is None:
        return None
    stored_canonical, stored_result = row
    if stored_canonical != canonical:
        raise FinanceError(
            'request_id %s reused with different content' % request_id)
    return json.loads(stored_result)


def apply_batch(db_path, request_data, bank_path):
    """Apply one validated request plus its bank CSV; atomically or not at all."""
    request = parse_request(request_data)
    canonical = request_fingerprint(request)
    request_id = request['request_id']
    operations = request['operations']

    conn = connect(db_path)
    try:
        # Replay precedes the revision check and every file access: a retried
        # request returns its first result even if the CSV has been deleted.
        replay = _lookup_replay(conn, request_id, canonical)
        if replay is not None:
            return replay

        # No transaction is held while the (possibly huge) CSV is streamed to
        # disk staging; other processes keep full access to the database.
        staged = stage_bank_file(bank_path, request['bank_sha256'])
        delay = os.environ.get('ADMISSION_MATCH_FINANCE_DELAY')
        if delay:
            time.sleep(float(delay))
        try:
            return _commit_batch(conn, staged, request, canonical)
        finally:
            staged.discard()
    finally:
        conn.close()


def _commit_batch(conn, staged, request, canonical):
    request_id = request['request_id']
    operations = request['operations']
    conn.execute('BEGIN IMMEDIATE')
    current_revision = int(conn.execute(
        "SELECT value FROM meta WHERE key='revision'").fetchone()[0])

    # Authoritative replay/conflict and revision checks inside the lock.
    locked_replay = _lookup_replay(conn, request_id, canonical)
    if locked_replay is not None:
        conn.commit()
        return locked_replay
    if request['expected_revision'] != current_revision:
        raise FinanceError(
            'revision conflict: expected %d but database is at %d'
            % (request['expected_revision'], current_revision))

    conn.execute('ATTACH DATABASE ? AS stage', (staged.staging_db,))
    try:
        _prepare_temp_scope(conn)
        _merge_bank_facts(conn)
        # Fault window: new bank facts are written, ledger is not yet
        # touched.  The journal must restore the complete old revision.
        _fault_point('after_bank_insert')
        new_revision = current_revision + 1
        _apply_ledger(conn, operations, new_revision)

        result = {
            'accepted': True,
            'request_id': request_id,
            'revision': new_revision,
            'bank_rows': staged.row_count,
            'operations': len(operations),
        }
        conn.execute(
            'INSERT INTO requests(request_id,revision,canonical,result) '
            'VALUES(?,?,?,?)',
            (request_id, new_revision, canonical,
             json.dumps(result, sort_keys=True, ensure_ascii=False,
                        allow_nan=False)))
        conn.execute("UPDATE meta SET value=? WHERE key='revision'",
                     (str(new_revision),))
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        # The rollback/commit must be settled before detaching; detaching a
        # database still participating in an open transaction would fail.
        try:
            conn.execute('DETACH DATABASE stage')
        except sqlite3.Error:
            pass


def _merge_bank_facts(conn):
    """Validate staged rows against history, then append and repoint current."""
    # One bank_id may appear at most once in a single file.
    dup = conn.execute(
        'SELECT bank_id FROM stage.staged GROUP BY bank_id '
        'HAVING COUNT(*) > 1 LIMIT 1').fetchone()
    if dup is not None:
        raise FinanceError(
            'bank_id appears more than once in one CSV: ' + dup[0])

    # New items start at version 1; later items must be exactly current+1 and
    # may never change kind/currency/origin_id.
    bad = conn.execute(
        'SELECT s.bank_id FROM stage.staged s '
        'LEFT JOIN bank_current c ON c.bank_id = s.bank_id '
        'WHERE (c.bank_id IS NULL AND s.version <> 1) '
        'OR (c.bank_id IS NOT NULL AND ('
        's.version <> c.version + 1 OR s.kind <> c.kind '
        'OR s.currency <> c.currency OR s.origin_id <> c.origin_id)) '
        'LIMIT 1').fetchone()
    if bad is not None:
        raise FinanceError(
            'bank version chain or immutable-field violation on: ' + bad[0])

    # Every refund must resolve (within this batch or in current facts) to a
    # same-currency credit.  Row order is irrelevant: a refund may precede its
    # credit in the file.
    bad = conn.execute(
        'SELECT s.bank_id FROM stage.staged s '
        "WHERE s.kind = 'refund' AND NOT EXISTS ("
        'SELECT 1 FROM ('
        'SELECT bank_id, kind, currency FROM stage.staged '
        'UNION ALL '
        'SELECT bank_id, kind, currency FROM bank_current '
        'WHERE bank_id NOT IN (SELECT bank_id FROM stage.staged)'
        ') o WHERE o.bank_id = s.origin_id '
        "AND o.kind = 'credit' AND o.currency = s.currency) LIMIT 1"
    ).fetchone()
    if bad is not None:
        raise FinanceError(
            'refund has no same-currency credit origin: ' + bad[0])

    # Immutable history: new versions are appended, old rows are never
    # overwritten or deleted.
    conn.execute(
        'INSERT INTO bank_facts(bank_id,version,kind,status,currency,'
        'amount_minor,origin_id) '
        'SELECT bank_id,version,kind,status,currency,amount_minor,origin_id '
        'FROM stage.staged')
    # The bundled SQLite build rejects UPSERT after INSERT ... SELECT, so the
    # current pointer is repointed portably: staged rows always carry the
    # newest version for their bank_id (the version-chain check above proves
    # it), therefore delete-then-insert inside the same transaction is an
    # exact, atomic replacement.
    conn.execute(
        'DELETE FROM bank_current WHERE bank_id IN ('
        'SELECT bank_id FROM stage.staged)')
    conn.execute(
        'INSERT INTO bank_current(bank_id,version,kind,status,currency,'
        'amount_minor,origin_id) '
        'SELECT bank_id,version,kind,status,currency,amount_minor,origin_id '
        'FROM stage.staged')


def _chunked(values, size=900):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _load_entries(conn, where_sql, params=()):
    rows = conn.execute(
        'SELECT id,kind,bank_id,applicant,currency,amount_minor,claim_id '
        'FROM entries WHERE ' + where_sql, params).fetchall()
    return {row[0]: {
        'id': row[0], 'kind': row[1], 'bank_id': row[2],
        'applicant': row[3], 'currency': row[4],
        'amount_minor': row[5], 'claim_id': row[6]} for row in rows}


def _prepare_temp_scope(conn):
    """Connection-private temp tables holding this batch's key scope.

    A legal CSV can name 200,000 distinct bank ids, which cannot be bound as
    one parameter list (SQLITE_MAX_VARIABLE_NUMBER) and must not be expanded
    into an ad-hoc SQL string.  The scope therefore lives in temp tables that
    disappear with the connection; every lookup below joins against them and
    is served by the bank/entry indexes rather than a full history scan.
    """
    conn.executescript(
        'DROP TABLE IF EXISTS tmp_bank_scope;'
        'CREATE TEMP TABLE tmp_bank_scope(bank_id TEXT PRIMARY KEY) WITHOUT ROWID;'
        'DROP TABLE IF EXISTS tmp_account_scope;'
        'CREATE TEMP TABLE tmp_account_scope('
        'applicant TEXT NOT NULL, currency TEXT NOT NULL,'
        'PRIMARY KEY(applicant,currency)) WITHOUT ROWID;'
        'DROP TABLE IF EXISTS tmp_entry_scope;'
        'CREATE TEMP TABLE tmp_entry_scope(id TEXT PRIMARY KEY) WITHOUT ROWID;')
    conn.execute(
        'INSERT OR IGNORE INTO tmp_bank_scope(bank_id) '
        'SELECT DISTINCT bank_id FROM stage.staged')


def _append_temp_banks(conn, bank_ids):
    conn.executemany(
        'INSERT OR IGNORE INTO tmp_bank_scope(bank_id) VALUES(?)',
        [(bank_id,) for bank_id in bank_ids])


def _append_temp_accounts(conn, accounts):
    conn.executemany(
        'INSERT OR IGNORE INTO tmp_account_scope(applicant,currency) '
        'VALUES(?,?)', list(accounts))


def _apply_ledger(conn, operations, new_revision):
    """Validate the simultaneous final state and insert the journal rows."""
    batch_claims = {op['id']: op for op in operations
                    if op['type'] == 'claim'}
    batch_refunds = {op['id']: op for op in operations
                     if op['type'] == 'refund'}
    batch_reverses = [op for op in operations if op['type'] == 'reverse']
    batch_ids = {op['id'] for op in operations}

    # Entry ids are globally unique and immutable.  At most 1000 operations
    # can appear in one request, so binding them directly stays well under
    # SQLite's variable limit.
    if batch_ids:
        rows = conn.execute(
            'SELECT id FROM entries WHERE id IN (%s)'
            % ','.join('?' for _ in batch_ids), tuple(batch_ids)).fetchall()
        if rows:
            raise FinanceError('entry id already exists: ' + rows[0][0])

    # Reverse targets must be previously committed, currently effective
    # claim/refund entries; a reverse can never be reversed, and one target
    # can be reversed at most once (including twice inside this batch).
    reverse_targets = [op['target_id'] for op in batch_reverses]
    if len(set(reverse_targets)) != len(reverse_targets):
        raise FinanceError('same entry reversed twice in one batch')
    existing_targets = {}
    if reverse_targets:
        for chunk in _chunked(reverse_targets):
            for row in conn.execute(
                    'SELECT id,kind FROM entries WHERE id IN (%s)'
                    % ','.join('?' for _ in chunk), chunk):
                existing_targets[row[0]] = row[1]
    already_reversed = set()
    if reverse_targets:
        for chunk in _chunked(reverse_targets):
            for row in conn.execute(
                    "SELECT target_id FROM entries WHERE kind='reverse' "
                    'AND target_id IN (%s)'
                    % ','.join('?' for _ in chunk), chunk):
                already_reversed.add(row[0])
    for op in batch_reverses:
        target = op['target_id']
        if target in batch_ids:
            raise FinanceError(
                'reverse can only target a previously committed entry: '
                + target)
        kind = existing_targets.get(target)
        if kind is None:
            raise FinanceError('reverse target does not exist: ' + target)
        if kind not in ('claim', 'refund'):
            raise FinanceError('cannot reverse a reverse entry: ' + target)
        if target in already_reversed:
            raise FinanceError('entry already reversed: ' + target)

    # The bank scope starts from every staged bank and grows with the banks
    # named by operations; all correction re-validation runs through it.
    named_banks = {op['bank_id'] for op in operations
                   if op['type'] in ('claim', 'refund')}
    _append_temp_banks(conn, named_banks)

    # Candidate current facts are only needed for banks the operations name
    # (at most 1000); banks touched solely by corrections are re-validated
    # through the scope join below without materializing their full rows.
    facts = {}
    for chunk in _chunked(named_banks):
        for row in conn.execute(
                'SELECT bank_id,version,kind,status,currency,amount_minor,'
                'origin_id FROM bank_current WHERE bank_id IN (%s)'
                % ','.join('?' for _ in chunk), chunk):
            facts[row[0]] = {
                'bank_id': row[0], 'version': row[1], 'kind': row[2],
                'status': row[3], 'currency': row[4],
                'amount_minor': row[5], 'origin_id': row[6]}

    # Resolve every referenced claim (committed entries or same-batch claims).
    referenced_claims = {op['claim_id'] for op in batch_refunds.values()}
    committed_claim_ids = referenced_claims - set(batch_claims)
    committed_claims = {}
    for chunk in _chunked(committed_claim_ids):
        for row in conn.execute(
                'SELECT id,bank_id,applicant,currency,amount_minor '
                'FROM entries WHERE id IN (%s)'
                % ','.join('?' for _ in chunk), chunk):
            committed_claims[row[0]] = {
                'id': row[0], 'bank_id': row[1], 'applicant': row[2],
                'currency': row[3], 'amount_minor': row[4]}

    accounts = {(row[0], row[1]) for row in conn.execute(
        'SELECT applicant, currency FROM accounts')}

    # ---- Per-operation semantic validation against candidate facts --------
    planned = {}
    for op in batch_claims.values():
        fact = facts.get(op['bank_id'])
        if fact is None or fact['kind'] != 'credit' \
                or fact['status'] != 'posted':
            raise FinanceError(
                'claim requires a posted credit bank item: '
                + op['bank_id'])
        if (op['applicant'], fact['currency']) not in accounts:
            raise FinanceError(
                'no fee account for %s/%s'
                % (op['applicant'], fact['currency']))
        planned[op['id']] = {
            'id': op['id'], 'kind': 'claim', 'bank_id': op['bank_id'],
            'applicant': op['applicant'], 'currency': fact['currency'],
            'amount_minor': op['amount_minor'], 'claim_id': None,
            'target_id': None}
    for op in batch_refunds.values():
        fact = facts.get(op['bank_id'])
        if fact is None or fact['kind'] != 'refund' \
                or fact['status'] != 'posted':
            raise FinanceError(
                'refund operation requires a posted refund bank item: '
                + op['bank_id'])
        claim = batch_claims.get(op['claim_id'])
        if claim is not None:
            if claim['bank_id'] != fact['origin_id']:
                raise FinanceError(
                    'refund origin must equal the referenced claim bank: '
                    + op['id'])
            claim_currency = facts[claim['bank_id']]['currency']
            claim_applicant = claim['applicant']
        else:
            claim = committed_claims.get(op['claim_id'])
            if claim is None:
                raise FinanceError(
                    'refund references unknown claim: ' + op['claim_id'])
            if claim['bank_id'] != fact['origin_id']:
                raise FinanceError(
                    'refund origin must equal the referenced claim bank: '
                    + op['id'])
            claim_currency = claim['currency']
            claim_applicant = claim['applicant']
        if fact['currency'] != claim_currency:
            raise FinanceError(
                'refund currency must match its claim currency: ' + op['id'])
        if (claim_applicant, claim_currency) not in accounts:
            raise FinanceError(
                'claim account missing for refund: ' + op['id'])
        planned[op['id']] = {
            'id': op['id'], 'kind': 'refund', 'bank_id': op['bank_id'],
            'applicant': claim_applicant, 'currency': claim_currency,
            'amount_minor': op['amount_minor'],
            'claim_id': op['claim_id'], 'target_id': None}

    # ---- Build the minimal account scope for the final state --------------
    touched_accounts = {(e['applicant'], e['currency'])
                        for e in planned.values()}
    for target in reverse_targets:
        if target in existing_targets:
            row = conn.execute(
                'SELECT applicant,currency FROM entries WHERE id=?',
                (target,)).fetchone()
            touched_accounts.add((row[0], row[1]))
    for claim in committed_claims.values():
        touched_accounts.add((claim['applicant'], claim['currency']))
    _append_temp_accounts(conn, touched_accounts)

    # Every claim/refund on an in-scope bank (indexed by bank_id): corrections
    # including voids must re-validate the full existing allocation.
    loaded = _load_entries(
        conn,
        "kind IN ('claim','refund') AND bank_id IN "
        '(SELECT bank_id FROM tmp_bank_scope)')
    # The complete journal of every in-scope account (composite index): a
    # bank-side correction can change an account's net even when the batch
    # names no operation on that account, so partial account state would
    # validate against a stale balance.
    for entry in _load_entries(
            conn,
            "kind IN ('claim','refund') AND (applicant,currency) IN "
            '(SELECT applicant,currency FROM tmp_account_scope)').values():
        loaded.setdefault(entry['id'], entry)
    loaded.update(planned)

    # An in-scope bank can carry effective entries on accounts not named by
    # the batch; widen the account scope so their whole journal is loaded too.
    extra_accounts = {
        (entry['applicant'], entry['currency'])
        for entry in loaded.values()} - touched_accounts
    if extra_accounts:
        _append_temp_accounts(conn, extra_accounts)
        for entry in _load_entries(
                conn,
                "kind IN ('claim','refund') AND (applicant,currency) IN "
                '(SELECT applicant,currency FROM tmp_account_scope)').values():
            loaded.setdefault(entry['id'], entry)
        touched_accounts |= extra_accounts

    interest_ids = set(loaded)
    reversed_ids = set(already_reversed)
    reversed_ids.update(reverse_targets)
    if interest_ids:
        conn.executemany(
            'INSERT OR IGNORE INTO tmp_entry_scope(id) VALUES(?)',
            [(entry_id,) for entry_id in interest_ids])
        for row in conn.execute(
                "SELECT e.target_id FROM entries e "
                "JOIN tmp_entry_scope s ON e.target_id = s.id "
                "WHERE e.kind='reverse'"):
            reversed_ids.add(row[0])

    def effective(entry_id):
        return entry_id in loaded and entry_id not in reversed_ids

    # ---- Conservation over the simultaneous final state -------------------
    credit_used = {}
    refund_used = {}
    claim_refunded = {}
    account_net = {}
    for entry in loaded.values():
        if not effective(entry['id']):
            continue
        key = (entry['applicant'], entry['currency'])
        if entry['kind'] == 'claim':
            credit_used[entry['bank_id']] = credit_used.get(
                entry['bank_id'], 0) + entry['amount_minor']
            account_net[key] = account_net.get(key, 0) \
                + entry['amount_minor']
        else:
            refund_used[entry['bank_id']] = refund_used.get(
                entry['bank_id'], 0) + entry['amount_minor']
            claim_refunded[entry['claim_id']] = claim_refunded.get(
                entry['claim_id'], 0) + entry['amount_minor']
            account_net[key] = account_net.get(key, 0) \
                - entry['amount_minor']
            if not effective(entry['claim_id']):
                raise FinanceError(
                    'effective refund depends on a non-effective claim: '
                    + entry['id'])

    for bank_id, total in credit_used.items():
        fact = facts.get(bank_id) or _load_fact(conn, bank_id)
        if fact['status'] != 'posted' or fact['kind'] != 'credit':
            raise FinanceError(
                'void/non-credit bank item carries effective claims: '
                + bank_id)
        if total > fact['amount_minor']:
            raise FinanceError(
                'credit %s is over-allocated: %d > %d'
                % (bank_id, total, fact['amount_minor']))
    for bank_id, total in refund_used.items():
        fact = facts.get(bank_id) or _load_fact(conn, bank_id)
        if fact['status'] != 'posted' or fact['kind'] != 'refund':
            raise FinanceError(
                'void/non-refund bank item carries effective allocations: '
                + bank_id)
        if total > fact['amount_minor']:
            raise FinanceError(
                'refund %s is over-allocated: %d > %d'
                % (bank_id, total, fact['amount_minor']))
        origin = facts.get(fact['origin_id']) or _load_fact(
            conn, fact['origin_id'])
        if origin['status'] != 'posted':
            raise FinanceError(
                'posted refund requires a still-posted origin credit: '
                + bank_id)
    for claim_id, total in claim_refunded.items():
        claim = loaded.get(claim_id)
        if claim is None:
            amount = conn.execute(
                'SELECT amount_minor FROM entries WHERE id=?',
                (claim_id,)).fetchone()[0]
        else:
            amount = claim['amount_minor']
        if total > amount:
            raise FinanceError(
                'refunds exceed claim amount on: ' + claim_id)

    due_map = {
        (row[0], row[1]): row[2] for row in conn.execute(
            'SELECT a.applicant,a.currency,a.due_minor FROM accounts a '
            'JOIN tmp_account_scope s '
            'ON a.applicant=s.applicant AND a.currency=s.currency')}
    for key, net in account_net.items():
        due = due_map[key]
        if not 0 <= net <= due:
            raise FinanceError(
                'account %s/%s net_paid %d outside 0..%d'
                % (key[0], key[1], net, due))

    # ---- Insert the immutable journal rows --------------------------------
    new_rows = []
    for op in operations:
        if op['type'] == 'reverse':
            new_rows.append((op['id'], 'reverse', None, None, None, None,
                             None, op['target_id'], new_revision))
        else:
            entry = planned[op['id']]
            new_rows.append((entry['id'], entry['kind'], entry['bank_id'],
                             entry['applicant'], entry['currency'],
                             entry['amount_minor'], entry['claim_id'],
                             None, new_revision))
    conn.executemany(
        'INSERT INTO entries(id,kind,bank_id,applicant,currency,'
        'amount_minor,claim_id,target_id,revision) '
        'VALUES(?,?,?,?,?,?,?,?,?)', new_rows)


def _load_fact(conn, bank_id):
    row = conn.execute(
        'SELECT bank_id,version,kind,status,currency,amount_minor,origin_id '
        'FROM bank_current WHERE bank_id=?', (bank_id,)).fetchone()
    if row is None:
        raise FinanceError('unknown bank item: ' + bank_id)
    return {
        'bank_id': row[0], 'version': row[1], 'kind': row[2],
        'status': row[3], 'currency': row[4],
        'amount_minor': row[5], 'origin_id': row[6]}
