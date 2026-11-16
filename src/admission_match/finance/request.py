"""Strict validation and canonical fingerprints of finance requests.

A request contains exactly ``request_id``, ``expected_revision``,
``bank_sha256`` and ``operations``.  The revision is a nonnegative integer,
the bank hash is 64 lowercase hexadecimal characters and there are at most
1000 operations, each exactly one of:

* ``{"type":"claim","id","bank_id","applicant","amount_minor"}``
* ``{"type":"refund","id","bank_id","claim_id","amount_minor"}``
* ``{"type":"reverse","id","target_id"}``

The canonical fingerprint ignores the order of JSON object keys and of the
operations array only; ``expected_revision`` and ``bank_sha256`` participate
in the content, so a re-ordered CSV with a different raw hash is a different
request.
"""
import json
import re

from ..common import ident, require, rows
from .charges import AMOUNT_MAX
from .roster import FinanceError

MAX_OPERATIONS = 1000
SHA256_RE = re.compile(r'^[0-9a-f]{64}\Z')
CLAIM_FIELDS = ('type', 'id', 'bank_id', 'applicant', 'amount_minor')
REFUND_FIELDS = ('type', 'id', 'bank_id', 'claim_id', 'amount_minor')
REVERSE_FIELDS = ('type', 'id', 'target_id')


def positive_amount(value):
    """Amount in ``1..10**12``; booleans are never integers here."""
    if type(value) is not int or not 1 <= value <= AMOUNT_MAX:
        raise FinanceError(
            'amount_minor must be an integer in 1..%d' % AMOUNT_MAX)
    return value


def parse_request(data):
    """Return the cleaned request; raise FinanceError on any contract issue."""
    try:
        return _parse_request(data)
    except FinanceError:
        raise
    except ValueError as error:
        raise FinanceError(str(error)) from None


def _parse_request(data):
    require(data, 'request_id expected_revision bank_sha256 operations')
    request_id = ident(data['request_id'])
    expected = data['expected_revision']
    if type(expected) is not int or expected < 0:
        raise FinanceError('expected_revision must be a nonnegative integer')
    bank_sha = data['bank_sha256']
    if not isinstance(bank_sha, str) or not SHA256_RE.match(bank_sha):
        raise FinanceError('bank_sha256 must be 64 lowercase hex characters')
    operations = rows(data['operations'], MAX_OPERATIONS)

    clean = []
    entry_ids = set()
    for op in operations:
        if not isinstance(op, dict):
            raise FinanceError('each operation must be an object')
        kind = op.get('type')
        if kind == 'claim':
            require(op, ' '.join(CLAIM_FIELDS))
            entry = {
                'type': 'claim',
                'id': ident(op['id']),
                'bank_id': ident(op['bank_id']),
                'applicant': ident(op['applicant']),
                'amount_minor': positive_amount(op['amount_minor']),
            }
        elif kind == 'refund':
            require(op, ' '.join(REFUND_FIELDS))
            entry = {
                'type': 'refund',
                'id': ident(op['id']),
                'bank_id': ident(op['bank_id']),
                'claim_id': ident(op['claim_id']),
                'amount_minor': positive_amount(op['amount_minor']),
            }
        elif kind == 'reverse':
            require(op, ' '.join(REVERSE_FIELDS))
            entry = {
                'type': 'reverse',
                'id': ident(op['id']),
                'target_id': ident(op['target_id']),
            }
        else:
            raise FinanceError('unknown operation type: ' + str(kind))
        if entry['id'] in entry_ids:
            raise FinanceError(
                'duplicate operation/entry id in request: ' + entry['id'])
        entry_ids.add(entry['id'])
        clean.append(entry)
    return {
        'request_id': request_id,
        'expected_revision': expected,
        'bank_sha256': bank_sha,
        'operations': clean,
    }


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def request_fingerprint(request):
    """Canonical semantic bytes; key order and operation order are ignored."""
    canonical_request = dict(request)
    canonical_request['operations'] = sorted(
        request['operations'], key=canonical_json)
    return canonical_json(canonical_request)
