"""Strict roster parsing for the finance library: identities only.

The roster reuses the ``applicants``/``programs`` structure and every boundary
of the single-shot contract in ``docs/contract.md`` (at most 500 applicants,
1..100 programs, strict unique preference/ranking lists, declared references,
capacity 0..500).  The finance library borrows *applicant identities only*:
the deferred-acceptance matcher is never imported and never runs, and nothing
about program capacities or preferences influences fee clearing.
"""
from ..common import ident, number, require, rows

MAX_APPLICANTS = 500
MAX_PROGRAMS = 100
MAX_PREFS = 100
MAX_RANKING = 500
CAP_MAX = 500


class FinanceError(ValueError):
    """Any contract, state or storage failure of the finance library."""


def parse_roster(data):
    """Validate the roster and return only the sorted applicant ids."""
    try:
        return _parse_roster(data)
    except FinanceError:
        raise
    except ValueError as error:
        raise FinanceError(str(error)) from None


def _parse_roster(data):
    require(data, 'applicants programs')
    applicants = {}
    for item in rows(data['applicants'], MAX_APPLICANTS):
        require(item, 'id preferences')
        aid = ident(item['id'])
        if aid in applicants:
            raise FinanceError('duplicate applicant id: ' + aid)
        prefs = [ident(p) for p in rows(item['preferences'], MAX_PREFS)]
        if len(set(prefs)) != len(prefs):
            raise FinanceError('duplicate preference for applicant ' + aid)
        applicants[aid] = prefs

    programs = {}
    for item in rows(data['programs'], MAX_PROGRAMS):
        require(item, 'id capacity ranking')
        pid = ident(item['id'])
        if pid in programs:
            raise FinanceError('duplicate program id: ' + pid)
        number(item['capacity'], 0, CAP_MAX)
        ranking = [ident(a) for a in rows(item['ranking'], MAX_RANKING)]
        if len(set(ranking)) != len(ranking):
            raise FinanceError('duplicate ranking entry for program ' + pid)
        programs[pid] = ranking
    if not programs:
        raise FinanceError('at least one program is required')

    # Identities and references on both sides must still be well-formed even
    # though the finance library never consumes the preference content.
    for aid, prefs in applicants.items():
        for pid in prefs:
            if pid not in programs:
                raise FinanceError(
                    'applicant ' + aid + ' prefers unknown program ' + pid)
    for pid, ranking in programs.items():
        for aid in ranking:
            if aid not in applicants:
                raise FinanceError(
                    'program ' + pid + ' ranks unknown applicant ' + aid)

    return {'applicants': sorted(applicants), 'programs': sorted(programs)}
