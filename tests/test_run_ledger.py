from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from recon.ledger import DuplicateSubmission, Ledger, file_sha, scrub


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'progress.sqlite3'
        self.ledger = Ledger(self.db)
        self.artifact = self.root / 'submission.csv.gz'
        self.artifact.write_bytes(b'fixture result')
        self.report_path = Path(str(self.artifact) + '.report.json')
        self.report = {'phase': 'live', 'sha256': file_sha(self.artifact),
                       'validation': {'ok': True, 'rows': 4, 'transactions': 3},
                       'official_structure': {'ok': True}, 'source_changed_during_run': False,
                       'source_hashes': {'matching.py': 'a' * 64}, 'run_id': 'fixture',
                       'environment': {'private_setting': 'do-not-store-this'}}
        self.write_report()

    def write_report(self):
        self.report_path.write_text(json.dumps(self.report))

    def register(self):
        return self.ledger.register_artifact(self.artifact, self.report_path)

    def test_registration_hash_validation_and_idempotence(self):
        first = self.register()
        self.assertEqual(first['sha256'], self.report['sha256'])
        before = self.ledger.status()['counts']
        self.assertEqual(self.register(), first)
        self.assertEqual(self.ledger.status()['counts'], before)
        self.artifact.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.register()
        self.assertEqual(self.ledger.status()['counts'], before)

    def test_unvalidated_or_unstable_artifact_rejected(self):
        for key, value in [('validation', {'ok': False}), ('official_structure', {'ok': False}),
                           ('source_changed_during_run', True)]:
            original = self.report[key]
            self.report[key] = value
            self.write_report()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.register()
            self.report[key] = original

    def test_atomic_reservation_deduplicates_concurrent_writers(self):
        artifact = self.register()
        def reserve(_):
            try:
                return self.ledger.begin_submission(artifact['sha256'], 'submission.csv.gz')['local_attempt_id']
            except DuplicateSubmission as error:
                return 'duplicate:' + error.existing['local_attempt_id']
        with ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(reserve, range(6)))
        self.assertEqual(sum(not x.startswith('duplicate:') for x in values), 1)
        self.assertEqual(self.ledger.status()['counts']['submissions'], 1)
        self.assertTrue(self.ledger.integrity_check()['ok'])

    def test_final_and_sample_cannot_be_submitted(self):
        artifact = self.register()
        with self.assertRaisesRegex(ValueError, 'Only LIVE'):
            self.ledger.begin_submission(artifact['sha256'], 'submission.csv.gz', phase='FINAL')
        self.report['phase'] = 'sample'
        self.artifact.write_bytes(b'new sample artifact')
        self.report['sha256'] = file_sha(self.artifact)
        self.write_report()
        sample = self.register()
        with self.assertRaisesRegex(ValueError, 'LIVE artifact'):
            self.ledger.begin_submission(sample['sha256'], 'submission.csv.gz')

    def test_transaction_rolls_back_events_and_entities_together(self):
        count = self.ledger.status()['counts']['events']
        with self.assertRaisesRegex(RuntimeError, 'abort'):
            with self.ledger.transaction() as db:
                self.ledger._event(db, 'testing', 'x', {'before': True})
                db.execute('INSERT INTO checkpoints VALUES(?,?,?)', ('x', '{}', 'now'))
                raise RuntimeError('abort')
        self.assertEqual(self.ledger.status()['counts']['events'], count)
        self.assertIsNone(self.ledger.checkpoint('x'))

    def test_terminal_official_score_and_ids_are_immutable(self):
        artifact = self.register()
        attempt = self.ledger.begin_submission(artifact['sha256'], 'submission.csv.gz')
        row = self.ledger.update_submission(attempt['local_attempt_id'], 'scored', sub_id='s_1',
                                           official_score=81.13, response={'status': 'scored', 'score': 81.13})
        self.assertEqual(row['official_score'], 81.13)
        self.assertEqual(self.ledger.status()['best_live'], 81.13)
        for kwargs in [{'status': 'scoring'}, {'status': 'scored', 'sub_id': 's_2'},
                       {'status': 'scored', 'official_score': 99}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.ledger.update_submission(attempt['local_attempt_id'], **kwargs)

    def test_duplicate_platform_id_rolls_back_update_event(self):
        first = self.ledger.begin_submission(self.register()['sha256'], 'first.csv.gz')
        self.ledger.update_submission(first['local_attempt_id'], 'scoring', sub_id='s_one')
        self.artifact.write_bytes(b'second artifact')
        self.report['sha256'] = file_sha(self.artifact)
        self.write_report()
        second = self.ledger.begin_submission(self.register()['sha256'], 'second.csv.gz')
        before = self.ledger.status()['counts']['events']
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.update_submission(second['local_attempt_id'], 'scoring', sub_id='s_one')
        self.assertEqual(self.ledger.status()['counts']['events'], before)
        self.assertEqual(self.ledger.get_submission(self.report['sha256'])['status'], 'reserved')

    def test_evaluation_immutable_source_bound_and_score_only_is_orphan(self):
        self.report['phase'] = 'sample'
        self.report['score'] = {'ok': True, 'score': 81.22, 'metrics': {'precision': .99, 'recall': .72}}
        self.write_report()
        result = self.ledger.import_report(self.report_path)
        self.assertIsNotNone(result['candidate_id'])
        before = self.ledger.status()['counts']
        self.ledger.import_report(self.report_path)
        self.assertEqual(self.ledger.status()['counts'], before)
        with self.assertRaises(ValueError):
            self.ledger.record_evaluation(result['candidate_id'], 'OTHER', self.report_path,
                                          artifact_sha=self.report['sha256'])
        score_only = self.root / 'score.json'
        score_only.write_text(json.dumps(self.report['score']))
        orphan = self.ledger.import_report(score_only)
        self.assertIsNone(orphan['candidate_id'])
        self.assertEqual(orphan['evaluation']['dataset_id'], 'UNSPECIFIED')
        with self.assertRaisesRegex(ValueError, 'Score-only'):
            self.ledger.record_evaluation(result['candidate_id'], 'SAMPLE', score_only)

    def test_events_and_evaluations_sql_append_only(self):
        self.ledger.event('fixture', 'x')
        with self.ledger.transaction() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('DELETE FROM events')
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE events SET kind="changed"')

    def test_secret_scrubbing_applies_to_reports_checkpoints_and_events(self):
        self.register()
        self.ledger.event('secret', 'x', {'Authorization': 'Bearer secret-token',
                          'upload': {'url': 'https://upload.test/?X-Amz-Signature=SECRET'},
                          'nested': {'password': 'password-secret', 'link': 'https://x.test/?key=SECRET'}})
        self.ledger.checkpoint('cursor', {'token': 'checkpoint-secret'}, write=True)
        with sqlite3.connect(self.db) as db:
            contents = '\n'.join(db.iterdump())
        for secret in ['secret-token', 'password-secret', 'checkpoint-secret', 'do-not-store-this', 'X-Amz-Signature']:
            self.assertNotIn(secret, contents)
        self.assertIn('[REDACTED]', contents)

    def test_official_import_idempotent_attaches_known_candidate(self):
        candidate = self.ledger.register_candidate('git-fixture', tag='candidate/test', source_manifest=self.report['source_hashes'])
        path = self.root / 'official.json'
        path.write_text(json.dumps({'phase': 'LIVE', 'source': str(self.artifact), 'sha256': self.report['sha256'],
            'sub_id': 's_fixture', 'response': {'phase': 'LIVE', 'sub_id': 's_fixture', 'status': 'scored',
                                            'created_at': '2026-11-07T09:50:29Z', 'score': 81.13}}))
        first = self.ledger.import_official_report(path, candidate['candidate_id'])
        counts = self.ledger.status()['counts']
        second = self.ledger.import_official_report(path, candidate['candidate_id'])
        self.assertEqual(first, second)
        self.assertEqual(counts, self.ledger.status()['counts'])
        self.assertEqual(first['created_at'], '2026-11-07T09:50:29Z')
        self.assertEqual(first['candidate_id'], 'git-fixture')

    def test_backup_and_integrity_are_consistent(self):
        self.register()
        self.ledger.checkpoint('chat', {'cursor': 'abc'}, write=True)
        result = self.ledger.backup(self.root / 'backup.sqlite3')
        backup = Ledger(result['path'])
        self.assertTrue(backup.integrity_check()['ok'])
        self.assertEqual(backup.status()['counts'], self.ledger.status()['counts'])
        self.assertEqual(backup.checkpoint('chat'), {'cursor': 'abc'})

    def test_rejected_import_observations_are_idempotent(self):
        first = self.ledger.import_observation(self.report_path, 'rejected', {'reason': 'missing source'})
        second = self.ledger.import_observation(self.report_path, 'rejected', {'reason': 'missing source'})
        self.assertEqual(first['evidence_sha'], second['evidence_sha'])
        self.assertTrue(second['already_recorded'])

    def test_wrong_source_candidate_cannot_reserve_or_attach_official_score(self):
        artifact = self.register()
        wrong = self.ledger.register_candidate('wrong', source_manifest={'matching.py': 'b' * 64})
        with self.assertRaisesRegex(ValueError, 'does not match artifact'):
            self.ledger.begin_submission(artifact['sha256'], 'submission.csv.gz', candidate_id=wrong['candidate_id'])
        attempt = self.ledger.begin_submission(artifact['sha256'], 'submission.csv.gz')
        with self.assertRaisesRegex(ValueError, 'does not match artifact'):
            self.ledger.link_submission_candidate(attempt['local_attempt_id'], wrong['candidate_id'])

    def test_evaluation_checks_artifact_source_even_when_report_and_candidate_agree(self):
        self.report['phase'] = 'sample'
        self.write_report()
        artifact = self.register()
        wrong_hashes = {'matching.py': 'b' * 64}
        candidate = self.ledger.register_candidate('other-source', source_manifest=wrong_hashes)
        evaluation = self.root / 'eval.json'
        evaluation.write_text(json.dumps({'source_hashes': wrong_hashes, 'ok': True, 'score': 90}))
        with self.assertRaisesRegex(ValueError, 'does not match artifact'):
            self.ledger.record_evaluation(candidate['candidate_id'], 'SAMPLE', evaluation, artifact_sha=artifact['sha256'])

    def test_same_output_with_second_validated_source_retains_both_provenances(self):
        artifact = self.register()
        self.report['source_hashes'] = {'matching.py': 'b' * 64}
        self.write_report()
        self.register()
        candidate = self.ledger.register_candidate('second-source', source_manifest=self.report['source_hashes'])
        attempt = self.ledger.begin_submission(artifact['sha256'], 'submission.csv.gz', candidate_id=candidate['candidate_id'])
        self.assertEqual(attempt['candidate_id'], 'second-source')
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM artifact_reports').fetchone()[0], 2)

    def test_verified_comparison_import_and_old_unproven_summary_remain_distinct(self):
        report = {'before_tag': 'before', 'before_sha': 'a' * 40,
                  'candidate_tag': 'after', 'candidate_sha': 'b' * 40,
                  'windows': {'sample': {'tag_source_hashes_verified': True,
                      'scorer_sha256': 'c' * 64, 'file_hashes': {'before': 'd' * 64, 'after': 'e' * 64},
                      'before_score': {'score': 80}, 'after_score': {'score': 81}}}}
        path = self.root / 'T2_VALIDATION.json'
        path.write_text(json.dumps(report))
        self.ledger.import_report(path)
        counts = self.ledger.status()['counts']
        self.ledger.import_report(path)
        self.assertEqual(self.ledger.status()['counts'], counts)
        self.assertEqual(counts['comparison_reports'], 1)
        report['candidate_sha'] = 'f' * 40
        report['windows']['sample'].pop('tag_source_hashes_verified')
        path.write_text(json.dumps(report))
        self.ledger.import_report(path)
        after = self.ledger.status()['counts']
        self.assertEqual(after['comparison_reports'], 1)
        self.assertEqual(after['candidates'], counts['candidates'])

    def test_observe_once_is_atomic_and_deduplicates_concurrent_calls(self):
        def observe(_):
            return self.ledger.observe_once('platform:revision1', 'platform_changed', 'team10', {'score': 81.13})
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(observe, range(6)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.ledger.checkpoint('platform:revision1'), {'score': 81.13})
        self.assertEqual(self.ledger.status()['counts']['events'], 1)
        # An event insertion failure must roll back the preceding checkpoint too.
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.observe_once('failed-observation', None, 'team10', {'score': 82})
        self.assertIsNone(self.ledger.checkpoint('failed-observation'))
        self.assertEqual(self.ledger.status()['counts']['events'], 1)

    def test_bad_second_comparison_commit_does_not_create_first_candidate(self):
        report = {'before_tag': 'before', 'before_sha': 'a' * 40,
                  'candidate_tag': 'after', 'candidate_sha': 'bad',
                  'windows': {'sample': {'tag_source_hashes_verified': True,
                      'scorer_sha256': 'c' * 64, 'file_hashes': {'before': 'd' * 64, 'after': 'e' * 64},
                      'before_score': {'score': 80}, 'after_score': {'score': 81}}}}
        path = self.root / 'comparison.json'
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'exact source commits'):
            self.ledger.import_comparison(path)
        self.assertEqual(self.ledger.status()['counts']['candidates'], 0)

    def test_official_import_rejects_inconsistent_phase_digest_id_and_missing_score(self):
        base = {'phase': 'LIVE', 'source': str(self.artifact), 'sha256': self.report['sha256'],
                'sub_id': 's_fixture', 'response': {'phase': 'LIVE', 'sub_id': 's_fixture',
                                                  'status': 'scored', 'score': 81.13}}
        path = self.root / 'official.json'
        for update in [{'phase': 'FINAL'}, {'file_sha256': 'f' * 64}, {'sub_id': 's_other'},
                       {'score': None}, {'score': float('nan')}]:
            record = {**base, 'response': {**base['response'], **update}}
            path.write_text(json.dumps(record))
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.ledger.import_official_report(path)
        self.assertEqual(self.ledger.status()['counts']['submissions'], 0)


if __name__ == '__main__':
    unittest.main()
