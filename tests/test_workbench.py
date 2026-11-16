"""End-to-end workbench tests: persistence, tokens, batches, concurrency,
crash recovery, deterministic export and the linked acceptance scenarios.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from admission_match.cli import main
from admission_match.common import read
from admission_match.store import (
    WorkbenchError, apply_request, initialize, export_report, build_export,
    connect, make_token, request_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]


def workbench_source():
    return json.loads((ROOT / 'fixtures' / 'workbench.json').read_text())


def request(rid, rev, *operations):
    return {'request_id': rid, 'expected_revision': rev,
            'operations': list(operations)}


def confirm(aid, token):
    return {'type': 'confirm', 'applicant': aid, 'offer_token': token}


class WorkbenchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.db = self.td / 'wb.sqlite'
        self.data = workbench_source()
        initialize(self.db, self.data, '0' * 64)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def export(self):
        return build_export(connect(self.db))

    def offers(self):
        report = self.export()
        return {o['applicant']: (o['program'], o['offer_token'])
                for o in report['offers']}

    def status(self):
        return {row[0]: row[1] for row in
                self.conn.execute('SELECT id,status FROM applicants')}


class InitializationTests(WorkbenchBase):
    def test_revision_zero_and_full_prefs_rebuild(self):
        report = self.export()
        self.assertEqual(report['revision'], 0)
        self.assertEqual(report['blocking_pairs'], [])
        # root cap 2, leaf caps 1: X and Y each admit their top rank.
        self.assertEqual({o['applicant'] for o in report['offers']},
                         {'B', 'D'})
        root = next(r for r in report['regions'] if r['region'] == 'root')
        self.assertEqual((root['occupied'], root['capacity']), (2, 2))

    def test_refuses_existing_database(self):
        with self.assertRaises(WorkbenchError):
            initialize(self.db, self.data, '0' * 64)

    def test_empty_applicants_and_zero_capacity(self):
        data, digest = read(ROOT / 'fixtures' / 'empty_workbench.json')
        db = self.td / 'empty.sqlite'
        initialize(db, data, digest)
        report = build_export(connect(db))
        self.assertEqual(report['applicants'], [])
        self.assertEqual(report['offers'], [])
        self.assertEqual(report['blocking_pairs'], [])

    def test_region_tree_validation(self):
        bad = json.loads(json.dumps(self.data))
        bad['regions'].append({'id': 'root', 'parent': None, 'capacity': 1})
        with self.assertRaises((ValueError, WorkbenchError)):
            initialize(self.td / 'dup.sqlite', bad, '0' * 64)
        bad = json.loads(json.dumps(self.data))
        bad['regions'][0]['parent'] = 'r-east'  # cycle
        with self.assertRaises((ValueError, WorkbenchError)):
            initialize(self.td / 'cyc.sqlite', bad, '0' * 64)
        bad = json.loads(json.dumps(self.data))
        bad['programs'][0]['region'] = 'ghost'
        with self.assertRaises((ValueError, WorkbenchError)):
            initialize(self.td / 'ghost.sqlite', bad, '0' * 64)


class RequestValidationTests(WorkbenchBase):
    def test_strict_fields_and_bounds(self):
        for bad in [
            {},
            {'request_id': 'q', 'expected_revision': -1, 'operations': []},
            {'request_id': 'q', 'expected_revision': 0,
             'operations': [{'type': 'withdraw'}]},
            {'request_id': 'q', 'expected_revision': 0,
             'operations': [{'type': 'set_capacity', 'kind': 'school',
                             'id': 'X', 'capacity': 1}]},
            {'request_id': 'q', 'expected_revision': 0,
             'operations': [{'type': 'set_capacity', 'kind': 'program',
                             'id': 'X', 'capacity': 501}]},
            {'request_id': 'q', 'expected_revision': 0,
             'operations': [{'type': 'confirm', 'applicant': 'B',
                             'offer_token': 12}]},
        ]:
            with self.assertRaises(WorkbenchError):
                apply_request(self.db, bad)

    def test_duplicate_applicant_and_target_in_batch(self):
        token = self.offers()['B'][1]
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('q', 0,
                                           confirm('B', token),
                                           {'type': 'withdraw',
                                            'applicant': 'B'}))
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('q', 0,
                                           {'type': 'set_capacity',
                                            'kind': 'program', 'id': 'X',
                                            'capacity': 0},
                                           {'type': 'set_capacity',
                                            'kind': 'program', 'id': 'X',
                                            'capacity': 1}))

    def test_too_many_operations(self):
        ops = [{'type': 'withdraw', 'applicant': 'A'}] * 1001
        with self.assertRaises(WorkbenchError):
            apply_request(self.db,
                          {'request_id': 'big', 'expected_revision': 0,
                           'operations': ops})


class TokenTests(WorkbenchBase):
    def test_token_bound_to_revision_and_db(self):
        pid, token = self.offers()['B']
        # Token from another database secret.
        foreign = make_token('ff' * 32, 0, 'B', pid)
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('q1', 0, confirm('B', foreign)))
        # Well-formed token but wrong revision.
        stale = make_token(self.conn.execute(
            "SELECT value FROM meta WHERE key='db_secret'").fetchone()[0],
            7, 'B', pid)
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('q2', 0, confirm('B', stale)))
        # The genuine token confirms.
        result = apply_request(self.db, request('q3', 0, confirm('B', token)))
        self.assertTrue(result['accepted'])
        # The old revision token can never be used again afterwards.
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('q4', 1, confirm('B', token)))

    def test_confirm_requires_actual_offer(self):
        # A and C hold no offer at revision 0.
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('q', 0,
                                           confirm('A', 'x' * 16)))


class StateTransitionTests(WorkbenchBase):
    def test_confirm_withdraw_lifecycle(self):
        token = self.offers()['B'][1]
        apply_request(self.db, request('c', 0, confirm('B', token)))
        self.assertEqual(self.status()['B'], 'confirmed')
        # A second confirm is an illegal transition.
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('c2', 1, confirm('B', token)))
        # A withdrawn applicant can never return.
        apply_request(self.db, request('w', 1,
                                       {'type': 'withdraw',
                                        'applicant': 'B'}))
        self.assertEqual(self.status()['B'], 'withdrawn')
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('w2', 2,
                                           {'type': 'withdraw',
                                            'applicant': 'B'}))
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('c3', 2, confirm('B', token)))

    def test_failure_leaves_no_log_and_no_revision_change(self):
        before = self.export()
        # Confirm B first, then cutting the ancestor below its confirmed
        # count makes the final state infeasible.
        token_b = self.offers()['B'][1]
        apply_request(self.db, request('ok', 0, confirm('B', token_b)))
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request(
                'bad', 1, {'type': 'set_capacity', 'kind': 'region',
                           'id': 'r-east', 'capacity': 0}))
        after = self.export()
        self.assertEqual(after['revision'], 1)  # the failed batch never counted
        self.assertEqual(len(after['request_log']), 1)
        self.assertEqual(self.status()['B'], 'confirmed')


class BatchSemanticsTests(WorkbenchBase):
    def test_withdraw_and_capacity_cut_order_equivalence(self):
        # Confirm B at X; root holds B and D.  Then withdraw B while cutting
        # the root capacity to 1 in the same batch.  Either operation order
        # describes the same final state and must succeed identically.
        token_b = self.offers()['B'][1]
        apply_request(self.db, request('c', 0, confirm('B', token_b)))
        ops = [{'type': 'withdraw', 'applicant': 'B'},
               {'type': 'set_capacity', 'kind': 'region', 'id': 'root',
                'capacity': 1}]
        r1 = apply_request(self.db,
                           request('batch', 1, *ops))
        # Reversed order inside a fresh database copy.
        db2 = self.td / 'copy.sqlite'
        import shutil
        shutil.copy(self.db, db2)
        r2 = apply_request(db2, request('batch', 1, *reversed(ops)))
        self.assertEqual(r1, r2)
        report = self.export()
        self.assertEqual(report['revision'], 2)
        self.assertEqual(self.status()['B'], 'withdrawn')
        root = next(r for r in report['regions'] if r['region'] == 'root')
        self.assertEqual(root['capacity'], 1)
        # Only the west seat survives the tighter root quota.
        self.assertEqual({o['program'] for o in report['offers']}, {'Y'})

    def test_withdraw_holder_then_zero_program_capacity(self):
        # The only confirmed holder of X withdraws while X is cut to zero
        # capacity in the same final state: feasible, so the batch succeeds.
        token_b = self.offers()['B'][1]
        apply_request(self.db, request('c', 0, confirm('B', token_b)))
        result = apply_request(self.db, request(
            'zero-x', 1,
            {'type': 'withdraw', 'applicant': 'B'},
            {'type': 'set_capacity', 'kind': 'program', 'id': 'X',
             'capacity': 0}))
        self.assertTrue(result['accepted'])
        report = self.export()
        self.assertEqual(report['revision'], 2)
        x = next(p for p in report['programs'] if p['program'] == 'X')
        self.assertEqual((x['capacity'], x['accepted']), (0, []))
        # No offer can ever point at a zero-capacity program.
        self.assertNotIn('X', {o['program'] for o in report['offers']})

    def test_capacity_cut_above_confirmed_count_fails_atomically(self):
        token_b = self.offers()['B'][1]
        apply_request(self.db, request('c', 0, confirm('B', token_b)))
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request(
                'tight', 1,
                {'type': 'set_capacity', 'kind': 'program', 'id': 'X',
                 'capacity': 0}))
        report = self.export()
        self.assertEqual(report['revision'], 1)
        self.assertEqual(self.status()['B'], 'confirmed')

    def test_confirmed_seat_protected_from_rebuild(self):
        # Shrink east (X's region) to the single confirmed seat and rebuild.
        token_b = self.offers()['B'][1]
        apply_request(self.db, request('c', 0, confirm('B', token_b)))
        apply_request(self.db, request(
            'cap', 1,
            {'type': 'set_capacity', 'kind': 'region', 'id': 'r-east',
             'capacity': 1}))
        report = self.export()
        b = next(a for a in report['applicants'] if a['id'] == 'B')
        self.assertEqual((b['status'], b['assignment']),
                         ('confirmed', 'X'))


class ReplayAndConflictTests(WorkbenchBase):
    def test_replay_ignores_key_and_operation_order(self):
        token = self.offers()['B'][1]
        body = request('r1', 0, confirm('B', token))
        first = apply_request(self.db, body)
        # Same canonical content with shuffled object keys, a different
        # operation order and a stale expectation: the id lookup still
        # recognizes the replay and returns the first result without changes.
        shuffled = {
            'operations': [{'offer_token': token, 'applicant': 'B',
                            'type': 'confirm'}],
            'expected_revision': 0, 'request_id': 'r1',
        }
        again = apply_request(self.db, shuffled)
        self.assertEqual(again, first)
        self.assertEqual(self.export()['revision'], 1)
        # Re-sending the identical request (same expectation as originally
        # recorded) still replays after the database advanced, because the id
        # lookup short-circuits the live expected-revision check.
        identical = {'request_id': 'r1', 'expected_revision': 0,
                     'operations': [confirm('B', token)]}
        self.assertEqual(apply_request(self.db, identical), first)
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('r1', 1, []))
        with self.assertRaises(WorkbenchError):
            # Different expectation under the same id is different content.
            apply_request(self.db, {'request_id': 'r1',
                                    'expected_revision': 99,
                                    'operations': [confirm('B', token)]})

    def test_same_id_different_content_conflicts(self):
        token = self.offers()['B'][1]
        apply_request(self.db, request('r1', 0, confirm('B', token)))
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('r1', 1, []))

    def test_expected_revision_mismatch(self):
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('late', 5, []))

    def test_fingerprint_canonicalizes_order_only(self):
        a = {'request_id': 'r', 'expected_revision': 0,
             'operations': [{'type': 'withdraw', 'applicant': 'A'},
                            {'type': 'withdraw', 'applicant': 'B'}]}
        b = {'expected_revision': 0, 'request_id': 'r',
             'operations': [{'applicant': 'B', 'type': 'withdraw'},
                            {'applicant': 'A', 'type': 'withdraw'}]}
        self.assertEqual(request_fingerprint(a), request_fingerprint(b))
        c = dict(a, expected_revision=1)
        self.assertNotEqual(request_fingerprint(a),
                            request_fingerprint(c))
        d = dict(a, operations=[])
        self.assertNotEqual(request_fingerprint(a),
                            request_fingerprint(d))


class CrossRegionRecompetitionTests(unittest.TestCase):
    """Ancestor-full rejection, freed quota, and token versioning together."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.db = self.td / 'wb.sqlite'
        # Two leaf regions under one root with a single shared seat.
        source = {
            'applicants': [
                {'id': 'A', 'preferences': ['X', 'Y']},
                {'id': 'B', 'preferences': ['X']},
            ],
            'programs': [
                {'id': 'X', 'capacity': 1, 'region': 'r1',
                 'ranking': ['A', 'B']},
                {'id': 'Y', 'capacity': 1, 'region': 'r2',
                 'ranking': ['A']},
            ],
            'regions': [
                {'id': 'root', 'parent': None, 'capacity': 1},
                {'id': 'r1', 'parent': 'root', 'capacity': 1},
                {'id': 'r2', 'parent': 'root', 'capacity': 1},
            ],
        }
        initialize(self.db, source, '11' * 32)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_withdraw_reopens_ancestor_quota_and_rotates_token(self):
        secret = self.conn.execute(
            "SELECT value FROM meta WHERE key='db_secret'").fetchone()[0]
        report = build_export(connect(self.db))
        # A wins the single root seat at X; B is rejected even though X
        # itself has spare capacity, because the ancestor is full.
        self.assertEqual([(o['applicant'], o['program'])
                          for o in report['offers']], [('A', 'X')])
        token_a = report['offers'][0]['offer_token']
        apply_request(self.db, request('c', 0, confirm('A', token_a)))

        # An old-revision token forged for B cannot confirm anything.
        forged = make_token(secret, 0, 'B', 'X')
        with self.assertRaises(WorkbenchError):
            apply_request(self.db,
                          request('forged', 1, confirm('B', forged)))

        # A withdraws; B re-competes for the freed ancestor seat and wins.
        apply_request(self.db, request('w', 1,
                                       {'type': 'withdraw',
                                        'applicant': 'A'}))
        report = build_export(connect(self.db))
        self.assertEqual(report['revision'], 2)
        self.assertEqual([(o['applicant'], o['program'])
                          for o in report['offers']], [('B', 'X')])
        new_token = report['offers'][0]['offer_token']
        self.assertEqual(report['offers'][0]['revision'], 2)
        # A revision-0 token for B (same db secret, old revision) fails;
        # the freshly issued revision-2 token succeeds.
        old = make_token(secret, 0, 'B', 'X')
        with self.assertRaises(WorkbenchError):
            apply_request(self.db,
                          request('old', 2, confirm('B', old)))
        result = apply_request(self.db,
                               request('new', 2, confirm('B', new_token)))
        self.assertTrue(result['accepted'])
        self.assertEqual(build_export(connect(self.db))['revision'], 3)


class LinkedScenarioTests(WorkbenchBase):
    """The acceptance narrative: protected confirm, freed ancestor quota,
    token versioning after the rebuilt offers."""

    def test_full_narrative(self):
        # Revision 0: B->X and D->Y fill both root seats.  A and C are
        # rejected even though programs have capacity, because the ancestor
        # root is full.
        self.assertEqual(set(self.offers()), {'B', 'D'})
        token_b = self.offers()['B'][1]
        # A confirms the shared-region seat first via B? The narrative uses
        # generic names; here B plays the protected applicant A-role.
        apply_request(self.db, request('confirm-b', 0,
                                       confirm('B', token_b)))
        # Adjusting the sibling program Y must not dislodge the confirmation.
        apply_request(self.db, request(
            'y-cap', 1,
            {'type': 'set_capacity', 'kind': 'program', 'id': 'Y',
             'capacity': 2}))
        report = self.export()
        self.assertEqual(
            next(a for a in report['applicants'] if a['id'] == 'B')
            ['status'], 'confirmed')

        # Withdraw B: the freed root seat lets a previously rejected
        # applicant compete again.
        apply_request(self.db, request(
            'release', 2, {'type': 'withdraw', 'applicant': 'B'}))
        report = self.export()
        winners = {o['applicant']: o['program'] for o in report['offers']}
        # X's ranking is B,A,C,D; B is gone, so A takes X.
        self.assertEqual(winners.get('A'), 'X')

        # The old revision-0 token for A never existed; fabricate one with
        # the current db secret at the old revision and prove it fails, then
        # confirm the freshly issued revision-3 token.
        secret = self.conn.execute(
            "SELECT value FROM meta WHERE key='db_secret'").fetchone()[0]
        old = make_token(secret, 0, 'A', 'X')
        with self.assertRaises(WorkbenchError):
            apply_request(self.db, request('old-token', 3,
                                           confirm('A', old)))
        new_token = next(
            o['offer_token'] for o in report['offers']
            if o['applicant'] == 'A')
        result = apply_request(self.db,
                               request('new-token', 3,
                                       confirm('A', new_token)))
        self.assertTrue(result['accepted'])
        self.assertEqual(self.status()['A'], 'confirmed')


class ExportTests(WorkbenchBase):
    def test_deterministic_bytes_and_independent_occupancy(self):
        token = self.offers()['B'][1]
        apply_request(self.db, request('c', 0, confirm('B', token)))
        out1 = self.td / 'r1.json'
        out2 = self.td / 'r2.json'
        export_report(self.db, out1)
        export_report(self.db, out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        report = json.loads(out1.read_text())
        # Region occupancy is recomputed from final assignments.
        occupied = {r['region']: r['occupied'] for r in report['regions']}
        east = next(r for r in report['regions'] if r['region'] == 'r-east')
        self.assertEqual(east['occupied'], 1)
        self.assertEqual(occupied['root'], 2)
        # The log keeps the canonical request plus its recorded result.
        self.assertEqual(len(report['request_log']), 1)
        entry = report['request_log'][0]
        self.assertEqual(entry['request']['request_id'], 'c')
        self.assertTrue(entry['result']['accepted'])
        # Offers are sorted by applicant; all ids sorted throughout.
        self.assertEqual([o['applicant'] for o in report['offers']],
                         sorted(o['applicant'] for o in report['offers']))

    def test_failed_export_keeps_old_report(self):
        out = self.td / 'report.json'
        export_report(self.db, out)
        first = out.read_bytes()
        # Pointing export at a directory path forces an OSError at replace.
        with self.assertRaises(OSError):
            export_report(self.db, self.td)
        self.assertEqual(out.read_bytes(), first)


class CrashRecoveryTests(WorkbenchBase):
    def _apply_subprocess(self, request_file, fault=None):
        env = dict(os.environ)
        if fault:
            env['ADMISSION_MATCH_FAULT'] = fault
        proc = subprocess.run(
            [sys.executable, '-m', 'admission_match.cli', 'apply',
             '--db', str(self.db), '--request', str(request_file)],
            capture_output=True, text=True,
            env={**env, 'PYTHONPATH': str(ROOT / 'src')})
        return proc

    def test_crash_between_confirm_and_offer_replacement(self):
        token = self.offers()['B'][1]
        req_path = self.td / 'req.json'
        req_path.write_text(json.dumps(request(
            'crash', 0, confirm('B', token))))
        proc = self._apply_subprocess(req_path, fault='before_offers')
        self.assertEqual(proc.returncode, 91)
        # Reopening must show the complete old revision only.
        report = self.export()
        self.assertEqual(report['revision'], 0)
        self.assertEqual(self.status()['B'], 'active')
        self.assertIn('B', {o['applicant'] for o in report['offers']})
        self.assertEqual(report['request_log'], [])
        # The request id was never consumed, so a clean retry succeeds.
        proc = self._apply_subprocess(req_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.export()
        self.assertEqual(report['revision'], 1)
        self.assertEqual(self.status()['B'], 'confirmed')

    def test_crash_before_commit_rolls_back(self):
        token = self.offers()['D'][1]
        req_path = self.td / 'req2.json'
        req_path.write_text(json.dumps(request(
            'crash2', 0, confirm('D', token))))
        proc = self._apply_subprocess(req_path, fault='before_commit')
        self.assertEqual(proc.returncode, 91)
        report = self.export()
        self.assertEqual(report['revision'], 0)
        self.assertEqual(self.status()['D'], 'active')


class ConcurrencyTests(WorkbenchBase):
    def test_two_processes_contend_same_revision(self):
        script = self.td / 'worker.py'
        script.write_text(
            'import json,sys\n'
            'from admission_match.store import apply_request\n'
            'db,rid,out=sys.argv[1:4]\n'
            'req={"request_id":rid,"expected_revision":0,"operations":[]}\n'
            'try:\n'
            '    r=apply_request(db,req)\n'
            '    open(out,"w").write("OK "+json.dumps(r["revision"]))\n'
            'except Exception as e:\n'
            '    open(out,"w").write("ERR "+str(e))\n')
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
        procs = [
            subprocess.Popen([sys.executable, str(script), str(self.db),
                              'w%d' % i, str(self.td / ('o%d' % i))],
                             env=env)
            for i in range(2)]
        codes = [p.wait(timeout=60) for p in procs]
        self.assertEqual(codes, [0, 0])
        outcomes = [(self.td / ('o%d' % i)).read_text() for i in range(2)]
        self.assertEqual(sorted(outcomes)[0].startswith('ERR'), True)
        self.assertEqual(sorted(outcomes)[1], 'OK 1')
        report = self.export()
        self.assertEqual(report['revision'], 1)
        self.assertEqual(len(report['request_log']), 1)
        # Offers and quotas remain internally consistent for the winner.
        self.assertEqual(report['blocking_pairs'], [])
        root = next(r for r in report['regions'] if r['region'] == 'root')
        self.assertLessEqual(root['occupied'], root['capacity'])


class CliDispatchTests(unittest.TestCase):
    def test_legacy_and_subcommands_via_main(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td, \
                contextlib.redirect_stdout(io.StringIO()):
            td = Path(td)
            db = td / 'wb.sqlite'
            src = ROOT / 'fixtures' / 'workbench.json'
            self.assertEqual(main(['init', '--source', str(src),
                                   '--db', str(db)]), 0)
            # init never overwrites an existing database.
            self.assertEqual(main(['init', '--source', str(src),
                                   '--db', str(db)]), 2)
            req = td / 'req.json'
            req.write_text(json.dumps(request('noop', 0)))
            self.assertEqual(main(['apply', '--db', str(db),
                                   '--request', str(req)]), 0)
            out = td / 'report.json'
            self.assertEqual(main(['export', '--db', str(db),
                                   '--output', str(out)]), 0)
            report = json.loads(out.read_text())
            self.assertEqual(report['revision'], 1)
            # Data errors exit 2.
            bad = td / 'bad.json'
            bad.write_text('{}')
            self.assertEqual(main(['apply', '--db', str(db),
                                   '--request', str(bad)]), 2)


if __name__ == '__main__':
    unittest.main()
