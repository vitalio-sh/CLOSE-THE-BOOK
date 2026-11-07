import csv
import gzip
import tempfile
import unittest
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from recon.history import enrich_referenced_closed, reference_tokens

class HistoryTests(unittest.TestCase):
    def context(self, root, phase='live'):
        p=Path(root);(p/phase).mkdir();(p/'train').mkdir(exist_ok=True)
        (p/phase/'open_items_at_start.csv.gz').touch()
        ctx=SimpleNamespace(phase_dir=p/phase,history_dirs=[p/'train'],items={},by_number=defaultdict(list),by_party=defaultdict(list))
        def add(r):
            r=dict(r);r['party_id']=r.get('customer_id') or r.get('supplier_id') or r.get('party_id');ctx.items[r['item_id']]=r
        ctx.add_item=add
        return ctx
    def write(self,ctx,rows):
        with gzip.open(ctx.history_dirs[0]/'ar_invoices.csv.gz','wt',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    def record(self,iid='ARI-2024-1',num='FV24-00001',date='2024-06-01',entity='ES01'):
        return dict(item_id=iid,document_number=num,issue_date=date,entity=entity,customer_id='C1',total_amount='100')
    def txn(self,text='PAGO FV24-00001',entity='ES01'):
        return dict(text=text,booking_date='2025-07-01',entity=entity)
    def test_closed_restoration_and_existing_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d);self.write(c,[self.record(),self.record('ARI-2024-2','FV24-00002')]);c.items['ARI-2024-2']={'open_amount':Decimal('25')}
            r=enrich_referenced_closed(c,[self.txn('FV24-00001 FV24-00002')])
            self.assertEqual(r['added_count'],1);self.assertEqual(c.items['ARI-2024-1']['open_amount'],Decimal(0));self.assertEqual(c.items['ARI-2024-2']['open_amount'],Decimal(25))
            self.assertEqual(c.by_number['FV2400001'],['ARI-2024-1'])
            self.assertEqual(enrich_referenced_closed(c,[self.txn()])['added_count'],0)
    def test_skip_sample(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d,'sample');self.write(c,[self.record()]);self.assertEqual(enrich_referenced_closed(c,[self.txn()])['added_count'],0)
    def test_no_future_no_other_entity_no_partial_reference(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d);self.write(c,[self.record(date='2025-07-02'),self.record('I2','FV24-00002',entity='FR01'),self.record('I3','FV24-00003')])
            r=enrich_referenced_closed(c,[self.txn('FV24-00001 FV24-00002 FV24-000')]);self.assertEqual(r['added_count'],0)
    def test_numeric_requires_cue_when_short(self):
        self.assertIn('2814',reference_tokens(self.txn('FRA 2814')))
        self.assertNotIn('2814',reference_tokens(self.txn('PAYMENT 2814')))
    def test_swift_tags_preserve_document_slashes(self):
        self.assertIn('FV250001',reference_tokens(self.txn('/REMI/FV25/0001/EREF/NOTPROVIDED')))
    def test_missing_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            c=self.context(d);(c.phase_dir/'open_items_at_start.csv.gz').unlink()
            self.assertIn('Missing',enrich_referenced_closed(c,[self.txn()])['skipped_reason'])
if __name__=='__main__':unittest.main()
