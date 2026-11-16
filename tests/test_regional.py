"""Regional choice-rule tests, with an independent brute-force oracle.

The oracle re-implements feasibility, the ``C_L`` greedy scan and the exact
blocking definition from scratch; it never imports the production choice
function, so agreement is an independent check of applicant optimality.
"""
import itertools
import random
import unittest

from admission_match.regional import regional_choice, run_matching, \
    audit_blocking_pairs
from admission_match.store import Snapshot


class _Snap:
    """Minimal snapshot matching the attributes used by the regional module."""

    def __init__(self, applicants, programs, capacities, chain,
                 region_parent):
        self.applicants = applicants
        self.programs = programs
        self.capacities = capacities
        self.program_chain = chain
        self.region_parent = region_parent
        self.rank_index = {p: {a: i for i, a in enumerate(spec['ranking'])}
                           for p, spec in programs.items()}

    def usage_from(self, contracts):
        pu = {p: 0 for p in self.programs}
        ru = {r: 0 for r in self.region_parent}
        for _a, p in contracts:
            pu[p] += 1
            for r in self.program_chain[p]:
                ru[r] += 1
        return {'program': pu, 'region': ru}


def _chain(region, parent):
    out = [region]
    cur = parent[region]
    while cur is not None:
        out.append(cur)
        cur = parent[cur]
    return out


def make_snap(applicants, programs, region_parent, region_capacity):
    """programs: pid -> {'capacity','ranking','region'}."""
    chain = {p: _chain(spec['region'], region_parent)
             for p, spec in programs.items()}
    caps = {
        'program': {p: spec['capacity'] for p, spec in programs.items()},
        'region': dict(region_capacity),
    }
    return _Snap(applicants, programs, caps, chain, region_parent)


# --------------------------------------------------------------------------
# Independent oracle (no reuse of production regional_choice).
# --------------------------------------------------------------------------
def oracle_choice(candidates, confirmed, snap):
    """Re-derived C_L: reserves confirmed seats, greedy global-rank scan."""
    kept = set(confirmed)
    usage = snap.usage_from(kept)
    pending = []
    for aid, pid in candidates:
        if (aid, pid) in kept:
            continue
        ranking = snap.programs[pid]['ranking']
        if aid not in ranking:
            continue
        pending.append((ranking.index(aid), pid, aid))
    pending.sort()
    for _rank, pid, aid in pending:
        if usage['program'][pid] >= snap.capacities['program'][pid]:
            continue
        if any(usage['region'][r] >= snap.capacities['region'][r]
               for r in snap.program_chain[pid]):
            continue
        kept.add((aid, pid))
        usage['program'][pid] += 1
        for r in snap.program_chain[pid]:
            usage['region'][r] += 1
    return kept


def feasible(assign, snap):
    usage = snap.usage_from((a, p) for a, p in assign.items() if p)
    return all(n <= snap.capacities['program'][p]
               for p, n in usage['program'].items()) and \
        all(n <= snap.capacities['region'][r]
            for r, n in usage['region'].items())


def is_stable(assign, snap, confirmed=()):
    """Blocking contract x iff x in C_L(H union {x}); L empty for the oracle."""
    if not feasible(assign, snap):
        return False
    l_set = set(confirmed)
    confirmed_aids = {a for a, _p in l_set}
    h = {(a, p) for a, p in assign.items() if p}
    for aid, prefs in snap.applicants.items():
        if aid in confirmed_aids:
            continue  # confirmed applicants do not join the blocking audit
        cur = assign.get(aid)
        for pid in prefs:
            if pid == cur:
                break
            if aid not in snap.programs[pid]['ranking']:
                continue
            x = (aid, pid)
            if x in oracle_choice(h | {x}, l_set, snap):
                return False
    return True


def pref_rank(aid, pid, snap):
    prefs = snap.applicants[aid]
    return prefs.index(pid) if pid in prefs else len(prefs)


def applicant_optimal(stable_matches, snap):
    """The matching every applicant weakly prefers to every other stable one."""
    def rank_of(assign, aid):
        p = assign.get(aid)
        return pref_rank(aid, p, snap) if p else len(snap.applicants[aid])
    best = []
    for m in stable_matches:
        if all(rank_of(m, a) <= rank_of(other, a)
               for other in stable_matches for a in snap.applicants):
            best.append(m)
    return best


def enumerate_stable(snap, confirmed=()):
    ids = sorted(snap.applicants)
    l_set = set(confirmed)
    confirmed_aids = {a for a, _p in l_set}
    free = [a for a in ids if a not in confirmed_aids]
    options = [[None] + [p for p in snap.applicants[a]
                         if a in snap.programs[p]['ranking']]
               for a in free]
    stable = []
    for combo in itertools.product(*options):
        assign = {a: p for a, p in zip(free, combo) if p}
        assign.update(l_set)  # confirmed seats are fixed and pre-reserved
        # Per-program and per-region capacities are all enforced by
        # feasibility() below, including multiple seats at one program.
        if is_stable(assign, snap, confirmed=l_set):
            stable.append({a: p for a, p in assign.items()
                           if a in free})  # compare residual markets only
    return stable


def literal_audit(snap, status, offers):
    """Spec-literal audit: x blocks iff x in C_L(H union {x})."""
    confirmed = {(a, p) for a, p in offers.items()
                 if status[a] == 'confirmed'}
    h = {(a, p) for a, p in offers.items() if status[a] == 'active'}
    blocks = []
    for aid in sorted(a for a, st in status.items() if st == 'active'):
        cur = offers.get(aid)
        for pid in snap.applicants[aid]:
            if pid == cur:
                break
            if aid not in snap.programs[pid]['ranking']:
                continue
            x = (aid, pid)
            if x in oracle_choice(h | {x}, confirmed, snap):
                blocks.append({'applicant': aid, 'program': pid})
    return blocks


class RegionalChoice(unittest.TestCase):
    def test_choice_keeps_confirmed_and_rank_order(self):
        applicants = {'A': ['X'], 'B': ['X'], 'C': ['Y']}
        programs = {
            'X': {'capacity': 1, 'region': 'r', 'ranking': ['B', 'A']},
            'Y': {'capacity': 1, 'region': 'r', 'ranking': ['C']},
        }
        parent = {'r': None}
        snap = make_snap(applicants, programs, parent, {'r': 2})
        # A is confirmed at X even though B ranks higher; the seat is
        # protected, B is turned away, C still takes the free Y seat.
        kept = regional_choice({('B', 'X'), ('C', 'Y')}, {('A', 'X')}, snap)
        self.assertEqual(kept, {('A', 'X'), ('C', 'Y')})
        # With only one region seat, the confirmed contract consumes it and
        # the sibling program cannot admit anyone.
        tight = make_snap(applicants, programs, parent, {'r': 1})
        kept = regional_choice({('B', 'X'), ('C', 'Y')}, {('A', 'X')}, tight)
        self.assertEqual(kept, {('A', 'X')})

    def test_siblings_unfilled_but_ancestor_full(self):
        # X and Y each have spare seats, but their common ancestor caps at 1.
        applicants = {a: ['X', 'Y'] for a in ('A', 'B')}
        programs = {
            'X': {'capacity': 1, 'region': 'r1', 'ranking': ['A', 'B']},
            'Y': {'capacity': 1, 'region': 'r2', 'ranking': ['A', 'B']},
        }
        parent = {'root': None, 'r1': 'root', 'r2': 'root'}
        caps = {'root': 1, 'r1': 1, 'r2': 1}
        snap = make_snap(applicants, programs, parent, caps)
        offers, _ = run_matching(snap, {}, sorted(applicants))
        self.assertEqual(len(offers), 1)  # ancestor quota, not program seats
        winner = next(iter(offers))
        self.assertEqual(offers[winner], 'X')  # global rank (0,'X',...) wins
        self.assertEqual(audit_blocking_pairs(
            snap, {a: 'active' for a in applicants}, offers), [])

    def test_cross_project_displacement_multiple_rounds(self):
        # B's loss at X cascades through Y and Z before everyone settles.
        applicants = {
            'A': ['X'], 'B': ['X', 'Y'],
            'C': ['Y', 'Z'], 'D': ['Z'],
        }
        programs = {
            'X': {'capacity': 1, 'region': 'east',
                  'ranking': ['A', 'B']},
            'Y': {'capacity': 1, 'region': 'east',
                  'ranking': ['C', 'B']},
            'Z': {'capacity': 1, 'region': 'west',
                  'ranking': ['C', 'D']},
        }
        parent = {'root': None, 'east': 'root', 'west': 'root'}
        # east=1 forces X and Y to share one seat; root=2 allows Z as well.
        caps = {'root': 2, 'east': 1, 'west': 1}
        snap = make_snap(applicants, programs, parent, caps)
        offers, proposals = run_matching(snap, {}, sorted(applicants))
        # A takes the single east seat.  C, rejected at Y by the east quota,
        # cascades to Z and displaces D (C outranks D there); B and D exhaust
        # their lists.  The displacement crosses program and region subtree.
        self.assertEqual(offers, {'A': 'X', 'C': 'Z'})
        proposed = [(p['applicant'], p['program']) for p in proposals]
        self.assertIn(('B', 'X'), proposed)
        self.assertIn(('B', 'Y'), proposed)   # B's second-round continuation
        self.assertIn(('C', 'Y'), proposed)
        self.assertIn(('C', 'Z'), proposed)   # C's cross-project continuation
        self.assertEqual(proposed.count(('D', 'Z')), 1)
        self.assertEqual(audit_blocking_pairs(
            snap, {a: 'active' for a in applicants}, offers), [])

    def test_two_ancestor_levels_tight(self):
        # A contract must clear both the leaf region and the root quota.
        applicants = {a: ['X'] for a in ('A', 'B')}
        programs = {
            'X': {'capacity': 2, 'region': 'leaf',
                  'ranking': ['A', 'B']},
        }
        parent = {'root': None, 'leaf': 'root'}
        snap = make_snap(applicants, programs, parent,
                         {'root': 1, 'leaf': 1})
        offers, _ = run_matching(snap, {}, sorted(applicants))
        self.assertEqual(offers, {'A': 'X'})
        self.assertEqual(audit_blocking_pairs(
            snap, {a: 'active' for a in applicants}, offers), [])

    def test_unacceptable_contracts_never_candidates(self):
        applicants = {'A': ['X'], 'B': ['X']}
        programs = {'X': {'capacity': 2, 'region': 'r',
                          'ranking': ['A']}}
        snap = make_snap(applicants, programs, {'r': None}, {'r': 2})
        kept = regional_choice({('A', 'X'), ('B', 'X')}, set(), snap)
        self.assertEqual(kept, {('A', 'X')})


class BruteForceOptimality(unittest.TestCase):
    """Random small instances: production result equals the applicant-optimal
    stable feasible matching found by exhaustive enumeration."""

    def setUp(self):
        self.rng = random.Random(20261116)

    def _random_instance(self):
        n_a = self.rng.randrange(2, 5)
        n_p = self.rng.randrange(2, 4)
        aids = ['a%d' % i for i in range(n_a)]
        pids = ['p%d' % i for i in range(n_p)]
        applicants = {}
        for aid in aids:
            prefs = [p for p in pids if self.rng.random() < 0.75]
            applicants[aid] = prefs
        # A random rooted tree over 1..3 regions.
        n_r = self.rng.randrange(1, 4)
        rids = ['r%d' % i for i in range(n_r)]
        parent = {rids[0]: None}
        for i in range(1, n_r):
            parent[rids[i]] = self.rng.choice(rids[:i])
        region_cap = {r: self.rng.randrange(0, n_a + 1) for r in rids}
        programs = {}
        for pid in pids:
            ranking = [a for a in aids if self.rng.random() < 0.7]
            programs[pid] = {
                'capacity': self.rng.randrange(0, n_a + 1),
                'ranking': ranking,
                'region': self.rng.choice(rids),
            }
        snap = make_snap(applicants, programs, parent, region_cap)
        return snap, aids

    def test_random_instances(self):
        checked = 0
        for _ in range(60):
            snap, aids = self._random_instance()
            stable = enumerate_stable(snap)
            offers, _ = run_matching(snap, {}, aids)
            # Production output must itself be stable.
            status = {a: 'active' for a in aids}
            self.assertEqual(audit_blocking_pairs(snap, status, offers), [])
            if not stable:
                # Infeasible preference structure can leave no stable
                # matching under hard caps; production must still respect caps.
                usage = snap.usage_from(offers.items())
                self.assertTrue(
                    all(n <= snap.capacities['program'][p]
                        for p, n in usage['program'].items()))
                continue
            optimal = applicant_optimal(stable, snap)
            prod = dict(offers)
            self.assertIn(prod, optimal,
                          msg='not applicant-optimal: %s vs %s'
                              % (prod, optimal))
            checked += 1
        self.assertGreater(checked, 20)

    def test_audit_shortcut_matches_literal_spec_audit(self):
        rng = random.Random(424242)
        for _ in range(80):
            snap, aids = self._random_instance()
            status = {a: 'active' for a in aids}
            offers, _ = run_matching(snap, {}, aids)
            self.assertEqual(
                audit_blocking_pairs(snap, status, offers),
                literal_audit(snap, status, offers))
            # The shortcut's precondition is that H itself came out of C_L.
            # Build such sets directly: partial greedy outputs usually still
            # contain blocking pairs, exercising both shortcut branches,
            # while remaining valid inputs to the equivalence.
            all_edges = [(a, p) for a in aids for p in snap.applicants[a]
                         if a in snap.programs[p]['ranking']]
            rng.shuffle(all_edges)
            chosen = regional_choice(all_edges[:rng.randrange(len(all_edges)
                                                              + 1)],
                                     set(), snap)
            partial = dict(chosen)
            self.assertEqual(
                audit_blocking_pairs(snap, status, partial),
                literal_audit(snap, status, partial))

    def test_random_instances_with_protected_confirmations(self):
        checked = 0
        for _ in range(40):
            snap, aids = self._random_instance()
            # Choose a feasible random protected confirmation set.
            rng = self.rng
            confirmed = {}
            for aid in aids:
                if rng.random() < 0.35:
                    prefs = [p for p in snap.applicants[aid]
                             if aid in snap.programs[p]['ranking']]
                    if not prefs:
                        continue
                    candidate = (aid, prefs[0])
                    trial = set(confirmed.items()) | {candidate}
                    if feasible({a: p for a, p in trial}, snap):
                        confirmed[aid] = prefs[0]
            free = [a for a in aids if a not in confirmed]
            stable = enumerate_stable(snap, confirmed=tuple(confirmed.items()))
            offers, _ = run_matching(snap, confirmed, free)
            status = {a: ('confirmed' if a in confirmed else 'active')
                      for a in aids}
            self.assertEqual(
                audit_blocking_pairs(
                    snap, status, {**confirmed, **offers}), [])
            if not stable:
                usage = snap.usage_from(
                    list(confirmed.items()) + list(offers.items()))
                self.assertTrue(
                    all(n <= snap.capacities['program'][p]
                        for p, n in usage['program'].items()))
                continue
            optimal = applicant_optimal(stable, snap)
            self.assertIn(dict(offers), optimal)
            checked += 1
        self.assertGreater(checked, 10)

    def test_confirmed_protected_against_higher_rank(self):
        # With a protected confirmation, enumeration runs over the residual
        # market and production must agree while the confirmed seat survives.
        applicants = {'A': ['X'], 'B': ['X', 'Y'], 'C': ['Y']}
        programs = {
            'X': {'capacity': 1, 'region': 'r', 'ranking': ['B', 'A']},
            'Y': {'capacity': 1, 'region': 'r', 'ranking': ['C', 'B']},
        }
        snap = make_snap(applicants, programs, {'r': None}, {'r': 2})
        offers, _ = run_matching(snap, {'A': 'X'}, ['B', 'C'])
        # Confirmed applicants are not re-offered; A's protected seat simply
        # stays in place and B cannot displace it.
        self.assertNotIn('A', offers)
        self.assertEqual(offers, {'C': 'Y'})
        status = {'A': 'confirmed', 'B': 'active', 'C': 'active'}
        all_offers = {'A': 'X', **offers}
        self.assertEqual(audit_blocking_pairs(snap, status, all_offers), [])


if __name__ == '__main__':
    unittest.main()
