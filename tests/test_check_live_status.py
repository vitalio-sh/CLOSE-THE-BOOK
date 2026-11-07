import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from recon.ledger import Ledger

spec = importlib.util.spec_from_file_location('check_live', Path(__file__).resolve().parents[1] / 'scripts/check_live_status.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CheckLiveStatusTests(unittest.TestCase):
    def test_records_official_best_latest_and_deduplicates_submission_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'outputs/platform/progress.sqlite3'
            ledger = Ledger(db)
            calls = []
            def fetch(path, auth=False):
                calls.append(path)
                if path == '/state': return {'phase': 'LIVE', 'accepting': ['LIVE'], 'limits': {'min_interval_s': 10}}
                if path == '/me': return {'team_id': module.TEAM, 'team_name': 'Team 10'}
                if path == '/submissions': return {'submissions': [
                    {'sub_id': 's_new', 'phase': 'LIVE', 'score': 80, 'status': 'scored', 'created_at': '2026-11-07T11:00:00Z'},
                    {'sub_id': 's_best', 'phase': 'LIVE', 'score': 81.13, 'status': 'scored', 'created_at': '2026-11-07T10:00:00Z'},
                    {'sub_id': 'final_hidden', 'phase': 'FINAL', 'score': None, 'status': 'received'}], 'final': {'used': True}}
                if path == '/leaderboard': return {'rows': [{'team_name': 'Team 10', 'rank': 3, 'best_live': 81.13}, {'team_name': 'Leader', 'rank': 1, 'best_live': 90.69}]}
                return {'sub_id': path.rsplit('/',1)[1], 'metrics': {'allocation_precision': .98}}
            with patch.object(module, 'ROOT', root):
                first = module.check(ledger, fetch)
                second = module.check(ledger, fetch)
            self.assertEqual(first['best_live'], 81.13)
            self.assertEqual(first['latest_live'], 80)
            self.assertEqual(first['gap'], 9.56)
            self.assertEqual(second['rank'], 3)
            self.assertTrue(first['final_used'])
            self.assertEqual(len(first['submissions']), 2)
            self.assertEqual(calls.count('/submissions/s_new'), 1)
            self.assertEqual(calls.count('/submissions/s_best'), 1)
            self.assertNotIn('/submissions/final_hidden', calls)
            saved = json.loads((root / 'outputs/platform/live_monitor_state.json').read_text())
            self.assertEqual(saved['platform']['best_live'], 81.13)

    def test_wrong_team_cannot_be_recorded_as_team10(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / 'ledger.sqlite3')
            def fetch(path, auth=False): return {'team_id': 'wrong', 'team_name': 'Other'} if path == '/me' else {}
            with self.assertRaisesRegex(ValueError, 'Unexpected team'):
                module.check(ledger, fetch)
            self.assertIsNone(ledger.checkpoint('platform:status'))


if __name__ == '__main__': unittest.main()
