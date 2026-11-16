"""Deterministic materials-readiness reporting.

A report is built from one consistent read snapshot of the SQLite directory.
For every preference an applicant actually lists it emits one pair row, sorted
by ``(applicant, program)``; program capacity and ranking never influence the
material requirements.

Only the *current* version of each slot counts.  A current ``put`` satisfies a
required kind exactly when ``issued_day <= day < expires_day``; withdrawals,
not-yet-valid and expired versions carry explicit reasons and an absent slot
is ``missing`` -- an older valid version is never silently reused.  Before a
current ``put`` can count as ready its immutable object is re-verified on disk
(size and full SHA256); any missing or corrupt referenced object fails the
whole report, so a damaged object can never be published as ready and the
previous report file stays untouched.
"""
import json
import os

from ..common import save
from . import SCHEMA_VERSION
from .manifest import MAX_DAY
from .objects import verify_slot_object
from .roster import MaterialsError

STATUS_VALID = 'valid'
STATUS_MISSING = 'missing'
STATUS_WITHDRAWN = 'withdrawn'
STATUS_NOT_YET_VALID = 'not_yet_valid'
STATUS_EXPIRED = 'expired'


def _day(value):
    if type(value) is not int or not 0 <= value <= MAX_DAY:
        raise MaterialsError('day must be an integer in 0..%d' % MAX_DAY)
    return value


def build_report(conn, store_root, day):
    """Assemble the deterministic report from one deferred read snapshot."""
    day = _day(day)
    conn.execute('BEGIN')
    try:
        return _build_locked(conn, store_root, day)
    finally:
        conn.rollback()


def _build_locked(conn, store_root, day):
    revision = int(conn.execute(
        "SELECT value FROM meta WHERE key='revision'").fetchone()[0])
    roster_sha = conn.execute(
        "SELECT value FROM meta WHERE key='roster_sha256'").fetchone()[0]
    policy_sha = conn.execute(
        "SELECT value FROM meta WHERE key='policy_sha256'").fetchone()[0]

    preferences = {}
    for aid, _position, pid in conn.execute(
            'SELECT applicant,position,program FROM preferences '
            'ORDER BY applicant,position'):
        preferences.setdefault(aid, []).append(pid)
    requirements = {}
    for pid, kinds in conn.execute('SELECT program,kinds FROM requirements'):
        requirements[pid] = json.loads(kinds)

    slots = {}
    for aid, kind, version, state, obj_sha, size, issued, expires in \
            conn.execute('SELECT applicant,kind,version,state,object_sha,'
                         'byte_size,issued_day,expires_day FROM slots'):
        slots[(aid, kind)] = {
            'version': version, 'state': state, 'object_sha': obj_sha,
            'byte_size': size, 'issued_day': issued,
            'expires_day': expires,
        }

    # Verify each distinct current-put object at most once per report.  Any
    # failure aborts before the output file is touched, keeping the old report.
    verified = set()

    def ensure_object(digest, size):
        if digest in verified:
            return
        if not digest or size is None:
            raise MaterialsError('put slot lacks an object reference')
        verify_slot_object(store_root, digest, size)
        verified.add(digest)

    pairs = []
    for aid in sorted(preferences):
        for pid in sorted(preferences[aid]):
            required = sorted(requirements[pid])
            entries = []
            missing = []
            for kind in required:
                slot = slots.get((aid, kind))
                if slot is None:
                    entries.append({'kind': kind, 'status': STATUS_MISSING,
                                    'version': None, 'object_sha256': None})
                    missing.append(kind)
                    continue
                if slot['state'] == 'withdrawn':
                    entries.append({
                        'kind': kind, 'status': STATUS_WITHDRAWN,
                        'version': slot['version'], 'object_sha256': None})
                    missing.append(kind)
                    continue
                # Current put: the referenced object must be intact.
                ensure_object(slot['object_sha'], slot['byte_size'])
                if day < slot['issued_day']:
                    status = STATUS_NOT_YET_VALID
                    missing.append(kind)
                elif day >= slot['expires_day']:
                    status = STATUS_EXPIRED
                    missing.append(kind)
                else:
                    status = STATUS_VALID
                entries.append({
                    'kind': kind, 'status': status,
                    'version': slot['version'],
                    'object_sha256': slot['object_sha']})
            pairs.append({
                'applicant': aid,
                'program': pid,
                'required_kinds': required,
                'kinds': entries,
                'missing_kinds': sorted(missing),
                'ready': not missing,
            })

    return {
        'schema_version': SCHEMA_VERSION,
        'roster_sha256': roster_sha,
        'policy_sha256': policy_sha,
        'revision': revision,
        'day': day,
        'pairs': pairs,
    }


def write_report(store_root, day, output_path):
    """Build and safely publish a report; failures keep any previous file."""
    from .store import connect
    conn = connect(store_root)
    try:
        report = build_report(conn, os.fspath(store_root), day)
    finally:
        conn.close()
    save(output_path, report)
    return report
