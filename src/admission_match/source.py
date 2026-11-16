"""Strict parsing and validation of the shared-quota workbench source contract.

A source document has exactly three top-level arrays: applicants, programs and
regions.  This contract is independent of the single-shot ``model.analyze``
contract: programs additionally carry a ``region`` and regions form a single
acyclic rooted tree whose capacities bound every descendant program.
"""
from .common import require, ident, number, rows

MAX_APPLICANTS = 500
MAX_PROGRAMS = 100
MAX_REGIONS = 200
MAX_PREFS = 100
MAX_RANKING = 500
CAP_MAX = 500


def parse_source(data):
    """Return a validated, immutable-ish snapshot of the source document."""
    require(data, 'applicants programs regions')
    applicants = {}
    prefs_order = {}
    for item in rows(data['applicants'], MAX_APPLICANTS):
        require(item, 'id preferences')
        aid = ident(item['id'])
        if aid in applicants:
            raise ValueError('duplicate applicant id: ' + aid)
        prefs = [ident(p) for p in rows(item['preferences'], MAX_PREFS)]
        if len(set(prefs)) != len(prefs):
            raise ValueError('duplicate preference for applicant ' + aid)
        applicants[aid] = prefs
        prefs_order[aid] = prefs

    regions_raw = rows(data['regions'], MAX_REGIONS)
    region_parent = {}
    region_capacity = {}
    for item in regions_raw:
        require(item, 'id parent capacity')
        rid = ident(item['id'])
        if rid in region_parent:
            raise ValueError('duplicate region id: ' + rid)
        parent = item['parent']
        if parent is not None:
            parent = ident(parent)
        region_parent[rid] = parent
        region_capacity[rid] = number(item['capacity'], 0, CAP_MAX)

    # The parent relation must be one acyclic rooted tree.
    roots = [rid for rid, parent in region_parent.items() if parent is None]
    if len(roots) != 1:
        raise ValueError('regions must form exactly one rooted tree')
    ancestors = {}
    for rid in region_parent:
        chain = []
        cur = rid
        seen = set()
        while cur is not None:
            if cur in seen:
                raise ValueError('region parent chain contains a cycle')
            seen.add(cur)
            chain.append(cur)
            cur = region_parent[cur]
            if cur is not None and cur not in region_parent:
                raise ValueError('region references undeclared parent: ' + cur)
        # chain walks the node up to the root; ancestors exclude the node.
        ancestors[rid] = chain[1:]

    programs = {}
    programs_order = []
    for item in rows(data['programs'], MAX_PROGRAMS):
        require(item, 'id capacity ranking region')
        pid = ident(item['id'])
        if pid in programs:
            raise ValueError('duplicate program id: ' + pid)
        cap = number(item['capacity'], 0, CAP_MAX)
        ranking = [ident(a) for a in rows(item['ranking'], MAX_RANKING)]
        if len(set(ranking)) != len(ranking):
            raise ValueError('duplicate ranking entry for program ' + pid)
        region = ident(item['region'])
        if region not in region_parent:
            raise ValueError('program ' + pid + ' references undeclared region')
        programs[pid] = {'capacity': cap, 'ranking': ranking, 'region': region}
        programs_order.append(pid)

    if not programs:
        raise ValueError('at least one program is required')
    # Every preference and every ranking entry must reference declared ids;
    # only mutually acceptable contracts are ever considered later.
    for aid, prefs in applicants.items():
        for pid in prefs:
            if pid not in programs:
                raise ValueError('applicant ' + aid + ' prefers unknown program ' + pid)
    for pid, spec in programs.items():
        for aid in spec['ranking']:
            if aid not in applicants:
                raise ValueError('program ' + pid + ' ranks unknown applicant ' + aid)

    # Program region -> inclusive ancestor chain (program region first).
    program_chain = {
        pid: [programs[pid]['region']] + ancestors[programs[pid]['region']]
        for pid in programs
    }
    return {
        'applicants': applicants,
        'programs': programs,
        'programs_order': programs_order,
        'region_parent': region_parent,
        'region_capacity': region_capacity,
        'region_ancestors': ancestors,
        'program_chain': program_chain,
        'root': roots[0],
    }
