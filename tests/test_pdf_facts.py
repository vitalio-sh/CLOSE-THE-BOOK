from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import subprocess
from recon.pdf_facts import PdfFacts, extract_facts

class PdfFactTests(unittest.TestCase):
    def test_replacement_month_explicit(self):
        f=extract_facts('Esta factura anula y sustituye a la factura FV25-001 de 01/02/2025. Cuota de mantenimiento mes de mayo de 2025.')
        self.assertEqual(f['replaces_document_number'],'FV25-001')
        self.assertEqual(f['service_month'],'2025-05')
        self.assertNotIn('service_month',extract_facts('Invoice date 01/05/2025. Service labour (h)'))

    def test_partial_hints(self):
        f=extract_facts('Retention 10% (123,45 EUR) withheld until final acceptance (02/06/2026). Amount payable at due date: 1.111,05 EUR. Payment terms: 3 instalments at 30/60/90 days.')
        self.assertEqual(f['retention_pct'],'10')
        self.assertEqual(f['retention_release_date'],'2026-06-02')
        self.assertEqual(f['net_due'],'1111.05')
        self.assertEqual(f['instalments_count'],3)
        a=extract_facts('A deducir anticipo recibido según proforma PF25-01 de 100,50 EUR. Importe neto a pagar 200,50 EUR.')
        self.assertEqual(a['advance_amount'],'100.50')

    def test_iban_only_explicit_change(self):
        self.assertEqual(extract_facts('IBAN ES1234567890123456789012'),{})
        f=extract_facts('desde la cuenta ES1234567890123456789012, en sustitución de la anterior ES9834567890123456789012.')
        self.assertEqual(f['new_iban'],'ES1234567890123456789012')

    def context(self,root):
        items={i:{'item_id':i,'document_number':n,'entity':'ES01','party_id':'C1'} for i,n in [('old','FV25-001'),('new','FV25-002')]}
        return SimpleNamespace(phase_dir=root,history_dirs=[],items=items,by_number={'FV25001':['old'],'FV25002':['new']})

    def test_cached_bounded_resolution(self):
        with TemporaryDirectory() as d:
            root=Path(d);p=root/'pdfs';p.mkdir()
            (p/'new.pdf').write_bytes(b'fake')
            (p/'index.csv').write_text('file,document_type,document_number,entity,party_id,document_date\nnew.pdf,invoice,FV25-002,ES01,C1,2025-06-01\n')
            ctx=self.context(root)
            with patch('recon.pdf_facts.shutil.which',return_value='/bin/pdftotext'), patch('recon.pdf_facts.subprocess.run',return_value=SimpleNamespace(stdout='This invoice cancels and replaces invoice FV25-001 dated 01/01/2025.')) as run:
                f=PdfFacts(ctx,cache_dir=root/'cache',max_documents=1)
                self.assertEqual(f.resolve('old'),'new')
                self.assertEqual(f.resolve('old'),'new')
                self.assertEqual(run.call_count,1)
                g=PdfFacts(ctx,cache_dir=root/'cache',max_documents=0)
                self.assertEqual(g.resolve('old'),'new')
                self.assertEqual(run.call_count,1)
                disabled=PdfFacts(ctx,enabled=False)
                self.assertEqual(disabled.resolve('old'),'old')
                self.assertEqual(disabled.get('new'),{})

    def test_budget_without_cache(self):
        with TemporaryDirectory() as d:
            root=Path(d);p=root/'pdfs';p.mkdir()
            (p/'new.pdf').write_bytes(b'fake')
            (p/'index.csv').write_text('file,document_type,document_number,entity,party_id,document_date\nnew.pdf,invoice,FV25-002,ES01,C1,2025-06-01\n')
            f=PdfFacts(self.context(root),max_documents=0,cache_dir=root/'empty')
            self.assertEqual(f.resolve('old'),'old')
            self.assertEqual(f.stats['extractions'],0)

    def test_active_time_budget_and_remaining_timeout(self):
        with TemporaryDirectory() as d:
            root=Path(d);p=root/'pdfs';p.mkdir()
            for name in ('old','new'):(p/(name+'.pdf')).write_bytes(b'fake')
            (p/'index.csv').write_text('file,document_type,document_number,entity,party_id,document_date\nold.pdf,invoice,FV25-001,ES01,C1,2025-05-01\nnew.pdf,invoice,FV25-002,ES01,C1,2025-06-01\n')
            now=[100.0]
            def process(*args,**kwargs):
                if args[0][2].endswith('old.pdf'):
                    self.assertEqual(kwargs['timeout'],3)
                    now[0]+=2
                    return SimpleNamespace(stdout='month of May 2025')
                self.assertEqual(kwargs['timeout'],1)
                now[0]+=1
                raise subprocess.TimeoutExpired(args[0],kwargs['timeout'])
            with patch('recon.pdf_facts.time.monotonic',side_effect=lambda:now[0]), patch('recon.pdf_facts.shutil.which',return_value='/bin/pdftotext'), patch('recon.pdf_facts.subprocess.run',side_effect=process) as run:
                f=PdfFacts(self.context(root),cache_dir=root/'cache',max_seconds=3,timeout_seconds=3)
                now[0]+=1000  # Time before first use must not consume budget.
                self.assertEqual(f.get('old')['service_month'],'2025-05')
                now[0]+=1000  # Neither does time between accesses.
                self.assertEqual(f.get('new'),{})
                self.assertEqual(f.stats['active_seconds'],3)
                self.assertTrue(f.stats['time_budget_exhausted'])
                self.assertEqual(f.stats['timeouts'],1)
                f._memory.clear()
                self.assertEqual(f.get('old'),{})  # No cache read after expiry.
                self.assertEqual(f.stats['time_budget_skips'],1)
                self.assertEqual(run.call_count,2)

if __name__=='__main__':unittest.main()
