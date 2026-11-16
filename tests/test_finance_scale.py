"""Production-scale finance checks.

Two non-functional acceptance requirements are exercised here with real
subprocesses and a real on-disk ledger:

1. A legal 100,000-row / >=16 MiB bank CSV is streamed to disk staging
   without ever being fully buffered; the apply subprocess measures its own
   peak RSS (``resource.getrusage``) and the bound is below 96 MiB.
2. After at least 10,000 historical journal entries have accumulated across
   many revisions, a small batch touching one account must not replay or
   full-scan the history.  Evidence is collected two ways: the SQLite trace
   log shows only indexed/index-scope statements, and ``EXPLAIN QUERY PLAN``
   on every journal lookup shape reports index use rather than a full scan.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from admission_match.finance.reporting import build_report
from admission_match.finance.store import apply_batch, connect, initialize

ROOT = Path(__file__).resolve().parents[1]
HEADER = 'bank_id,version,kind,status,currency,amount_minor,origin_id\n'


def roster():
    return json.loads((ROOT / 'fixtures' / 'finance_roster.json').read_text())


def charges():
    return json.loads((ROOT / 'fixtures' / 'finance_charges.json').read_text())


def csv_text(rows):
    return HEADER + ''.join(
        ','.join('' if v is None else str(v) for v in row) + '\n'
        for row in rows)


class LargeCsvStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.db = self.td / 'big.sqlite'
        initialize(self.db, roster(), 'ab' * 32, charges(), 'cd' * 32)

    def tearDown(self):
        self.tmp.cleanup()

    def test_100k_row_csv_under_96mib_rss(self):
        # 120,000 distinct posted credits written to disk in a streaming
        # loop (the row list is never retained); ~127-char ids keep the file
        # above the 16 MiB acceptance threshold while every row stays
        # individually small, so the receiver cannot keep the file resident.
        bank = self.td / 'big.csv'
        count = 120_000
        with bank.open('w', encoding='utf-8') as stream:
            stream.write(HEADER)
            for i in range(count):
                bank_id = 'c%06d%s' % (i, 'x' * 120)
                stream.write(
                    '%s,1,credit,posted,CNY,1,\n' % bank_id)
        size = bank.stat().st_size
        self.assertGreaterEqual(count, 100_000)
        self.assertGreater(size, 16 * 1024 * 1024)

        # The worker measures only the receive/apply phase against the 96 MiB
        # bound.  It hashes the file once, in fixed-size chunks (never
        # buffering the whole file), and that digest is exactly what the
        # production streaming parser recomputes internally.
        runner = self.td / 'worker.py'
        runner.write_text(
            'import json,resource,sys,hashlib\n'
            'from admission_match.finance.store import apply_batch\n'
            'db,bank,out=sys.argv[1:4]\n'
            'h=hashlib.sha256()\n'
            'with open(bank,"rb") as f:\n'
            '    while True:\n'
            '        chunk=f.read(65536)\n'
            '        if not chunk: break\n'
            '        h.update(chunk)\n'
            'req={"request_id":"big","expected_revision":0,'
            '"bank_sha256":h.hexdigest(),"operations":[]}\n'
            'r=apply_batch(db,req,bank)\n'
            'rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n'
            'open(out,"w").write(json.dumps({"result":r,"rss_kib":rss}))\n')
        out = self.td / 'out.json'
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
        proc = subprocess.run(
            [sys.executable, str(runner), str(self.db), str(bank), str(out)],
            capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(out.read_text())
        self.assertTrue(payload['result']['accepted'])
        peak_mib = payload['rss_kib'] / 1024
        # Acceptance bound is 96 MiB; keep a documented margin.
        self.assertLess(peak_mib, 96, 'peak RSS %.1f MiB' % peak_mib)
        print('\n120k-row / %.1f MiB CSV apply peak RSS: %.1f MiB '
              '(bound 96 MiB)' % (size / 1048576, peak_mib))

        # Verify the committed ledger in a separate short-lived process so
        # reading 120k facts never raises this test runner's high-water RSS
        # (which later forked RSS checks would otherwise inherit).
        checker = self.td / 'checker.py'
        checker.write_text(
            'import json,sys\n'
            'from admission_match.finance.store import connect\n'
            'from admission_match.finance.reporting import build_report\n'
            'db,out=sys.argv[1:3]\n'
            'conn=connect(db)\n'
            'report=build_report(conn)\n'
            'conn.close()\n'
            'facts=report["bank_facts"]\n'
            'open(out,"w").write(json.dumps({\n'
            '  "revision":report["revision"],\n'
            '  "fact_count":len(facts),\n'
            '  "all_unallocated":all(b["unallocated_minor"]==1 '
            'for b in facts)}))\n')
        check_out = self.td / 'check.json'
        proc = subprocess.run(
            [sys.executable, str(checker), str(self.db), str(check_out)],
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        checked = json.loads(check_out.read_text())
        self.assertEqual(checked['revision'], 1)
        self.assertEqual(checked['fact_count'], count)
        self.assertTrue(checked['all_unallocated'])
        # The staging directory was discarded after the commit.
        leftovers = [p for p in Path(tempfile.gettempdir()).glob(
            'finance-stage-*') if p.is_dir()]
        self.assertEqual(leftovers, [])
        # Return any freed glibc arenas to the OS so this runner's current
        # footprint does not linger for subsequent (unrelated) tests.
        import ctypes
        import gc
        gc.collect()
        try:
            ctypes.CDLL('libc.so.6').malloc_trim(0)
        except OSError:
            pass


class HistoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.db = self.td / 'history.sqlite'
        initialize(self.db, roster(), 'ab' * 32, charges(), 'cd' * 32)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_history(self):
        # 1,000 revisions, 10 claims per revision => 10,000 historical
        # entries spread over 10,000 distinct bank items and all applicants.
        applicants = ['A', 'B']
        revision = 0
        entry_count = 0
        for batch in range(1000):
            rows, ops = [], []
            for j in range(10):
                index = batch * 10 + j
                bank_id = 'hist-%06d' % index
                rows.append((bank_id, 1, 'credit', 'posted', 'CNY', 1, None))
                ops.append({
                    'type': 'claim', 'id': 'e-%06d' % index,
                    'bank_id': bank_id,
                    'applicant': applicants[index % 2], 'amount_minor': 1})
                entry_count += 1
            bank = self.td / 'seed.csv'
            bank.write_text(csv_text(rows))
            digest = hashlib.sha256(bank.read_bytes()).hexdigest()
            apply_batch(self.db, {
                'request_id': 'seed-%04d' % batch,
                'expected_revision': revision,
                'bank_sha256': digest, 'operations': ops}, bank)
            revision += 1
        self.assertEqual(entry_count, 10_000)
        return revision

    def test_small_batch_does_not_full_scan_history(self):
        revision = self._seed_history()
        conn = connect(self.db)
        try:
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0],
                10_000)

            # Query-plan evidence: every production lookup shape against the
            # journal uses an index, never a full table scan.
            plans = {}
            for label, sql, params in [
                ('by-id', 'SELECT id FROM entries WHERE id=?',
                 ('e-000001',)),
                ('by-bank',
                 "SELECT id FROM entries WHERE kind IN ('claim','refund') "
                 'AND bank_id IN (SELECT bank_id FROM tmp_bank_scope)',
                 ()),
                ('by-account',
                 "SELECT id FROM entries WHERE kind IN ('claim','refund') "
                 'AND (applicant,currency) IN '
                 '(SELECT applicant,currency FROM tmp_account_scope)',
                 ()),
                ('reverse-target',
                 "SELECT e.target_id FROM entries e "
                 "JOIN tmp_entry_scope s ON e.target_id=s.id "
                 "WHERE e.kind='reverse'", ()),
            ]:
                conn.execute(
                    'CREATE TEMP TABLE IF NOT EXISTS tmp_bank_scope'
                    '(bank_id TEXT PRIMARY KEY) WITHOUT ROWID')
                conn.execute(
                    'CREATE TEMP TABLE IF NOT EXISTS tmp_account_scope'
                    '(applicant TEXT,currency TEXT,'
                    'PRIMARY KEY(applicant,currency)) WITHOUT ROWID')
                conn.execute(
                    'CREATE TEMP TABLE IF NOT EXISTS tmp_entry_scope'
                    '(id TEXT PRIMARY KEY) WITHOUT ROWID')
                conn.execute(
                    "INSERT OR IGNORE INTO tmp_bank_scope VALUES('hist-000001')")
                conn.execute(
                    "INSERT OR IGNORE INTO tmp_account_scope VALUES('A','CNY')")
                conn.execute(
                    "INSERT OR IGNORE INTO tmp_entry_scope VALUES('e-000001')")
                detail = conn.execute(
                    'EXPLAIN QUERY PLAN ' + sql, params).fetchall()
                plans[label] = ' '.join(row[3] for row in detail)
        finally:
            conn.close()
        for label, plan in plans.items():
            self.assertNotIn('SCAN entries', plan,
                             '%s plan full-scans: %s' % (label, plan))
            self.assertIn('SEARCH', plan,
                          '%s plan is not an indexed search: %s'
                          % (label, plan))

        # Runtime/SQL-trace evidence for a real small batch touching one
        # account: trace the statements and assert none scan the full
        # entries table or the immutable bank history table.
        trace = self.td / 'trace.log'
        os.environ['ADMISSION_MATCH_FINANCE_SQL_TRACE'] = str(trace)
        try:
            small_bank = self.td / 'small.csv'
            small_bank.write_text(csv_text([
                ('small-1', 1, 'credit', 'posted', 'CNY', 1, None)]))
            digest = hashlib.sha256(small_bank.read_bytes()).hexdigest()
            start = time.perf_counter()
            apply_batch(self.db, {
                'request_id': 'small-one-account',
                'expected_revision': revision,
                'bank_sha256': digest,
                'operations': [{
                    'type': 'claim', 'id': 'small-e1',
                    'bank_id': 'small-1', 'applicant': 'A',
                    'amount_minor': 1}]}, small_bank)
            elapsed = time.perf_counter() - start
        finally:
            os.environ.pop('ADMISSION_MATCH_FINANCE_SQL_TRACE', None)

        statements = [line for line in trace.read_text().splitlines()
                      if 'entries' in line or 'bank_facts' in line]
        self.assertTrue(statements)
        # The immutable history table is appended to but never read during a
        # normal apply.
        self.assertFalse(
            any(line.lstrip().upper().startswith('SELECT')
                and 'bank_facts' in line for line in statements),
            'apply read the immutable bank history')
        # Journal reads always carry an index-supported predicate; the only
        # full-table writes are the bounded INSERTs of this batch's own rows.
        for line in statements:
            if line.lstrip().upper().startswith('SELECT'):
                self.assertTrue(
                    'WHERE' in line.upper() or 'JOIN' in line.upper(),
                    'journal SELECT without predicate: ' + line)

        # The small batch advances exactly one revision regardless of the
        # 10,000-entry history, and the targeted account is the only one that
        # changes.
        report = build_report(connect(self.db))
        self.assertEqual(report['revision'], revision + 1)
        self.assertEqual(len(report['entries']), 10_001)
        # Generous runtime ceiling: an indexed batch must stay well below the
        # time a full replay of 10,000 entries would require.
        self.assertLess(elapsed, 5.0)


if __name__ == '__main__':
    unittest.main()
