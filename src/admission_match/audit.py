"""Pre-import bilateral declaration audit (data-quality product).

This module only states whether the applicant preference lists and the
program ranking lists agree with each other.  It never runs the matching
algorithm, never touches the SQLite workbench and never modifies the source.

Responsibilities are kept in three separate layers:

* :func:`parse_declaration_source` -- strict parsing and reference
  validation of the ``applicants``/``programs`` source contract from
  ``docs/contract.md``.  It is deliberately independent of
  :mod:`admission_match.model`: reusing ``analyze`` would execute the
  matching, which this product must not do.
* :func:`build_declaration_ledger` -- sparse bilateral relation ledger over
  the *union* of both sides' declarations.  The applicant x program cross
  product is never enumerated; pairs declared by neither side produce no
  record.
* :func:`parse_policy` / :func:`evaluate_policy` -- policy validation and
  threshold judgment.  Share limits are compared by integer cross
  multiplication only; no floating point is involved.
"""
from .common import require, ident, number, rows

MAX_APPLICANTS = 500
MAX_PROGRAMS = 100
MAX_PREFS = 100
MAX_RANKING = 500
CAP_MAX = 500
MAX_UNRESOLVED_PER_PROGRAM = 500
MAX_SHARE_DENOMINATOR = 100000
# A waiver must name one real unilateral declaration, so a valid policy can
# never hold more waivers than the largest possible declaration union.
MAX_WAIVERS = MAX_APPLICANTS * MAX_PREFS + MAX_PROGRAMS * MAX_RANKING

SCHEMA_VERSION = 1


def parse_declaration_source(data):
    """Strictly parse and validate the applicants/programs source contract."""
    require(data, 'applicants programs')
    applicants = {}
    for item in rows(data['applicants'], MAX_APPLICANTS):
        require(item, 'id preferences')
        aid = ident(item['id'])
        if aid in applicants:
            raise ValueError('duplicate applicant id: ' + aid)
        prefs = [ident(p) for p in rows(item['preferences'], MAX_PREFS)]
        if len(set(prefs)) != len(prefs):
            raise ValueError('duplicate preference for applicant ' + aid)
        applicants[aid] = prefs
    programs = {}
    for item in rows(data['programs'], MAX_PROGRAMS):
        require(item, 'id capacity ranking')
        pid = ident(item['id'])
        if pid in programs:
            raise ValueError('duplicate program id: ' + pid)
        capacity = number(item['capacity'], 0, CAP_MAX)
        ranking = [ident(a) for a in rows(item['ranking'], MAX_RANKING)]
        if len(set(ranking)) != len(ranking):
            raise ValueError('duplicate ranking entry for program ' + pid)
        programs[pid] = {'capacity': capacity, 'ranking': ranking}
    if not programs:
        raise ValueError('at least one program is required')
    for aid, prefs in applicants.items():
        for pid in prefs:
            if pid not in programs:
                raise ValueError(
                    'applicant ' + aid + ' prefers unknown program ' + pid)
    for pid, spec in programs.items():
        for aid in spec['ranking']:
            if aid not in applicants:
                raise ValueError(
                    'program ' + pid + ' ranks unknown applicant ' + aid)
    return {'applicants': applicants, 'programs': programs}


def build_declaration_ledger(source):
    """Sparse union of both sides' declarations, keyed by (applicant, program).

    Only pairs declared by at least one side appear; the full cross product
    is never enumerated.  Each entry keeps the 0-based position in each
    original list, ``None`` on the side that did not declare the pair.
    """
    positions = {}
    for aid, prefs in source['applicants'].items():
        for index, pid in enumerate(prefs):
            positions.setdefault((aid, pid), [None, None])[0] = index
    for pid, spec in source['programs'].items():
        for index, aid in enumerate(spec['ranking']):
            positions.setdefault((aid, pid), [None, None])[1] = index
    ledger = {}
    for (aid, pid), (applicant_pos, program_pos) in positions.items():
        if applicant_pos is not None and program_pos is not None:
            kind = 'mutual'
        elif applicant_pos is not None:
            kind = 'applicant_only'
        else:
            kind = 'program_only'
        ledger[(aid, pid)] = {
            'applicant': aid,
            'program': pid,
            'kind': kind,
            'applicant_position': applicant_pos,
            'program_position': program_pos,
        }
    return ledger


def parse_policy(data, source_sha256):
    """Validate the strict policy contract against this run's source digest."""
    require(data, 'source_sha256 waivers limits')
    declared = data['source_sha256']
    if not isinstance(declared, str):
        raise ValueError('policy source_sha256 must be a string')
    if declared != source_sha256:
        raise ValueError('policy source_sha256 does not match source bytes')
    waivers = []
    seen = set()
    for item in rows(data['waivers'], MAX_WAIVERS):
        require(item, 'applicant program reason')
        aid = ident(item['applicant'])
        pid = ident(item['program'])
        reason = item['reason']
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('waiver reason must be a non-empty string')
        if (aid, pid) in seen:
            raise ValueError(
                'duplicate waiver for ' + aid + ' / ' + pid)
        seen.add((aid, pid))
        waivers.append({'applicant': aid, 'program': pid, 'reason': reason})
    limits = data['limits']
    require(limits, 'max_unresolved_per_program max_unresolved_share')
    max_unresolved = number(limits['max_unresolved_per_program'],
                            0, MAX_UNRESOLVED_PER_PROGRAM)
    share = limits['max_unresolved_share']
    require(share, 'numerator denominator')
    denominator = number(share['denominator'], 1, MAX_SHARE_DENOMINATOR)
    numerator = number(share['numerator'], 0, denominator)
    return {
        'waivers': waivers,
        'limits': {
            'max_unresolved_per_program': max_unresolved,
            'max_unresolved_share': {
                'numerator': numerator,
                'denominator': denominator,
            },
        },
    }


def evaluate_policy(source, ledger, policy, source_sha256, policy_sha256):
    """Apply waivers, then judge per-program thresholds with integers only.

    A waiver is accepted only when the ledger proves the pair is a real
    unilateral declaration of this source; it changes the record's status
    and the program's unresolved count, but never removes the record or
    rewrites either side's position.
    """
    unilateral = {key: record for key, record in ledger.items()
                  if record['kind'] != 'mutual'}
    waived = {}
    for waiver in policy['waivers']:
        key = (waiver['applicant'], waiver['program'])
        if key not in unilateral:
            raise ValueError(
                'waiver does not match a unilateral declaration: '
                + waiver['applicant'] + ' / ' + waiver['program'])
        waived[key] = waiver['reason']

    totals = {'mutual': 0, 'applicant_only': 0, 'program_only': 0}
    stats = {pid: {'declarations': 0, 'mutual': 0, 'applicant_only': 0,
                   'program_only': 0, 'waived': 0, 'unresolved': 0}
             for pid in source['programs']}
    records = []
    for key in sorted(ledger):
        record = ledger[key]
        kind = record['kind']
        totals[kind] += 1
        program_stats = stats[record['program']]
        program_stats['declarations'] += 1
        program_stats[kind] += 1
        if kind == 'mutual':
            continue
        if key in waived:
            status, reason = 'waived', waived[key]
            program_stats['waived'] += 1
        else:
            status, reason = 'unresolved', None
            program_stats['unresolved'] += 1
        records.append({
            'applicant': record['applicant'],
            'program': record['program'],
            'kind': kind,
            'applicant_position': record['applicant_position'],
            'program_position': record['program_position'],
            'status': status,
            'waiver_reason': reason,
        })

    max_unresolved = policy['limits']['max_unresolved_per_program']
    share_num = policy['limits']['max_unresolved_share']['numerator']
    share_den = policy['limits']['max_unresolved_share']['denominator']
    programs = []
    exceeded = []
    for pid in sorted(stats):
        program_stats = stats[pid]
        unresolved = program_stats['unresolved']
        declarations = program_stats['declarations']
        within_count = unresolved <= max_unresolved
        # Integer cross multiplication only; an empty declaration union
        # means share 0 and therefore always passes.
        within_share = unresolved * share_den <= share_num * declarations
        ok = within_count and within_share
        if not ok:
            exceeded.append(pid)
        programs.append({
            'program': pid,
            'declarations': declarations,
            'mutual': program_stats['mutual'],
            'applicant_only': program_stats['applicant_only'],
            'program_only': program_stats['program_only'],
            'waived': program_stats['waived'],
            'unresolved': unresolved,
            'within_count_limit': within_count,
            'within_share_limit': within_share,
            'ok': ok,
        })

    return {
        'schema_version': SCHEMA_VERSION,
        'source_sha256': source_sha256,
        'policy_sha256': policy_sha256,
        'limits': policy['limits'],
        'totals': totals,
        'programs': programs,
        'records': records,
        'exceeded_programs': exceeded,
        'ok': not exceeded,
    }


def audit_report(source_data, source_sha256, policy_data, policy_sha256):
    """Build the deterministic audit report for already-read input bytes."""
    source = parse_declaration_source(source_data)
    policy = parse_policy(policy_data, source_sha256)
    ledger = build_declaration_ledger(source)
    return evaluate_policy(source, ledger, policy,
                           source_sha256, policy_sha256)
