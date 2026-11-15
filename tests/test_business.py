
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,itertools
from pathlib import Path
from fractions import Fraction
from admission_match.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['unknown']=1;self.bad(d)

    def test_matching(self):
        r=analyze(sample());self.assertEqual(r['assignments'],[{'applicant':'A','program':'Y'},{'applicant':'B','program':'X'},{'applicant':'C','program':None}]);self.assertEqual(r['blocking_pairs'],[])
    def test_zero_capacity(self):
        d=sample()
        for p in d['programs']:p['capacity']=0
        r=analyze(d);self.assertEqual(r['unmatched'],['A','B','C']);self.assertEqual(r['blocking_pairs'],[])
    def test_unacceptable(self):
        d=sample()
        for p in d['programs']:p['ranking']=[]
        self.assertEqual(analyze(d)['unmatched'],['A','B','C'])
    def test_unknown(self):
        d=sample();d['applicants'][0]['preferences']=['missing'];self.bad(d)
    def test_duplicate_ranking(self):
        d=sample();d['programs'][0]['ranking']=['A','A'];self.bad(d)
    def test_order(self):
        d=sample();d['applicants'].reverse();d['programs'].reverse();self.assertEqual(analyze(d),analyze(sample()))
    def test_random_independent_stability(self):
        rng=random.Random(5)
        for _ in range(50):
            names=list('ABCD');ps=list('XYZ');d={'applicants':[{'id':a,'preferences':rng.sample(ps,rng.randrange(4))} for a in names],'programs':[{'id':p,'capacity':rng.randrange(3),'ranking':rng.sample(names,rng.randrange(5))} for p in ps]};r=analyze(d);match={a['applicant']:a['program'] for a in r['assignments']}
            for p in d['programs']:
                held=[a for a in names if match[a]==p['id']];self.assertLessEqual(len(held),p['capacity'])
                for a in d['applicants']:
                    if p['id'] not in a['preferences'] or a['id'] not in p['ranking'] or p['capacity']==0:continue
                    prefers=match[a['id']] is None or a['preferences'].index(p['id'])<a['preferences'].index(match[a['id']])
                    accepts=len(held)<p['capacity'] or any(p['ranking'].index(a['id'])<p['ranking'].index(h) for h in held)
                    self.assertFalse(prefers and accepts)

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','admission_match.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_same_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
