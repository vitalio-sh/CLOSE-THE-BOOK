import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_experiment.py'
spec = importlib.util.spec_from_file_location('experiment', SCRIPT)
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


class ExperimentTests(unittest.TestCase):
    def bundle(self, root):
        (root / 'submission.csv.gz').write_bytes(b'validated artifact')
        (root / 'submission.csv.gz.audit.jsonl').write_text('{}\n')
        digest = experiment.sha256(root / 'submission.csv.gz')
        release = {'publication': 'validated', 'validation': {'ok': True}, 'official_structure': {'ok': True},
                   'sha256': digest, 'audit_sha256': experiment.sha256(root / 'submission.csv.gz.audit.jsonl')}
        (root / 'submission.csv.gz.report.json').write_text(json.dumps(release))
        hashes = {name: experiment.sha256(root / name) for name in
                  ('submission.csv.gz', 'submission.csv.gz.audit.jsonl', 'submission.csv.gz.report.json')}
        (root / 'experiment.json').write_text(json.dumps({'status': 'verified', 'submission_sha256': digest,
                                                         'artifact_hashes': hashes}))
        return digest

    def test_manifest_excludes_labels_and_detects_input_content_change(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'sample').mkdir()
            data = root / 'sample/bank_transactions.csv.gz'
            data.write_bytes(b'first')
            (root / 'sample/golden_allocations.csv.gz').write_bytes(b'labels')
            first = experiment.input_manifest(root, 'sample')
            self.assertEqual(list(first), ['sample/bank_transactions.csv.gz'])
            data.write_bytes(b'other')
            self.assertNotEqual(first, experiment.input_manifest(root, 'sample'))

    def test_unverified_run_cannot_replace_pointer(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pointer = root / 'current.json'
            pointer.write_text('old release')
            (root / 'experiment.json').write_text(json.dumps({'status': 'running'}))
            with self.assertRaises(ValueError):
                experiment.promote(pointer, root)
            self.assertEqual(pointer.read_text(), 'old release')

    def test_pointer_binds_complete_verified_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            digest = self.bundle(root)
            pointer = root / 'current.json'
            experiment.promote(pointer, root)
            result = json.loads(pointer.read_text())
            self.assertEqual(result['manifest_sha256'], experiment.sha256(root / 'experiment.json'))
            self.assertEqual(result['submission_sha256'], digest)

    def test_changed_artifact_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            submission = root / 'submission.csv.gz'
            self.bundle(root)
            submission.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'changed'):
                experiment.promote(root / 'current.json', root)

    def test_incomplete_manifest_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'submission.csv.gz').write_bytes(b'not a complete bundle')
            (root / 'experiment.json').write_text(json.dumps({'status': 'verified',
                'submission_sha256': experiment.sha256(root / 'submission.csv.gz')}))
            with self.assertRaisesRegex(ValueError, 'complete'):
                experiment.promote(root / 'current.json', root)
            self.assertFalse((root / 'current.json').exists())


class HistoricalAdviceManifestTests(unittest.TestCase):
    def test_live_hashes_prior_advice_index_and_nested_body(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            folder = root / 'train/remittance_advices/E'
            folder.mkdir(parents=True)
            index = folder.parent / 'index.csv'
            index.write_text('file,received_date\nE/a.txt,2025-06-30\n')
            body = folder / 'a.txt'
            body.write_text('first!')
            first = experiment.input_manifest(root, 'live')
            self.assertIn('train/remittance_advices/index.csv', first)
            self.assertIn('train/remittance_advices/E/a.txt', first)
            body.write_text('second')
            self.assertNotEqual(first, experiment.input_manifest(root, 'live'))
            second = experiment.input_manifest(root, 'live')
            body.unlink()
            self.assertNotEqual(second, experiment.input_manifest(root, 'live'))
            index.write_text('file,received_date\n')
            self.assertNotEqual(first, experiment.input_manifest(root, 'live'))

    def test_final_hashes_both_histories_excludes_labels_and_future_for_live(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for phase in ('train', 'live', 'final'):
                folder = root / phase / 'remittance_advices'
                folder.mkdir(parents=True)
                (folder / 'index.csv').write_text('file\n')
                (root / phase / 'golden_allocations.csv.gz').write_bytes(b'labels')
            final = experiment.input_manifest(root, 'final')
            self.assertEqual(set(final), {p + '/remittance_advices/index.csv' for p in ('train', 'live', 'final')})
            live = experiment.input_manifest(root, 'live')
            self.assertEqual(set(live), {p + '/remittance_advices/index.csv' for p in ('train', 'live')})
