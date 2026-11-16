"""Strict parsing of the fee schedule (the ``charges`` init input).

The document contains exactly one key, ``charges``.  Each entry is exactly
``applicant``/``currency``/``due_minor``; the same ``(applicant, currency)``
pair appears at most once, the applicant must exist in the roster, the
currency is one of ``CNY``/``USD``/``EUR`` and ``due_minor`` is an integer in
``0..10**12``.  Booleans are not accepted as integers.
"""
from ..common import ident, require, rows
from .roster import FinanceError

MAX_CHARGES = 5000
AMOUNT_MAX = 10 ** 12
CURRENCIES = ('CNY', 'USD', 'EUR')


def amount_minor(value):
    """Strict nonnegative amount: integers only, never bool, bounded."""
    if type(value) is not int or not 0 <= value <= AMOUNT_MAX:
        raise FinanceError(
            'amount must be an integer in 0..%d' % AMOUNT_MAX)
    return value


def parse_charges(data, applicants):
    try:
        return _parse_charges(data, applicants)
    except FinanceError:
        raise
    except ValueError as error:
        raise FinanceError(str(error)) from None


def _parse_charges(data, applicants):
    require(data, 'charges')
    entries = rows(data['charges'], MAX_CHARGES)
    known = set(applicants)
    seen = set()
    clean = []
    for entry in entries:
        require(entry, 'applicant currency due_minor')
        aid = ident(entry['applicant'])
        if aid not in known:
            raise FinanceError(
                'charge references unknown applicant: ' + aid)
        currency = entry['currency']
        if currency not in CURRENCIES:
            raise FinanceError(
                'currency must be one of CNY/USD/EUR: ' + str(currency))
        due = amount_minor(entry['due_minor'])
        key = (aid, currency)
        if key in seen:
            raise FinanceError(
                'duplicate charge for %s/%s' % key)
        seen.add(key)
        clean.append({'applicant': aid, 'currency': currency,
                      'due_minor': due})
    return clean
