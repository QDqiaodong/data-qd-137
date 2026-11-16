"""Tests for the independent admission finance library.

Coverage: strict init/request/CSV contracts, the bank-fact acceptance rules,
the allocation-ledger acceptance scenarios, both linked correction/void
scenarios, idempotent replay, deterministic reports, real two-process
revision contention and crash recovery between the bank write and the
ledger commit.  Streaming/RSS and history-index evidence live in
``test_finance_scale.py``.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from admission_match.cli import main as cli_main
from admission_match.finance.reporting import build_report, write_report
from admission_match.finance.roster import FinanceError
from admission_match.finance.store import (
    apply_batch, connect, initialize,
)

ROOT = Path(__file__).resolve().parents[1]

HEADER = 'bank_id,version,kind,status,currency,amount_minor,origin_id\n'


def roster():
    return json.loads((ROOT / 'fixtures' / 'finance_roster.json').read_text())


def charges():
    return json.loads((ROOT / 'fixtures' / 'finance_charges.json').read_text())


def csv_bytes(rows):
    return HEADER.encode() + b''.join(
        (','.join('' if v is None else str(v) for v in row) + '\n')
        .encode('utf-8') for row in rows)


class FinanceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.db = self.td / 'finance.sqlite'
        initialize(self.db, roster(), 'ab' * 32, charges(), 'cd' * 32)

    def tearDown(self):
        self.tmp.cleanup()

    def bank_file(self, name, rows=None, raw=None):
        path = self.td / name
        path.write_bytes(raw if raw is not None else csv_bytes(rows))
        return path

    def request(self, request_id, rev, bank_path, operations):
        return {
            'request_id': request_id,
            'expected_revision': rev,
            'bank_sha256': hashlib.sha256(bank_path.read_bytes()).hexdigest(),
            'operations': operations,
        }

    def apply(self, request_id, rev, rows, operations, name=None):
        path = self.bank_file((name or request_id) + '.csv', rows)
        return apply_batch(
            self.db, self.request(request_id, rev, path, operations), path)

    def apply_raw(self, request_id, rev, raw, operations):
        path = self.bank_file(request_id + '.csv', raw=raw)
        return apply_batch(
            self.db, self.request(request_id, rev, path, operations), path)

    def report(self):
        return build_report(connect(self.db))

    def claim(self, entry_id, bank_id, applicant, amount):
        return {'type': 'claim', 'id': entry_id, 'bank_id': bank_id,
                'applicant': applicant, 'amount_minor': amount}

    def refund(self, entry_id, bank_id, claim_id, amount):
        return {'type': 'refund', 'id': entry_id, 'bank_id': bank_id,
                'claim_id': claim_id, 'amount_minor': amount}

    def reverse(self, entry_id, target_id):
        return {'type': 'reverse', 'id': entry_id, 'target_id': target_id}

    def account(self, report, applicant, currency):
        return next(a for a in report['accounts']
                    if a['applicant'] == applicant
                    and a['currency'] == currency)

    def bank(self, report, bank_id):
        return next(b for b in report['bank_facts']
                    if b['bank_id'] == bank_id)


class InitializationTests(FinanceBase):
    def test_revision_zero_and_input_hashes(self):
        report = self.report()
        self.assertEqual(report['revision'], 0)
        self.assertEqual(report['roster_sha256'], 'ab' * 32)
        self.assertEqual(report['charges_sha256'], 'cd' * 32)
        self.assertEqual(report['bank_facts'], [])
        self.assertEqual(report['entries'], [])
        self.assertEqual(len(report['accounts']), 4)

    def test_refuses_existing_database(self):
        with self.assertRaises(FinanceError):
            initialize(self.db, roster(), 'ab' * 32, charges(), 'cd' * 32)

    def test_init_inputs_are_independent_afterwards(self):
        # The store keeps only hashes; once init succeeded, replacing or
        # removing the original inputs cannot affect later reads.
        original = (ROOT / 'fixtures' / 'finance_roster.json').read_bytes()
        decoy = self.td / 'decoy-roster.json'
        decoy.write_bytes(original)
        db = self.td / 'independent.sqlite'
        initialize(db, roster(), 'ab' * 32, charges(), 'cd' * 32)
        decoy.unlink()
        self.assertEqual(build_report(connect(db))['revision'], 0)

    def test_bad_charges_rejected_before_db_created(self):
        bad_db = self.td / 'bad.sqlite'
        bad = {'charges': [
            {'applicant': 'ghost', 'currency': 'CNY', 'due_minor': 1}]}
        with self.assertRaises(FinanceError):
            initialize(bad_db, roster(), 'ab' * 32, bad, 'cd' * 32)
        self.assertFalse(bad_db.exists())

    def test_roster_matching_fields_are_ignored(self):
        # Capacity/ranking content never feeds the finance ledger; only the
        # applicant identities matter and matching never runs.
        data = roster()
        for program in data['programs']:
            program['capacity'] = 0
        db = self.td / 'nomatch.sqlite'
        initialize(db, data, 'ab' * 32, charges(), 'cd' * 32)
        self.assertEqual(build_report(connect(db))['revision'], 0)


class BankCsvContractTests(FinanceBase):
    def test_refund_row_precedes_its_credit(self):
        rows = [('r1', 1, 'refund', 'posted', 'CNY', 20, 'c1'),
                ('c1', 1, 'credit', 'posted', 'CNY', 100, None)]
        result = self.apply('q', 0, rows, [
            self.claim('e1', 'c1', 'A', 60),
            self.refund('e2', 'r1', 'e1', 20)])
        self.assertEqual(result['revision'], 1)
        self.assertEqual(self.account(self.report(), 'A', 'CNY')['net_paid'],
                         40)

    def test_cross_currency_reference_fails_batch(self):
        rows = [('c1', 1, 'credit', 'posted', 'USD', 100, None),
                ('r1', 1, 'refund', 'posted', 'CNY', 10, 'c1')]
        with self.assertRaises(FinanceError):
            self.apply('q', 0, rows, [])
        self.assertEqual(self.report()['revision'], 0)
        self.assertEqual(self.report()['bank_facts'], [])

    def test_skipped_version_fails_batch(self):
        self.apply('q0', 0,
                   [('c1', 1, 'credit', 'posted', 'CNY', 100, None)], [])
        with self.assertRaises(FinanceError):
            self.apply('q1', 1,
                       [('c1', 3, 'credit', 'posted', 'CNY', 100, None)], [])
        self.assertEqual(self.bank(self.report(), 'c1')['version'], 1)

    def test_immutable_fields_cannot_change(self):
        self.apply('q0', 0,
                   [('c1', 1, 'credit', 'posted', 'CNY', 100, None)], [])
        for row in [
            ('c1', 2, 'refund', 'posted', 'CNY', 100, 'c9'),
            ('c1', 2, 'credit', 'posted', 'USD', 100, None),
            ('c1', 2, 'credit', 'posted', 'CNY', 100, 'c9'),
        ]:
            with self.assertRaises(FinanceError):
                self.apply('q' + row[2] + str(row[4]), 1, [row], [])
        # Status and amount are correctable.
        self.apply('qfix', 1,
                   [('c1', 2, 'credit', 'void', 'CNY', 0, None)], [])
        self.assertEqual(self.bank(self.report(), 'c1')['status'], 'void')

    def test_duplicate_bank_id_in_one_file(self):
        with self.assertRaises(FinanceError):
            self.apply('q', 0, [
                ('c1', 1, 'credit', 'posted', 'CNY', 100, None),
                ('c1', 2, 'credit', 'posted', 'CNY', 90, None)], [])

    def test_last_line_error_commits_nothing(self):
        raw = csv_bytes([('c9', 1, 'credit', 'posted', 'CNY', 50, None)]) \
            + b'BROKEN,LINE\n'
        with self.assertRaises(FinanceError):
            self.apply_raw('q', 0, raw, [])
        report = self.report()
        self.assertEqual(report['revision'], 0)
        self.assertEqual(report['bank_facts'], [])
        # Every staging directory from the failed parse is removed.
        leftovers = [p for p in Path(tempfile.gettempdir()).glob(
            'finance-stage-*') if p.is_dir()]
        self.assertEqual(leftovers, [])

    def test_sha_mismatch_fails_before_database_touch(self):
        path = self.bank_file('b.csv',
                              [('c9', 1, 'credit', 'posted', 'CNY', 50, None)])
        request = self.request('q', 0, path, [])
        request['bank_sha256'] = '00' * 32
        with self.assertRaises(FinanceError):
            apply_batch(self.db, request, path)
        self.assertEqual(self.report()['revision'], 0)

    def test_strict_csv_surface(self):
        cases = [
            b'',
            b'bank_id,version,kind,status,currency,amount_minor,origin_id\n\n',
            b'bank_id,version,kind,status,currency,amount_minor,origin_id\n'
            b'c9,01,credit,posted,CNY,50,\n',
            b'bank_id,version,kind,status,currency,amount_minor,origin_id\n'
            b'c9,1,credit,posted,CNY,50\n',
            b'bank_id,version,kind,status,currency,amount_minor,origin_id\n'
            b' c9,1,credit,posted,CNY,50,\n',
            b'bank_id,version,kind,status,currency,amount_minor,origin_id\n'
            b'c9,1,credit,posted,CNY,0,\n',
            b'bank_id,version,kind,status,currency,amount_minor,origin_id\n'
            b'c9,1,refund,posted,CNY,20,\n',
            b'bank_id,version,kind,status,currency,amount_minor,origin_id\n'
            b'c9,1,credit,posted,CNY,1000000000001,\n',
            b'bank_id,version,kind,status,currency,amount_minor,origin_id\n'
            b'c9,1000000001,credit,posted,CNY,50,\n',
            b'bank_id,kind,version,status,currency,amount_minor,origin_id\n'
            b'c9,1,credit,posted,CNY,50,\n',
        ]
        for raw in cases:
            with self.subTest(raw=raw[:32]):
                with self.assertRaises(FinanceError):
                    self.apply_raw('q', 0, raw, [])
                self.assertEqual(self.report()['revision'], 0)

    def test_void_amount_zero_and_credit_origin_empty(self):
        self.apply_raw('q', 0, csv_bytes([
            ('c1', 1, 'credit', 'void', 'CNY', 0, None)]), [])
        fact = self.bank(self.report(), 'c1')
        self.assertEqual((fact['status'], fact['amount_minor']),
                         ('void', 0))

    def test_refund_to_unknown_origin(self):
        with self.assertRaises(FinanceError):
            self.apply('q', 0,
                       [('r1', 1, 'refund', 'posted', 'CNY', 10, 'ghost')], [])

    def test_row_count_cap_is_enforced_strictly(self):
        from admission_match.finance.bankcsv import MAX_ROWS
        # Stream MAX_ROWS+1 rows straight to disk; never materialize the list
        # in the test runner (that would inflate this process's RSS).
        over = self.td / 'over-rows.csv'
        with over.open('w', encoding='utf-8') as stream:
            stream.write(HEADER)
            for i in range(MAX_ROWS + 1):
                stream.write('b%07d,1,credit,posted,CNY,1,\n' % i)
        sha = hashlib.sha256(over.read_bytes()).hexdigest()
        with self.assertRaises(FinanceError):
            apply_batch(self.db, {
                'request_id': 'over-rows', 'expected_revision': 0,
                'bank_sha256': sha, 'operations': []}, over)
        self.assertEqual(self.report()['revision'], 0)

    def test_quoted_comma_field_is_standard_csv(self):
        # Standard RFC-4180 quoting is accepted; the quoted bank id stays one
        # field with its exact (un-stripped) content.
        raw = (HEADER + '"c,1",1,credit,posted,CNY,50,\n').encode('utf-8')
        result = self.apply_raw('quoted', 0, raw, [
            self.claim('e1', 'c,1', 'A', 50)])
        self.assertEqual(result['revision'], 1)
        self.assertEqual(self.bank(self.report(), 'c,1')['amount_minor'], 50)


class RequestContractTests(FinanceBase):
    def _base(self):
        return {'request_id': 'q', 'expected_revision': 0,
                'bank_sha256': 'a' * 64, 'operations': []}

    def test_strict_fields_bools_and_bounds(self):
        from admission_match.finance.request import parse_request
        bad_bodies = [
            {},
            dict(self._base(), request_id=''),
            dict(self._base(), request_id=' has space'),
            dict(self._base(), expected_revision=-1),
            dict(self._base(), expected_revision=True),
            dict(self._base(), bank_sha256='A' * 64),
            dict(self._base(), bank_sha256='g' * 64),
            dict(self._base(), bank_sha256='a' * 63),
            dict(self._base(), extra=1),
            dict(self._base(), operations=[
                {'type': 'claim', 'id': 'e', 'bank_id': 'b',
                 'applicant': 'A', 'amount_minor': True}]),
            dict(self._base(), operations=[
                {'type': 'claim', 'id': 'e', 'bank_id': 'b',
                 'applicant': 'A', 'amount_minor': 0}]),
            dict(self._base(), operations=[
                {'type': 'claim', 'id': 'e', 'bank_id': 'b',
                 'applicant': 'A', 'amount_minor': 10 ** 12 + 1}]),
            dict(self._base(), operations=[
                {'type': 'reverse', 'id': 'e', 'target_id': 't',
                 'extra': 1}]),
            dict(self._base(), operations=[
                {'type': 'mystery', 'id': 'e', 'target_id': 't'}]),
            dict(self._base(), operations=[
                {'type': 'claim', 'id': 'e', 'bank_id': 'b',
                 'applicant': 'A', 'amount_minor': 1},
                {'type': 'claim', 'id': 'e', 'bank_id': 'b',
                 'applicant': 'A', 'amount_minor': 1}]),
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                with self.assertRaises(FinanceError):
                    parse_request(body)

    def test_more_than_1000_operations(self):
        from admission_match.finance.request import parse_request
        body = self._base()
        body['operations'] = [
            self.reverse('e%04d' % i, 't%04d' % i)
            for i in range(1001)]
        with self.assertRaises(FinanceError):
            parse_request(body)

    def test_canonical_fingerprint_ignores_order_only(self):
        from admission_match.finance.request import request_fingerprint
        a = self._base()
        a['operations'] = [self.reverse('a', 't1'),
                           self.reverse('b', 't2')]
        b = {
            'operations': list(reversed(a['operations'])),
            'bank_sha256': a['bank_sha256'],
            'expected_revision': 0, 'request_id': 'q',
        }
        self.assertEqual(request_fingerprint(a),
                         request_fingerprint(b))
        self.assertNotEqual(request_fingerprint(a),
                            request_fingerprint(dict(a, expected_revision=1)))
        self.assertNotEqual(request_fingerprint(a),
                            request_fingerprint(dict(a, bank_sha256='b' * 64)))


class LedgerAcceptanceTests(FinanceBase):
    def test_one_credit_split_between_two_applicants(self):
        self.apply('q', 0,
                   [('c1', 1, 'credit', 'posted', 'CNY', 100, None)],
                   [self.claim('e1', 'c1', 'A', 60),
                    self.claim('e2', 'c1', 'B', 40)])
        report = self.report()
        self.assertEqual(self.bank(report, 'c1')['unallocated_minor'], 0)
        self.assertEqual(self.account(report, 'A', 'CNY')['net_paid'], 60)
        self.assertEqual(self.account(report, 'B', 'CNY')['net_paid'], 40)

    def test_one_account_accepts_many_credits(self):
        self.apply('q', 0, [
            ('c1', 1, 'credit', 'posted', 'CNY', 60, None),
            ('c2', 1, 'credit', 'posted', 'CNY', 40, None)],
            [self.claim('e1', 'c1', 'A', 60),
             self.claim('e2', 'c2', 'A', 40)])
        report = self.report()
        self.assertEqual(self.account(report, 'A', 'CNY')['net_paid'], 100)
        self.assertEqual(self.account(report, 'A', 'CNY')['due_remaining'],
                         9900)

    def test_replace_old_claim_with_new_claim_in_one_batch(self):
        self.apply('q0', 0,
                   [('c1', 1, 'credit', 'posted', 'CNY', 100, None)],
                   [self.claim('old', 'c1', 'A', 80),
                    self.claim('b', 'c1', 'B', 20)])
        self.apply('q1', 1,
                   [('c1', 2, 'credit', 'posted', 'CNY', 60, None)],
                   [self.reverse('rv', 'old'),
                    self.claim('new', 'c1', 'A', 40)])
        report = self.report()
        self.assertEqual(self.bank(report, 'c1')['unallocated_minor'], 0)
        self.assertEqual(self.account(report, 'A', 'CNY')['net_paid'], 40)
        old = next(e for e in report['entries'] if e['id'] == 'old')
        self.assertEqual(old['state'], 'reversed')

    def test_refund_then_top_up_in_same_final_state(self):
        # Refund allocation appears before the replacement claim in the
        # operation list; feasibility is judged on the final state only.
        self.apply('q0', 0, [
            ('c1', 1, 'credit', 'posted', 'CNY', 100, None),
            ('r1', 1, 'refund', 'posted', 'CNY', 30, 'c1')],
            [self.claim('old', 'c1', 'A', 100)])
        ops = [
            self.refund('rf', 'r1', 'new', 30),
            self.reverse('rv', 'old'),
            self.claim('new', 'c1', 'A', 70),
        ]
        self.apply('q1', 1, [], ops)
        report = self.report()
        self.assertEqual(self.account(report, 'A', 'CNY')['net_paid'], 40)
        self.assertEqual(self.bank(report, 'r1')['unallocated_minor'], 0)

    def test_reversing_claim_with_live_refund_fails_alone_works_together(self):
        seed_rows = [
            ('c1', 1, 'credit', 'posted', 'CNY', 100, None),
            ('r1', 1, 'refund', 'posted', 'CNY', 20, 'c1')]
        seed_ops = [self.claim('ca', 'c1', 'A', 100),
                    self.refund('rf', 'r1', 'ca', 20)]
        self.apply('q0', 0, seed_rows, seed_ops)
        with self.assertRaises(FinanceError):
            self.apply('bad', 1, [], [self.reverse('rv1', 'ca')])

        # Reversing both in one batch succeeds; reversed operation order on a
        # fresh database yields the identical final state.
        order_a = [self.reverse('rv1', 'ca'), self.reverse('rv2', 'rf')]
        self.apply('good', 1, [], order_a)
        report = self.report()
        self.assertEqual(self.account(report, 'A', 'CNY')['net_paid'], 0)
        self.assertEqual(self.bank(report, 'r1')['allocated_minor'], 0)

        fresh = self.td / 'fresh.sqlite'
        initialize(fresh, roster(), 'ab' * 32, charges(), 'cd' * 32)
        seed_bank = self.td / 'seed.csv'
        seed_bank.write_bytes(csv_bytes(seed_rows))
        apply_batch(fresh, {
            'request_id': 'q0', 'expected_revision': 0,
            'bank_sha256': hashlib.sha256(seed_bank.read_bytes()).hexdigest(),
            'operations': seed_ops}, seed_bank)
        empty_bank = self.td / 'empty.csv'
        empty_bank.write_bytes(csv_bytes([]))
        apply_batch(fresh, {
            'request_id': 'good', 'expected_revision': 1,
            'bank_sha256': hashlib.sha256(
                empty_bank.read_bytes()).hexdigest(),
            'operations': list(reversed(order_a))}, empty_bank)
        fresh_report = build_report(connect(fresh))
        self.assertEqual(
            [(e['id'], e['state']) for e in fresh_report['entries']],
            [(e['id'], e['state']) for e in report['entries']])
        self.assertEqual(
            [a['net_paid'] for a in fresh_report['accounts']],
            [a['net_paid'] for a in report['accounts']])

    def test_over_credit_over_refund_and_over_claim_rejected(self):
        self.apply('q0', 0, [
            ('c1', 1, 'credit', 'posted', 'CNY', 100, None),
            ('r1', 1, 'refund', 'posted', 'CNY', 20, 'c1')],
            [self.claim('ca', 'c1', 'A', 100)])
        with self.assertRaises(FinanceError):
            self.apply('over-credit', 1, [],
                       [self.claim('x', 'c1', 'B', 1)])
        with self.assertRaises(FinanceError):
            self.apply('over-refund', 1, [],
                       [self.refund('x', 'r1', 'ca', 21)])
        with self.assertRaises(FinanceError):
            self.apply('over-claim', 1, [],
                       [self.refund('x', 'r1', 'ca', 101)])
        self.assertEqual(self.report()['revision'], 1)

    def test_account_balance_bounds_and_currency_isolation(self):
        # A/CNY due is 10000 in the fixture.
        big = self.td / 'big.csv'
        big.write_bytes(csv_bytes([
            ('c1', 1, 'credit', 'posted', 'CNY', 10001, None)]))
        with self.assertRaises(FinanceError):
            apply_batch(self.db, self.request(
                'over-due', 0, big,
                [self.claim('x', 'c1', 'A', 10001)]), big)
        # USD money can never net against CNY obligations.
        usd = self.td / 'usd.csv'
        usd.write_bytes(csv_bytes([
            ('u1', 1, 'credit', 'posted', 'USD', 10, None)]))
        with self.assertRaises(FinanceError):
            apply_batch(self.db, self.request(
                'wrong-currency', 0, usd,
                [self.claim('x', 'u1', 'B', 10)]), usd)
        self.assertEqual(self.report()['revision'], 0)

    def test_new_refund_references_same_batch_new_claim(self):
        self.apply('q', 0, [
            ('c1', 1, 'credit', 'posted', 'CNY', 100, None),
            ('r1', 1, 'refund', 'posted', 'CNY', 20, 'c1')],
            [self.claim('ca', 'c1', 'A', 100),
             self.refund('rf', 'r1', 'ca', 20)])
        self.assertEqual(
            self.account(self.report(), 'A', 'CNY')['net_paid'], 80)

    def test_reverse_rules(self):
        self.apply('q0', 0,
                   [('c1', 1, 'credit', 'posted', 'CNY', 100, None)],
                   [self.claim('ca', 'c1', 'A', 100)])
        with self.assertRaises(FinanceError):
            self.apply('missing', 1, [],
                       [self.reverse('rv', 'ghost')])
        self.apply('q1', 1, [], [self.reverse('rv', 'ca')])
        with self.assertRaises(FinanceError):
            self.apply('again', 2, [], [self.reverse('rv2', 'ca')])
        with self.assertRaises(FinanceError):
            self.apply('reverse-of-reverse', 2, [],
                       [self.reverse('rv3', 'rv')])
        with self.assertRaises(FinanceError):
            self.apply('double', 2, [],
                       [self.reverse('a', 'ca'),
                        self.reverse('b', 'ca')])


class LinkedScenarioTests(FinanceBase):
    """The two end-to-end scenarios from the finance contract."""

    def seed_credit_with_two_claims(self):
        self.apply('seed', 0,
                   [('c', 1, 'credit', 'posted', 'CNY', 100, None)],
                   [self.claim('A-claim', 'c', 'A', 60),
                    self.claim('B-claim', 'c', 'B', 40)])

    def test_credit_correction_scenario(self):
        self.seed_credit_with_two_claims()
        # Correcting C from 100 to 70 alone must fail and leave no trace.
        with self.assertRaises(FinanceError):
            self.apply('cut-alone', 1,
                       [('c', 2, 'credit', 'posted', 'CNY', 70, None)], [])
        report = self.report()
        self.assertEqual(report['revision'], 1)
        self.assertEqual(self.bank(report, 'c')['amount_minor'], 100)

        # Same batch: reverse B's old claim and claim B=10 instead.
        ops = [self.reverse('rv-b', 'B-claim'),
               self.claim('B-new', 'c', 'B', 10)]
        self.apply('cut-fix', 1,
                   [('c', 2, 'credit', 'posted', 'CNY', 70, None)], ops)
        report = self.report()
        self.assertEqual(report['revision'], 2)
        c = self.bank(report, 'c')
        self.assertEqual((c['amount_minor'], c['allocated_minor'],
                          c['unallocated_minor']), (70, 70, 0))
        self.assertEqual(self.account(report, 'A', 'CNY')['net_paid'], 60)
        self.assertEqual(self.account(report, 'B', 'CNY')['net_paid'], 10)

        # Reversed operation order on a fresh database gives the identical
        # final state.
        fresh = self.td / 'fresh.sqlite'
        initialize(fresh, roster(), 'ab' * 32, charges(), 'cd' * 32)
        apply_batch(fresh, {
            'request_id': 'seed', 'expected_revision': 0,
            'bank_sha256': hashlib.sha256(csv_bytes([
                ('c', 1, 'credit', 'posted', 'CNY', 100, None)])
            ).hexdigest(),
            'operations': [self.claim('A-claim', 'c', 'A', 60),
                           self.claim('B-claim', 'c', 'B', 40)]},
            self.bank_file('s1.csv',
                           [('c', 1, 'credit', 'posted', 'CNY', 100, None)]))
        apply_batch(fresh, {
            'request_id': 'cut-fix', 'expected_revision': 1,
            'bank_sha256': hashlib.sha256(csv_bytes([
                ('c', 2, 'credit', 'posted', 'CNY', 70, None)])
            ).hexdigest(),
            'operations': list(reversed(ops))},
            self.bank_file('s2.csv',
                           [('c', 2, 'credit', 'posted', 'CNY', 70, None)]))
        fresh_report = build_report(connect(fresh))
        self.assertEqual(
            [(b['bank_id'], b['amount_minor'], b['unallocated_minor'])
             for b in fresh_report['bank_facts']],
            [(b['bank_id'], b['amount_minor'], b['unallocated_minor'])
             for b in report['bank_facts']])
        self.assertEqual(
            [(a['applicant'], a['net_paid'])
             for a in fresh_report['accounts']],
            [(a['applicant'], a['net_paid']) for a in report['accounts']])

    def test_void_blocked_until_all_dependencies_released(self):
        self.seed_credit_with_two_claims()
        # Refund 20 allocated against A's claim.
        self.apply('refund', 1,
                   [('r', 1, 'refund', 'posted', 'CNY', 20, 'c')],
                   [self.refund('A-refund', 'r', 'A-claim', 20)])
        # Voiding C directly must fail and change nothing.
        with self.assertRaises(FinanceError):
            self.apply('void-alone', 2,
                       [('c', 2, 'credit', 'void', 'CNY', 0, None)], [])
        report = self.report()
        self.assertEqual(report['revision'], 2)
        self.assertEqual(self.bank(report, 'c')['status'], 'posted')

        # Full unwind: reverse the refund allocation and every claim, void
        # the refund bank item and the credit in one batch.
        unwind_rows = [('r', 2, 'refund', 'void', 'CNY', 0, 'c'),
                       ('c', 2, 'credit', 'void', 'CNY', 0, None)]
        unwind_ops = [self.reverse('rv-rf', 'A-refund'),
                      self.reverse('rv-a', 'A-claim'),
                      self.reverse('rv-b', 'B-claim')]
        self.apply('unwind', 2, unwind_rows, unwind_ops)
        report = self.report()
        self.assertEqual(report['revision'], 3)
        for fact in report['bank_facts']:
            self.assertEqual(fact['status'], 'void')
            self.assertEqual(fact['allocated_minor'], 0)
        for account in report['accounts']:
            self.assertEqual(account['net_paid'], 0)
        # Original entries are retained but marked reversed.
        states = {e['id']: e['state'] for e in report['entries']}
        self.assertEqual(states['A-claim'], 'reversed')
        self.assertEqual(states['A-refund'], 'reversed')
        self.assertEqual(states['B-claim'], 'reversed')
        self.assertIn('rv-rf', states)


class ReplayTests(FinanceBase):
    def _request_pair(self):
        path = self.bank_file('b.csv',
                              [('c1', 1, 'credit', 'posted', 'CNY', 100, None)])
        request = self.request('q', 0, path,
                               [self.claim('e1', 'c1', 'A', 100)])
        return path, request

    def test_replay_returns_first_result_without_bank_file(self):
        path, request = self._request_pair()
        first = apply_batch(self.db, request, path)
        os.unlink(path)
        again = apply_batch(self.db, request, path)
        self.assertEqual(again, first)
        self.assertEqual(self.report()['revision'], 1)

    def test_replay_precedes_revision_check(self):
        path, request = self._request_pair()
        apply_batch(self.db, request, path)
        # Advance the database with another request so revision 0 is stale.
        path2 = self.bank_file('b2.csv', [])
        apply_batch(self.db, self.request('other', 1, path2, []), path2)
        os.unlink(path)
        self.assertEqual(apply_batch(self.db, request, path)['revision'], 1)

    def test_same_id_different_content_conflicts(self):
        path, request = self._request_pair()
        apply_batch(self.db, request, path)
        conflict = dict(request, expected_revision=1)
        with self.assertRaises(FinanceError):
            apply_batch(self.db, conflict, path)
        self.assertEqual(self.report()['revision'], 1)

    def test_reordered_csv_is_different_request_content(self):
        rows = [('r1', 1, 'refund', 'posted', 'CNY', 20, 'c1'),
                ('c1', 1, 'credit', 'posted', 'CNY', 100, None)]
        path_a = self.bank_file('a.csv', list(reversed(rows)))
        path_b = self.bank_file('b.csv', rows)
        req_a = self.request('q', 0, path_a, [])
        apply_batch(self.db, req_a, path_a)
        # Same business result, different raw bytes/SHA: different content
        # under the same id must conflict.
        req_b = dict(req_a)
        req_b['bank_sha256'] = hashlib.sha256(
            path_b.read_bytes()).hexdigest()
        with self.assertRaises(FinanceError):
            apply_batch(self.db, req_b, path_b)


class ReportTests(FinanceBase):
    def test_deterministic_bytes_and_sort_orders(self):
        self.apply('q', 0, [
            ('c2', 1, 'credit', 'posted', 'CNY', 50, None),
            ('c1', 1, 'credit', 'posted', 'USD', 10, None)],
            [self.claim('z', 'c2', 'B', 50),
             self.claim('a', 'c1', 'A', 10)])
        out1, out2 = self.td / 'r1.json', self.td / 'r2.json'
        write_report(self.db, out1)
        write_report(self.db, out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        report = json.loads(out1.read_text())
        self.assertEqual([b['bank_id'] for b in report['bank_facts']],
                         sorted(b['bank_id'] for b in report['bank_facts']))
        self.assertEqual([e['id'] for e in report['entries']],
                         sorted(e['id'] for e in report['entries']))
        keys = [(a['applicant'], a['currency']) for a in report['accounts']]
        self.assertEqual(keys, sorted(keys))

    def test_unallocated_and_remaining_are_auditable(self):
        self.apply('q', 0, [
            ('c1', 1, 'credit', 'posted', 'CNY', 100, None),
            ('r1', 1, 'refund', 'posted', 'CNY', 30, 'c1')],
            [self.claim('ca', 'c1', 'A', 60),
             self.refund('rf', 'r1', 'ca', 20)])
        report = self.report()
        self.assertEqual(self.bank(report, 'c1')['unallocated_minor'], 40)
        self.assertEqual(self.bank(report, 'r1')['unallocated_minor'], 10)
        account = self.account(report, 'A', 'CNY')
        self.assertEqual(
            (account['due_minor'], account['net_paid'],
             account['due_remaining']), (10000, 40, 9960))

    def test_legal_arrears_still_exit_zero_via_cli(self):
        from admission_match.finance.cli import main as finance_main
        rc = finance_main(['report', '--db', str(self.db),
                           '--output', str(self.td / 'r.json')])
        self.assertEqual(rc, 0)

    def test_report_rejects_symlink_and_database_path(self):
        link = self.td / 'link.json'
        link.symlink_to(self.td / 'target.json')
        with self.assertRaises(FinanceError):
            write_report(self.db, link)
        with self.assertRaises(FinanceError):
            write_report(self.db, self.db)

    def test_failed_report_keeps_old_bytes(self):
        out = self.td / 'r.json'
        write_report(self.db, out)
        first = out.read_bytes()
        with self.assertRaises(OSError):
            write_report(self.db, self.td)  # directory path
        self.assertEqual(out.read_bytes(), first)


class CliDispatchTests(unittest.TestCase):
    def test_finance_group_and_module_entry_point(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            db = td / 'f.sqlite'
            self.assertEqual(cli_main([
                'finance', 'init',
                '--source', str(ROOT / 'fixtures' / 'finance_roster.json'),
                '--charges', str(ROOT / 'fixtures' / 'finance_charges.json'),
                '--db', str(db)]), 0)
            self.assertEqual(cli_main([
                'finance', 'init',
                '--source', str(ROOT / 'fixtures' / 'finance_roster.json'),
                '--charges', str(ROOT / 'fixtures' / 'finance_charges.json'),
                '--db', str(db)]), 2)

            bank = td / 'bank.csv'
            bank.write_bytes(csv_bytes([
                ('c1', 1, 'credit', 'posted', 'CNY', 100, None)]))
            request = td / 'request.json'
            request.write_text(json.dumps({
                'request_id': 'q', 'expected_revision': 0,
                'bank_sha256': hashlib.sha256(bank.read_bytes()).hexdigest(),
                'operations': [
                    {'type': 'claim', 'id': 'e1', 'bank_id': 'c1',
                     'applicant': 'A', 'amount_minor': 100}]}))
            env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
            # The dedicated module entry point (no extra group word).
            proc = subprocess.run(
                [sys.executable, '-m', 'admission_match.finance.cli',
                 'apply', '--db', str(db), '--request', str(request),
                 '--bank', str(bank)],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # python -m admission_match.cli finance ... is equivalent.
            proc = subprocess.run(
                [sys.executable, '-m', 'admission_match.cli', 'finance',
                 'report', '--db', str(db),
                 '--output', str(td / 'report.json')],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads((td / 'report.json').read_text())
            self.assertEqual(report['revision'], 1)
            # Data errors exit 2.
            bad = td / 'bad.json'
            bad.write_text('{}')
            proc = subprocess.run(
                [sys.executable, '-m', 'admission_match.finance.cli',
                 'apply', '--db', str(db), '--request', str(bad),
                 '--bank', str(bank)],
                capture_output=True, text=True, env=env)
            self.assertEqual(proc.returncode, 2)


class CrashAndConcurrencyTests(FinanceBase):
    env = None

    def _env(self):
        return {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}

    def _apply_subprocess(self, request_path, bank_path, fault=None,
                          delay=None):
        env = self._env()
        if fault:
            env['ADMISSION_MATCH_FINANCE_FAULT'] = fault
        if delay:
            env['ADMISSION_MATCH_FINANCE_DELAY'] = delay
        return subprocess.run(
            [sys.executable, '-m', 'admission_match.cli',
             'finance', 'apply', '--db', str(self.db),
             '--request', str(request_path), '--bank', str(bank_path)],
            capture_output=True, text=True, env=env)

    def test_crash_between_bank_write_and_ledger_commit(self):
        bank = self.bank_file('b.csv',
                              [('c1', 1, 'credit', 'posted', 'CNY', 100, None)])
        request = self.td / 'req.json'
        request.write_text(json.dumps(
            self.request('crash', 0, bank,
                         [self.claim('e1', 'c1', 'A', 100)])))
        proc = self._apply_subprocess(request, bank,
                                      fault='after_bank_insert')
        self.assertEqual(proc.returncode, 91)

        # Reopening shows the complete old revision: no new bank amount, no
        # ledger entry, no revision movement.
        report = self.report()
        self.assertEqual(report['revision'], 0)
        self.assertEqual(report['bank_facts'], [])
        self.assertEqual(report['entries'], [])

        # The killed process left a staging directory behind; it must never
        # be auto-posted on reopen (the retry creates its own staging).
        leftovers = [p for p in Path(tempfile.gettempdir()).glob(
            'finance-stage-*') if p.is_dir()]
        self.assertTrue(leftovers)
        for leftover in leftovers:
            import shutil
            shutil.rmtree(leftover, ignore_errors=True)

        # Clean retry of the same request commits exactly once.
        proc = self._apply_subprocess(request, bank)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = self.report()
        self.assertEqual(report['revision'], 1)
        self.assertEqual(self.bank(report, 'c1')['amount_minor'], 100)
        self.assertEqual(len(report['entries']), 1)

    def test_two_processes_contend_same_revision(self):
        # Both processes validate their CSVs, then sleep before taking the
        # write lock, so they reliably contend for revision 0.  They carry
        # distinct request ids and disjoint bank facts; exactly one may win.
        paths = []
        for i in range(2):
            bank = self.bank_file('bank%d.csv' % i,
                                  [('c%d' % i, 1, 'credit', 'posted',
                                    'CNY', 100, None)])
            request = self.td / ('req%d.json' % i)
            request.write_text(json.dumps(self.request(
                'worker%d' % i, 0, bank,
                [self.claim('e%d' % i, 'c%d' % i, 'A', 100)])))
            paths.append((request, bank))
        procs = [subprocess.Popen(
            [sys.executable, '-m', 'admission_match.cli',
             'finance', 'apply', '--db', str(self.db),
             '--request', str(request), '--bank', str(bank)],
            env={**self._env(),
                 'ADMISSION_MATCH_FINANCE_DELAY': '1.0'})
            for request, bank in paths]
        codes = [p.wait(timeout=60) for p in procs]
        self.assertEqual(sorted(codes), [0, 2])
        report = self.report()
        self.assertEqual(report['revision'], 1)
        self.assertEqual(len(report['entries']), 1)
        self.assertEqual(len(report['bank_facts']), 1)

        # Identify the loser by request id: its id must not appear in the
        # request log, proving no allocation was committed on a stale base.
        logged = {row[0] for row in connect(self.db).execute(
            'SELECT request_id FROM requests')}
        loser_index = 0 if 'worker0' not in logged else 1
        loser_req, _loser_bank = paths[loser_index]
        self.assertIn('worker%d' % loser_index,
                      ['worker0', 'worker1'])  # sanity only
        self.assertNotIn('worker%d' % loser_index, logged)

        # The loser can be replayed unchanged once the database advances: a
        # fresh, non-conflicting bank batch under a new request id rebased to
        # revision 1 commits and advances exactly once.
        follow_bank = self.bank_file('follow.csv',
                                     [('f1', 1, 'credit', 'posted',
                                       'CNY', 50, None)])
        follow_req = self.td / 'follow.json'
        follow_req.write_text(json.dumps(self.request(
            'follow-up', 1, follow_bank,
            [self.claim('f-e1', 'f1', 'A', 50)])))
        proc = self._apply_subprocess(follow_req, follow_bank)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.report()['revision'], 2)


if __name__ == '__main__':
    unittest.main()
