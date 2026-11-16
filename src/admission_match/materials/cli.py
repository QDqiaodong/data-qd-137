"""Command line for the independent admission materials library.

Equivalent invocations::

    admission-match materials init   --store ROOT --source ROSTER --policy POLICY
    admission-match materials ingest --store ROOT --manifest FILE --files DIR
    admission-match materials report --store ROOT --day D --output FILE
    python -m admission_match.materials.cli <same arguments>

Data and state errors print a readable message and exit 2 without changing the
directory or replacing an existing report.  A legal report whose pairs are not
all ready still exits 0.
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .manifest import MAX_DAY, read_manifest
from .reporting import write_report
from .roster import MaterialsError
from .store import ingest, initialize

DESCRIPTION = 'Admission materials library: receive, version and report'


def _parser():
    parser = argparse.ArgumentParser(
        prog='admission-match materials', description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command', required=True)

    init = sub.add_parser('init', help='create a dedicated materials store')
    init.add_argument('--store', type=Path, required=True)
    init.add_argument('--source', type=Path, required=True,
                      help='roster following docs/contract.md')
    init.add_argument('--policy', type=Path, required=True)

    take = sub.add_parser('ingest', help='receive one manifest batch')
    take.add_argument('--store', type=Path, required=True)
    take.add_argument('--manifest', type=Path, required=True)
    take.add_argument('--files', type=Path, required=True)

    report = sub.add_parser('report', help='readiness report for one day')
    report.add_argument('--store', type=Path, required=True)
    report.add_argument('--day', type=int, required=True)
    report.add_argument('--output', type=Path, required=True)
    return parser


def _ensure_safe_output(output, store_root):
    """Output cannot be a symlink or live inside the dedicated store."""
    dest = Path(output)
    # lexists sees the link itself, so an existing symlink is rejected before
    # any path resolution could follow it.
    if dest.is_symlink() or os.path.islink(os.fspath(dest)):
        raise MaterialsError('output cannot be a symlink: ' + str(dest))
    store_abs = os.path.abspath(os.fspath(store_root))
    dest_abs = os.path.abspath(str(dest))
    try:
        shared = os.path.commonpath([store_abs, dest_abs])
    except ValueError:
        # Different drives on Windows etc.; nothing in common is safe.
        shared = None
    if shared == store_abs:
        raise MaterialsError(
            'output must not be inside the store: ' + str(dest))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    try:
        if args.command == 'init':
            if args.policy.resolve() == args.source.resolve():
                raise MaterialsError(
                    'roster and policy files must differ')
            report = initialize(args.store, args.source, args.policy)
        elif args.command == 'ingest':
            manifest, _digest = read_manifest(args.manifest)
            report = ingest(args.store, manifest, args.files)
        else:
            if not 0 <= args.day <= MAX_DAY:
                raise MaterialsError(
                    'day must be an integer in 0..%d' % MAX_DAY)
            _ensure_safe_output(args.output, args.store)
            report = write_report(args.store, args.day, args.output)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, json.JSONDecodeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
