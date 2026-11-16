"""Pre-import bilateral declaration audit tests.

Covers declaration classification (mutual / applicant_only / program_only /
undeclared), 0-based positions on both sides, empty lists, the strict source
and policy contracts, integer-only share thresholds, waiver semantics, exit
codes 0/1/2, output path safety, byte determinism, and a monkeypatch proof
that the audit path never executes matching or touches the workbench.
"""
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import admission_match.cli as cli
import admission_match.matching as matching
import admission_match.model as model
import admission_match.store as store
from admission_match.audit import (
    audit_report, build_declaration_ledger, parse_declaration_source,
    parse_policy,
)
from admission_match.cli import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SOURCE = ROOT / 'fixtures' / 'audit_source.json'
FIXTURE_POLICY = ROOT / 'fixtures' / 'audit_policy.json'
UNIT_DIGEST = hashlib.sha256(b'unit').hexdigest()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source(applicants, programs):
    return {
        'applicants': [{'id': a, 'preferences': list(prefs)}
                       for a, prefs in applicants],
        'programs': [{'id': p, 'capacity': cap, 'ranking': list(ranking)}
                     for p, cap, ranking in programs],
    }


def waiver(aid, pid, reason='confirmed with the school'):
    return {'applicant': aid, 'program': pid, 'reason': reason}


def policy(digest, waivers=(), count=500, share=(500, 500)):
    return {
        'source_sha256': digest,
        'waivers': [dict(w) for w in waivers],
        'limits': {
            'max_unresolved_per_program': count,
            'max_unresolved_share': {'numerator': share[0],
                                     'denominator': share[1]},
        },
    }


def run_audit(data, waivers=(), count=500, share=(500, 500)):
    return audit_report(data, UNIT_DIGEST,
                        policy(UNIT_DIGEST, waivers, count, share), 'p' * 64)


class ClassificationTests(unittest.TestCase):
    """Sparse ledger: union of declarations, never the cross product."""

    def test_unilateral_positions_and_record_order(self):
        data = source([('B', ['P', 'Q']), ('A', ['Q']), ('C', [])],
                      [('P', 1, ['B']), ('Q', 2, ['A', 'C'])])
        report = run_audit(data)
        self.assertEqual(report['totals'],
                         {'mutual': 2, 'applicant_only': 1,
                          'program_only': 1})
        # Records hold only unilateral pairs, sorted by (applicant, program),
        # with 0-based positions and null on the undeclared side.
        self.assertEqual(report['records'], [
            {'applicant': 'B', 'program': 'Q', 'kind': 'applicant_only',
             'applicant_position': 1, 'program_position': None,
             'status': 'unresolved', 'waiver_reason': None},
            {'applicant': 'C', 'program': 'Q', 'kind': 'program_only',
             'applicant_position': None, 'program_position': 1,
             'status': 'unresolved', 'waiver_reason': None},
        ])

    def test_both_positions_of_mutual_pair_stay_in_ledger(self):
        data = source([('A', ['Q', 'P'])], [('P', 1, ['B', 'A']), ('Q', 1, ['A']), ])
        # B must exist for P's ranking; give B empty preferences.
        data['applicants'].append({'id': 'B', 'preferences': []})
        ledger = build_declaration_ledger(parse_declaration_source(data))
        self.assertEqual(ledger[('A', 'P')]['applicant_position'], 1)
        self.assertEqual(ledger[('A', 'P')]['program_position'], 1)
        self.assertEqual(ledger[('A', 'P')]['kind'], 'mutual')
        self.assertEqual(ledger[('B', 'P')]['kind'], 'program_only')
        self.assertEqual(ledger[('B', 'P')]['program_position'], 0)

    def test_mutual_and_undeclared_pairs_produce_no_records(self):
        data = source([('A', ['P'])], [('P', 1, ['A']), ('Q', 1, [])])
        report = run_audit(data)
        self.assertEqual(report['records'], [])
        self.assertEqual(report['totals'],
                         {'mutual': 1, 'applicant_only': 0,
                          'program_only': 0})
        # (A, Q) is declared by neither side: no record, no ledger entry.
        programs = {p['program']: p for p in report['programs']}
        self.assertEqual(programs['Q']['declarations'], 0)
        self.assertEqual(programs['Q']['unresolved'], 0)
        self.assertTrue(programs['Q']['ok'])
        self.assertTrue(report['ok'])

    def test_only_unilateral_declarations(self):
        data = source([('A', ['P']), ('B', [])],
                      [('P', 1, ['B']), ('Q', 1, [])])
        report = run_audit(data)
        self.assertEqual(report['totals'],
                         {'mutual': 0, 'applicant_only': 1,
                          'program_only': 1})
        self.assertEqual([(r['applicant'], r['program'], r['kind'])
                          for r in report['records']],
                         [('A', 'P', 'applicant_only'),
                          ('B', 'P', 'program_only')])

    def test_empty_lists(self):
        report = run_audit(source([], [('P', 0, [])]))
        self.assertEqual(report['totals'],
                         {'mutual': 0, 'applicant_only': 0,
                          'program_only': 0})
        self.assertEqual(report['records'], [])
        only = report['programs'][0]
        self.assertEqual(only['declarations'], 0)
        self.assertTrue(only['within_share_limit'])  # empty union: share 0
        self.assertTrue(report['ok'])

    def test_per_program_summary_counts(self):
        report = run_audit(source(
            [('A', ['X', 'Y']), ('B', ['X']), ('C', ['Y']), ('D', [])],
            [('X', 2, ['A', 'B', 'C']), ('Y', 1, ['A'])]))
        programs = {p['program']: p for p in report['programs']}
        self.assertEqual(programs['X'], {
            'program': 'X', 'declarations': 3, 'mutual': 2,
            'applicant_only': 0, 'program_only': 1, 'waived': 0,
            'unresolved': 1, 'within_count_limit': True,
            'within_share_limit': True, 'ok': True})
        self.assertEqual(programs['Y'], {
            'program': 'Y', 'declarations': 2, 'mutual': 1,
            'applicant_only': 1, 'program_only': 0, 'waived': 0,
            'unresolved': 1, 'within_count_limit': True,
            'within_share_limit': True, 'ok': True})


class SourceContractTests(unittest.TestCase):
    """The audit re-implements the strict source contract on its own."""

    def bad(self, data):
        with self.assertRaises(ValueError):
            parse_declaration_source(data)

    def test_unknown_references_rejected(self):
        self.bad(source([('A', ['missing'])], [('P', 1, [])]))
        self.bad(source([], [('P', 1, ['missing'])]))

    def test_duplicate_ids_rejected(self):
        self.bad(source([('A', []), ('A', [])], [('P', 1, [])]))
        self.bad(source([], [('P', 1, []), ('P', 1, [])]))

    def test_duplicate_list_entries_rejected(self):
        self.bad(source([('A', ['P', 'P'])], [('P', 1, [])]))
        self.bad(source([('A', [])], [('P', 1, ['A', 'A'])]))

    def test_shape_and_bounds_rejected(self):
        self.bad({'applicants': [], 'programs': [], 'extra': 1})
        self.bad({'applicants': []})
        self.bad(source([], []))  # at least one program
        self.bad(source([], [('P', 501, [])]))
        self.bad(source([], [('P', -1, [])]))
        self.bad(source([('a%d' % i, []) for i in range(501)],
                        [('P', 1, [])]))
        self.bad(source([], [('p%d' % i, 1, []) for i in range(101)]))
        self.bad(source([('A', ['P'] * 101)], [('P', 1, [])]))
        self.bad(source([('a%d' % i, []) for i in range(500)],
                        [('P', 1, ['a%d' % i for i in range(500)] + ['a0'])]))

    def test_valid_boundaries_accepted(self):
        data = source([('a%d' % i, []) for i in range(500)],
                      [('p%d' % i, 500, []) for i in range(100)])
        parsed = parse_declaration_source(data)
        self.assertEqual(len(parsed['applicants']), 500)
        self.assertEqual(len(parsed['programs']), 100)


class PolicyContractTests(unittest.TestCase):
    def bad(self, pol, digest=UNIT_DIGEST):
        with self.assertRaises(ValueError):
            parse_policy(pol, digest)

    def test_exact_keys_required(self):
        good = policy(UNIT_DIGEST)
        parse_policy(good, UNIT_DIGEST)  # accepted
        extra = policy(UNIT_DIGEST)
        extra['note'] = 'x'
        self.bad(extra)
        for key in ('source_sha256', 'waivers', 'limits'):
            missing = policy(UNIT_DIGEST)
            del missing[key]
            self.bad(missing)

    def test_source_sha_must_match(self):
        self.bad(policy('0' * 64), digest='1' * 64)
        self.bad(policy(123))  # not a string

    def test_duplicate_waiver_rejected(self):
        self.bad(policy(UNIT_DIGEST, [waiver('A', 'P'), waiver('A', 'P')]))

    def test_reason_must_be_nonempty(self):
        self.bad(policy(UNIT_DIGEST, [waiver('A', 'P', '')]))
        self.bad(policy(UNIT_DIGEST, [waiver('A', 'P', '   ')]))
        self.bad(policy(UNIT_DIGEST, [waiver('A', 'P', None)]))
        self.bad(policy(UNIT_DIGEST, [{'applicant': 'A', 'program': 'P'}]))

    def test_limits_shape_and_bounds(self):
        def bad_limits(limits):
            pol = policy(UNIT_DIGEST)
            pol['limits'] = limits
            self.bad(pol)
        bad_limits({'max_unresolved_per_program': 1})
        bad_limits({'max_unresolved_per_program': 1,
                    'max_unresolved_share': {'numerator': 1, 'denominator': 2},
                    'extra': 1})
        for count in (-1, 501, 1.5, '1', True):
            bad_limits({'max_unresolved_per_program': count,
                        'max_unresolved_share': {'numerator': 1,
                                                 'denominator': 2}})
        for num, den in ((1, 0), (1, 100001), (3, 2), (-1, 2), (1.5, 2),
                         (1, 2.5)):
            bad_limits({'max_unresolved_per_program': 1,
                        'max_unresolved_share': {'numerator': num,
                                                 'denominator': den}})
        # numerator == denominator and denominator == 100000 are valid.
        parse_policy(policy(UNIT_DIGEST, share=(100000, 100000)), UNIT_DIGEST)
        parse_policy(policy(UNIT_DIGEST, share=(0, 1)), UNIT_DIGEST)

    def test_waiver_must_match_real_unilateral_declaration(self):
        data = source([('A', ['P']), ('B', [])], [('P', 1, ['A'])])
        # (A, P) is mutual: waiver rejected even though the pair exists.
        with self.assertRaises(ValueError):
            run_audit(data, [waiver('A', 'P')])
        # (A, Q) does not exist at all.
        with self.assertRaises(ValueError):
            run_audit(data, [waiver('A', 'Q')])
        # (B, P) is declared by neither side.
        with self.assertRaises(ValueError):
            run_audit(data, [waiver('B', 'P')])


class EvaluationTests(unittest.TestCase):
    def two_program_source(self):
        # X keeps one program_only record, Y keeps one applicant_only record.
        return source(
            [('A', ['X', 'Y']), ('B', ['X']), ('C', ['Y']), ('D', [])],
            [('X', 2, ['A', 'B', 'C']), ('Y', 1, ['A'])])

    def test_valid_waiver_reduces_only_its_program(self):
        data = self.two_program_source()
        baseline = run_audit(data)
        waived = run_audit(data, [waiver('C', 'X')])
        # Both unilateral records are still reported; nothing is hidden.
        self.assertEqual(len(waived['records']), 2)
        self.assertEqual(
            [(r['applicant'], r['program'], r['kind'],
              r['applicant_position'], r['program_position'])
             for r in waived['records']],
            [(r['applicant'], r['program'], r['kind'],
              r['applicant_position'], r['program_position'])
             for r in baseline['records']])
        self.assertEqual(waived['totals'], baseline['totals'])
        programs = {p['program']: p for p in waived['programs']}
        # Only the waived program's unresolved count drops, by exactly one.
        self.assertEqual(programs['X']['unresolved'], 0)
        self.assertEqual(programs['Y']['unresolved'], 1)
        self.assertEqual(programs['X']['unresolved']
                         - programs['Y']['unresolved'], -1)
        self.assertEqual(programs['X']['waived'], 1)
        self.assertEqual(programs['Y']['waived'], 0)
        record = next(r for r in waived['records'] if r['program'] == 'X')
        self.assertEqual(record['status'], 'waived')
        self.assertEqual(record['waiver_reason'], 'confirmed with the school')
        # Positions are never rewritten by a waiver.
        self.assertEqual(record['program_position'], 2)
        self.assertIsNone(record['applicant_position'])

    def test_share_boundary_equal_passes_one_unit_above_fails(self):
        data = source([('A', []), ('B', []), ('C', []), ('D', [])],
                      [('P', 4, ['A', 'B', 'C', 'D'])])
        # 2 unresolved of 4 declarations: exactly the 1/2 limit passes.
        report = run_audit(data, [waiver('A', 'P'), waiver('B', 'P')],
                           count=500, share=(1, 2))
        self.assertTrue(report['programs'][0]['within_share_limit'])
        self.assertTrue(report['ok'])
        # One unresolved unit above the same limit fails.
        report = run_audit(data, [waiver('A', 'P')], count=500, share=(1, 2))
        only = report['programs'][0]
        self.assertEqual(only['unresolved'], 3)
        self.assertFalse(only['within_share_limit'])
        self.assertFalse(report['ok'])
        self.assertEqual(report['exceeded_programs'], ['P'])

    def test_share_comparison_uses_integers(self):
        # Share is exactly 1/3.  A limit a hair below (33333/100000) must
        # fail, the equal fraction must pass, and a hair above must pass;
        # only exact integer cross multiplication pins all three outcomes.
        data = source([('A', []), ('B', []), ('C', [])],
                      [('P', 3, ['A', 'B', 'C'])])
        waivers = [waiver('A', 'P'), waiver('B', 'P')]
        report = run_audit(data, waivers, share=(33333, 100000))
        self.assertFalse(report['programs'][0]['within_share_limit'])
        report = run_audit(data, waivers, share=(1, 3))
        self.assertTrue(report['programs'][0]['within_share_limit'])
        report = run_audit(data, waivers, share=(33334, 100000))
        self.assertTrue(report['programs'][0]['within_share_limit'])

    def test_count_boundary(self):
        data = source([('A', []), ('B', [])], [('P', 2, ['A', 'B'])])
        report = run_audit(data, [waiver('A', 'P')], count=1, share=(1, 1))
        self.assertTrue(report['ok'])
        report = run_audit(data, count=1, share=(1, 1))
        self.assertFalse(report['ok'])
        self.assertFalse(report['programs'][0]['within_count_limit'])
        self.assertEqual(report['exceeded_programs'], ['P'])

    def test_empty_union_share_is_zero(self):
        data = source([('A', ['P'])], [('P', 1, ['A']), ('Q', 0, [])])
        report = run_audit(data, share=(0, 1))  # zero tolerance
        self.assertTrue(report['ok'])
        # A single unilateral declaration under zero tolerance fails.
        data = source([('A', ['Q'])], [('P', 1, []), ('Q', 0, [])])
        report = run_audit(data, share=(0, 1))
        self.assertFalse(report['ok'])
        self.assertEqual(report['exceeded_programs'], ['Q'])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.src = self.td / 'source.json'
        self.pol = self.td / 'policy.json'
        self.out = self.td / 'report.json'
        self.src.write_bytes(FIXTURE_SOURCE.read_bytes())
        self.pol.write_bytes(FIXTURE_POLICY.read_bytes())

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def audit(self):
        return self.run_cli(['audit', '--source', str(self.src),
                             '--policy', str(self.pol),
                             '--output', str(self.out)])

    def write_policy(self, pol):
        self.pol.write_text(json.dumps(pol))

    def fixture_policy(self):
        return json.loads(self.pol.read_text())

    def test_fixture_passes_with_exit_zero(self):
        code, stdout, _ = self.audit()
        self.assertEqual(code, 0)
        report = json.loads(self.out.read_text())
        self.assertEqual(json.loads(stdout), report)
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['source_sha256'], sha256(self.src))
        self.assertEqual(report['policy_sha256'], sha256(self.pol))
        self.assertEqual(report['totals'], {'mutual': 3, 'applicant_only': 1,
                                            'program_only': 1})
        self.assertTrue(report['ok'])
        self.assertEqual(report['exceeded_programs'], [])
        # Two programs each hold one unilateral record; only one is waived,
        # both records remain and the unresolved counts differ by one.
        self.assertEqual(len(report['records']), 2)
        programs = {p['program']: p for p in report['programs']}
        self.assertEqual(programs['X']['unresolved'], 0)
        self.assertEqual(programs['Y']['unresolved'], 1)
        self.assertEqual(abs(programs['X']['unresolved']
                             - programs['Y']['unresolved']), 1)

    def test_module_invocation_matches_entry_point(self):
        out2 = self.td / 'report2.json'
        proc = subprocess.run(
            [sys.executable, '-m', 'admission_match.cli', 'audit',
             '--source', str(self.src), '--policy', str(self.pol),
             '--output', str(out2)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        code, _, _ = self.audit()
        self.assertEqual(code, 0)
        self.assertEqual(out2.read_bytes(), self.out.read_bytes())

    def test_limit_breach_exit_one_still_writes_full_report(self):
        pol = self.fixture_policy()
        pol['limits']['max_unresolved_per_program'] = 0
        self.write_policy(pol)
        code, _, _ = self.audit()
        self.assertEqual(code, 1)
        report = json.loads(self.out.read_text())
        self.assertFalse(report['ok'])
        self.assertEqual(report['exceeded_programs'], ['Y'])
        self.assertEqual(len(report['records']), 2)  # report stays complete
        self.assertEqual(report['totals']['program_only'], 1)

    def test_input_and_policy_errors_exit_two_and_preserve_report(self):
        self.out.write_text('prior')
        cases = []
        # Unknown reference in the source.
        bad_source = json.loads(self.src.read_text())
        bad_source['applicants'][0]['preferences'] = ['missing']
        cases.append(('bad_source', bad_source, self.fixture_policy()))
        # Stale policy: digest does not match the source bytes.
        stale = self.fixture_policy()
        stale['source_sha256'] = '0' * 64
        cases.append(('stale', None, stale))
        # Duplicate waiver.
        dup = self.fixture_policy()
        dup['waivers'] = dup['waivers'] * 2
        cases.append(('duplicate', None, dup))
        # Waiver for a mutual pair and for a nonexistent pair.
        for w in (waiver('A', 'X'), waiver('A', 'Z')):
            bad = self.fixture_policy()
            bad['waivers'] = [w]
            cases.append(('non_unilateral', None, bad))
        # Empty waiver reason.
        bad = self.fixture_policy()
        bad['waivers'][0]['reason'] = ''
        cases.append(('empty_reason', None, bad))
        for name, new_source, new_policy in cases:
            with self.subTest(name=name):
                if new_source is not None:
                    self.src.write_text(json.dumps(new_source))
                self.write_policy(new_policy)
                code, _, _ = self.audit()
                self.assertEqual(code, 2)
                self.assertEqual(self.out.read_text(), 'prior')
                self.src.write_bytes(FIXTURE_SOURCE.read_bytes())

    def test_source_byte_change_invalidates_old_policy(self):
        code, _, _ = self.audit()
        self.assertEqual(code, 0)
        self.out.write_text('prior')
        self.src.write_bytes(FIXTURE_SOURCE.read_bytes() + b'\n')
        code, _, stderr = self.audit()
        self.assertEqual(code, 2)
        self.assertIn('source_sha256', stderr)
        self.assertEqual(self.out.read_text(), 'prior')

    def test_malformed_json_and_duplicate_keys_rejected(self):
        self.out.write_text('prior')
        for body in ('{}', '{"a":1,"a":2}', 'NaN'):
            self.pol.write_text(body)
            code, _, _ = self.audit()
            self.assertEqual(code, 2)
            self.assertEqual(self.out.read_text(), 'prior')

    def test_output_rejects_symlink_and_input_paths(self):
        for target in (self.src, self.pol):
            code, _, _ = self.run_cli(['audit', '--source', str(self.src),
                                       '--policy', str(self.pol),
                                       '--output', str(target)])
            self.assertEqual(code, 2)
            self.assertEqual(target.read_bytes(),
                             FIXTURE_SOURCE.read_bytes()
                             if target is self.src
                             else FIXTURE_POLICY.read_bytes())
        link = self.td / 'link.json'
        link.symlink_to(self.src)
        code, _, _ = self.run_cli(['audit', '--source', str(self.src),
                                   '--policy', str(self.pol),
                                   '--output', str(link)])
        self.assertEqual(code, 2)
        self.assertEqual(self.src.read_bytes(), FIXTURE_SOURCE.read_bytes())
        # A symlink to an unrelated third path is rejected as well.
        third = self.td / 'third.json'
        third.write_text('third')
        alias = self.td / 'alias.json'
        alias.symlink_to(third)
        code, _, _ = self.run_cli(['audit', '--source', str(self.src),
                                   '--policy', str(self.pol),
                                   '--output', str(alias)])
        self.assertEqual(code, 2)
        self.assertEqual(third.read_text(), 'third')

    def test_repeated_runs_are_byte_identical(self):
        code, _, _ = self.audit()
        self.assertEqual(code, 0)
        first = self.out.read_bytes()
        code, _, _ = self.audit()
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_bytes(), first)
        out2 = self.td / 'again.json'
        code, _, _ = self.run_cli(['audit', '--source', str(self.src),
                                   '--policy', str(self.pol),
                                   '--output', str(out2)])
        self.assertEqual(code, 0)
        self.assertEqual(out2.read_bytes(), first)

    def test_audit_never_runs_matching_or_workbench(self):
        def fail(*args, **kwargs):
            raise AssertionError('matching/workbench must not run')
        with mock.patch.object(model, 'analyze', fail), \
                mock.patch.object(matching, 'allocate', fail), \
                mock.patch.object(cli, 'analyze', fail), \
                mock.patch.object(store, 'initialize', fail), \
                mock.patch.object(store, 'apply_request', fail), \
                mock.patch.object(store, 'export_report', fail):
            code, _, _ = self.audit()
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(self.out.read_text())['ok'])

    def test_legacy_and_workbench_entries_still_work(self):
        once_out = self.td / 'once.json'
        code, _, _ = self.run_cli(['--source', str(ROOT / 'fixtures'
                                                          / 'input.json'),
                                   '--output', str(once_out)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(once_out.read_text())['schema_version'], 1)
        db = self.td / 'wb.sqlite'
        code, _, _ = self.run_cli(['init', '--source',
                                   str(ROOT / 'fixtures' / 'workbench.json'),
                                   '--db', str(db)])
        self.assertEqual(code, 0)
        self.assertTrue(db.exists())


if __name__ == '__main__':
    unittest.main()
