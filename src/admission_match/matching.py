from collections import deque

def allocate(applicants,programs):
    rank={p:{a:i for i,a in enumerate(v['ranking'])} for p,v in programs.items()};held={p:[] for p in programs};next_choice={a:0 for a in applicants};assigned={a:None for a in applicants};queue=deque(sorted(applicants));proposals=[]
    while queue:
        a=queue.popleft()
        if next_choice[a]>=len(applicants[a]):continue
        p=applicants[a][next_choice[a]];next_choice[a]+=1;rejected=a
        if a in rank[p] and programs[p]['capacity']:
            held[p].append(a);held[p].sort(key=rank[p].get)
            rejected=held[p].pop() if len(held[p])>programs[p]['capacity'] else None
            assigned[a]=p if a!=rejected else None
            if rejected is not None:assigned[rejected]=None
        proposals.append({'applicant':a,'program':p,'rejected':rejected})
        if rejected is not None:queue.append(rejected)
    blocks=[]
    for a,prefs in sorted(applicants.items()):
        current=assigned[a]
        for p in prefs:
            if p==current:break
            if a not in rank[p] or programs[p]['capacity']==0:continue
            if len(held[p])<programs[p]['capacity'] or rank[p][a]<max(rank[p][x] for x in held[p]):blocks.append({'applicant':a,'program':p})
    return {'assignments':[{'applicant':a,'program':p} for a,p in sorted(assigned.items())],'programs':[{'program':p,'accepted':held[p]} for p in sorted(programs)],'unmatched':sorted(a for a,p in assigned.items() if p is None),'blocking_pairs':blocks,'proposals':proposals}
