"""Command line: single-shot matching plus the regional-quota workbench.

The historical ``--source/--output`` contract is unchanged.  The workbench uses
independent subcommands with their own contract::

    admission-match init   --source FILE --db DB
    admission-match apply  --db DB --request FILE
    admission-match export --db DB --output FILE

Subcommands are detected manually so the legacy flag-only invocation keeps
working exactly as before.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .common import read, save
from .model import analyze
from .store import initialize, apply_request, export_report

DESCRIPTION = 'Applicant-proposing deferred acceptance with blocking-pair audit'
COMMANDS = ('init', 'apply', 'export', 'once')


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
    else:
        return _legacy_parser()
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv and argv[0] in COMMANDS else None
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
