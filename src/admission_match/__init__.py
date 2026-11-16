"""Applicant-proposing deferred acceptance with blocking-pair audit.

The package contains three independent contracts:

* :mod:`admission_match.model` / :mod:`admission_match.matching` implement the
  single-shot ``--source/--output`` analysis, unchanged;
* :mod:`admission_match.source`, :mod:`admission_match.regional` and
  :mod:`admission_match.store` implement the persistent shared regional-quota
  admission workbench (init/apply/export);
* :mod:`admission_match.audit` implements the read-only pre-import bilateral
  declaration audit (``audit`` subcommand), which never runs matching and
  never touches the workbench.
* :mod:`admission_match.materials` implements the independent admission
  materials library (``materials init/ingest/report``): content-addressed
  immutable file objects, per-applicant per-kind slot versioning in a SQLite
  directory and day-based readiness reports.  It neither allocates places nor
  performs the bilateral audit.
"""
