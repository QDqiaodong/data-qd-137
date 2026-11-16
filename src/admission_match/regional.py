"""Cross-project regional choice rule and applicant-proposing matching.

The school/program side never runs a standalone deferred-acceptance pass.
Instead a single choice rule :func:`regional_choice` decides which contracts of
a candidate set survive:

* confirmed contracts (set ``L``) reserve seats at their program and at every
  ancestor region first;
* the remaining candidate contracts are scanned in the global order
  ``(applicant's zero-based rank at the program, program id, applicant id)``;
* a contract is kept only if adding it breaks no program or ancestor-region
  capacity;
* unacceptable contracts never enter the candidate set.

:func:`run_matching` drives applicant-proposing deferred acceptance with that
choice rule.  Every rebuild starts from the full preference lists of all
applicants who are neither withdrawn nor confirmed.
"""


import bisect


def acceptable(applicants, programs):
    """Set of mutually acceptable ``(applicant, program)`` contracts."""
    edges = set()
    for aid, prefs in applicants.items():
        for pid in prefs:
            if aid in programs[pid]['ranking']:
                edges.add((aid, pid))
    return edges


def regional_choice(candidates, confirmed, snapshot):
    """Apply ``C_L`` over an iterable of ``(applicant, program)`` contracts.

    ``confirmed`` contracts are assumed feasible (the persistence layer checks
    the batch feasibility before rebuilding).  Returns the set of kept
    contracts including the confirmed ones.
    """
    programs = snapshot.programs
    capacities = snapshot.capacities
    chain = snapshot.program_chain
    kept = set(confirmed)
    usage = snapshot.usage_from(kept)

    pending = []
    rank_index = getattr(snapshot, 'rank_index', None)
    for aid, pid in candidates:
        if (aid, pid) in kept:
            continue
        ranking = programs[pid]['ranking']
        if aid not in ranking:
            # Unacceptable contracts never enter S.
            continue
        rank = rank_index[pid][aid] if rank_index is not None \
            else ranking.index(aid)
        pending.append((rank, pid, aid))
    pending.sort()

    for _, pid, aid in pending:
        chain_ids = chain[pid]
        if usage['program'][pid] >= capacities['program'][pid]:
            continue
        if any(usage['region'][rid] >= capacities['region'][rid]
               for rid in chain_ids):
            continue
        kept.add((aid, pid))
        usage['program'][pid] += 1
        for rid in chain_ids:
            usage['region'][rid] += 1
    return kept


def run_matching(snapshot, confirmed, active_applicants):
    """Applicant-proposing DA driven by :func:`regional_choice`.

    ``confirmed`` maps applicant -> program for protected confirmations.
    Returns ``(offers, proposals)`` where offers maps applicant -> program for
    active applicants (only those holding an offer are present) and proposals is
    an ordered audit log of every proposal round.
    """
    applicants = snapshot.applicants
    programs = snapshot.programs
    # A contract the other side does not accept never gets proposed.
    ok = acceptable(applicants, programs)

    confirmed_pairs = set(confirmed.items())
    next_index = {aid: 0 for aid in active_applicants}
    current = dict(confirmed_pairs)
    # Active applicants currently holding no offer still have to propose.
    pending = sorted(active_applicants)
    proposals = []

    while pending:
        round_contracts = []
        proposers = []
        for aid in pending:
            prefs = applicants[aid]
            idx = next_index[aid]
            if idx >= len(prefs):
                continue
            pid = prefs[idx]
            next_index[aid] = idx + 1
            proposers.append(aid)
            if (aid, pid) in ok:
                round_contracts.append((aid, pid))
            proposals.append({'applicant': aid, 'program': pid,
                              'acceptable': (aid, pid) in ok})
        if not proposers:
            break
        candidate = set(confirmed_pairs)
        candidate.update((a, p) for a, p in current.items()
                         if a in active_applicants)
        candidate.update(round_contracts)
        chosen = regional_choice(candidate, confirmed_pairs, snapshot)

        new_current = dict(confirmed_pairs)
        for aid, pid in chosen:
            if aid in active_applicants:
                new_current[aid] = pid
        rejected = []
        for aid in proposers:
            if aid not in new_current:
                rejected.append(aid)
        # Any active holder that lost its seat during the re-choice must also
        # fall through to its next preference.
        for aid, pid in list(current.items()):
            if aid in active_applicants and new_current.get(aid) != pid:
                if aid not in rejected:
                    rejected.append(aid)
        current = new_current
        pending = sorted(rejected)

    offers = {aid: pid for aid, pid in current.items()
              if aid in active_applicants}
    return offers, proposals


def audit_blocking_pairs(snapshot, status, offers):
    """Independent blocking-pair audit over unconfirmed contracts.

    For every active, unconfirmed applicant, each preferred acceptable
    contract ``x`` ahead of the current offer is tested with the exact choice
    rule: ``x`` blocks when ``x in C_L(H union {x})`` where ``H`` is the set of
    current unconfirmed contracts.  Confirmed applicants do not participate.

    Equivalent shortcut, avoiding a full greedy run per contract.  Precondition
    (always met in this workbench): every contract in ``H`` was itself selected
    by ``C_L``, so the smaller-keyed ``H`` contracts are exactly the selections
    made before ``x``'s scan position.  Contracts with larger keys cannot change
    whether ``x`` is admitted.  So ``x`` is accepted exactly when the seats still
    free at ``x``'s program and each ancestor region, after the confirmed seats
    and the smaller-keyed ``H`` contracts, cover one more contract.
    """
    confirmed = {a: p for a, p in offers.items() if status[a] == 'confirmed'}
    h_set = {(a, p) for a, p in offers.items() if status[a] == 'active'}
    rank_index = getattr(snapshot, 'rank_index', None)

    def key_of(aid, pid):
        if rank_index is not None:
            rank = rank_index[pid][aid]
        else:
            rank = snapshot.programs[pid]['ranking'].index(aid)
        return (rank, pid, aid)

    confirmed_usage = snapshot.usage_from(confirmed.items())
    # Per-program and per-region sorted key lists of H contracts.
    prog_keys = {pid: [] for pid in snapshot.programs}
    region_keys = {rid: [] for rid in snapshot.region_parent}
    for aid, pid in h_set:
        k = key_of(aid, pid)
        prog_keys[pid].append(k)
        for rid in snapshot.program_chain[pid]:
            region_keys[rid].append(k)
    for values in prog_keys.values():
        values.sort()
    for values in region_keys.values():
        values.sort()

    blocks = []
    for aid in sorted(a for a, st in status.items() if st == 'active'):
        current_pid = offers.get(aid)
        for pid in snapshot.applicants[aid]:
            if pid == current_pid:
                break
            if aid not in snapshot.programs[pid]['ranking']:
                continue
            xkey = key_of(aid, pid)
            program_used = confirmed_usage['program'][pid] + \
                bisect.bisect_left(prog_keys[pid], xkey)
            if program_used >= snapshot.capacities['program'][pid]:
                continue
            if any(confirmed_usage['region'][rid]
                   + bisect.bisect_left(region_keys[rid], xkey)
                   >= snapshot.capacities['region'][rid]
                   for rid in snapshot.program_chain[pid]):
                continue
            blocks.append({'applicant': aid, 'program': pid})
    return blocks
