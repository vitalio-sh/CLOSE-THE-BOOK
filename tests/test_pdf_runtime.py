import argparse,contextlib,csv,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from recon import pdf_runtime as P
from scripts.evaluate_window import prepare_pdf_inputs,input_hashes

class PdfRuntimeTests(unittest.TestCase):
    def args(self,r,*flags):
        p=argparse.ArgumentParser();P.add_options(p)
        return p.parse_args(['--pdf-facts','--pdf-cache-dir',str(r/'cache')]+list(flags))
    def ctx(self,r,n=1):
        folder=r/'data/train/pdfs';folder.mkdir(parents=True)
        rows=[];items={};numbers={}
        for i in range(n):
            (folder/(str(i)+'.pdf')).write_bytes(('pdf '+str(i)).encode())
            rows.append(dict(file=str(i)+'.pdf',document_number='INV'+str(i),entity='E',party_id='P',document_date='2024-01-01',document_type='invoice'))
            items[str(i)]={'entity':'E','party_id':'P'};numbers['INV'+str(i)]=[str(i)]
        with (folder/'index.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        return SimpleNamespace(phase_dir=r/'data/train',history_dirs=[],items=items,by_number=numbers)
    def create(self,c,a,out,text='MONTH OF MAY 2024'):
        with patch('recon.pdf_facts.shutil.which',return_value='/fake/pdftotext'),patch('recon.pdf_facts.subprocess.run',return_value=SimpleNamespace(stdout=text)):
            return P.configure(c,a,out)
    def test_absolute_cache_survives_cwd_change(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);c=self.ctx(r);a=self.args(r,'--require-pdf-complete')
            first=self.create(c,a,r/'a');cwd=Path.cwd();(r/'nested').mkdir()
            try:
                os.chdir(r/'nested');second=self.create(c,a,r/'b')
            finally:os.chdir(cwd)
            self.assertTrue(second.report['complete']);self.assertEqual(second.provider.stats['extractions'],0)
            self.assertEqual(second.provider.stats['cache_hits'],1)
            self.assertTrue(Path(second.report['config']['cache_dir']).is_absolute())
            self.assertEqual(first.report['facts_sha256'],second.report['facts_sha256'])
    def test_empty_parsed_facts_are_complete(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=self.create(self.ctx(r),self.args(r,'--require-pdf-complete'),r/'out','No target clause')
            self.assertTrue(s.report['complete']);self.assertEqual(s.report['loaded_sources'],1)
    def test_source_mutation_detected_and_cache_invalidated(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);c=self.ctx(r);a=self.args(r,'--require-pdf-complete');s=self.create(c,a,r/'out')
            (r/'data/train/pdfs/0.pdf').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'pdf_sources_changed'):s.finish()
            self.assertEqual(self.create(c,a,r/'other').provider.stats['extractions'],1)
    def test_caps_fail_with_diagnostic_report(self):
        for flag,value,reason,n in [('--pdf-max-documents','0','budget_skips',1),('--pdf-budget-seconds','0','time_budget',1),('--pdf-max-party-documents','1','party_or_notice_limit',2)]:
            with self.subTest(flag=flag),tempfile.TemporaryDirectory() as d:
                r=Path(d)
                with self.assertRaisesRegex(ValueError,reason):self.create(self.ctx(r,n),self.args(r,'--require-pdf-complete',flag,value),r/'out')
                self.assertFalse(json.loads((r/'out/pdf_readiness.json').read_text())['complete'])
    def test_optional_mode_reports_degradation(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=self.create(self.ctx(r),self.args(r,'--pdf-prewarm','--pdf-max-documents','0'),r/'out')
            self.assertIn('budget_skips',s.report['reasons'])
    def test_missing_backend_or_failed_extraction_blocks_strict(self):
        for missing in (True,False):
            with tempfile.TemporaryDirectory() as d:
                r=Path(d);c=self.ctx(r)
                with patch('recon.pdf_facts.shutil.which',return_value=None if missing else '/fake/pdftotext'),patch('recon.pdf_facts.subprocess.run',side_effect=OSError('failure')):
                    with self.assertRaisesRegex(ValueError,'missing_pdf_backend' if missing else 'failures'):
                        P.configure(c,self.args(r,'--require-pdf-complete'),r/'out')
    def test_dependent_flags_require_pdf_mode(self):
        p=argparse.ArgumentParser();P.add_options(p)
        with self.assertRaisesRegex(ValueError,'require --pdf-facts'):P.check_options(p.parse_args(['--require-pdf-complete']))
    def test_failed_release_preserves_existing_bundle(self):
        from test_release import fixture
        from recon.run import main
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);fixture(r);out=r/'old.gz';out.write_bytes(b'old artifact')
            with patch('recon.pdf_facts.shutil.which',return_value=None),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError,'PDF evidence incomplete'):
                    main(['--data',str(r),'--phase','sample','--modules','','--out',str(out),'--pdf-facts','--require-pdf-complete'])
            self.assertEqual(out.read_bytes(),b'old artifact');self.assertTrue((r/'old.gz.pdf/pdf_readiness.json').is_file())
    def test_temporal_partition_and_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);self.ctx(r,3);index=r/'data/train/pdfs/index.csv'
            with index.open() as f:rows=list(csv.DictReader(f))
            for row,day in zip(rows,['2024-09-30','2024-10-01','2024-11-01']):row['document_date']=day
            with index.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            meta=prepare_pdf_inputs(r/'data',r/'frozen','2024-10-01','2024-10-31');h=input_hashes(r/'frozen')
            self.assertEqual(meta['pdf_documents_copied'],2);self.assertTrue(meta['pdf_inputs_prepared'])
            self.assertIn('train/pdfs/0.pdf',h);self.assertIn('live/pdfs/1.pdf',h);self.assertFalse(any('2.pdf' in k for k in h))
    def test_temporal_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);self.ctx(r);(r/'data/train/pdfs/index.csv').write_text('file,document_date\n../outside.pdf,2024-01-01\n')
            with self.assertRaisesRegex(ValueError,'escapes'):prepare_pdf_inputs(r/'data',r/'frozen','2024-10-01','2024-10-31')

    def test_full_evaluator_pdf_parity_and_old_manifest_rejection(self):
        from scripts.evaluate_window import main
        from test_evaluate_window import write
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);self.ctx(r);src=r/'data';train=src/'train'
            write(src/'masters/categories.csv',[{'category':'UNIDENTIFIED'}],['category'])
            write(src/'masters/payment_terms.csv',[{'code':'T30','days':'30'}],['code','days'])
            write(src/'masters/customers.csv',[{'customer_id':'P','name':'Party','entity':'E'}],['customer_id','name','entity'])
            txn=dict(bank_txn_id='BT-2024-0000001',booking_date='2024-10-02',account_id='A',entity='E',amount='100',currency='EUR',text='INV0',counterparty_name='Party')
            write(train/'bank_transactions.csv.gz',[txn],list(txn))
            invoice=dict(item_id='ARI-2024-0000001',entity='E',customer_id='P',document_number='INV0',issue_date='2024-09-01',due_date='2024-10-01',total_amount='100',currency='EUR',payment_terms='T30')
            for name,rows in [('ar_invoices',[invoice]),('ap_invoices',[]),('credit_notes',[])]:write(train/(name+'.csv.gz'),rows,list(invoice))
            label=dict(bank_txn_id=txn['bank_txn_id'],item_id=invoice['item_id'],amount='100',exception='',category='',segment='ar_1to1')
            write(train/'golden_allocations.csv.gz',[label],list(label))
            write(train/'remittance_advices/index.csv',[],['file','received_date'])
            common=['--data',str(src),'--start','2024-10-01','--end','2024-10-31']
            with contextlib.redirect_stdout(io.StringIO()):
                baseline=main(common+['--out',str(r/'baseline'),'--prepare-pdf-inputs'])
                with patch('shutil.which',return_value='/fake/pdftotext'),patch('subprocess.run',return_value=SimpleNamespace(stdout='MONTH OF MAY 2024')):
                    candidate=main(common+['--out',str(r/'candidate'),'--prepared-input',str(r/'baseline/input'),'--pdf-facts','--require-pdf-complete','--pdf-cache-dir',str(r/'cache')])
            self.assertEqual(baseline['input_manifest_sha256'],candidate['input_manifest_sha256'])
            self.assertEqual(baseline['output_sha256'],candidate['output_sha256'])
            self.assertTrue(candidate['pdf_readiness']['complete'])
            self.assertEqual(candidate['pdf_readiness']['loaded_sources'],1)
            manifest=r/'baseline/input_manifest.json';m=json.loads(manifest.read_text());m['preparation'].pop('pdf_inputs_prepared');manifest.write_text(json.dumps(m))
            with self.assertRaisesRegex(ValueError,'lack PDF evidence'),contextlib.redirect_stdout(io.StringIO()):
                main(common+['--out',str(r/'rejected'),'--prepared-input',str(r/'baseline/input'),'--pdf-facts'])

    def test_provenance_tampering_blocks_verification(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=self.create(self.ctx(r),self.args(r,'--require-pdf-complete'),r/'out')
            P.verify_snapshot(s.report)
            f=r/'out/pdf_facts_snapshot.json';payload=json.loads(f.read_text());payload['facts']={};f.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError,'snapshot changed'):P.verify_snapshot(s.report)

    def test_optional_unread_sources_not_claimed_complete(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);s=self.create(self.ctx(r),self.args(r),r/'out')
            self.assertFalse(s.report['complete']);self.assertIn('unread_sources',s.report['reasons'])
