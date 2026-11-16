"""SQLite directory ledger for the admission materials library.

Storage layout under the dedicated store root::

    meta.json-like rows in SQLite   <root>/ledger/materials.sqlite
    immutable content objects       <root>/objects/xx/<sha256>
    incoming temporary objects      <root>/objects/xx/.incoming-*

Objects and the SQLite ledger are intentionally *not* one transaction.  The
commit order and recovery boundary are:

1. Every referenced object is fully received, fsynced and atomically renamed
   under ``objects/`` first, without any SQLite write transaction open.
2. Only then does the directory transition run in one ``BEGIN IMMEDIATE``
   transaction: the revision is re-read, each slot version is re-checked, the
   slot rows, the immutable batch record and the new revision are written
   together and committed atomically.

A process killed after step 1 but before step 2 leaves complete, unreferenced
objects on disk and the complete old revision in SQLite; the retry reuses the
already-verified objects and commits once.  A killed step-2 transaction is
rolled back by SQLite's journal, again exposing only the complete old or the
complete new revision.  No ``ready`` row can ever reference a missing object,
and no part of a failed batch ever reaches the live slots.
"""
import json
import os
import shutil
import sqlite3

from ..common import read
from . import SCHEMA_VERSION
from .manifest import canonical_manifest, parse_manifest
from .objects import object_path, receive_object, verify_existing_object
from .roster import MaterialsError, parse_policy, parse_roster

LEDGER_DIR = 'ledger'
LEDGER_FILE = 'materials.sqlite'
OBJECTS_DIR = 'objects'

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE applicants (id TEXT PRIMARY KEY);
CREATE TABLE programs (id TEXT PRIMARY KEY, ranking TEXT NOT NULL);
CREATE TABLE preferences (
    applicant TEXT NOT NULL,
    position INTEGER NOT NULL,
    program TEXT NOT NULL,
    PRIMARY KEY (applicant, position)
);
CREATE TABLE requirements (
    program TEXT PRIMARY KEY,
    kinds TEXT NOT NULL
);
CREATE TABLE slots (
    applicant TEXT NOT NULL,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('put', 'withdrawn')),
    object_sha TEXT,
    byte_size INTEGER,
    issued_day INTEGER,
    expires_day INTEGER,
    PRIMARY KEY (applicant, kind)
);
CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    canonical TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


def _fault_point(name):
    """Test-only crash injection via ``ADMISSION_MATCH_MATERIALS_FAULT``.

    Fires after every object of the batch is on disk but before the SQLite
    write transaction is opened, exercising the object/directory recovery
    boundary.  Unset in normal operation, hence zero overhead.
    """
    if os.environ.get('ADMISSION_MATCH_MATERIALS_FAULT') == name:
        os._exit(91)


def ledger_path(store_root):
    return os.path.join(store_root, LEDGER_DIR, LEDGER_FILE)


def connect(store_root):
    db = ledger_path(store_root)
    if not os.path.isfile(db):
        raise MaterialsError(
            'materials store does not exist: ' + str(store_root))
    conn = sqlite3.connect(db, timeout=10)
    conn.execute('PRAGMA busy_timeout=10000')
    conn.execute('PRAGMA foreign_keys=ON')
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone() is None:
        conn.close()
        raise MaterialsError(
            'not an admission materials store: ' + str(store_root))
    return conn


def _meta(conn, key):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if row is None:
        raise MaterialsError('corrupt ledger: missing meta ' + key)
    return row[0]


def initialize(store_root, roster_path, policy_path):
    """Create a brand-new materials store; refuses any existing root path."""
    root = os.fspath(store_root)
    if os.path.lexists(root):
        raise MaterialsError('store root already exists: ' + root)

    # Validate every input before anything is created, so a rejected init
    # never leaves a partial root behind.
    roster_data, roster_sha = read(roster_path)
    policy_data, policy_sha = read(policy_path)
    roster = parse_roster(roster_data)
    requirements, program_kinds = parse_policy(policy_data, roster)

    # Atomic existence check plus creation: a concurrent initializer racing
    # for the same root gets FileExistsError instead of interleaving writes.
    try:
        os.mkdir(root)
    except FileExistsError:
        raise MaterialsError('store root already exists: ' + root)
    # The original roster/policy bytes are digested now; the store never
    # depends on those files still existing afterwards.
    os.makedirs(os.path.join(root, LEDGER_DIR))
    os.makedirs(os.path.join(root, OBJECTS_DIR))
    conn = sqlite3.connect(ledger_path(root), timeout=10)
    try:
        conn.executescript(SCHEMA)
        conn.executemany('INSERT INTO meta(key,value) VALUES(?,?)', [
            ('schema_version', str(SCHEMA_VERSION)),
            ('roster_sha256', roster_sha),
            ('policy_sha256', policy_sha),
            ('revision', '0'),
        ])
        conn.executemany('INSERT INTO applicants(id) VALUES(?)',
                         [(aid,) for aid in sorted(roster['applicants'])])
        conn.executemany(
            'INSERT INTO programs(id,ranking) VALUES(?,?)',
            [(pid, json.dumps(roster['programs'][pid]['ranking'],
                              ensure_ascii=False))
             for pid in roster['programs_order']])
        pref_rows = [(aid, position, pid)
                     for aid in sorted(roster['applicants'])
                     for position, pid in
                     enumerate(roster['applicants'][aid])]
        conn.executemany(
            'INSERT INTO preferences(applicant,position,program) '
            'VALUES(?,?,?)', pref_rows)
        conn.executemany(
            'INSERT INTO requirements(program,kinds) VALUES(?,?)',
            [(entry['program'],
              json.dumps(program_kinds[entry['program']],
                         ensure_ascii=False))
             for entry in requirements])
        conn.commit()
    except BaseException:
        conn.rollback()
        conn.close()
        # Never leave a half-initialized root behind.
        shutil.rmtree(root, ignore_errors=True)
        raise
    conn.close()
    return {
        'initialized': True,
        'store': root,
        'schema_version': SCHEMA_VERSION,
        'roster_sha256': roster_sha,
        'policy_sha256': policy_sha,
        'applicants': len(roster['applicants']),
        'programs': len(roster['programs']),
    }


def _load_identities(conn):
    applicants = {row[0] for row in conn.execute('SELECT id FROM applicants')}
    preferences = {}
    for aid, position, pid in conn.execute(
            'SELECT applicant,position,program FROM preferences '
            'ORDER BY applicant,position'):
        preferences.setdefault(aid, []).append(pid)
    required_kinds = {}
    for pid, kinds in conn.execute('SELECT program,kinds FROM requirements'):
        required_kinds[pid] = json.loads(kinds)
    all_kinds = set()
    for kinds in required_kinds.values():
        all_kinds.update(kinds)
    return applicants, preferences, required_kinds, all_kinds


def _lookup_replay(conn, batch_id, canonical):
    row = conn.execute(
        'SELECT canonical,result FROM batches WHERE batch_id=?',
        (batch_id,)).fetchone()
    if row is None:
        return None
    stored_canonical, stored_result = row
    if stored_canonical != canonical:
        raise MaterialsError(
            'batch_id %s reused with different content' % batch_id)
    return json.loads(stored_result)


def ingest(store_root, manifest, files_dir):
    """Apply one validated manifest; return the recorded batch result.

    The manifest is parsed strictly up front, then every referenced byte is
    received outside any write transaction.  The final directory transition
    re-reads the revision and every slot version under the reserved write
    lock, so a transition judged against stale state can never be committed.
    """
    parsed = parse_manifest(manifest)
    canonical = canonical_manifest(parsed)
    batch_id = parsed['batch_id']
    expected = parsed['expected_revision']
    documents = parsed['documents']
    root = os.fspath(store_root)

    conn = connect(root)
    try:
        # Early replay short-circuit so retrying a finished batch never needs
        # the original source files (or even the files directory) again.
        replay = _lookup_replay(conn, batch_id, canonical)
        if replay is not None:
            return replay
        if not os.path.isdir(files_dir):
            raise MaterialsError('files directory does not exist: '
                                 + str(files_dir))
        applicants, _prefs, required_kinds, all_kinds = _load_identities(conn)

        # Reference and transition pre-checks fail before any byte is copied.
        _validate_against_directory(documents, applicants, all_kinds, conn)

        # Phase 1: receive bytes with no write transaction held.  Identical
        # content in the same batch or in earlier batches yields one object.
        new_objects = set()
        for doc in documents:
            if doc['action'] != 'put':
                continue
            digest = doc['sha256']
            dest = object_path(root, digest)
            if os.path.lexists(dest):
                # Reuse only after re-verifying size and full hash; a corrupt
                # object aborts the batch and is never overwritten.
                verify_existing_object(root, digest, doc['byte_size'])
            else:
                receive_object(root, files_dir, doc['path'], digest,
                               doc['byte_size'])
                new_objects.add(digest)

        # Crash window: all objects are durable, the directory is unchanged.
        _fault_point('objects_durable_before_commit')

        # Phase 2: one reserved write transaction for the whole transition.
        conn.execute('BEGIN IMMEDIATE')
        current_revision = int(_meta(conn, 'revision'))

        # The authoritative replay/conflict check inside the write lock
        # covers a batch another process committed while we copied bytes.
        locked_replay = _lookup_replay(conn, batch_id, canonical)
        if locked_replay is not None:
            conn.commit()
            return locked_replay
        if expected != current_revision:
            raise MaterialsError(
                'revision conflict: expected %d but store is at %d'
                % (expected, current_revision))

        # Re-validate every slot transition against the live revision: the
        # commit must never rely on the pre-lock judgments.
        _validate_against_directory(documents, applicants, all_kinds, conn)
        for digest in new_objects:
            if not os.path.isfile(object_path(root, digest)):
                raise MaterialsError(
                    'received object vanished before commit: ' + digest)

        put_count = 0
        withdrawn_count = 0
        for doc in documents:
            slot = (doc['applicant'], doc['kind'])
            row = conn.execute(
                'SELECT version,state FROM slots WHERE applicant=? AND kind=?',
                slot).fetchone()
            current_version = 0 if row is None else row[0]
            if doc['version'] != current_version + 1:
                raise MaterialsError(
                    'slot %s/%s version must be %d'
                    % (slot[0], slot[1], current_version + 1))
            if doc['action'] == 'withdraw':
                if row is None or row[1] != 'put':
                    raise MaterialsError(
                        'withdraw only accepts a slot currently in put state: '
                        '%s/%s' % slot)
                conn.execute(
                    'UPDATE slots SET version=?,state=? '
                    'WHERE applicant=? AND kind=?',
                    (doc['version'], 'withdrawn') + slot)
                withdrawn_count += 1
            else:
                if row is None:
                    conn.execute(
                        'INSERT INTO slots(applicant,kind,version,state,'
                        'object_sha,byte_size,issued_day,expires_day) '
                        'VALUES(?,?,?,?,?,?,?,?)',
                        (doc['applicant'], doc['kind'], doc['version'],
                         'put', doc['sha256'], doc['byte_size'],
                         doc['issued_day'], doc['expires_day']))
                else:
                    conn.execute(
                        'UPDATE slots SET version=?,state=?,object_sha=?,'
                        'byte_size=?,issued_day=?,expires_day=? '
                        'WHERE applicant=? AND kind=?',
                        (doc['version'], 'put', doc['sha256'],
                         doc['byte_size'], doc['issued_day'],
                         doc['expires_day'], doc['applicant'], doc['kind']))
                put_count += 1

        new_revision = current_revision + 1
        conn.execute("UPDATE meta SET value=? WHERE key='revision'",
                     (str(new_revision),))
        result = {
            'accepted': True,
            'batch_id': batch_id,
            'revision': new_revision,
            'documents': len(documents),
            'put': put_count,
            'withdrawn': withdrawn_count,
            'new_objects': sorted(new_objects),
        }
        conn.execute(
            'INSERT INTO batches(batch_id,revision,canonical,result) '
            'VALUES(?,?,?,?)',
            (batch_id, new_revision, canonical,
             json.dumps(result, sort_keys=True, ensure_ascii=False,
                        allow_nan=False)))
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validate_against_directory(documents, applicants, all_kinds, conn):
    """Reference and state-transition checks against one directory view."""
    for doc in documents:
        if doc['applicant'] not in applicants:
            raise MaterialsError(
                'unknown applicant in manifest: ' + doc['applicant'])
        if doc['kind'] not in all_kinds:
            raise MaterialsError(
                'kind is not required by any program policy: '
                + doc['kind'])
        row = conn.execute(
            'SELECT version,state FROM slots WHERE applicant=? AND kind=?',
            (doc['applicant'], doc['kind'])).fetchone()
        current_version = 0 if row is None else row[0]
        if doc['version'] != current_version + 1:
            raise MaterialsError(
                'slot %s/%s expects version %d'
                % (doc['applicant'], doc['kind'], current_version + 1))
        if doc['action'] == 'withdraw' and (row is None
                                            or row[1] != 'put'):
            raise MaterialsError(
                'withdraw only accepts a slot currently in put state: '
                '%s/%s' % (doc['applicant'], doc['kind']))
