import csv
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.build_dashboard_data import Examples, export_dashboard


class DashboardExportTests(unittest.TestCase):
    def write_csv(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'wt', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def fixture(self, root):
        data = root / 'data'
        submission = root / 'submission.csv.gz'
        self.write_csv(submission, [
            {'bank_txn_id': 'B1', 'item_id': 'I1', 'amount': '100.00', 'category': '', 'exception': ''},
            {'bank_txn_id': 'B2', 'item_id': '', 'amount': '', 'category': 'BANK_FEES', 'exception': ''},
            {'bank_txn_id': 'B3', 'item_id': '', 'amount': '', 'category': 'UNIDENTIFIED', 'exception': 'UNIDENTIFIED'},
        ])
        banks = []
        for identifier, amount, currency in [('B1', '100.00', 'EUR'), ('B2', '-5.25', 'USD'), ('B3', '9.00', 'EUR')]:
            banks.append({'bank_txn_id': identifier, 'booking_date': '2026-11-01', 'amount': amount,
                'currency': currency, 'entity': 'E1', 'counterparty_name': 'Example customer', 'text': 'Raw evidence ' + identifier})
        self.write_csv(data / 'live/bank_transactions.csv.gz', banks)
        self.write_csv(data / 'train/ar_invoices.csv.gz', [{'item_id': 'I1', 'document_number': 'INV-1',
            'customer_id': 'C1', 'currency': 'EUR', 'total_amount': '100.00', 'issue_date': '2026-10-01', 'entity': 'E1'}])
        self.write_csv(data / 'masters/customers.csv', [{'customer_id': 'C1', 'name': 'Master customer'}])
        self.write_csv(data / 'masters/entities.csv', [{'entity': 'E1', 'legal_name': 'Example entity'}])
        counts = {'matching': 1, 'category': 1, 'unidentified': 1}
        Path(str(submission) + '.report.json').write_text(json.dumps({'phase': 'live', 'modules': counts,
            'sha256': hashlib.sha256(submission.read_bytes()).hexdigest(), 'seconds': 1.5, 'validation': {'ok': True}}))
        Path(str(submission) + '.audit.jsonl').write_text('\n'.join(json.dumps({'bank_txn_id': bid, 'module': module})
            for bid, module in [('B1', 'matching'), ('B2', 'category'), ('B3', 'unidentified')]))
        return submission, data

    def test_real_evidence_currency_separation_and_historical_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            submission, data = self.fixture(Path(folder))
            result = export_dashboard(submission, data, 3)
            self.assertEqual(result['summary']['outcomes'], {'matched': 1, 'categorized': 1, 'unidentified': 1})
            currencies = {r['currency']: r for r in result['summary']['currencies']}
            self.assertEqual(currencies['EUR']['net'], '109.00')
            self.assertEqual(currencies['USD']['net'], '-5.25')
            self.assertEqual(currencies['USD']['outflow'], '5.25')
            first = next(t for t in result['transactions'] if t['id'] == 'B1')
            self.assertEqual(first['description'], 'Raw evidence B1')
            self.assertEqual(first['allocations'][0]['invoice_number'], 'INV-1')
            self.assertEqual(first['allocations'][0]['party'], 'Master customer')
            self.assertIsNone(result['pipeline']['live_accuracy'])
            self.assertTrue(result['pipeline']['hash_verified'])
            self.assertTrue(result['export_checks']['module_counts_match_report'])
            self.assertEqual(result['sample']['missing_invoice_metadata'], 0)

    def test_mismatched_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            submission, data = self.fixture(Path(folder))
            report = Path(str(submission) + '.report.json')
            report.write_text(json.dumps({'phase': 'live', 'sha256': 'wrong'}))
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                export_dashboard(submission, data)

    def test_selection_is_order_independent_and_keeps_rare_exception(self):
        forward, backward = Examples(30), Examples(30)
        records = [(str(n), 'matching', 'matched', ['RETURNED'] if n == 42 else [], [{'item_id': str(n)}]) for n in range(100)]
        for record in records:
            forward.add(*record)
        for record in reversed(records):
            backward.add(*record)
        self.assertEqual(set(forward.selected()), set(backward.selected()))
        self.assertEqual(len(forward.selected()), 30)
        self.assertIn('42', forward.selected())


if __name__ == '__main__':
    unittest.main()
