"""Production-scale checks: capped sizes, caps respected, and matching cost
independent of request-log length.  No exhaustive enumeration is used at
production scale; the brute-force oracle only exists in test_regional.py for
tiny instances.
"""
import json
import random
import tempfile
import time
import unittest
from pathlib import Path

from admission_match.store import (
    WorkbenchError, apply_request, initialize, build_export, connect,
)


def build_large_source(seed=7):
    rng = random.Random(seed)
    n_a, n_p, n_r = 500, 100, 40
    aids = ['a%03d' % i for i in range(n_a)]
    pids = ['p%03d' % i for i in range(n_p)]
    rids = ['r%03d' % i for i in range(n_r)]
    regions = [{'id': rids[0], 'parent': None, 'capacity': 500}]
    for i in range(1, n_r):
        regions.append({'id': rids[i], 'parent': rids[rng.randrange(i)],
                        'capacity': rng.randrange(50, 501)})
    prefs = {a: rng.sample(pids, rng.randrange(5, 30)) for a in aids}
    by_program = {p: [] for p in pids}
    for a, choices in prefs.items():
        for p in choices:
            by_program[p].append(a)
    programs = []
    for p in pids:
        ranking = by_program[p][:]
        rng.shuffle(ranking)
        programs.append({
            'id': p,
            'capacity': rng.randrange(0, 20),
            'region': rids[rng.randrange(n_r)],
            'ranking': ranking,
        })
    source = {
        'applicants': [{'id': a, 'preferences': prefs[a]} for a in aids],
        'programs': programs,
        'regions': regions,
    }
    return source


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.db = self.td / 'big.sqlite'
        initialize(self.db, build_large_source(), 'ab' * 32)

    def tearDown(self):
        self.tmp.cleanup()

    def test_caps_and_audit_at_full_size(self):
        report = build_export(connect(self.db))
        self.assertEqual(report['blocking_pairs'], [])
        for region in report['regions']:
            self.assertLessEqual(region['occupied'], region['capacity'])
        for program in report['programs']:
            self.assertLessEqual(len(program['accepted']),
                                 program['capacity'])
        # At most one offer per applicant; offer list sorted by applicant.
        names = [o['applicant'] for o in report['offers']]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), len(set(names)))

    def test_apply_cost_independent_of_log_length(self):
        # Each request changes one program capacity, so the log genuinely
        # grows while the current instance stays the same size.
        def timed_apply(rid, rev):
            req = {'request_id': rid, 'expected_revision': rev,
                   'operations': [
                       {'type': 'set_capacity', 'kind': 'program',
                        'id': 'p000', 'capacity': 10}]}
            start = time.perf_counter()
            apply_request(self.db, req)
            return time.perf_counter() - start

        first = timed_apply('r00', 0)
        for i in range(1, 25):
            apply_request(self.db, {
                'request_id': 'r%02d' % i, 'expected_revision': i,
                'operations': [
                    {'type': 'set_capacity', 'kind': 'program',
                     'id': 'p000', 'capacity': 10 + (i % 3)}]})
        later = timed_apply('r25', 25)
        # Generous bound: replay lookup is indexed and matching never scans
        # the log, so a 4x allowance is far above any measured noise.
        self.assertLess(later, max(first * 4, 2.0))
        report = build_export(connect(self.db))
        self.assertEqual(report['revision'], 26)
        self.assertEqual(len(report['request_log']), 26)
        self.assertEqual(report['blocking_pairs'], [])

    def test_batches_above_1000_operations_rejected(self):
        ops = [{'type': 'withdraw', 'applicant': 'a000'}] * 1001
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, {
                'request_id': 'huge', 'expected_revision': 0,
                'operations': ops})


if __name__ == '__main__':
    unittest.main()
