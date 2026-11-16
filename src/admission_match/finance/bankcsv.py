"""Streaming, bounded ingestion of the strict bank-fact CSV.

The bank file is never fully read into memory and the main SQLite database
never holds a read or write transaction while it is parsed.  Instead the
bytes are streamed line by line:

* every chunk feeds the running SHA-256 hasher, so the digest equals the
  digest of the raw file without buffering it;
* every parsed, validated row is inserted into a throwaway *staging* SQLite
  database in a private temporary directory, so even the maximum legal input
  (200,000 rows / 256 MiB) keeps the Python process well under 96 MiB RSS;
* only after the whole file parses does the caller compare the digest with
  ``request.bank_sha256`` and, later, merge staging rows inside the single
  main-database revision transaction.

A parse failure removes the whole staging directory.  Staging directories are
never discovered on startup and leftovers are never auto-posted: each apply
creates a unique directory and only the atomic main-database transaction can
promote its rows.

The CSV is strict UTF-8, standard comma CSV with the fixed seven-column
header ``bank_id,version,kind,status,currency,amount_minor,origin_id``;
blank lines are forbidden, fields are never stripped and numbers are ASCII
decimal without leading zeros.  Row order is never a dependency: a refund may
appear before the credit it references.
"""
import csv
import hashlib
import io
import os
import shutil
import sqlite3
import tempfile

from ..common import ident
from .roster import FinanceError


def _ident(value, line_no, field):
    try:
        return ident(value)
    except ValueError:
        raise FinanceError(
            'line %d: invalid %s identifier' % (line_no, field)) from None

HEADER = ('bank_id', 'version', 'kind', 'status', 'currency',
          'amount_minor', 'origin_id')
MAX_ROWS = 200_000
MAX_BYTES = 256 * 1024 * 1024
MAX_VERSION = 10 ** 9
MAX_AMOUNT = 10 ** 12
# One physical line is a bounded record; the overall file is bounded separately.
MAX_LINE_BYTES = 64 * 1024
KINDS = ('credit', 'refund')
STATUSES = ('posted', 'void')
CURRENCIES = ('CNY', 'USD', 'EUR')

_STAGING_SCHEMA = """
CREATE TABLE staged (
    line_no INTEGER PRIMARY KEY,
    bank_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    origin_id TEXT NOT NULL
);
"""


def _ascii_uint(value, maximum, *, allow_zero):
    """Parse an ASCII decimal integer with no sign, spaces or leading zeros."""
    if not value or not all('0' <= ch <= '9' for ch in value):
        raise FinanceError('expected ASCII decimal integer: '
                           + value[:32].encode('ascii', 'replace').decode())
    if len(value) > 1 and value[0] == '0':
        raise FinanceError('integer must not have leading zeros: ' + value)
    number = int(value)
    lowest = 0 if allow_zero else 1
    if not lowest <= number <= maximum:
        raise FinanceError('integer outside allowed bounds: ' + value)
    return number


def _decode(line, line_no):
    try:
        return line.decode('utf-8')
    except UnicodeDecodeError:
        raise FinanceError('line %d is not valid UTF-8' % line_no) from None


class StagedBatch:
    """A parsed bank file held in a private on-disk staging database."""

    def __init__(self, staging_dir, staging_db, digest, row_count):
        self.staging_dir = staging_dir
        self.staging_db = staging_db
        self.digest = digest
        self.row_count = row_count

    def discard(self):
        shutil.rmtree(self.staging_dir, ignore_errors=True)


def stage_bank_file(bank_path, expected_sha256):
    """Stream and validate one bank CSV into a private staging database.

    Raises FinanceError on any contract violation or hash mismatch; on error
    every staging byte is removed and nothing else is touched.
    """
    staging_dir = tempfile.mkdtemp(prefix='finance-stage-')
    staging_db = os.path.join(staging_dir, 'staged.sqlite')
    staged = None
    hasher = hashlib.sha256()
    total_bytes = 0
    row_count = 0
    try:
        stage_conn = sqlite3.connect(staging_db)
        # Keep the staging connection's page cache small even for 200k rows:
        # the staging file is throwaway scratch space, so spilling to disk is
        # exactly what keeps the process peak RSS below the 96 MiB bound.
        stage_conn.execute('PRAGMA cache_size=-1024')   # ~1 MiB page cache
        stage_conn.execute('PRAGMA temp_store=MEMORY')
        stage_conn.execute('PRAGMA journal_mode=MEMORY')
        stage_conn.execute('PRAGMA synchronous=OFF')
        stage_conn.executescript(_STAGING_SCHEMA)
        pending = []

        def flush():
            if pending:
                stage_conn.executemany(
                    'INSERT INTO staged(line_no,bank_id,version,kind,status,'
                    'currency,amount_minor,origin_id) '
                    'VALUES(?,?,?,?,?,?,?,?)', pending)
                pending.clear()

        # One explicit transaction around the streaming inserts; small batches
        # bound the Python-side pending list while the bounded page cache
        # spills older pages to disk.
        stage_conn.execute('BEGIN')
        with open(bank_path, 'rb') as stream:
            header_seen = False
            line_no = 0
            while True:
                line = stream.readline()
                if not line:
                    break
                line_no += 1
                if len(line) > MAX_LINE_BYTES:
                    raise FinanceError(
                        'line %d exceeds %d bytes' % (line_no, MAX_LINE_BYTES))
                total_bytes += len(line)
                if total_bytes > MAX_BYTES:
                    raise FinanceError(
                        'bank CSV exceeds %d bytes' % MAX_BYTES)
                hasher.update(line)
                # A completely empty physical line (or whitespace-only) is a
                # contract violation; fields themselves are never stripped.
                if not line.strip():
                    raise FinanceError(
                        'blank line at line %d is not allowed' % line_no)
                text = _decode(line, line_no)
                rows = list(csv.reader(io.StringIO(text),
                                       skipinitialspace=False,
                                       strict=True))
                if len(rows) != 1:
                    raise FinanceError(
                        'line %d must contain exactly one CSV record'
                        % line_no)
                fields = rows[0]
                if not header_seen:
                    if tuple(fields) != HEADER:
                        raise FinanceError(
                            'CSV header must be exactly: '
                            + ','.join(HEADER))
                    header_seen = True
                    continue
                if len(fields) != 7:
                    raise FinanceError(
                        'line %d must contain exactly 7 columns' % line_no)
                row_count += 1
                if row_count > MAX_ROWS:
                    raise FinanceError(
                        'bank CSV exceeds %d data rows' % MAX_ROWS)
                bank_id, version_s, kind, status, currency, amount_s, \
                    origin_id = fields
                bank_id = _ident(bank_id, line_no, 'bank_id')
                version = _ascii_uint(version_s, MAX_VERSION,
                                      allow_zero=False)
                if kind not in KINDS:
                    raise FinanceError(
                        'line %d: kind must be credit/refund' % line_no)
                if status not in STATUSES:
                    raise FinanceError(
                        'line %d: status must be posted/void' % line_no)
                if currency not in CURRENCIES:
                    raise FinanceError(
                        'line %d: currency must be CNY/USD/EUR' % line_no)
                amount = _ascii_uint(amount_s, MAX_AMOUNT,
                                     allow_zero=(status == 'void'))
                if status == 'posted' and amount < 1:
                    raise FinanceError(
                        'line %d: posted amount must be >= 1' % line_no)
                if origin_id == '':
                    if kind != 'credit':
                        raise FinanceError(
                            'line %d: only credit rows may have an empty '
                            'origin_id' % line_no)
                else:
                    if kind != 'refund':
                        raise FinanceError(
                            'line %d: only refund rows may carry an '
                            'origin_id' % line_no)
                    _ident(origin_id, line_no, 'origin_id')
                pending.append((line_no, bank_id, version, kind, status,
                                currency, amount, origin_id))
                if len(pending) >= 256:
                    flush()
            flush()
        if not header_seen:
            raise FinanceError('bank CSV is empty: header required')
        stage_conn.commit()

        digest = hasher.hexdigest()
        if digest != expected_sha256:
            raise FinanceError(
                'bank CSV bytes do not match request.bank_sha256')
        stage_conn.close()
        staged = StagedBatch(staging_dir, staging_db, digest, row_count)
        return staged
    except (OSError, csv.Error) as error:
        # Normalize stream/CSV errors onto the finance contract error.
        raise FinanceError('malformed bank CSV: ' + str(error)) from None
    finally:
        if staged is None:
            try:
                stage_conn.rollback()
                stage_conn.close()
            except Exception:
                pass
            shutil.rmtree(staging_dir, ignore_errors=True)
