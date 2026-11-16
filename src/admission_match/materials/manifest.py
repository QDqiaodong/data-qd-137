"""Strict ingestion-manifest contract and canonical fingerprints.

A manifest contains exactly ``batch_id``, ``expected_revision`` and
``documents`` (1..1000 entries).  Every document is either a ``put`` or a
``withdraw``; the same ``(applicant, kind)`` slot may appear at most once per
batch.

Booleans are never accepted as integers (all numeric checks use ``type(x) is
int``), every object must carry exactly its documented fields, and every
reference is checked against the directory when the manifest is applied.
"""
import hashlib
import json
from pathlib import Path

from ..common import ident
from .roster import MaterialsError

MAX_DOCUMENTS = 1000
MAX_DAY = 3_652_058
MAX_BYTE_SIZE = 67_108_864  # 64 MiB per object
MAX_BATCH_BYTES = 512 * 1024 * 1024  # 512 MiB per batch
# A manifest may hold up to 1000 documents, each with several bounded
# identifiers, so its JSON cap is larger than the generic 8 MiB reader's.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
PUT_FIELDS = ('action', 'applicant', 'kind', 'version', 'issued_day',
              'expires_day', 'path', 'sha256', 'byte_size')
WITHDRAW_FIELDS = ('action', 'applicant', 'kind', 'version')


def read_manifest(path):
    """Strictly read a manifest file: bounded size, no duplicate JSON keys."""
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise MaterialsError(
            'manifest JSON exceeds %d bytes' % MAX_MANIFEST_BYTES)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MaterialsError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    def invalid(value):
        raise MaterialsError('nonfinite JSON number: ' + value)

    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=invalid), hashlib.sha256(raw).hexdigest()


def _day(value, name):
    if type(value) is not int or not 0 <= value <= MAX_DAY:
        raise MaterialsError(name + ' must be an integer in 0..%d' % MAX_DAY)
    return value


def _sha256(value):
    if not isinstance(value, str) or len(value) != 64:
        raise MaterialsError('sha256 must be 64 lowercase hex characters')
    try:
        int(value, 16)
    except ValueError:
        raise MaterialsError('sha256 must be 64 lowercase hex characters')
    if value != value.lower():
        raise MaterialsError('sha256 must be 64 lowercase hex characters')
    return value


def _relative_path(value):
    """A safe relative POSIX path inside the manifest's files directory."""
    if not isinstance(value, str) or not value:
        raise MaterialsError('path must be a nonempty relative POSIX path')
    if value.startswith('/'):
        raise MaterialsError('absolute paths are rejected: ' + value)
    if '\\' in value:
        raise MaterialsError('backslash paths are rejected: ' + value)
    parts = value.split('/')
    if '' in parts:
        raise MaterialsError('empty path segment rejected: ' + value)
    if '.' in parts or '..' in parts:
        raise MaterialsError('. and .. path segments are rejected: ' + value)
    return value


def parse_manifest(data):
    """Validate manifest shape and value bounds; return a cleaned document."""
    if not isinstance(data, dict) or \
            set(data) != {'batch_id', 'expected_revision', 'documents'}:
        raise MaterialsError(
            'manifest must contain exactly batch_id, expected_revision, '
            'documents')
    batch_id = ident(data['batch_id'])
    expected = data['expected_revision']
    if type(expected) is not int or expected < 0:
        raise MaterialsError(
            'expected_revision must be a nonnegative integer')
    documents = data['documents']
    if not isinstance(documents, list) or not (1 <= len(documents)
                                               <= MAX_DOCUMENTS):
        raise MaterialsError(
            'documents must be an array of 1..%d items' % MAX_DOCUMENTS)

    cleaned = []
    seen_slots = set()
    total_bytes = 0
    for doc in documents:
        if not isinstance(doc, dict):
            raise MaterialsError('each document must be an object')
        action = doc.get('action')
        if action == 'put':
            if set(doc) != set(PUT_FIELDS):
                raise MaterialsError(
                    'put document must contain exactly: '
                    + ', '.join(PUT_FIELDS))
            applicant = ident(doc['applicant'])
            kind = ident(doc['kind'])
            version = doc['version']
            if type(version) is not int or version < 1:
                raise MaterialsError('version must be a positive integer')
            issued = _day(doc['issued_day'], 'issued_day')
            expires = _day(doc['expires_day'], 'expires_day')
            if not issued < expires:
                raise MaterialsError('issued_day must be < expires_day')
            path = _relative_path(doc['path'])
            digest = _sha256(doc['sha256'])
            size = doc['byte_size']
            if type(size) is not int or not 0 <= size <= MAX_BYTE_SIZE:
                raise MaterialsError(
                    'byte_size must be an integer in 0..%d' % MAX_BYTE_SIZE)
            total_bytes += size
            if total_bytes > MAX_BATCH_BYTES:
                raise MaterialsError(
                    'batch exceeds the cumulative 512 MiB limit')
            clean = {
                'action': 'put', 'applicant': applicant, 'kind': kind,
                'version': version, 'issued_day': issued,
                'expires_day': expires, 'path': path, 'sha256': digest,
                'byte_size': size,
            }
        elif action == 'withdraw':
            if set(doc) != set(WITHDRAW_FIELDS):
                raise MaterialsError(
                    'withdraw document must contain exactly: '
                    + ', '.join(WITHDRAW_FIELDS))
            applicant = ident(doc['applicant'])
            kind = ident(doc['kind'])
            version = doc['version']
            if type(version) is not int or version < 1:
                raise MaterialsError('version must be a positive integer')
            clean = {
                'action': 'withdraw', 'applicant': applicant,
                'kind': kind, 'version': version,
            }
        else:
            raise MaterialsError("document action must be 'put' or 'withdraw'")

        slot = (clean['applicant'], clean['kind'])
        if slot in seen_slots:
            raise MaterialsError(
                'duplicate slot in one batch: %s / %s' % slot)
        seen_slots.add(slot)
        cleaned.append(clean)

    return {
        'batch_id': batch_id,
        'expected_revision': expected,
        'documents': cleaned,
    }


def canonical_manifest(manifest):
    """Canonical semantic bytes of a manifest.

    The comparison ignores object keys and the order of ``documents`` only:
    every field value participates, including the object key ``sha256`` and
    the relative ``path``.  The same ``batch_id`` with different content
    therefore produces a different fingerprint and conflicts on replay.
    """
    canonical = {
        'batch_id': manifest['batch_id'],
        'expected_revision': manifest['expected_revision'],
        'documents': sorted(
            manifest['documents'],
            key=lambda doc: json.dumps(doc, sort_keys=True,
                                       separators=(',', ':'),
                                       ensure_ascii=False)),
    }
    return json.dumps(canonical, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)
