import csv
import gzip
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from scripts.evaluate_window import prepare_inputs, prior_balance, read


def write(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'wt', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


class TemporalInputTests(unittest.TestCase):
    def test_prior_balance_matches_capped_apply_return_duplicate(self):
        self.assertEqual(prior_balance('100', [('120',''), ('20','RETURNED'), ('20','DUPLICATE_PAYMENT')]), 20)

    def test_opening_phase_history_and_target_label_isolation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); source = root/'source'; train = source/'train'
            (source/'masters').mkdir(parents=True)
            (source/'masters'/'example.csv').write_text('id\n1\n')
            transactions = [dict(bank_txn_id='before', booking_date='2024-09-01'),
                            dict(bank_txn_id='target', booking_date='2024-10-02'),
                            dict(bank_txn_id='future', booking_date='2024-11-01')]
            write(train/'bank_transactions.csv.gz', transactions, list(transactions[0]))
            fields = ['item_id','entity','customer_id','document_number','issue_date','total_amount','currency']
            rows = [dict(zip(fields, values)) for values in [
                ('ARI-2024-0000001','E','C','old-open','2024-09-01','100','EUR'),
                ('ARI-2024-0000002','E','C','old-closed','2024-09-01','50','EUR'),
                ('ARI-2024-0000003','E','C','new','2024-10-01','30','EUR'),
                ('ARI-2024-0000004','E','C','future','2024-11-01','40','EUR')]]
            write(train/'ar_invoices.csv.gz', rows, fields)
            # Empty source tables preserve their schema in both phase partitions.
            write(train/'ap_invoices.csv.gz', [], fields)
            write(train/'credit_notes.csv.gz', [], fields)
            labels = [dict(bank_txn_id='before',item_id=rows[0]['item_id'],amount='40',exception='PARTIAL'),
                      dict(bank_txn_id='before',item_id=rows[1]['item_id'],amount='50',exception=''),
                      dict(bank_txn_id='target',item_id=rows[0]['item_id'],amount='999999',exception='')]
            write(train/'golden_allocations.csv.gz', labels, list(labels[0]))
            write(train/'remittance_advices/index.csv', [], ['file','received_date'])
            first = root/'a'
            manifest = prepare_inputs(source, first, '2024-10-01','2024-10-31')
            opening = list(read(first/'live/open_items_at_start.csv.gz'))
            self.assertEqual([(row['document_number'], row['open_amount']) for row in opening], [('old-open','60')])
            self.assertEqual([row['document_number'] for row in read(first/'live/ar_invoices.csv.gz')], ['new'])
            self.assertEqual([row['document_number'] for row in read(first/'train/ar_invoices.csv.gz')], ['old-open','old-closed'])
            self.assertEqual(manifest['prior_allocations'], 2)
            labels[-1].update(amount='0', exception='RETURNED')
            write(train/'golden_allocations.csv.gz', labels, list(labels[0]))
            second = root/'b'
            prepare_inputs(source, second, '2024-10-01','2024-10-31')
            for path in first.rglob('*.gz'):
                self.assertEqual(gzip.decompress(path.read_bytes()), gzip.decompress((second/path.relative_to(first)).read_bytes()))


if __name__ == '__main__':
    unittest.main()
