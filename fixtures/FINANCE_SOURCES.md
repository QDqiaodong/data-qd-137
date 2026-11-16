# Finance demo fixtures

These small, hand-written fixtures exercise the independent admission finance
contract end to end without any real payment:

* `finance_roster.json` — roster following `docs/contract.md`; the finance
  library keeps applicant identities only and never runs matching.
* `finance_charges.json` — fee schedule (`charges` array) for those
  applicants in CNY/USD/EUR minor units.
* `finance_bank.csv` — strict seven-column bank-fact CSV (one posted credit,
  one posted refund whose `origin_id` points at the credit).
* `finance_request.json` — one request whose `bank_sha256` is the SHA-256 of
  the exact bytes of `finance_bank.csv`.  It splits the credit across two
  applicants and allocates the refund back to applicant A's claim.

Recompute the hash after editing the CSV, e.g. `sha256sum finance_bank.csv`,
and update `finance_request.json` accordingly; a mismatch makes the whole
batch fail.
