"""Strict parsing of the materials roster and requirements policy.

The roster reuses the ``applicants``/``programs`` structure and every boundary
of the single-shot contract in ``docs/contract.md`` (at most 500 applicants,
1..100 programs, strict unique preference/ranking lists, declared references,
capacity 0..500).  The parser is deliberately independent of
:func:`admission_match.model.analyze`: the materials library only borrows
applicant, program and preference identities and must never execute matching.

The policy document contains exactly one key, ``requirements``: one entry per
declared program (each program exactly once), each entry exactly
``program``/``kinds``, with 0..20 distinct material kind identifiers.
"""
from ..common import ident, number, require, rows

MAX_APPLICANTS = 500
MAX_PROGRAMS = 100
MAX_PREFS = 100
MAX_RANKING = 500
CAP_MAX = 500
MAX_KINDS_PER_PROGRAM = 20


class MaterialsError(ValueError):
    """Any contract, state or storage failure of the materials library."""


def parse_roster(data):
    """Validate the applicants/programs roster; return identities only."""
    require(data, 'applicants programs')
    applicants = {}
    for item in rows(data['applicants'], MAX_APPLICANTS):
        require(item, 'id preferences')
        aid = ident(item['id'])
        if aid in applicants:
            raise MaterialsError('duplicate applicant id: ' + aid)
        prefs = [ident(p) for p in rows(item['preferences'], MAX_PREFS)]
        if len(set(prefs)) != len(prefs):
            raise MaterialsError('duplicate preference for applicant ' + aid)
        applicants[aid] = prefs

    programs = {}
    programs_order = []
    for item in rows(data['programs'], MAX_PROGRAMS):
        require(item, 'id capacity ranking')
        pid = ident(item['id'])
        if pid in programs:
            raise MaterialsError('duplicate program id: ' + pid)
        capacity = number(item['capacity'], 0, CAP_MAX)
        ranking = [ident(a) for a in rows(item['ranking'], MAX_RANKING)]
        if len(set(ranking)) != len(ranking):
            raise MaterialsError('duplicate ranking entry for program ' + pid)
        programs[pid] = {'capacity': capacity, 'ranking': ranking}
        programs_order.append(pid)
    if not programs:
        raise MaterialsError('at least one program is required')

    # Every reference on both sides must be declared.
    for aid, prefs in applicants.items():
        for pid in prefs:
            if pid not in programs:
                raise MaterialsError(
                    'applicant ' + aid + ' prefers unknown program ' + pid)
    for pid, spec in programs.items():
        for aid in spec['ranking']:
            if aid not in applicants:
                raise MaterialsError(
                    'program ' + pid + ' ranks unknown applicant ' + aid)

    return {
        'applicants': applicants,
        'programs': programs,
        'programs_order': programs_order,
    }


def parse_policy(data, roster):
    """Validate the requirements policy against the parsed roster.

    Returns ``(requirements, program_kinds)`` where ``requirements`` is the
    cleaned, order-preserving list of ``{'program', 'kinds'}`` entries and
    ``program_kinds`` maps every program id to its sorted unique kind list.
    """
    require(data, 'requirements')
    entries = rows(data['requirements'], MAX_PROGRAMS)
    requirements = []
    seen_programs = set()
    for entry in entries:
        require(entry, 'program kinds')
        pid = ident(entry['program'])
        if pid not in roster['programs']:
            raise MaterialsError(
                'requirement references undeclared program: ' + pid)
        if pid in seen_programs:
            raise MaterialsError(
                'duplicate requirements entry for program: ' + pid)
        seen_programs.add(pid)
        raw_kinds = entry['kinds']
        if not isinstance(raw_kinds, list) \
                or len(raw_kinds) > MAX_KINDS_PER_PROGRAM:
            raise MaterialsError(
                'kinds must be an array of at most %d items'
                % MAX_KINDS_PER_PROGRAM)
        kinds = []
        seen_kinds = set()
        for kind in raw_kinds:
            kid = ident(kind)
            if kid in seen_kinds:
                raise MaterialsError(
                    'duplicate kind %s for program %s' % (kid, pid))
            seen_kinds.add(kid)
            kinds.append(kid)
        requirements.append({'program': pid, 'kinds': kinds})

    # Every declared program must appear exactly once.
    declared = set(roster['programs'])
    if seen_programs != declared:
        missing = sorted(declared - seen_programs)
        raise MaterialsError(
            'requirements must name every declared program exactly once; '
            'missing: ' + ','.join(missing))
    program_kinds = {
        entry['program']: sorted(entry['kinds']) for entry in requirements}
    return requirements, program_kinds
