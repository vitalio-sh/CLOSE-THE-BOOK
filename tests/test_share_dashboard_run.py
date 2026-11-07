import copy
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from scripts.share_dashboard_run import build_summary, publish


class ShareDashboardRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv = self.root / 'submission.csv.gz'
        self.csv.write_bytes(b'not real dataset: checksum fixture')
        self.sha = hashlib.sha256(self.csv.read_bytes()).hexdigest()
        self.report = {
            'phase': 'sample', 'run_id': 'fixture-run', 'finished_at': '2026-11-07T10:00:00Z',
            'validation': {'ok': True, 'rows': 10, 'transactions': 4, 'missing': 0},
            'official_structure': {'ok': True}, 'source_changed_during_run': False,
            'sha256': self.sha, 'seconds': 2,
            'artifacts': {'submission': self.csv.name},
            'environment': {'PUBLISHTOKEN': 'must-never-be-published'},
            'path': '/private/path/customer-data',
        }
        self.report_path = self.root / 'submission.csv.gz.report.json'
        self.scorer = {'ok': True, 'score': 81.5, 'metrics': {
            'rows': 10, 'txns_total': 4, 'precision': .99, 'recall': .75}}
        self.evaluation_path = self.root / 'score.txt'
        self.provenance = patch('scripts.share_dashboard_run.provenance', return_value=('solution/marina', None))
        self.provenance.start()
        self.addCleanup(self.provenance.stop)

    def build(self, evaluation=False):
        self.report_path.write_text(json.dumps(self.report))
        if evaluation:
            self.evaluation_path.write_text(json.dumps(self.scorer) + '\nsegment listing follows')
        return build_summary(self.report_path, 'marina', 'Candidate',
                             self.evaluation_path if evaluation else None, root=self.root)

    def test_public_summary_is_allowlisted_without_secret_paths_or_invented_score(self):
        result = self.build()
        self.assertIsNone(result['local_score'])
        self.assertIsNone(result['benchmark'])
        self.assertIsNone(result['commit'])
        self.assertEqual(result['sha256'], self.sha)
        serialized = json.dumps(result)
        self.assertNotIn('must-never-be-published', serialized)
        self.assertNotIn('/private/path', serialized)
        self.assertNotIn('environment', serialized)
        self.assertEqual(result['validation'], {'ok': True, 'official_structure_ok': True})

    def test_companion_score_with_text_tail_has_explicit_sample_benchmark(self):
        result = self.build(evaluation=True)
        self.assertEqual(result['local_score'], 81.5)
        self.assertEqual(result['benchmark'], 'SAMPLE')
        self.assertEqual(result['precision'], .99)

    def test_missing_failed_validation_and_format_checks_reject(self):
        original = copy.deepcopy(self.report)
        for update in [{'validation': {'ok': False}}, {'official_structure': {'ok': False}},
                       {'source_changed_during_run': True}, {'checked_inputs_changed': ['bank']}]:
            self.report = {**original, **update}
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.build()

    def test_artifact_mismatch_rejects(self):
        self.csv.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.build()

    def test_unmatched_counts_wrong_phase_failed_score_and_nan_reject(self):
        original = copy.deepcopy(self.scorer)
        for update in [{'ok': False}, {'phase': 'train'}, {'score': float('nan')},
                       {'metrics': {**original['metrics'], 'txns_total': 99}}]:
            self.scorer = {**original, **update}
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.build(evaluation=True)

    def test_live_score_is_never_attached_from_local_evaluation(self):
        self.report['phase'] = 'live'
        self.assertIsNone(self.build()['local_score'])
        with self.assertRaisesRegex(ValueError, 'LIVE'):
            self.build(evaluation=True)

    def test_external_evaluation_requires_checksum_binding(self):
        folder = self.root / 'elsewhere'
        folder.mkdir()
        self.evaluation_path = folder / 'score.json'
        with self.assertRaisesRegex(ValueError, 'External evaluation'):
            self.build(evaluation=True)
        self.scorer['submission_sha256'] = self.sha
        self.assertEqual(self.build(evaluation=True)['local_score'], 81.5)
        self.scorer['submission_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'different submission'):
            self.build(evaluation=True)

    def test_temporal_report_keeps_format_check_false_and_names_window(self):
        self.report.pop('phase')
        self.report.pop('official_structure')
        self.report['start'] = '2024-10-01'
        self.report['end'] = '2024-10-31'
        self.report['score'] = self.scorer
        self.report['output_sha256'] = self.report.pop('sha256')
        result = self.build()
        self.assertEqual(result['phase'], 'train')
        self.assertEqual(result['benchmark'], 'TRAIN 2024-10-01 → 2024-10-31')
        self.assertFalse(result['validation']['official_structure_ok'])

    def test_identity_is_idempotent_but_separates_evaluated_view(self):
        unscored = self.build()
        self.assertEqual(unscored['run_id'], self.build()['run_id'])
        self.assertNotEqual(unscored['run_id'], self.build(evaluation=True)['run_id'])

    def test_unsafe_artifact_and_author_are_rejected(self):
        self.report['artifacts']['submission'] = '../submission.csv.gz'
        with self.assertRaisesRegex(ValueError, 'sibling'):
            self.build()
        with self.assertRaisesRegex(ValueError, 'Author'):
            build_summary(self.report_path, '../someone', root=self.root)

    def test_publish_is_idempotent_preserves_solver_branch_and_retries_push_race(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, remote = base / 'solver', base / 'remote.git'
            source.mkdir()
            real_run = subprocess.run

            def command(*args, cwd=source):
                return real_run(['git', '-C', str(cwd), *args], check=True,
                                capture_output=True, text=True).stdout.strip()

            real_run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
            command('init', '-b', 'solution/me')
            command('config', 'user.name', 'Fixture')
            command('config', 'user.email', 'fixture@example.invalid')
            (source / 'solver.py').write_text('original source')
            command('add', '.')
            command('commit', '-m', 'Fixture source')
            command('push', str(remote), 'HEAD:refs/heads/dashboard/runs')
            command('remote', 'add', 'origin', 'https://github.com/vitalio-sh/CLOSE-THE-BOOK.git')
            (source / 'solver.py').write_text('uncommitted teammate work')
            before = command('rev-parse', 'HEAD')
            failures = [1]

            def local_transport(args, **kwargs):
                args = list(args)
                if args[:2] == ['git', 'clone']:
                    args[-2] = str(remote)
                if 'push' in args and failures[0]:
                    failures[0] -= 1
                    return subprocess.CompletedProcess(args, 1, '', 'simulated race')
                return real_run(args, **kwargs)

            summary = self.build(evaluation=True)
            with patch('scripts.share_dashboard_run.subprocess.run', side_effect=local_transport):
                self.assertEqual(publish(summary, source)['status'], 'shared')
                self.assertEqual(publish(summary, source)['status'], 'already_shared')
            self.assertEqual(command('branch', '--show-current'), 'solution/me')
            self.assertEqual(command('rev-parse', 'HEAD'), before)
            self.assertEqual((source / 'solver.py').read_text(), 'uncommitted teammate work')
            record = command('show', 'dashboard/runs:runs/marina/' + summary['run_id'] + '.json', cwd=remote)
            self.assertEqual(json.loads(record), summary)


if __name__ == '__main__':
    unittest.main()
