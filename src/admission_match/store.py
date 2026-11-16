"""SQLite persistence for the shared regional-quota admission workbench.

Concurrency and crash model
---------------------------
Every successful ``apply`` is one SQLite write transaction opened with
``BEGIN IMMEDIATE``.  The reserved write lock means no other process can change
the revision while validation, feasibility checks and matching run; the
expected revision is re-checked inside that lock and every state change
(statuses, capacities, offers, request log and the revision counter) is written
in the same transaction.  A crash therefore exposes either the complete old
revision or the complete new one -- offers can never be replaced while the
revision counter still shows the old value.  Replay handling is an indexed
lookup, never an unbounded scan, so transaction time depends only on the
current instance size and never on the length of the request log.
"""
import hashlib
import hmac
import json
import os
import sqlite3

from .common import ident, number, require, rows, save
from .regional import run_matching, audit_blocking_pairs
from .source import parse_source, CAP_MAX

SCHEMA_VERSION = 1
MAX_OPERATIONS = 1000

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE applicants (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active','confirmed','withdrawn')),
    preferences TEXT NOT NULL,
    confirmed_program TEXT
);
CREATE TABLE programs (
    id TEXT PRIMARY KEY,
    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 0 AND 500),
    region TEXT NOT NULL,
    ranking TEXT NOT NULL
);
CREATE TABLE regions (
    id TEXT PRIMARY KEY,
    parent TEXT,
    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 0 AND 500)
);
CREATE TABLE offers (
    revision INTEGER NOT NULL,
    applicant TEXT NOT NULL,
    program TEXT NOT NULL,
    offer_token TEXT NOT NULL,
    PRIMARY KEY (revision, applicant)
);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    canonical TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


class WorkbenchError(ValueError):
    """Any contract, state or concurrency failure of the workbench."""


def _fault_point(name):
    """Test-only crash injection: ``ADMISSION_MATCH_FAULT=<name>``.

    The process exits while the write transaction is still open, so SQLite's
    rollback journal has to restore the complete previous revision.  No fault
    variable means zero overhead in normal operation.
    """
    if os.environ.get('ADMISSION_MATCH_FAULT') == name:
        os._exit(91)


def canonical_json(value):
    """Canonical serialization: key order and operation order ignored."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def request_fingerprint(request):
    """Canonical semantic bytes of a request.

    JSON key order and the order of the operations array are ignored; every
    field -- including ``expected_revision`` -- is part of the content, so the
    same ``request_id`` with a different expectation or different operations
    counts as different content and conflicts.  Replays still succeed after
    the database has advanced because the id lookup short-circuits before the
    live expected-revision check.  The same canonical document is stored in the
    request log, keeping logged requests as normal nested JSON objects.
    """
    canonical_request = dict(request)
    canonical_request['operations'] = sorted(
        request['operations'], key=canonical_json)
    return canonical_json(canonical_request)


def make_token(secret, revision, aid, pid):
    if isinstance(secret, str):
        secret = bytes.fromhex(secret)
    msg = ('%d\n%s\n%s' % (revision, aid, pid)).encode('utf-8')
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


class Snapshot:
    """In-memory view of one revision; matching depends only on this state."""

    def __init__(self, conn):
        self.applicants = {}
        self.status = {}
        self.confirmed = {}
        for aid, status, prefs, confirmed_pid in conn.execute(
                'SELECT id,status,preferences,confirmed_program '
                'FROM applicants'):
            self.applicants[aid] = json.loads(prefs)
            self.status[aid] = status
            if status == 'confirmed':
                self.confirmed[aid] = confirmed_pid
        self.programs = {}
        self.program_chain = {}
        self.rank_index = {}
        for pid, cap, region, ranking in conn.execute(
                'SELECT id,capacity,region,ranking FROM programs'):
            ranking = json.loads(ranking)
            self.programs[pid] = {
                'capacity': cap, 'region': region, 'ranking': ranking}
            self.rank_index[pid] = {aid: i for i, aid in enumerate(ranking)}
        self.region_parent = {}
        capacities_region = {}
        for rid, parent, cap in conn.execute(
                'SELECT id,parent,capacity FROM regions'):
            self.region_parent[rid] = parent
            capacities_region[rid] = cap
        for pid, spec in self.programs.items():
            chain = [spec['region']]
            cur = self.region_parent[spec['region']]
            while cur is not None:
                chain.append(cur)
                cur = self.region_parent[cur]
            self.program_chain[pid] = chain
        self.capacities = {
            'program': {pid: spec['capacity'] for pid, spec in
                        self.programs.items()},
            'region': capacities_region,
        }
        self.revision = int(conn.execute(
            "SELECT value FROM meta WHERE key='revision'").fetchone()[0])
        self.secret = conn.execute(
            "SELECT value FROM meta WHERE key='db_secret'").fetchone()[0]
        self.secret_bytes = bytes.fromhex(self.secret)
        self.source_sha256 = conn.execute(
            "SELECT value FROM meta WHERE key='source_sha256'").fetchone()[0]

    def with_capacities(self, program_caps, region_caps):
        """Return a clone with adjusted capacities for a candidate batch."""
        snap = Snapshot.__new__(Snapshot)
        snap.applicants = self.applicants
        snap.status = self.status
        snap.confirmed = self.confirmed
        snap.programs = {pid: dict(spec) for pid, spec in self.programs.items()}
        snap.program_chain = self.program_chain
        snap.rank_index = self.rank_index
        snap.region_parent = self.region_parent
        snap.capacities = {
            'program': dict(self.capacities['program'], **program_caps),
            'region': dict(self.capacities['region'], **region_caps),
        }
        for pid, cap in program_caps.items():
            snap.programs[pid]['capacity'] = cap
        snap.revision = self.revision
        snap.secret = self.secret
        snap.secret_bytes = self.secret_bytes
        snap.source_sha256 = self.source_sha256
        return snap

    def usage_from(self, contracts):
        program_usage = {pid: 0 for pid in self.programs}
        region_usage = {rid: 0 for rid in self.region_parent}
        for _aid, pid in contracts:
            program_usage[pid] += 1
            for rid in self.program_chain[pid]:
                region_usage[rid] += 1
        return {'program': program_usage, 'region': region_usage}

    def current_offers(self, conn):
        rows = conn.execute(
            'SELECT applicant,program,offer_token FROM offers '
            'WHERE revision=? ORDER BY applicant', (self.revision,))
        return {aid: (pid, token) for aid, pid, token in rows}


def connect(db_path):
    if not os.path.exists(db_path):
        raise WorkbenchError('database does not exist: ' + str(db_path))
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('PRAGMA foreign_keys=ON')
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone() is None:
        conn.close()
        raise WorkbenchError(
            'not an admission workbench database: ' + str(db_path))
    return conn


def initialize(db_path, source_data, source_digest):
    """Create a new workbench database.  Refuses to overwrite an existing one."""
    if os.path.lexists(db_path):
        raise WorkbenchError('database already exists: ' + str(db_path))
    parsed = parse_source(source_data)
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    secret = os.urandom(32).hex()
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.executescript(SCHEMA)
        conn.executemany('INSERT INTO meta(key,value) VALUES(?,?)', [
            ('schema_version', str(SCHEMA_VERSION)),
            ('source_sha256', source_digest),
            ('db_secret', secret),
            ('revision', '0'),
        ])
        conn.executemany(
            'INSERT INTO applicants(id,status,preferences,confirmed_program) '
            'VALUES(?,?,?,NULL)',
            [(aid, 'active', json.dumps(parsed['applicants'][aid],
                                        ensure_ascii=False))
             for aid in sorted(parsed['applicants'])])
        conn.executemany(
            'INSERT INTO programs(id,capacity,region,ranking) VALUES(?,?,?,?)',
            [(pid, parsed['programs'][pid]['capacity'],
              parsed['programs'][pid]['region'],
              json.dumps(parsed['programs'][pid]['ranking'],
                         ensure_ascii=False))
             for pid in parsed['programs_order']])
        conn.executemany(
            'INSERT INTO regions(id,parent,capacity) VALUES(?,?,?)',
            [(rid, parsed['region_parent'][rid],
              parsed['region_capacity'][rid])
             for rid in sorted(parsed['region_parent'])])
        snap = Snapshot(conn)
        offers, _ = run_matching(snap, confirmed={},
                                 active_applicants=sorted(snap.applicants))
        blocks = audit_blocking_pairs(
            snap, snap.status,
            {aid: pid for aid, pid in offers.items()})
        if blocks:
            raise WorkbenchError('internal error: initial matching unstable')
        conn.executemany(
            'INSERT INTO offers(revision,applicant,program,offer_token) '
            'VALUES(0,?,?,?)',
            [(aid, pid, make_token(secret, 0, aid, pid))
             for aid, pid in sorted(offers.items())])
        conn.commit()
    except BaseException:
        conn.rollback()
        conn.close()
        if os.path.exists(db_path):
            os.unlink(db_path)
        raise
    conn.close()


def _validate_request(request):
    try:
        return _validate_request_body(request)
    except ValueError as error:
        # Normalize every strict-contract failure onto the workbench error.
        raise WorkbenchError(str(error)) from None


def _validate_request_body(request):
    require(request, 'request_id expected_revision operations')
    request_id = ident(request['request_id'])
    expected = request['expected_revision']
    if type(expected) is not int or expected < 0:
        raise WorkbenchError('expected_revision must be a nonnegative integer')
    operations = rows(request['operations'], MAX_OPERATIONS)
    seen_applicants = set()
    seen_targets = set()
    clean = []
    for op in operations:
        if not isinstance(op, dict):
            raise WorkbenchError('each operation must be an object')
        kind = op.get('type')
        if kind == 'confirm':
            require(op, 'type applicant offer_token')
            aid = ident(op['applicant'])
            token = op['offer_token']
            if not isinstance(token, str) or not token or len(token) > 128:
                raise WorkbenchError('offer_token must be a bounded string')
            if aid in seen_applicants:
                raise WorkbenchError(
                    'applicant appears in multiple operations: ' + aid)
            seen_applicants.add(aid)
            clean.append({'type': 'confirm', 'applicant': aid,
                          'offer_token': token})
        elif kind == 'withdraw':
            require(op, 'type applicant')
            aid = ident(op['applicant'])
            if aid in seen_applicants:
                raise WorkbenchError(
                    'applicant appears in multiple operations: ' + aid)
            seen_applicants.add(aid)
            clean.append({'type': 'withdraw', 'applicant': aid})
        elif kind == 'set_capacity':
            require(op, 'type kind id capacity')
            if op['kind'] not in ('program', 'region'):
                raise WorkbenchError("kind must be 'program' or 'region'")
            target_id = ident(op['id'])
            cap = number(op['capacity'], 0, CAP_MAX)
            target = (op['kind'], target_id)
            if target in seen_targets:
                raise WorkbenchError(
                    'duplicate set_capacity target: %s %s' % target)
            seen_targets.add(target)
            clean.append({'type': 'set_capacity', 'kind': op['kind'],
                          'id': target_id, 'capacity': cap})
        else:
            raise WorkbenchError('unknown operation type: ' + str(kind))
    return request_id, expected, clean


def apply_request(db_path, request):
    """Process one request batch atomically; return the response payload."""
    request_id, expected, operations = _validate_request(request)
    canonical = request_fingerprint(request)
    conn = connect(db_path)
    try:
        # One reserved write lock for the whole revision transition.
        conn.execute('BEGIN IMMEDIATE')
        current_revision = int(conn.execute(
            "SELECT value FROM meta WHERE key='revision'").fetchone()[0])

        prior = conn.execute(
            'SELECT revision,canonical,result FROM requests '
            'WHERE request_id=?', (request_id,)).fetchone()
        if prior is not None:
            _prior_revision, stored_canonical, stored_result = prior
            if stored_canonical != canonical:
                raise WorkbenchError(
                    'request_id %s reused with different content' % request_id)
            conn.commit()
            return json.loads(stored_result)
        if expected != current_revision:
            raise WorkbenchError(
                'revision conflict: expected %d but database is at %d'
                % (expected, current_revision))

        snap = Snapshot(conn)
        offers_now = snap.current_offers(conn)

        # Phase 1: validate every transition against the old revision.
        program_caps, region_caps = {}, {}
        confirms = {}
        withdrawals = set()
        for op in operations:
            aid = op.get('applicant')
            if op['type'] == 'confirm':
                if snap.status.get(aid) != 'active':
                    raise WorkbenchError(
                        'confirm rejected for %s: not active' % aid)
                issued = offers_now.get(aid)
                if issued is None:
                    raise WorkbenchError(
                        'confirm rejected for %s: no current offer' % aid)
                pid, token = issued
                expected_token = make_token(
                    snap.secret_bytes, current_revision, aid, pid)
                if not hmac.compare_digest(token, op['offer_token']) or \
                        not hmac.compare_digest(token, expected_token):
                    raise WorkbenchError(
                        'confirm rejected for %s: invalid offer token' % aid)
                confirms[aid] = pid
            elif op['type'] == 'withdraw':
                if snap.status.get(aid) not in ('active', 'confirmed'):
                    raise WorkbenchError(
                        'withdraw rejected for %s: terminal state' % aid)
                withdrawals.add(aid)
            else:
                target = op['id']
                if op['kind'] == 'program':
                    if target not in snap.programs:
                        raise WorkbenchError(
                            'unknown program in set_capacity: ' + target)
                    program_caps[target] = op['capacity']
                else:
                    if target not in snap.region_parent:
                        raise WorkbenchError(
                            'unknown region in set_capacity: ' + target)
                    region_caps[target] = op['capacity']

        # Phase 2: the batch describes one simultaneous final state.
        final_confirmed = {
            aid: pid for aid, pid in snap.confirmed.items()
            if aid not in withdrawals}
        final_confirmed.update(confirms)

        final_snap = snap.with_capacities(program_caps, region_caps)
        usage = final_snap.usage_from(final_confirmed.items())
        for pid, count in usage['program'].items():
            if count > final_snap.capacities['program'][pid]:
                raise WorkbenchError(
                    'batch infeasible: program %s over capacity' % pid)
        for rid, count in usage['region'].items():
            if count > final_snap.capacities['region'][rid]:
                raise WorkbenchError(
                    'batch infeasible: region %s over capacity' % rid)

        # Phase 3: rebuild every active offer from full preference lists.
        final_status = dict(snap.status)
        for aid in withdrawals:
            final_status[aid] = 'withdrawn'
        for aid in confirms:
            final_status[aid] = 'confirmed'
        active = sorted(aid for aid, st in final_status.items()
                        if st == 'active')
        new_offers, proposals = run_matching(
            final_snap, confirmed=final_confirmed, active_applicants=active)

        # The audit must be empty before the revision may be committed.
        audit_offers = dict(final_confirmed)
        audit_offers.update(new_offers)
        blocks = audit_blocking_pairs(final_snap, final_status, audit_offers)
        if blocks:
            raise WorkbenchError(
                'internal error: blocking pairs remain: ' +
                canonical_json(blocks))

        new_revision = current_revision + 1
        conn.execute('UPDATE meta SET value=? WHERE key=?',
                     (str(new_revision), 'revision'))
        for aid in withdrawals:
            conn.execute(
                'UPDATE applicants SET status=?,confirmed_program=NULL '
                'WHERE id=?', ('withdrawn', aid))
        for aid in confirms:
            conn.execute(
                'UPDATE applicants SET status=?,confirmed_program=? '
                'WHERE id=?', ('confirmed', confirms[aid], aid))
        for pid, cap in program_caps.items():
            conn.execute('UPDATE programs SET capacity=? WHERE id=?',
                         (cap, pid))
        for rid, cap in region_caps.items():
            conn.execute('UPDATE regions SET capacity=? WHERE id=?',
                         (cap, rid))
        # Fault point: status/capacity writes are done but replacement offers
        # are not yet written.  On restart the journal must roll everything
        # back so only one complete revision is ever observable.
        _fault_point('before_offers')
        conn.executemany(
            'INSERT INTO offers(revision,applicant,program,offer_token) '
            'VALUES(?,?,?,?)',
            [(new_revision, aid, pid,
              make_token(snap.secret_bytes, new_revision, aid, pid))
             for aid, pid in sorted(new_offers.items())])

        result = {
            'accepted': True,
            'revision': new_revision,
            'confirmed': sorted(final_confirmed),
            'withdrawn': sorted(withdrawals),
            'offers': [{'applicant': aid, 'program': pid}
                       for aid, pid in sorted(new_offers.items())],
            'blocking_pairs': [],
            'proposal_count': len(proposals),
        }
        conn.execute(
            'INSERT INTO requests(request_id,revision,canonical,result) '
            'VALUES(?,?,?,?)',
            (request_id, new_revision, canonical,
             json.dumps(result, sort_keys=True, ensure_ascii=False,
                        allow_nan=False)))
        # Fault point just before commit: the whole new revision must either
        # become visible atomically or be discarded completely.
        _fault_point('before_commit')
        conn.commit()
        return result
    finally:
        conn.close()


def build_export(conn):
    """Assemble the deterministic export report from one read snapshot.

    A single deferred read transaction covers every SELECT, so a concurrent
    committer cannot mix rows from different revisions into one report.
    """
    conn.execute('BEGIN')
    try:
        return _build_export_locked(conn)
    finally:
        conn.rollback()


def _build_export_locked(conn):
    snap = Snapshot(conn)
    offers_now = snap.current_offers(conn)
    assignments = {aid: None for aid in snap.applicants}
    for aid, pid in snap.confirmed.items():
        assignments[aid] = pid
    for aid, (pid, _token) in offers_now.items():
        assignments[aid] = pid

    program_accepted = {pid: [] for pid in snap.programs}
    region_occupied = {rid: 0 for rid in snap.region_parent}
    for aid, pid in assignments.items():
        if pid is None:
            continue
        program_accepted[pid].append(aid)
        for rid in snap.program_chain[pid]:
            region_occupied[rid] += 1

    applicants_report = [
        {'id': aid, 'status': snap.status[aid],
         'assignment': assignments[aid]}
        for aid in sorted(snap.applicants)]
    programs_report = [
        {'program': pid,
         'capacity': snap.capacities['program'][pid],
         'region': snap.programs[pid]['region'],
         'accepted': sorted(program_accepted[pid])}
        for pid in sorted(snap.programs)]
    regions_report = [
        {'region': rid, 'parent': snap.region_parent[rid],
         'capacity': snap.capacities['region'][rid],
         'occupied': region_occupied[rid]}
        for rid in sorted(snap.region_parent)]
    offers_report = [
        {'applicant': aid, 'program': pid, 'offer_token': token,
         'revision': snap.revision}
        for aid, (pid, token) in sorted(offers_now.items())]

    blocks = audit_blocking_pairs(
        snap, snap.status,
        {**{aid: pid for aid, pid in assignments.items()
            if snap.status[aid] == 'confirmed'},
         **{aid: pid for aid, (pid, _t) in offers_now.items()}})

    log = []
    for request_id, revision, canonical, result in conn.execute(
            'SELECT request_id,revision,canonical,result FROM requests '
            'ORDER BY revision,request_id'):
        log.append({'request_id': request_id, 'revision': revision,
                    'request': json.loads(canonical),
                    'result': json.loads(result)})

    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': snap.source_sha256,
        'revision': snap.revision,
        'applicants': applicants_report,
        'programs': programs_report,
        'regions': regions_report,
        'offers': offers_report,
        'blocking_pairs': blocks,
        'request_log': log,
    }


def export_report(db_path, output_path):
    conn = connect(db_path)
    try:
        report = build_export(conn)
    finally:
        conn.close()
    save(output_path, report)
    return report
