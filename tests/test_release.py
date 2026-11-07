import contextlib
import csv
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recon.context import Context
from recon.run import main, publish_bundle


def write(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with (gzip.open if path.suffix == '.gz' else open)(path, 'wt', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fixture(root):
    write(root/'masters/categories.csv', ['category'], [{'category': 'UNIDENTIFIED'}])
    write(root/'masters/payment_terms.csv', ['code', 'days'], [{'code': 'T30', 'days': '30'}])
    write(root/'masters/customers.csv', ['customer_id', 'name'], [{'customer_id': 'C1', 'name': 'Base'}])
    write(root/'sample/bank_transactions.csv.gz', ['bank_txn_id', 'booking_date', 'account_id', 'amount'],
          [{'bank_txn_id': 'T1', 'booking_date': '2025-01-01', 'account_id': 'A1', 'amount': '100'}])


class ReleaseTests(unittest.TestCase):
    def test_misspelled_module_cannot_publish_all_unidentified(self):
        with self.assertRaisesRegex(ValueError, 'Unknown reconciliation modules'):
            main(['--data', '/nonexistent-input', '--out', '/unused-output', '--modules', 'matcing'])

    def run_fixture(self, root, out):
        with contextlib.redirect_stdout(io.StringIO()):
            return main(['--data', str(root), '--phase', 'sample', '--modules', '', '--out', str(out)])

    def test_final_file_override_and_base_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            write(root/'final/masters_final/customers.csv', ['customer_id', 'name'],
                  [{'customer_id': 'C2', 'name': 'Final'}])
            ctx = Context(root, 'final')
            self.assertEqual(set(ctx.customers), {'C2'})
            self.assertEqual(ctx.payment_terms['T30']['days'], '30')
            self.assertEqual(ctx.categories, {'UNIDENTIFIED'})
            self.assertEqual(ctx.master_coverage['customers']['source'], 'final')
            self.assertEqual(ctx.master_coverage['categories']['source'], 'base')

    def test_final_keeps_previous_master_names_and_ibans_as_aliases(self):
        # POLICIES 5.5/6: renamed customers and new bank details; early FINAL lines carry the old ones.
        from recon.matching import party_candidates
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            write(root/'masters/customers.csv', ['customer_id', 'name', 'iban'],
                  [{'customer_id': 'C1', 'name': 'Iberia Logistica', 'iban': 'ES9100000000000000000001'}])
            write(root/'final/masters_final/customers.csv', ['customer_id', 'name', 'iban'],
                  [{'customer_id': 'C1', 'name': 'Nova Transportes Global', 'iban': 'ES9100000000000000000002'}])
            ctx = Context(root, 'final')
            self.assertEqual(ctx.customers['C1']['name'], 'Nova Transportes Global')
            self.assertEqual(party_candidates({'text': 'TRANSFERENCIA DE IBERIA LOGISTICA'}, ctx), ['C1'])
            self.assertEqual(party_candidates({'text': 'X', 'counterparty_account': 'ES9100000000000000000001'}, ctx), ['C1'])
            self.assertEqual(party_candidates({'text': 'TRANSFERENCIA NOVA TRANSPORTES GLOBAL'}, ctx), ['C1'])

    def test_empty_override_fails_instead_of_silently_using_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            write(root/'final/masters_final/payment_terms.csv', ['code'], [])
            with self.assertRaisesRegex(ValueError, 'nonempty'):
                Context(root, 'final')

    def test_opening_term_enrichment_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            fields = ['item_id', 'document_number', 'party_id', 'total_amount', 'open_amount']
            rows = [dict(zip(fields, ['ARI-1', 'INV1', 'C1', '100', '40'])),
                    dict(zip(fields, ['ARI-2', 'INV2', 'C1', '100', '80']))]
            write(root/'final/open_items_at_start.csv.gz', fields, rows)
            enriched = {**rows[0], 'payment_terms': 'T30'}
            del enriched['open_amount']
            write(root/'train/ar_invoices.csv.gz', list(enriched), [enriched])
            ctx = Context(root, 'final')
            self.assertEqual(ctx.opening_coverage['ar_with_known_payment_terms'], 1)
            self.assertEqual(ctx.opening_coverage['ar_payment_terms_fraction'], 0.5)
            self.assertEqual(ctx.items['ARI-1']['open_amount'], 40)

    def test_repeated_run_is_filename_independent_and_publishes_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            a, b = root/'a.csv.gz', root/'b.csv.gz'
            first = self.run_fixture(root, a)
            second = self.run_fixture(root, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(a.read_bytes()[4:8], bytes(4))
            self.assertEqual(first['sha256'], second['sha256'])
            self.assertTrue(first['official_structure']['ok'])
            self.assertEqual(first['validation']['covered'], 1)
            self.assertEqual(json.loads(Path(str(a)+'.report.json').read_text())['publication'], 'validated')
            self.assertEqual(len(Path(str(a)+'.audit.jsonl').read_text().splitlines()), 1)

    def test_rejected_or_source_changed_run_preserves_previous_bundle(self):
        for failure in ('validation', 'source'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture(root)
                out = root/'output.csv.gz'
                self.run_fixture(root, out)
                previous = {s: Path(str(out)+s).read_bytes() for s in ('', '.audit.jsonl', '.report.json')}
                override = (patch('recon.run.validate', return_value={'ok': False, 'errors': ['injected']})
                            if failure == 'validation' else
                            patch('recon.run.source_hashes', side_effect=[{'run.py': 'old'}, {'run.py': 'changed'}]))
                with override, self.assertRaises(SystemExit):
                    self.run_fixture(root, out)
                for suffix, content in previous.items():
                    self.assertEqual(Path(str(out)+suffix).read_bytes(), content)
                diagnostic = json.loads(Path(str(out)+'.failed.report.json').read_text())
                self.assertEqual(diagnostic['publication'], 'rejected')

    def test_publication_error_rolls_back_already_replaced_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root/'staging'
            staging.mkdir()
            candidate, out = staging/'a.csv.gz', root/'a.csv.gz'
            for suffix in ('', '.audit.jsonl', '.report.json'):
                Path(str(candidate)+suffix).write_text('new'+suffix)
                Path(str(out)+suffix).write_text('old'+suffix)
            import os
            original = os.replace
            def fail_report(src, dst):
                if str(src).endswith('.report.json'):
                    raise OSError('injected publication error')
                original(src, dst)
            with patch('recon.run.os.replace', side_effect=fail_report), self.assertRaises(OSError):
                publish_bundle(candidate, out)
            for suffix in ('', '.audit.jsonl', '.report.json'):
                self.assertEqual(Path(str(out)+suffix).read_text(), 'old'+suffix)

    def test_input_mutation_during_inference_preserves_previous_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            out = root / 'output.csv.gz'
            self.run_fixture(root, out)
            old = {s: Path(str(out)+s).read_bytes() for s in ('', '.audit.jsonl', '.report.json')}
            from recon.engine import execute
            def mutate_after_inference(*args, **kwargs):
                result = execute(*args, **kwargs)
                # Keep ID coverage valid: only provenance can detect this change.
                write(root/'sample/bank_transactions.csv.gz', ['bank_txn_id', 'booking_date', 'account_id', 'amount'],
                      [{'bank_txn_id': 'T1', 'booking_date': '2025-01-02', 'account_id': 'A1', 'amount': '100'}])
                return result
            with patch('recon.run.execute', side_effect=mutate_after_inference), self.assertRaises(SystemExit):
                self.run_fixture(root, out)
            for suffix, content in old.items():
                self.assertEqual(Path(str(out)+suffix).read_bytes(), content)
            report = json.loads(Path(str(out)+'.failed.report.json').read_text())
            self.assertEqual(report['checked_inputs_changed'], ['sample/bank_transactions.csv.gz'])


if __name__ == '__main__':
    unittest.main()
