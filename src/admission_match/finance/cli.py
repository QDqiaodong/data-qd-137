"""Command line for the independent admission finance library.

Equivalent invocations::

    admission-match finance init   --source ROSTER --charges FILE --db DB
    admission-match finance apply  --db DB --request FILE --bank CSV
    admission-match finance report --db DB --output FILE
    python -m admission_match.finance.cli <same arguments>

Data and state errors print a readable message and exit 2 without changing
the database or replacing an existing report.  A legal ledger with unpaid
fees still exits 0; no real payment is ever initiated.
"""
import argparse
import json
import sqlite3
import sys

from ..common import read
from .reporting import write_report
from .roster import FinanceError
from .store import apply_batch, initialize

DESCRIPTION = 'Admission finance library: bank-fact fee clearing'


def _parser():
    parser = argparse.ArgumentParser(
        prog='admission-match finance', description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command', required=True)

    init = sub.add_parser('init', help='create a dedicated finance database')
    init.add_argument('--source', type=str, required=True,
                      help='roster following docs/contract.md')
    init.add_argument('--charges', type=str, required=True)
    init.add_argument('--db', type=str, required=True)

    apply = sub.add_parser('apply', help='receive one request plus bank CSV')
    apply.add_argument('--db', type=str, required=True)
    apply.add_argument('--request', type=str, required=True)
    apply.add_argument('--bank', type=str, required=True)

    report = sub.add_parser('report', help='write a consistent snapshot')
    report.add_argument('--db', type=str, required=True)
    report.add_argument('--output', type=str, required=True)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    try:
        if args.command == 'init':
            roster_data, roster_sha = read(args.source)
            charges_data, charges_sha = read(args.charges)
            report = initialize(args.db, roster_data, roster_sha,
                                charges_data, charges_sha)
        elif args.command == 'apply':
            request_data, _digest = read(args.request)
            report = apply_batch(args.db, request_data, args.bank)
        else:
            report = write_report(args.db, args.output)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, json.JSONDecodeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
