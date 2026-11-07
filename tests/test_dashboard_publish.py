import contextlib
import copy
import csv
import gzip
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from recon.dashboard_publish import (STANDARD_MODULES, check_snapshot, publish_dashboard,
    send_snapshot, update_local, publish_outbox)


def snapshot():
    return {'schema_version': 1,
        'pipeline': {'phase': 'live', 'run_id': 'real-run-1',
            'started_at': '2026-11-07T09:00:00+00:00', 'finished_at': '2026-11-07T09:02:00+00:00',
            'sha256': 'a' * 64, 'hash_verified': True, 'audit_hash_verified': True,
            'source_changed_during_run': False, 'enabled_modules': STANDARD_MODULES,
            'validation': {'ok': True, 'transactions': 1, 'covered': 1, 'missing': 0},
            'official_structure': {'ok': True}},
        'summary': {'transactions': 1, 'submission_transactions': 1},
        'sample': {'size': 1, 'population': 1}, 'transactions': [{'id': 'T1'}],
        'export_checks': {'missing_submission_transactions': 0, 'submission_ids_absent_from_bank': 0,
            'audit_ids_missing': 0, 'module_counts_match_report': True}}


class DashboardPublishTests(unittest.TestCase):
    def setup_files(self, root, phase='live', token='private-secret'):
        submission = root / 'release.csv.gz'
        submission.write_bytes(b'validated submission must remain unchanged')
        report = {'phase': phase, 'run_id': 'real-run-1', 'enabled_modules': STANDARD_MODULES}
        Path(str(submission) + '.report.json').write_text(json.dumps(report))
        (root / '.dashboard-publish.json').write_text(json.dumps({'enabled': True, 'url': 'https://dashboard.example/api/snapshot', 'token': token}))
        return submission

    def test_network_failure_keeps_csv_and_durable_outbox_without_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            submission = self.setup_files(root)
            before = submission.read_bytes()
            with patch('scripts.build_dashboard_data.export_dashboard', return_value=snapshot()):
                status = publish_dashboard(submission, root=root, env={}, attempt=lambda *a: {'code': 503})
            self.assertEqual(status['state'], 'pending')
            self.assertEqual(status['attempts'], 2)
            self.assertEqual(submission.read_bytes(), before)
            outbox = root / 'outputs/dashboard-outbox/real-run-1.json'
            self.assertTrue(outbox.exists())
            self.assertEqual(json.loads((root / 'dashboard/data.json').read_text())['pipeline']['run_id'], 'real-run-1')
            for path in [outbox, outbox.with_suffix('.status.json'), Path(str(submission) + '.dashboard-status.json')]:
                self.assertNotIn('private-secret', path.read_text())

    def test_outbox_retry_works_after_csv_removed_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            submission = self.setup_files(root)
            with patch('scripts.build_dashboard_data.export_dashboard', return_value=snapshot()):
                status = publish_dashboard(submission, root=root, env={}, attempt=lambda *a: {'code': 503})
            outbox = Path(status['outbox'])
            submission.unlink()
            retry = publish_outbox(outbox, root=root, env={}, attempt=lambda *a: {
                'code': 200, 'run_id': 'real-run-1', 'hash': 'a' * 64, 'status': 'unchanged'})
            self.assertEqual(retry['state'], 'published')
            outbox.write_bytes(outbox.read_bytes() + b' ')
            rejected = publish_outbox(outbox, root=root, env={}, attempt=lambda *a: self.fail('No network expected'))
            self.assertEqual(rejected['reason'], 'invalid_outbox')

    def test_missing_token_sample_and_ablation_skip_without_export_or_network(self):
        for phase, token, modules, reason in [('sample', 'token', STANDARD_MODULES, 'phase_not_live_or_final'),
                ('live', '', STANDARD_MODULES, 'missing_token'), ('live', 'token', ['matching'], 'nonstandard_modules')]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                submission = self.setup_files(root, phase, token)
                with patch('scripts.build_dashboard_data.export_dashboard') as export:
                    status = publish_dashboard(submission, root=root, env={}, enabled_modules=modules,
                        attempt=lambda *a: self.fail('No network expected'))
                    export.assert_not_called()
                self.assertEqual(status['reason'], reason)
                self.assertEqual(status['state'], 'skipped')

    def test_success_requires_matching_acknowledgment(self):
        value = snapshot()
        config = {'url': 'https://dashboard.example/api/snapshot', 'token': 'secret'}
        self.assertEqual(send_snapshot(config, value, lambda *a: {'code': 200})['state'], 'pending')
        result = send_snapshot(config, value, lambda *a: {'code': 201, 'run_id': 'real-run-1', 'hash': 'a' * 64, 'status': 'published'})
        self.assertEqual(result['state'], 'published')
        self.assertEqual(result['attempts'], 1)
        self.assertEqual(send_snapshot(config, value, lambda *a: {'code': 409})['state'], 'superseded')

    def test_timeouts_have_finite_retries_and_budget(self):
        calls = []
        def timeout(url, token, body, limit):
            calls.append(limit)
            raise subprocess.TimeoutExpired('sanitized', limit)
        status = send_snapshot({'url': 'https://dashboard.example/api/snapshot', 'token': 'secret'}, snapshot(), timeout)
        self.assertEqual(status['reason'], 'network_timeout')
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(sum(calls), 20)

    def test_old_local_snapshot_cannot_replace_newer_or_final(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.json'
            old = snapshot()
            newer = copy.deepcopy(old)
            newer['pipeline']['started_at'] = '2026-11-07T10:00:00+00:00'
            update_local(path, newer)
            self.assertFalse(update_local(path, old))
            final = copy.deepcopy(old)
            final['pipeline']['phase'] = 'final'
            self.assertTrue(update_local(path, final))
            self.assertFalse(update_local(path, newer))

    def test_invalid_hash_coverage_or_timestamps_are_rejected(self):
        for field, value in [('hash_verified', False), ('audit_hash_verified', False),
                ('started_at', '2026-11-07T11:00:00+00:00'), ('started_at', '2026-11-07T09:00:00')]:
            candidate = snapshot()
            candidate['pipeline'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                check_snapshot(candidate)
        candidate = snapshot()
        candidate['export_checks']['audit_ids_missing'] = 1
        with self.assertRaises(ValueError):
            check_snapshot(candidate)

    def test_runner_hook_failure_preserves_published_bundle_and_identity(self):
        from recon.run import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def write(path, rows):
                path.parent.mkdir(parents=True, exist_ok=True)
                opener = gzip.open if path.suffix == '.gz' else open
                with opener(path, 'wt', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
            write(root / 'masters/categories.csv', [{'category': 'UNIDENTIFIED'}])
            write(root / 'masters/payment_terms.csv', [{'code': 'T30', 'days': '30'}])
            write(root / 'masters/customers.csv', [{'customer_id': 'C1', 'name': 'Example'}])
            write(root / 'sample/bank_transactions.csv.gz', [{'bank_txn_id': 'T1', 'booking_date': '2025-01-01', 'account_id': 'A1'}])
            out = root / 'answer.csv.gz'
            def failing_hook(*args, **kwargs):
                self.assertTrue(out.exists())
                self.assertTrue(Path(str(out) + '.report.json').exists())
                raise RuntimeError('simulated dashboard outage')
            with contextlib.redirect_stdout(io.StringIO()), patch('recon.dashboard_publish.publish_dashboard', side_effect=failing_hook):
                report = main(['--data', str(root), '--phase', 'sample', '--modules', '', '--out', str(out)])
            self.assertTrue(report['validation']['ok'])
            self.assertTrue(report['run_id'])
            self.assertLessEqual(report['started_at'], report['finished_at'])
            self.assertEqual(report['enabled_modules'], [''])
            self.assertEqual(json.loads(Path(str(out) + '.report.json').read_text())['run_id'], report['run_id'])


if __name__ == '__main__':
    unittest.main()
