"""Deterministic finance reports built from one consistent read snapshot.

A single deferred read transaction covers every SELECT, so a concurrent
committer cannot mix bank facts, journal entries and balances from different
revisions into one report.  All money math uses Python integers; the report
carries per-bank unallocated amounts and per-account net balances, so every
posted cent remains traceable either as allocated or as unclaimed/unallocated.
"""
import os
from pathlib import Path

from ..common import save
from . import SCHEMA_VERSION
from .roster import FinanceError
from .store import connect


def build_report(conn):
    conn.execute('BEGIN')
    try:
        return _build_locked(conn)
    finally:
        conn.rollback()


def _build_locked(conn):
    meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
    revision = int(meta['revision'])

    entries = []
    reversed_ids = set()
    for row in conn.execute(
            'SELECT id,kind,bank_id,applicant,currency,amount_minor,'
            'claim_id,target_id,revision FROM entries ORDER BY id'):
        entry = {
            'id': row[0], 'type': row[1], 'bank_id': row[2],
            'applicant': row[3], 'currency': row[4],
            'amount_minor': row[5], 'claim_id': row[6],
            'target_id': row[7], 'revision': row[8],
        }
        if row[1] == 'reverse':
            entry['state'] = 'reverse'
            reversed_ids.add(row[7])
        entries.append(entry)
    for entry in entries:
        if entry['type'] in ('claim', 'refund'):
            entry['state'] = 'reversed' if entry['id'] in reversed_ids \
                else 'effective'

    # Effective allocations per bank item and per account.
    bank_used = {}
    account_net = {}
    for entry in entries:
        if entry['type'] == 'reverse' or entry['id'] in reversed_ids:
            continue
        key = (entry['applicant'], entry['currency'])
        if entry['type'] == 'claim':
            bank_used[entry['bank_id']] = bank_used.get(
                entry['bank_id'], 0) + entry['amount_minor']
            account_net[key] = account_net.get(key, 0) \
                + entry['amount_minor']
        else:
            bank_used[entry['bank_id']] = bank_used.get(
                entry['bank_id'], 0) + entry['amount_minor']
            account_net[key] = account_net.get(key, 0) \
                - entry['amount_minor']

    banks = []
    for row in conn.execute(
            'SELECT bank_id,version,kind,status,currency,amount_minor,'
            'origin_id FROM bank_current ORDER BY bank_id'):
        bank_id, version, kind, status, currency, amount, origin = row
        used = bank_used.get(bank_id, 0)
        unallocated = amount - used
        if unallocated < 0:
            raise FinanceError(
                'corrupt ledger: bank item %s over-allocated' % bank_id)
        banks.append({
            'bank_id': bank_id, 'version': version, 'kind': kind,
            'status': status, 'currency': currency,
            'amount_minor': amount, 'origin_id': origin or None,
            'allocated_minor': used,
            'unallocated_minor': unallocated,
        })

    accounts = []
    for row in conn.execute(
            'SELECT applicant,currency,due_minor FROM accounts '
            'ORDER BY applicant,currency'):
        applicant, currency, due = row
        net = account_net.get((applicant, currency), 0)
        if not 0 <= net <= due:
            raise FinanceError(
                'corrupt ledger: account %s/%s outside 0..due'
                % (applicant, currency))
        accounts.append({
            'applicant': applicant, 'currency': currency,
            'due_minor': due, 'net_paid': net,
            'due_remaining': due - net,
        })

    return {
        'schema_version': SCHEMA_VERSION,
        'roster_sha256': meta['roster_sha256'],
        'charges_sha256': meta['charges_sha256'],
        'revision': revision,
        'bank_facts': banks,
        'entries': entries,
        'accounts': accounts,
    }


def ensure_safe_output(output, db_path):
    """Reports cannot be symlinks and cannot overwrite the live database."""
    dest = Path(output)
    if dest.is_symlink() or os.path.islink(os.fspath(dest)):
        raise FinanceError('output cannot be a symlink: ' + str(dest))
    if os.path.exists(db_path) and os.path.exists(dest) \
            and os.path.samefile(db_path, dest):
        raise FinanceError('report output must not overwrite the database')


def write_report(db_path, output_path):
    ensure_safe_output(output_path, db_path)
    conn = connect(db_path)
    try:
        report = build_report(conn)
    finally:
        conn.close()
    save(output_path, report)
    return report
