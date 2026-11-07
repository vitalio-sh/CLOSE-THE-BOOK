import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from recon.ledger import Ledger

SPEC = importlib.util.spec_from_file_location('submit_live', Path(__file__).resolve().parents[1] / 'scripts/submit_live.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.rows = []
        self.filename = None
        self.failed_reservation = False
        self.upload_failed = False
        self.phase = 'LIVE'
        self.remote_status = 'scored'
        self.uploads = 0

    def api(self, path, body=None):
        self.calls.append((path, body))
        if path == '/state':
            return {'accepting': [self.phase], 'frozen': False, 'limits': {'max_upload_mb': 60, 'daily_per_phase': 20, 'min_interval_s': 10}}
        if path == '/me':
            return {'team_id': M.TEAM_ID, 'approval': 'APPROVED'}
        if path == '/submissions':
            return {'submissions': self.rows}
        if path == '/submissions/upload-url':
            self.filename = body['filename']
            if self.failed_reservation:
                raise M.SubmissionError('network uncertainty')
            return {'sub_id': 's_test', 'upload': {'url': 'https://uploads.example.test/credential', 'fields': {'key': 'private-presigned'}}}
        return {'sub_id': 's_test', 'phase': 'LIVE', 'filename': self.filename, 'status': self.remote_status,
                'score': 82.5 if self.remote_status == 'scored' else None, 'metrics': {'allocation_precision': .99}}

    def upload(self, upload, filename, payload):
        self.uploads += 1
        if self.upload_failed:
            raise M.SubmissionError('uncertain upload')


class SubmitLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        source = self.root / 'recon'
        source.mkdir()
        self.hashes = {}
        for name in ('context.py', 'matching.py', 'batches.py', 'documents.py', 'run.py', 'validate.py'):
            content = ('# ' + name + '\n').encode()
            (source / name).write_bytes(content)
            self.hashes[name] = M.digest(content)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'candidate'], check=True)
        self.baseline_commit = M.resolve(self.root, 'HEAD')
        (source/'matching.py').write_text('# changed matcher\n')
        self.hashes['matching.py'] = M.file_digest(source/'matching.py')
        subprocess.run(['git','-C',str(self.root),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Test','-c','user.email=test@example.test','commit','-qm','improved'],check=True)
        self.commit = M.resolve(self.root, 'HEAD')
        self.path = self.root / 'submission.csv.gz'
        csv = b'bank_txn_id,item_id,amount,category,exception\nB1,I1,1.00,,\n'
        payload = gzip.compress(csv)
        self.path.write_bytes(payload)
        self.report = {'phase': 'live', 'sha256': M.digest(payload), 'source_hashes': self.hashes,
            'source_changed_during_run': False, 'checked_inputs_changed': [], 'run_id': 'run-1',
            'validation': {'ok': True, 'transactions': 1, 'rows': 1, 'covered': 1, 'missing': 0, 'uncompressed_bytes': len(csv)},
            'official_structure': {'ok': True}, 'audit_sha256': M.digest(b'audit')}
        self.report_path = Path(str(self.path) + '.report.json')
        self.report_path.write_text(json.dumps(self.report))
        Path(str(self.path) + '.audit.jsonl').write_bytes(b'audit')
        scorer = self.root / 'scorer.py'; scorer.write_text('# official scorer fixture\n')
        before = {'score': 81, 'metrics': {'precision': .99, 'recall': .7, 'txn_accuracy': .86, 'exception_macro_f1': .67}}
        after = {'score': 82, 'metrics': {'precision': .99, 'recall': .72, 'txn_accuracy': .85, 'exception_macro_f1': .72}}
        inputs = {k: M.digest(k.encode()) for k in ('before', 'after', 'bank', 'golden', 'categories', 'exceptions')}
        window = {'before_score': before, 'after_score': after, 'after_tag': self.commit, 'before_tag': self.baseline_commit,
                  'tag_source_hashes_verified': True, 'file_hashes': inputs, 'scorer': str(scorer), 'scorer_sha256': M.file_digest(scorer)}
        october = {**window, 'file_hashes': {k:M.digest(('october-'+k).encode()) for k in inputs}}
        comparison = self.root / 'comparison.json'; comparison.write_text(json.dumps({'candidate_sha':self.commit,'before_sha':self.baseline_commit,'windows': {'sample': window, 'october': october}}))
        input_hash = M.digest(json.dumps({k:v for k,v in inputs.items() if k not in ('before','after')},sort_keys=True,separators=(',',':')).encode())
        self.evidence = {'schema_version':1,'candidate':self.commit,'candidate_source_hashes':self.hashes,'candidate_commit':self.commit,'baseline_candidate':self.baseline_commit,'baseline_commit':self.baseline_commit,
            'validation':{'reviewed':True,'no_label_leakage':True},'checks':[
                {'dataset':dataset,'input_manifest_sha256':input_hash if name=='sample' else M.digest(json.dumps({k:v for k,v in october['file_hashes'].items() if k not in ('before','after')},sort_keys=True,separators=(',',':')).encode()),'scorer_sha256':M.file_digest(scorer),
                 'baseline':M.normalized_score(before),'candidate':M.normalized_score(after),
                 'evidence_path':str(comparison),'evidence_sha256':M.file_digest(comparison),'evidence_window':name}
                for dataset,name in [('sample','sample'),('train:2024-10-01:2024-10-31','october')]]}
        self.evidence_path=self.root/'evidence.json';self.evidence_path.write_text(json.dumps(self.evidence))
        self.artifact=M.verify_artifact(self.path)
        self.verified=M.verify_evidence(self.evidence_path,self.commit,self.report,self.root)
        self.ledger=Ledger(self.root/'progress.sqlite3')
        self.transport=FakeTransport()

    def tearDown(self):
        self.temp.cleanup()

    def execute(self):
        return M.execute(self.artifact,self.commit,self.verified,self.ledger,self.transport,self.root)

    def test_success_is_recorded_and_repeated_execution_never_reserves_twice(self):
        result=self.execute();self.assertEqual(result['score'],82.5)
        second=self.execute();self.assertEqual(second['sub_id'],'s_test')
        reservations=[x for x in self.transport.calls if x[0]=='/submissions/upload-url']
        self.assertEqual(len(reservations),1);self.assertEqual(self.transport.uploads,1)
        row=self.ledger.get_submission(self.artifact['sha256']);self.assertEqual(row['status'],'scored')
        for p in [Path(result['report']),self.root/'progress.sqlite3']:
            self.assertNotIn(b'private-presigned',p.read_bytes())

    def test_final_phase_is_not_reserved(self):
        self.transport.phase='FINAL'
        with self.assertRaisesRegex(M.SubmissionError,'not accepting'):self.execute()
        self.assertFalse(any(x[0]=='/submissions/upload-url' for x in self.transport.calls))

    def test_reservation_uncertainty_survives_retry_without_new_slot(self):
        self.transport.failed_reservation=True
        with self.assertRaisesRegex(M.SubmissionError,'Reservation outcome unresolved'):self.execute()
        row=self.ledger.get_submission(self.artifact['sha256']);self.assertEqual(row['status'],'reservation_unknown')
        with self.assertRaisesRegex(M.SubmissionError,'Reservation outcome unresolved'):self.execute()
        self.assertEqual(sum(x[0]=='/submissions/upload-url' for x in self.transport.calls),1)

    def test_uncertain_reservation_recovers_by_unique_filename(self):
        self.transport.failed_reservation=True
        with self.assertRaises(M.SubmissionError):self.execute()
        self.transport.rows=[{'phase':'LIVE','filename':self.transport.filename,'sub_id':'s_test'}]
        result=self.execute();self.assertEqual(result['score'],82.5)
        self.assertEqual(sum(x[0]=='/submissions/upload-url' for x in self.transport.calls),1)

    def test_unknown_upload_is_not_reuploaded_or_reserved(self):
        self.transport.upload_failed=True;self.transport.remote_status='pending'
        with self.assertRaisesRegex(M.SubmissionError,'Upload outcome'):self.execute()
        with self.assertRaisesRegex(M.SubmissionError,'no confirmed upload'):self.execute()
        self.assertEqual(self.transport.uploads,1)
        self.assertEqual(sum(x[0]=='/submissions/upload-url' for x in self.transport.calls),1)

    def test_artifact_tampering_and_final_report_rejected(self):
        self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(M.SubmissionError,'SHA-256'):M.verify_artifact(self.path)
        self.report['phase']='final';self.report_path.write_text(json.dumps(self.report))
        with self.assertRaisesRegex(M.SubmissionError,'Only LIVE'):M.verify_artifact(self.path)

    def test_evidence_fabricated_score_and_missing_temporal_check_rejected(self):
        self.evidence['checks'][0]['candidate']['score']=99
        self.evidence_path.write_text(json.dumps(self.evidence))
        with self.assertRaisesRegex(M.SubmissionError,'does not match'):M.verify_evidence(self.evidence_path,self.commit,self.report,self.root)
        self.evidence['checks']=self.evidence['checks'][:1]
        self.evidence_path.write_text(json.dumps(self.evidence))
        with self.assertRaisesRegex(M.SubmissionError,'temporal'):M.verify_evidence(self.evidence_path,self.commit,self.report,self.root)

    def test_source_manifest_must_match_tag_not_current_working_tree(self):
        (self.root/'recon/matching.py').write_text('# another running agent\n')
        M.verify_evidence(self.evidence_path,self.commit,self.report,self.root)
        self.report['source_hashes']['matching.py']='f'*64
        self.evidence['candidate_source_hashes']=self.report['source_hashes'];self.evidence_path.write_text(json.dumps(self.evidence))
        with self.assertRaisesRegex(M.SubmissionError,'Tagged code differs'):M.verify_evidence(self.evidence_path,self.commit,self.report,self.root)

    def test_file_lock_rejects_parallel_sender(self):
        lock=self.root/'lock'
        with M.submission_lock(lock):
            with self.assertRaisesRegex(M.SubmissionError,'already running'):
                with M.submission_lock(lock):pass

    def test_pinned_commit_and_independent_windows_are_required(self):
        self.evidence['candidate_commit']='f'*40
        self.evidence_path.write_text(json.dumps(self.evidence))
        with self.assertRaisesRegex(M.SubmissionError,'pinned evidence'):M.verify_evidence(self.evidence_path,self.commit,self.report,self.root)
        self.evidence['candidate_commit']=self.commit
        self.evidence['checks'][1]={**self.evidence['checks'][0],'dataset':'train:2024-10-01:2024-10-31'}
        self.evidence_path.write_text(json.dumps(self.evidence))
        with self.assertRaisesRegex(M.SubmissionError,'reused|inconsistent'):M.verify_evidence(self.evidence_path,self.commit,self.report,self.root)

    def test_closed_phase_allows_existing_submission_read_recovery(self):
        self.execute();self.transport.phase='FINAL'
        result=self.execute()
        self.assertEqual(result['score'],82.5)
        self.assertEqual(sum(x[0]=='/submissions/upload-url' for x in self.transport.calls),1)

    def test_exact_payload_audit_and_report_are_retained(self):
        result=self.execute();record=json.loads(Path(result['report']).read_text())
        archived=Path(record['source'])
        self.assertNotEqual(archived,self.path)
        self.assertEqual(archived.read_bytes(),self.artifact['payload'])
        self.assertEqual(Path(str(archived)+'.audit.jsonl').read_bytes(),b'audit')
        original_report=archived.parent/'original_report.json'
        self.assertEqual(original_report.read_bytes(),self.report_path.read_bytes())
        self.assertEqual(M.file_digest(original_report),self.artifact['report_sha256'])
        self.path.write_bytes(b'next run overwrote original')
        self.assertEqual(M.verify_artifact(archived)['sha256'],self.artifact['sha256'])

    def test_stale_weaker_baseline_cannot_trigger_another_reservation(self):
        self.execute()
        csv=b'bank_txn_id,item_id,amount,category,exception\nB1,I1,2.00,,\n'
        self.path.write_bytes(gzip.compress(csv));self.report['sha256']=M.file_digest(self.path)
        self.report['validation']['uncompressed_bytes']=len(csv)
        self.report_path.write_text(json.dumps(self.report));self.artifact=M.verify_artifact(self.path)
        with self.assertRaisesRegex(M.SubmissionError,'not the latest scored source'):self.execute()
        self.assertEqual(sum(x[0]=='/submissions/upload-url' for x in self.transport.calls),1)

    def test_failed_score_queue_preserves_confirmed_upload_for_safe_resume(self):
        original=self.transport.api
        def api(path,body=None):
            if path.endswith('/score'):
                raise M.SubmissionError('queue request did not return')
            return original(path,body)
        self.transport.api=api;self.transport.remote_status='pending'
        with self.assertRaisesRegex(M.SubmissionError,'Scoring request outcome'):self.execute()
        self.assertEqual(self.ledger.get_submission(self.artifact['sha256'])['status'],'uploaded')
        self.transport.api=original;self.transport.remote_status='scored'
        self.assertEqual(self.execute()['score'],82.5)
        self.assertEqual(sum(x[0]=='/submissions/upload-url' for x in self.transport.calls),1)


if __name__=='__main__':unittest.main()
