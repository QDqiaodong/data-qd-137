"""Independent admission materials library.

This package is a third contract, fully independent of both the single-shot
matching command (``model``/``matching``) and the regional-quota workbench
(``source``/``store``):

* :mod:`admission_match.materials.roster` -- strict parsing of the
  ``applicants``/``programs`` roster contract from ``docs/contract.md`` plus
  the materials ``requirements`` policy.  Only applicant, program and
  preference *identities* are borrowed; neither ``analyze`` nor ``allocate``
  ever runs.
* :mod:`admission_match.materials.manifest` -- strict validation and canonical
  fingerprints of ingestion manifests.
* :mod:`admission_match.materials.objects` -- streaming, content-addressed,
  immutable file objects (SHA256 dedup, never fully buffered in memory).
* :mod:`admission_match.materials.store` -- the SQLite directory ledger and
  the crash/concurrency protocol that ties objects and slots together.
* :mod:`admission_match.materials.reporting` -- the deterministic readiness
  report built from one consistent directory snapshot.
* :mod:`admission_match.materials.cli` -- the ``materials`` subcommand group
  (``init`` / ``ingest`` / ``report``), also runnable as
  ``python -m admission_match.materials.cli ...``.

The library never allocates places and never performs the bilateral
declaration audit; it only receives application materials and answers whether
the required material slots are complete for a given day.
"""

SCHEMA_VERSION = 1
