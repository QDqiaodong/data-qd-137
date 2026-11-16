"""Command line: single-shot matching plus the regional-quota workbench.

The historical ``--source/--output`` contract is unchanged.  The workbench uses
independent subcommands with their own contract::

    admission-match init   --source FILE --db DB
    admission-match apply  --db DB --request FILE
    admission-match export --db DB --output FILE

The pre-import bilateral declaration audit is a read-only data-quality
subcommand that never runs matching or touches the workbench::

    admission-match audit  --source FILE --policy FILE --output FILE

The independent admission materials library owns its own subcommand group and
contract (``admission_match.materials``)::

    admission-match materials init|ingest|report ...

The independent admission finance library owns its own subcommand group and
contract (``admission_match.finance``)::

    admission-match finance init|apply|report ...

Subcommands are detected manually so the legacy flag-only invocation keeps
working exactly as before.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .audit import audit_report
from .common import read, save
from .model import analyze
from .store import initialize, apply_request, export_report

DESCRIPTION = 'Applicant-proposing deferred acceptance with blocking-pair audit'
COMMANDS = ('init', 'apply', 'export', 'once', 'audit', 'materials',
            'finance')


def run_once(source):
    data, digest = read(source)
    return {'schema_version': 1, 'source_sha256': digest,
            'result': analyze(data)}


def _legacy_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    return parser


def _parser_for(command):
    parser = argparse.ArgumentParser(prog='admission-match ' + command,
                                     description=DESCRIPTION)
    if command == 'init':
        parser.add_argument('--source', type=Path, required=True)
        parser.add_argument('--db', type=Path, required=True)
    elif command == 'apply':
        parser.add_argument('--db', type=Path, required=True)
        parser.add_argument('--request', type=Path, required=True)
    elif command == 'export':
        parser.add_argument('--db', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
    elif command == 'audit':
        parser.add_argument('--source', type=Path, required=True)
        parser.add_argument('--policy', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
    else:
        return _legacy_parser()
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv and argv[0] in COMMANDS else None
    if command == 'materials':
        # The materials library is an independent contract with its own
        # parser, exit codes and error messages; just forward the rest.
        from .materials.cli import main as materials_main
        return materials_main(argv[1:])
    if command == 'finance':
        # The finance library is likewise a fully independent contract.
        from .finance.cli import main as finance_main
        return finance_main(argv[1:])
    if command is not None:
        argv = argv[1:]
    parser = _parser_for(command) if command else _legacy_parser()
    args = parser.parse_args(argv)

    try:
        if command in (None, 'once'):
            if args.source.resolve() == args.output.resolve():
                raise ValueError('input and output must differ')
            report = run_once(args.source)
            save(args.output, report)
        elif command == 'audit':
            if args.output.resolve() in (args.source.resolve(),
                                         args.policy.resolve()):
                raise ValueError('output must differ from source and policy')
            source_data, source_digest = read(args.source)
            policy_data, policy_digest = read(args.policy)
            report = audit_report(source_data, source_digest,
                                  policy_data, policy_digest)
            save(args.output, report)
            print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                             allow_nan=False))
            return 0 if report['ok'] else 1
        elif command == 'init':
            data, digest = read(args.source)
            initialize(args.db, data, digest)
            report = {'initialized': True, 'db': str(args.db)}
        elif command == 'apply':
            data, _digest = read(args.request)
            report = apply_request(args.db, data)
        else:
            report = export_report(args.db, args.output)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, json.JSONDecodeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
