"""Independent admission finance library: fee clearing, not admission.

This package is a fourth contract, fully independent of the single-shot
matching command, the regional-quota workbench, the bilateral audit and the
materials library.  It never allocates places, never judges material
completeness and never checks bilateral preference declarations.  It only
turns bank facts and an administrator's per-item claims/refund allocations
into auditable applicant fee balances.

Modules:

* :mod:`admission_match.finance.roster` -- strict parsing of the
  ``applicants``/``programs`` roster from ``docs/contract.md``; only applicant
  identities are retained and matching never runs.
* :mod:`admission_match.finance.charges` -- strict parsing of the fee schedule.
* :mod:`admission_match.finance.request` -- strict request validation and the
  canonical request fingerprint used for idempotent replay.
* :mod:`admission_match.finance.bankcsv` -- streaming, bounded, disk-staged
  ingestion of the strict bank-fact CSV (the main database holds no
  transaction while the file is parsed).
* :mod:`admission_match.finance.store` -- the SQLite ledger, the single
  atomic revision transaction and the indexed final-state conservation
  checks.
* :mod:`admission_match.finance.reporting` -- the deterministic report built
  from one consistent read snapshot.
* :mod:`admission_match.finance.cli` -- the ``finance init/apply/report``
  subcommand group, also runnable as
  ``python -m admission_match.finance.cli ...``.

All amounts are integer minor units.  Money is never stored or computed as
floating point, currencies are never netted against each other, and every
sum is evaluated with Python's arbitrary-precision integers.
"""

SCHEMA_VERSION = 1
