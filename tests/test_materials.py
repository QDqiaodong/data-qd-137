"""End-to-end tests for the independent admission materials library.

Covers the strict init roster/policy contract, manifest validation, streaming
content reception and dedup, slot versioning, replay, real two-process
revision contention, the object/directory crash boundary, deterministic
readiness reports with all status reasons and date edges, object corruption
handling, output path safety, the 128 MiB streaming RSS bound and CLI/module
entry points.  All fixtures are self-made inside temporary directories.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from admission_match.cli import main as root_main
from admission_match.materials.cli import main
from admission_match.materials.manifest import (
    canonical_manifest, parse_manifest, read_manifest,
)
from admission_match.materials.objects import object_path
from admission_match.materials.reporting import build_report, write_report
from admission_match.materials.roster import (
    MaterialsError, parse_policy, parse_roster,
)
from admission_match.materials.store import connect, ingest, initialize

ROOT = Path(__file__).resolve().parents[1]
ROSTER_FIXTURE = ROOT / 'fixtures' / 'materials_roster.json'
POLICY_FIXTURE = ROOT / 'fixtures' / 'materials_policy.json'
MAX_DAY = 3_652_058


def put(applicant, kind, path, content, version=1, issued=10, expires=100):
    if isinstance(content, str):
        content = content.encode('utf-8')
    return {
        'action': 'put', 'applicant': applicant, 'kind': kind,
        'version': version, 'issued_day': issued, 'expires_day': expires,
        'path': path, 'sha256': hashlib.sha256(content).hexdigest(),
        'byte_size': len(content),
    }


def withdraw(applicant, kind, version):
    return {'action': 'withdraw', 'applicant': applicant,
            'kind': kind, 'version': version}


def manifest(batch_id, expected, *documents):
    return {'batch_id': batch_id, 'expected_revision': expected,
            'documents': list(documents)}


def write_file(files_dir, relative, content):
    if isinstance(content, str):
        content = content.encode('utf-8')
    path = files_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def roster_data():
    return json.loads(ROSTER_FIXTURE.read_text())


def policy_data():
    return json.loads(POLICY_FIXTURE.read_text())


class MaterialsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.store = self.td / 'store'
        self.files = self.td / 'files'
        self.files.mkdir()
        initialize(self.store, ROSTER_FIXTURE, POLICY_FIXTURE)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, day):
        conn = connect(self.store)
        try:
            return build_report(conn, str(self.store), day)
        finally:
            conn.close()

    def revision(self):
        conn = connect(self.store)
        try:
            return int(conn.execute(
                "SELECT value FROM meta WHERE key='revision'").fetchone()[0])
        finally:
            conn.close()

    def slot(self, applicant, kind):
        conn = connect(self.store)
        try:
            return conn.execute(
                'SELECT version,state,object_sha,byte_size,issued_day,'
                'expires_day FROM slots WHERE applicant=? AND kind=?',
                (applicant, kind)).fetchone()
        finally:
            conn.close()

    def all_objects(self):
        objects = []
        for dirpath, _dirs, names in os.walk(self.store / 'objects'):
            for name in names:
                if not name.startswith('.incoming-'):
                    objects.append(Path(dirpath, name))
        return objects


class InitTests(MaterialsBase):
    def test_layout_shas_and_independence_from_inputs(self):
        self.assertTrue((self.store / 'ledger' / 'materials.sqlite').is_file())
        self.assertTrue((self.store / 'objects').is_dir())
        conn = connect(self.store)
        try:
            meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
            self.assertEqual(meta['schema_version'], '1')
            self.assertEqual(meta['revision'], '0')
            self.assertEqual(len(meta['roster_sha256']), 64)
            self.assertEqual(len(meta['policy_sha256']), 64)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM applicants').fetchone()[0],
                4)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM programs').fetchone()[0],
                2)
            # D has no preferences and must generate no preference rows.
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM preferences WHERE applicant='D'"
                ).fetchone()[0], 0)
        finally:
            conn.close()
        # The store keeps the input byte hashes, not the files: moving the
        # inputs away leaves the store fully usable.
        moved = self.td / 'moved'
        moved.mkdir()
        roster_copy = moved / 'roster.json'
        policy_copy = moved / 'policy.json'
        roster_copy.write_bytes(ROSTER_FIXTURE.read_bytes())
        policy_copy.write_bytes(POLICY_FIXTURE.read_bytes())
        store2 = self.td / 'store2'
        initialize(store2, roster_copy, policy_copy)
        os.unlink(roster_copy)
        os.unlink(policy_copy)
        write_file(self.files, 'x', b'abc')
        self.assertEqual(
            ingest(store2, manifest('b', 0, put('A', 'id_copy', 'x', b'abc')),
                   self.files)['accepted'], True)

    def test_refuses_any_existing_root(self):
        with self.assertRaises(MaterialsError):
            initialize(self.store, ROSTER_FIXTURE, POLICY_FIXTURE)
        existing_file = self.td / 'a-file'
        existing_file.write_text('x')
        with self.assertRaises(MaterialsError):
            initialize(existing_file, ROSTER_FIXTURE, POLICY_FIXTURE)
        link = self.td / 'link'
        os.symlink(self.store, link)
        with self.assertRaises(MaterialsError):
            initialize(link, ROSTER_FIXTURE, POLICY_FIXTURE)

    def test_failed_init_leaves_no_root(self):
        bad_roster = self.td / 'bad_roster.json'
        data = roster_data()
        data['applicants'][0]['preferences'].append('GHOST')
        bad_roster.write_text(json.dumps(data))
        target = self.td / 'never'
        with self.assertRaises(MaterialsError):
            initialize(target, bad_roster, POLICY_FIXTURE)
        self.assertFalse(target.exists())

    def test_concurrent_init_same_root_only_one_wins(self):
        # Two real processes race to create the same dedicated root.
        script = self.td / 'init_worker.py'
        script.write_text(
            'import sys\n'
            'from admission_match.materials.store import initialize\n'
            'store, roster, policy, out = sys.argv[1:5]\n'
            'try:\n'
            '    initialize(store, roster, policy)\n'
            '    open(out, "w").write("OK")\n'
            'except Exception as exc:\n'
            '    open(out, "w").write("ERR " + str(exc))\n')
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
        target = self.td / 'race-store'
        procs = [
            subprocess.Popen([sys.executable, str(script), str(target),
                              str(ROSTER_FIXTURE), str(POLICY_FIXTURE),
                              str(self.td / ('io%d' % i))], env=env)
            for i in range(2)]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0, 0])
        outcomes = sorted((self.td / ('io%d' % i)).read_text()
                          for i in range(2))
        self.assertEqual(outcomes[0].startswith('ERR'), True, outcomes)
        self.assertEqual(outcomes[1], 'OK')
        # Exactly one intact store exists and is usable.
        self.assertTrue((target / 'ledger' / 'materials.sqlite').is_file())
        conn = connect(target)
        try:
            self.assertEqual(conn.execute(
                "SELECT value FROM meta WHERE key='revision'").fetchone()[0],
                '0')
        finally:
            conn.close()

    def test_roster_contract_boundaries(self):
        base = roster_data()
        # Duplicate applicant.
        bad = json.loads(json.dumps(base))
        bad['applicants'].append(dict(bad['applicants'][0]))
        with self.assertRaises(MaterialsError):
            parse_roster(bad)
        # Duplicate preference.
        bad = json.loads(json.dumps(base))
        bad['applicants'][0]['preferences'].append(bad['applicants'][0]
                                                   ['preferences'][0])
        with self.assertRaises(MaterialsError):
            parse_roster(bad)
        # Unknown ranking reference.
        bad = json.loads(json.dumps(base))
        bad['programs'][0]['ranking'].append('NOBODY')
        with self.assertRaises(MaterialsError):
            parse_roster(bad)
        # Extra top-level field is rejected by the strict contract.
        bad = json.loads(json.dumps(base))
        bad['extra'] = 1
        with self.assertRaises(ValueError):
            parse_roster(bad)
        # Capacity boundary is still enforced.
        bad = json.loads(json.dumps(base))
        bad['programs'][0]['capacity'] = 501
        with self.assertRaises(ValueError):
            parse_roster(bad)

    def test_policy_contract(self):
        roster = parse_roster(roster_data())
        requirements, program_kinds = parse_policy(policy_data(), roster)
        self.assertEqual([e['program'] for e in requirements], ['X', 'Y'])
        self.assertEqual(program_kinds['X'], ['id_copy', 'transcript'])

        def policy(entries):
            return {'requirements': entries}

        # A declared program missing from requirements fails.
        with self.assertRaises(MaterialsError):
            parse_policy(policy([
                {'program': 'X', 'kinds': []}]), roster)
        # Duplicate program entry fails.
        with self.assertRaises(MaterialsError):
            parse_policy(policy([
                {'program': 'X', 'kinds': []},
                {'program': 'X', 'kinds': []},
                {'program': 'Y', 'kinds': []}]), roster)
        # Unknown program fails.
        with self.assertRaises(MaterialsError):
            parse_policy(policy([
                {'program': 'X', 'kinds': []},
                {'program': 'Y', 'kinds': []},
                {'program': 'Z', 'kinds': []}]), roster)
        # Duplicate kind fails.
        with self.assertRaises(MaterialsError):
            parse_policy(policy([
                {'program': 'X', 'kinds': ['k', 'k']},
                {'program': 'Y', 'kinds': []}]), roster)
        # More than 20 kinds fails.
        with self.assertRaises(MaterialsError):
            parse_policy(policy([
                {'program': 'X', 'kinds': ['k%02d' % i for i in range(21)]},
                {'program': 'Y', 'kinds': []}]), roster)
        # Extra field on an entry and on the policy document fail.
        with self.assertRaises(ValueError):
            parse_policy({'requirements': [
                {'program': 'X', 'kinds': [], 'note': 'x'},
                {'program': 'Y', 'kinds': []}]}, roster)
        with self.assertRaises(ValueError):
            parse_policy({'requirements': [], 'other': 1}, roster)

    def test_empty_kinds_is_legal(self):
        roster = parse_roster(roster_data())
        _req, kinds = parse_policy(
            {'requirements': [
                {'program': 'X', 'kinds': []},
                {'program': 'Y', 'kinds': []}]}, roster)
        self.assertEqual(kinds, {'X': [], 'Y': []})


class ManifestValidationTests(unittest.TestCase):
    def _put_doc(self, **overrides):
        doc = put('A', 'id_copy', 'a.bin', b'data')
        doc.update(overrides)
        return doc

    def test_strict_fields(self):
        good = manifest('b', 0, self._put_doc(),
                        withdraw('A', 'transcript', 1))
        parsed = parse_manifest(good)
        self.assertEqual(len(parsed['documents']), 2)
        bad = json.loads(json.dumps(good))
        bad['documents'][0]['unexpected'] = 1
        with self.assertRaises(MaterialsError):
            parse_manifest(bad)
        bad = json.loads(json.dumps(good))
        del bad['documents'][0]['path']
        with self.assertRaises(MaterialsError):
            parse_manifest(bad)
        bad = json.loads(json.dumps(good))
        bad['documents'][1]['path'] = 'x'
        with self.assertRaises(MaterialsError):
            parse_manifest(bad)
        with self.assertRaises(MaterialsError):
            parse_manifest({'batch_id': 'b', 'expected_revision': 0,
                           'documents': [], 'extra': 1})

    def test_bools_are_not_integers_and_bounds(self):
        for bad in [
            manifest('b', True, self._put_doc()),
            manifest('b', -1, self._put_doc()),
            manifest('b', 0, self._put_doc(version=True)),
            manifest('b', 0, self._put_doc(version=0)),
            manifest('b', 0, self._put_doc(issued_day=1.0)),
            manifest('b', 0, self._put_doc(expires_day=MAX_DAY + 1)),
            manifest('b', 0, self._put_doc(issued_day=5, expires_day=5)),
            manifest('b', 0, self._put_doc(byte_size=True)),
            manifest('b', 0, self._put_doc(byte_size=-1)),
            manifest('b', 0, self._put_doc(byte_size=67_108_865)),
        ]:
            with self.assertRaises(MaterialsError):
                parse_manifest(bad)
        # Day extremes are legal.
        parse_manifest(manifest(
            'b', 0, self._put_doc(issued_day=0, expires_day=MAX_DAY)))

    def test_document_count_and_duplicate_slots(self):
        with self.assertRaises(MaterialsError):
            parse_manifest({'batch_id': 'b', 'expected_revision': 0,
                            'documents': []})
        docs = [put('A', 'id_copy', 'f%d' % i, b'x') for i in range(1001)]
        with self.assertRaises(MaterialsError):
            parse_manifest(manifest('b', 0, *docs))
        with self.assertRaises(MaterialsError):
            parse_manifest(manifest(
                'b', 0, put('A', 'id_copy', 'f', b'x'),
                withdraw('A', 'id_copy', 2)))

    def test_unsafe_paths(self):
        for bad_path in ['/etc/passwd', 'a\\b', 'a//b', 'a/./b', 'a/../b',
                         '..', '.', '']:
            with self.assertRaises(MaterialsError):
                parse_manifest(manifest(
                    'b', 0, self._put_doc(path=bad_path)))

    def test_sha256_shape(self):
        good = hashlib.sha256(b'x').hexdigest()
        for bad in [good.upper(), good[:-1], 'z' + good[1:], 123, None]:
            with self.assertRaises(MaterialsError):
                parse_manifest(manifest(
                    'b', 0, self._put_doc(sha256=bad)))

    def test_cumulative_byte_limit(self):
        big = 300 * 1024 * 1024
        doc = self._put_doc(byte_size=big,
                            sha256='a' * 64)
        with self.assertRaises(MaterialsError):
            parse_manifest(manifest('b', 0, doc, dict(doc, path='f2')))

    def test_reader_rejects_duplicate_keys_oversize_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            good = td / 'good.json'
            good.write_text(json.dumps(
                manifest('b', 0, self._put_doc())))
            data, digest = read_manifest(good)
            self.assertEqual(data['batch_id'], 'b')
            self.assertEqual(len(digest), 64)
            dup = td / 'dup.json'
            dup.write_text('{"batch_id":"b","batch_id":"c",'
                           '"expected_revision":0,"documents":[]}')
            with self.assertRaises(MaterialsError):
                read_manifest(dup)
            nonfinite = td / 'nan.json'
            nonfinite.write_text('{"batch_id":"b","expected_revision":NaN,'
                                 '"documents":[]}')
            with self.assertRaises(MaterialsError):
                read_manifest(nonfinite)
            oversized = td / 'big.json'
            oversized.write_text('{"batch_id":"%s"}' % ('x' * (17 << 20)))
            with self.assertRaises(MaterialsError):
                read_manifest(oversized)

    def test_canonical_fingerprint_ignores_order_only(self):
        d1 = put('A', 'id_copy', 'a', b'1')
        d2 = put('B', 'id_copy', 'b', b'2')
        m1 = parse_manifest(manifest('b', 0, d1, d2))
        m2 = parse_manifest(manifest('b', 0, d2, d1))
        self.assertEqual(canonical_manifest(m1), canonical_manifest(m2))
        # Shuffled JSON keys still fingerprint identically.
        shuffled = {'documents': [d2, d1], 'expected_revision': 0,
                    'batch_id': 'b'}
        self.assertEqual(canonical_manifest(m1),
                         canonical_manifest(parse_manifest(shuffled)))
        # Every value participates: changed expectation, sha, path or day.
        for changed in [
            manifest('b', 1, d1, d2),
            manifest('b', 0, dict(d1, sha256='b' * 64), d2),
            manifest('b', 0, dict(d1, path='other'), d2),
            manifest('b', 0, dict(d1, issued_day=11), d2),
            manifest('b', 0, dict(d1, version=2), d2),
        ]:
            self.assertNotEqual(
                canonical_manifest(m1),
                canonical_manifest(parse_manifest(changed)))


class ObjectReceptionTests(MaterialsBase):
    def test_dedup_two_paths_one_object(self):
        write_file(self.files, 'a.bin', b'shared-content')
        write_file(self.files / 'nested', 'copy.bin', b'shared-content')
        sha = hashlib.sha256(b'shared-content').hexdigest()
        ingest(self.store, manifest(
            'b1', 0, put('A', 'id_copy', 'a.bin', b'shared-content')),
            self.files)
        ingest(self.store, manifest(
            'b2', 1, put('B', 'id_copy', 'nested/copy.bin',
                         b'shared-content')), self.files)
        self.assertEqual(len(self.all_objects()), 1)
        self.assertEqual(self.all_objects()[0],
                         Path(object_path(str(self.store), sha)))
        # Both slots reference the same immutable object.
        self.assertEqual(self.slot('A', 'id_copy')[2], sha)
        self.assertEqual(self.slot('B', 'id_copy')[2], sha)

    def test_corruption_and_misdeclared_size_fail_without_slot_change(self):
        content = b'original-bytes'
        write_file(self.files, 'good.bin', content)
        before = self.revision()
        # Trailing-byte corruption: declared sha belongs to the original.
        write_file(self.files, 'bad-tail.bin', content + b'X')
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'bad1', 0, put('A', 'id_copy', 'bad-tail.bin', content)),
                self.files)
        # Declared length larger than the actual file.
        write_file(self.files, 'short.bin', content[:-1])
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'bad2', 0, put('A', 'id_copy', 'short.bin', content)),
                self.files)
        # Declared length smaller than the actual file.
        write_file(self.files, 'long.bin', content + b'extra')
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'bad3', 0,
                dict(put('A', 'id_copy', 'long.bin', content),
                     byte_size=len(content) - 1)), self.files)
        self.assertEqual(self.revision(), before)
        self.assertIsNone(self.slot('A', 'id_copy'))
        # No leftover incoming temporary files; failed batches create no
        # permanent object.
        self.assertEqual(self.all_objects(), [])

    def test_path_traversal_and_symlinks_rejected(self):
        write_file(self.files, 'ok.bin', b'ok')
        outside = self.td / 'outside.bin'
        outside.write_bytes(b'secret')
        link = self.files / 'link.bin'
        os.symlink(outside, link)
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'sym', 0, put('A', 'id_copy', 'link.bin', b'secret')),
                self.files)
        # A symlink at an intermediate directory is rejected as well.
        linked_dir = self.files / 'linked-dir'
        os.symlink(self.td, linked_dir)
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'symdir', 0,
                put('A', 'id_copy', 'linked-dir/outside.bin', b'secret')),
                self.files)
        # A directory path is not a regular file.
        write_file(self.files / 'adir', 'placeholder', b'x')
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'dir', 0, put('A', 'id_copy', 'adir', b'')), self.files)
        # Traversal never reaches the parent directory.
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'trav', 0,
                put('A', 'id_copy', '../outside.bin', b'secret')),
                self.files)
        self.assertEqual(self.revision(), 0)
        self.assertEqual(self.all_objects(), [])

    def test_zero_byte_object(self):
        write_file(self.files, 'empty.bin', b'')
        result = ingest(self.store, manifest(
            'zero', 0, put('A', 'id_copy', 'empty.bin', b'')), self.files)
        self.assertEqual(result['put'], 1)
        self.assertEqual(len(self.all_objects()), 1)

    def test_existing_corrupt_object_is_reverified_not_overwritten(self):
        content = b'persisted-content'
        write_file(self.files, 'a.bin', content)
        digest = hashlib.sha256(content).hexdigest()
        ingest(self.store, manifest(
            'b1', 0, put('A', 'id_copy', 'a.bin', content)), self.files)
        # Corrupt the committed object on disk.
        obj = Path(object_path(str(self.store), digest))
        obj.write_bytes(b'')
        # A new batch reusing that content must fail on re-verification and
        # must never overwrite the object.
        write_file(self.files, 'again.bin', content)
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'b2', 1, put('B', 'id_copy', 'again.bin', content)),
                self.files)
        self.assertEqual(obj.read_bytes(), b'')
        self.assertEqual(self.revision(), 1)
        # The report must surface the corruption instead of calling it ready.
        with self.assertRaises(MaterialsError):
            self.report(50)


class SlotVersioningTests(MaterialsBase):
    def _seed(self):
        write_file(self.files, 'v1.bin', b'v1')
        ingest(self.store, manifest(
            'b1', 0, put('A', 'id_copy', 'v1.bin', b'v1')), self.files)

    def test_new_slot_must_start_at_version_one(self):
        write_file(self.files, 'v.bin', b'v')
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'bad', 0, put('A', 'id_copy', 'v.bin', b'v', version=2)),
                self.files)
        self.assertEqual(self.revision(), 0)

    def test_put_replaces_put_and_withdraw_with_continuous_versions(self):
        self._seed()
        write_file(self.files, 'v2.bin', b'v2')
        ingest(self.store, manifest(
            'b2', 1, put('A', 'id_copy', 'v2.bin', b'v2', version=2,
                         issued=1, expires=10)), self.files)
        self.assertEqual(self.slot('A', 'id_copy')[:2], (2, 'put'))
        # A stale version number is rejected.
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'stale', 2, put('A', 'id_copy', 'v2.bin', b'v2',
                                version=2)), self.files)
        # Withdraw only accepts the current put, at exactly current+1.
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'wbad', 2, withdraw('A', 'id_copy', 4)), self.files)
        ingest(self.store, manifest(
            'w', 2, withdraw('A', 'id_copy', 3)), self.files)
        self.assertEqual(self.slot('A', 'id_copy')[:2], (3, 'withdrawn'))
        # A second withdraw is illegal.
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'w2', 3, withdraw('A', 'id_copy', 4)), self.files)
        # A put can take over a withdrawn slot; the version chain continues.
        write_file(self.files, 'v4.bin', b'v4')
        ingest(self.store, manifest(
            'b4', 3, put('A', 'id_copy', 'v4.bin', b'v4', version=4,
                         issued=1, expires=10)), self.files)
        self.assertEqual(self.slot('A', 'id_copy')[:2], (4, 'put'))
        self.assertEqual(self.revision(), 4)

    def test_withdraw_requires_existing_put_slot(self):
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'w', 0, withdraw('A', 'id_copy', 1)), self.files)

    def test_unknown_applicant_and_kind_rejected(self):
        write_file(self.files, 'x.bin', b'x')
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'a', 0, put('NOBODY', 'id_copy', 'x.bin', b'x')),
                self.files)
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'k', 0, put('A', 'not_required', 'x.bin', b'x')),
                self.files)
        self.assertEqual(self.revision(), 0)

    def test_batch_record_is_immutable_and_revision_advances_once(self):
        self._seed()
        conn = connect(self.store)
        try:
            row = conn.execute(
                'SELECT batch_id,revision FROM batches').fetchall()
            self.assertEqual(row, [('b1', 1)])
        finally:
            conn.close()
        # A failed batch leaves no batch record and no revision change.
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'bad', 1, withdraw('A', 'id_copy', 9)), self.files)
        conn = connect(self.store)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM batches')
                             .fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual(self.revision(), 1)


class ReplayTests(MaterialsBase):
    def test_replay_returns_first_result_even_after_advance_and_deleted_files(self):
        write_file(self.files, 'a.bin', b'data-a')
        body = manifest('b1', 0, put('A', 'id_copy', 'a.bin', b'data-a'))
        first = ingest(self.store, body, self.files)
        # Another batch advances the revision.
        write_file(self.files, 'b.bin', b'data-b')
        ingest(self.store, manifest(
            'b2', 1, put('B', 'id_copy', 'b.bin', b'data-b')), self.files)
        # Delete every source file: the replay must not need them.
        for entry in self.files.iterdir():
            if entry.is_file():
                entry.unlink()
        again = ingest(self.store, body, self.files)
        self.assertEqual(again, first)
        self.assertEqual(self.revision(), 2)
        # Shuffled keys and document order still identify the same batch.
        shuffled = {
            'documents': [dict(body['documents'][0])],
            'expected_revision': 0, 'batch_id': 'b1',
        }
        self.assertEqual(ingest(self.store, shuffled, self.files), first)

    def test_same_id_different_content_conflicts(self):
        write_file(self.files, 'a.bin', b'data-a')
        ingest(self.store, manifest(
            'b1', 0, put('A', 'id_copy', 'a.bin', b'data-a')), self.files)
        write_file(self.files, 'c.bin', b'data-c')
        with self.assertRaises(MaterialsError):
            ingest(self.store, manifest(
                'b1', 1, put('A', 'id_copy', 'c.bin', b'data-c',
                             version=2)), self.files)
        self.assertEqual(self.revision(), 1)

    def test_replay_does_not_require_the_files_directory(self):
        write_file(self.files, 'a.bin', b'data-a')
        body = manifest('b1', 0, put('A', 'id_copy', 'a.bin', b'data-a'))
        first = ingest(self.store, body, self.files)
        # The files directory is removed outright; the replay still resolves
        # from the immutable batch record and returns the first result.
        import shutil
        shutil.rmtree(self.files)
        self.assertEqual(ingest(self.store, body, self.files), first)
        self.assertEqual(self.revision(), 1)


class ReportTests(MaterialsBase):
    def test_pairs_follow_preferences_sorted_and_ignore_capacity(self):
        report = self.report(50)
        pairs = [(p['applicant'], p['program']) for p in report['pairs']]
        # A lists X then Y; B lists X; C lists Y; D has no preferences and
        # therefore no rows at all.
        self.assertEqual(pairs, [('A', 'X'), ('A', 'Y'), ('B', 'X'),
                                 ('C', 'Y')])
        self.assertEqual(report['revision'], 0)
        self.assertEqual(len(report['roster_sha256']), 64)
        self.assertEqual(len(report['policy_sha256']), 64)

    def test_same_applicant_two_programs_require_different_kinds(self):
        report = self.report(50)
        x_row = next(p for p in report['pairs']
                     if p['applicant'] == 'A' and p['program'] == 'X')
        y_row = next(p for p in report['pairs']
                     if p['applicant'] == 'A' and p['program'] == 'Y')
        self.assertEqual(x_row['required_kinds'],
                         ['id_copy', 'transcript'])
        self.assertEqual(y_row['required_kinds'],
                         ['id_copy', 'statement'])

    def test_empty_requirements_ready_immediately(self):
        empty_store = self.td / 'empty-req'
        (self.td / 'policy-empty.json').write_text(json.dumps(
            {'requirements': [
                {'program': 'X', 'kinds': []},
                {'program': 'Y', 'kinds': []}]}))
        initialize(empty_store, ROSTER_FIXTURE,
                   self.td / 'policy-empty.json')
        conn = connect(empty_store)
        try:
            report = build_report(conn, str(empty_store), 1)
        finally:
            conn.close()
        self.assertTrue(all(p['ready'] for p in report['pairs']))
        self.assertTrue(all(p['required_kinds'] == []
                            for p in report['pairs']))
        self.assertTrue(all(p['missing_kinds'] == []
                            for p in report['pairs']))

    def test_date_boundaries_and_status_reasons(self):
        # Valid on the half-open interval [issued, expires).
        write_file(self.files, 'a.bin', b'id-a')
        ingest(self.store, manifest(
            'b', 0, put('A', 'id_copy', 'a.bin', b'id-a',
                        issued=10, expires=20)), self.files)
        def status_of(day):
            row = next(p for p in self.report(day)['pairs']
                       if p['applicant'] == 'A' and p['program'] == 'X')
            entry = next(k for k in row['kinds'] if k['kind'] == 'id_copy')
            return entry['status']
        self.assertEqual(status_of(9), 'not_yet_valid')
        self.assertEqual(status_of(10), 'valid')
        self.assertEqual(status_of(19), 'valid')
        self.assertEqual(status_of(20), 'expired')
        # Missing and withdrawn carry their own explicit reasons.
        row = next(p for p in self.report(10)['pairs']
                   if p['applicant'] == 'A' and p['program'] == 'X')
        entry = next(k for k in row['kinds'] if k['kind'] == 'transcript')
        self.assertEqual(entry['status'], 'missing')
        self.assertIsNone(entry['version'])
        self.assertIsNone(entry['object_sha256'])
        ingest(self.store, manifest(
            'w', 1, withdraw('A', 'id_copy', 2)), self.files)
        row = next(p for p in self.report(15)['pairs']
                   if p['applicant'] == 'A' and p['program'] == 'X')
        entry = next(k for k in row['kinds'] if k['kind'] == 'id_copy')
        self.assertEqual(entry['status'], 'withdrawn')
        self.assertEqual(entry['version'], 2)
        self.assertIsNone(entry['object_sha256'])
        self.assertIn('id_copy', row['missing_kinds'])
        self.assertFalse(row['ready'])

    def test_expired_replacement_never_falls_back_to_old_version(self):
        write_file(self.files, 'v1.bin', b'v1')
        ingest(self.store, manifest(
            'b1', 0, put('A', 'id_copy', 'v1.bin', b'v1',
                         issued=0, expires=100)), self.files)
        self.assertTrue(next(
            p for p in self.report(50)['pairs']
            if p['applicant'] == 'A' and p['program'] == 'X')
            ['kinds'][0]['status'] == 'valid')
        write_file(self.files, 'v2.bin', b'v2')
        ingest(self.store, manifest(
            'b2', 1, put('A', 'id_copy', 'v2.bin', b'v2', version=2,
                         issued=0, expires=10)), self.files)
        row = next(p for p in self.report(50)['pairs']
                   if p['applicant'] == 'A' and p['program'] == 'X')
        entry = row['kinds'][0]
        self.assertEqual(entry['status'], 'expired')
        self.assertEqual(entry['version'], 2)
        self.assertFalse(row['ready'])

    def test_deterministic_bytes(self):
        write_file(self.files, 'a.bin', b'id-a')
        ingest(self.store, manifest(
            'b', 0, put('A', 'id_copy', 'a.bin', b'id-a')), self.files)
        out1 = self.td / 'r1.json'
        out2 = self.td / 'r2.json'
        write_report(self.store, 50, out1)
        write_report(self.store, 50, out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())

    def test_corrupt_referenced_object_fails_and_keeps_old_report(self):
        write_file(self.files, 'a.bin', b'id-a')
        digest = hashlib.sha256(b'id-a').hexdigest()
        ingest(self.store, manifest(
            'b', 0, put('A', 'id_copy', 'a.bin', b'id-a')), self.files)
        out = self.td / 'report.json'
        write_report(self.store, 50, out)
        first = out.read_bytes()
        Path(object_path(str(self.store), digest)).write_bytes(b'broken')
        with self.assertRaises(MaterialsError):
            write_report(self.store, 50, out)
        self.assertEqual(out.read_bytes(), first)


class LinkedAcceptanceTests(MaterialsBase):
    def test_shared_object_withdraw_a_keeps_b_ready(self):
        # A and B submit identical content but with different validity days.
        write_file(self.files, 'a.bin', b'shared')
        write_file(self.files, 'b.bin', b'shared')
        ingest(self.store, manifest(
            'a1', 0, put('A', 'id_copy', 'a.bin', b'shared',
                         issued=0, expires=100)), self.files)
        ingest(self.store, manifest(
            'b1', 1, put('B', 'id_copy', 'b.bin', b'shared',
                         issued=0, expires=400)), self.files)
        ingest(self.store, manifest(
            'aw', 2, withdraw('A', 'id_copy', 2)), self.files)
        # B keeps its own validity window and stays ready; A is withdrawn.
        report = self.report(150)
        b_row = next(p for p in report['pairs']
                     if p['applicant'] == 'B' and p['program'] == 'X')
        b_id = next(k for k in b_row['kinds'] if k['kind'] == 'id_copy')
        self.assertEqual(b_id['status'], 'valid')
        a_row = next(p for p in report['pairs']
                     if p['applicant'] == 'A' and p['program'] == 'X')
        a_id = next(k for k in a_row['kinds'] if k['kind'] == 'id_copy')
        self.assertEqual(a_id['status'], 'withdrawn')
        # The shared object is physically untouched by A's logical withdraw.
        self.assertEqual(len(self.all_objects()), 1)

    def _ingest_subprocess(self, manifest_path, fault=None):
        env = dict(os.environ)
        if fault:
            env['ADMISSION_MATCH_MATERIALS_FAULT'] = fault
        worker = self.td / 'worker.py'
        worker.write_text(
            'import sys\n'
            'from admission_match.materials.store import ingest\n'
            'store, manifest_path, files = sys.argv[1:4]\n'
            'import json\n'
            'ingest(store, json.load(open(manifest_path)), files)\n')
        return subprocess.run(
            [sys.executable, str(worker), str(self.store),
             str(manifest_path), str(self.files)],
            capture_output=True, text=True,
            env={**env, 'PYTHONPATH': str(ROOT / 'src')})

    def test_crash_between_object_durable_and_directory_commit(self):
        # A currently holds a valid version-1 id_copy.
        write_file(self.files, 'v1.bin', b'v1')
        ingest(self.store, manifest(
            'a1', 0, put('A', 'id_copy', 'v1.bin', b'v1',
                         issued=0, expires=100)), self.files)
        write_file(self.files, 'v2.bin', b'v2-new')
        body = manifest('a2', 1, put('A', 'id_copy', 'v2.bin', b'v2-new',
                                     version=2, issued=0, expires=100))
        manifest_path = self.td / 'a2.json'
        manifest_path.write_text(json.dumps(body))
        proc = self._ingest_subprocess(
            manifest_path, fault='objects_durable_before_commit')
        self.assertEqual(proc.returncode, 91, proc.stderr)
        # The object is fully on disk but unreferenced; the directory stayed
        # at the complete old revision, so A must not be early-ready on v2.
        self.assertEqual(self.revision(), 1)
        self.assertEqual(self.slot('A', 'id_copy')[0], 1)
        new_digest = hashlib.sha256(b'v2-new').hexdigest()
        self.assertTrue(
            Path(object_path(str(self.store), new_digest)).is_file())
        # Retrying the original batch reuses the object and advances the
        # revision exactly once.
        proc = self._ingest_subprocess(manifest_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.revision(), 2)
        self.assertEqual(self.slot('A', 'id_copy')[0], 2)
        proc = self._ingest_subprocess(manifest_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.revision(), 2)  # replay, no second advance

    def test_two_processes_contend_same_revision(self):
        write_file(self.files, 'a.bin', b'data-a')
        write_file(self.files, 'b.bin', b'data-b')
        worker = self.td / 'contender.py'
        worker.write_text(
            'import json,sys\n'
            'from admission_match.materials.store import ingest\n'
            'store, manifest_path, files, out = sys.argv[1:5]\n'
            'try:\n'
            '    r = ingest(store, json.load(open(manifest_path)), files)\n'
            '    open(out, "w").write("OK " + str(r["revision"]))\n'
            'except Exception as exc:\n'
            '    open(out, "w").write("ERR " + str(exc))\n')
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
        m0 = self.td / 'm0.json'
        m1 = self.td / 'm1.json'
        m0.write_text(json.dumps(manifest(
            'w0', 0, put('A', 'id_copy', 'a.bin', b'data-a'))))
        m1.write_text(json.dumps(manifest(
            'w1', 0, put('B', 'id_copy', 'b.bin', b'data-b'))))
        procs = [
            subprocess.Popen([sys.executable, str(worker), str(self.store),
                              str(m), str(self.files),
                              str(self.td / ('out%d' % i))], env=env)
            for i, m in enumerate((m0, m1))]
        codes = [p.wait(timeout=60) for p in procs]
        self.assertEqual(codes, [0, 0])
        outcomes = sorted((self.td / ('out%d' % i)).read_text()
                          for i in range(2))
        self.assertTrue(outcomes[0].startswith('ERR'), outcomes)
        self.assertEqual(outcomes[1], 'OK 1')
        self.assertEqual(self.revision(), 1)
        # Exactly one batch record and one winner's slots exist.
        conn = connect(self.store)
        try:
            batches = conn.execute('SELECT batch_id FROM batches').fetchall()
            self.assertEqual(len(batches), 1)
            slot_count = conn.execute('SELECT COUNT(*) FROM slots') \
                .fetchone()[0]
            self.assertEqual(slot_count, 1)
        finally:
            conn.close()

    def test_large_multifile_ingest_under_rss_limit(self):
        # 3 x 48 MiB = 144 MiB of distinct content, streamed in 1 MiB windows.
        size = 48 * 1024 * 1024
        chunk = (b'0123456789abcdef' * 65536)  # exactly 1 MiB
        docs = []
        for index, (applicant, kind) in enumerate(
                [('A', 'id_copy'), ('A', 'transcript'),
                 ('B', 'id_copy')]):
            relative = 'big%d.bin' % index
            path = self.files / relative
            with open(path, 'wb') as stream:
                for block in range(size // len(chunk)):
                    stream.write(chunk)
            # Distinct content per slot while keeping the streaming pattern.
            with open(path, 'r+b') as stream:
                stream.seek(0)
                stream.write(('tag%d' % index).encode())
            h = hashlib.sha256()
            with open(path, 'rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            docs.append({
                'action': 'put', 'applicant': applicant, 'kind': kind,
                'version': 1, 'issued_day': 0, 'expires_day': 100,
                'path': relative, 'sha256': h.hexdigest(),
                'byte_size': size,
            })
        body = manifest('big', 0, *docs)
        manifest_path = self.td / 'big.json'
        manifest_path.write_text(json.dumps(body))
        worker = self.td / 'big_worker.py'
        worker.write_text(
            'import json, resource, sys\n'
            'from admission_match.materials.store import ingest\n'
            'store, manifest_path, files, out = sys.argv[1:5]\n'
            'r = ingest(store, json.load(open(manifest_path)), files)\n'
            'rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n'
            'open(out, "w").write(\n'
            '    json.dumps({"revision": r["revision"], "rss_kb": rss_kb}))\n')
        proc = subprocess.run(
            [sys.executable, str(worker), str(self.store),
             str(manifest_path), str(self.files),
             str(self.td / 'big-out.json')],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outcome = json.loads((self.td / 'big-out.json').read_text())
        self.assertEqual(outcome['revision'], 1)
        total = sum(p.stat().st_size for p in self.all_objects())
        self.assertGreaterEqual(total, 128 * 1024 * 1024)
        # Peak RSS must stay below 64 MiB while streaming 64 MiB objects.
        self.assertLess(outcome['rss_kb'], 64 * 1024,
                        'peak RSS %d KiB exceeded 64 MiB'
                        % outcome['rss_kb'])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.store = self.td / 'store'
        self.files = self.td / 'files'
        self.files.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest_file(self, name, content):
        write_file(self.files, name, content)
        return self.td / (name + '.manifest.json')

    def test_full_cli_lifecycle_and_module_entry(self):
        # Via the package entry point.
        self.assertEqual(main(['init', '--store', str(self.store),
                               '--source', str(ROSTER_FIXTURE),
                               '--policy', str(POLICY_FIXTURE)]), 0)
        write_file(self.files, 'a.bin', b'id-a')
        mpath = self.td / 'm.json'
        mpath.write_text(json.dumps(manifest(
            'b', 0, put('A', 'id_copy', 'a.bin', b'id-a'))))
        self.assertEqual(main(['ingest', '--store', str(self.store),
                               '--manifest', str(mpath),
                               '--files', str(self.files)]), 0)
        out = self.td / 'report.json'
        self.assertEqual(main(['report', '--store', str(self.store),
                               '--day', '50', '--output', str(out)]), 0)
        report = json.loads(out.read_text())
        self.assertEqual(report['day'], 50)
        # Re-init is a state error and exits 2.
        self.assertEqual(main(['init', '--store', str(self.store),
                               '--source', str(ROSTER_FIXTURE),
                               '--policy', str(POLICY_FIXTURE)]), 2)
        # Malformed manifest exits 2 and leaves the directory untouched.
        bad = self.td / 'bad.json'
        bad.write_text('{}')
        self.assertEqual(main(['ingest', '--store', str(self.store),
                               '--manifest', str(bad),
                               '--files', str(self.files)]), 2)
        # Equivalent module entry: python -m admission_match.materials.cli.
        out2 = self.td / 'report2.json'
        proc = subprocess.run(
            [sys.executable, '-m', 'admission_match.materials.cli', 'report',
             '--store', str(self.store), '--day', '50',
             '--output', str(out2)],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out.read_bytes(), out2.read_bytes())

    def test_report_output_safety(self):
        main(['init', '--store', str(self.store),
              '--source', str(ROSTER_FIXTURE),
              '--policy', str(POLICY_FIXTURE)])
        # Inside the store is rejected.
        inside = self.store / 'report.json'
        self.assertEqual(main(['report', '--store', str(self.store),
                               '--day', '1', '--output', str(inside)]), 2)
        self.assertFalse(inside.exists())
        # A symlink output is rejected.
        target = self.td / 'target.json'
        target.write_text('old')
        link = self.td / 'link.json'
        os.symlink(target, link)
        self.assertEqual(main(['report', '--store', str(self.store),
                               '--day', '1', '--output', str(link)]), 2)
        self.assertEqual(target.read_text(), 'old')

    def test_not_ready_report_is_still_success(self):
        main(['init', '--store', str(self.store),
              '--source', str(ROSTER_FIXTURE),
              '--policy', str(POLICY_FIXTURE)])
        out = self.td / 'report.json'
        self.assertEqual(main(['report', '--store', str(self.store),
                               '--day', '1', '--output', str(out)]), 0)
        self.assertFalse(any(p['ready']
                             for p in json.loads(out.read_text())['pairs']))

    def test_materials_dispatch_through_root_cli_and_legacy_untouched(self):
        self.assertEqual(root_main(['materials', 'init',
                                    '--store', str(self.store),
                                    '--source', str(ROSTER_FIXTURE),
                                    '--policy', str(POLICY_FIXTURE)]), 0)
        # The legacy single-shot command still works exactly as before.
        legacy_out = self.td / 'legacy.json'
        self.assertEqual(root_main([
            '--source', str(ROOT / 'fixtures' / 'input.json'),
            '--output', str(legacy_out)]), 0)
        legacy = json.loads(legacy_out.read_text())
        self.assertEqual(
            legacy['result']['assignments'],
            [{'applicant': 'A', 'program': 'Y'},
             {'applicant': 'B', 'program': 'X'},
             {'applicant': 'C', 'program': None}])


class IndependenceTests(unittest.TestCase):
    def test_materials_never_import_matching_or_workbench(self):
        # A fresh interpreter importing only the materials CLI must not pull
        # in the matching, workbench or audit modules.
        proc = subprocess.run(
            [sys.executable, '-I', '-c',
             'import sys, json\n'
             'sys.path.insert(0, %r)\n'
             'import admission_match.materials.cli\n'
             'print(json.dumps(sorted(m for m in sys.modules '
             'if m.startswith("admission_match"))))'
             % str(ROOT / 'src')],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        loaded = set(json.loads(proc.stdout))
        self.assertIn('admission_match.materials.cli', loaded)
        for forbidden in ('admission_match.model', 'admission_match.matching',
                          'admission_match.store', 'admission_match.regional',
                          'admission_match.audit', 'admission_match.source'):
            self.assertNotIn(forbidden, loaded)


if __name__ == '__main__':
    unittest.main()
