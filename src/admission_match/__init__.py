"""Applicant-proposing deferred acceptance with blocking-pair audit.

The package contains two independent contracts:

* :mod:`admission_match.model` / :mod:`admission_match.matching` implement the
  single-shot ``--source/--output`` analysis, unchanged;
* :mod:`admission_match.source`, :mod:`admission_match.regional` and
  :mod:`admission_match.store` implement the persistent shared regional-quota
  admission workbench (init/apply/export).
"""
