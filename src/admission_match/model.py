from .common import require,ident,number,rows
from .matching import allocate

def analyze(data):
    require(data,'applicants programs');applicants={};programs={}
    for a in rows(data['applicants'],500):
        require(a,'id preferences');aid=ident(a['id']);prefs=[ident(p) for p in rows(a['preferences'],100)]
        if aid in applicants or len(set(prefs))!=len(prefs):raise ValueError('duplicate applicant or preference')
        applicants[aid]=prefs
    for p in rows(data['programs'],100):
        require(p,'id capacity ranking');pid=ident(p['id']);cap=number(p['capacity'],0,500);ranking=[ident(a) for a in rows(p['ranking'],500)]
        if pid in programs or len(set(ranking))!=len(ranking):raise ValueError('duplicate program or ranking')
        programs[pid]={'capacity':cap,'ranking':ranking}
    if not programs:raise ValueError('programs required')
    if any(p not in programs for prefs in applicants.values() for p in prefs) or any(a not in applicants for p in programs.values() for a in p['ranking']):raise ValueError('unknown reference')
    return allocate(applicants,programs)
