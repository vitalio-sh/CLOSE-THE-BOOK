import unittest
from test_matching import context,invoice,txn
from recon.financial_references import finance_reference_sets
class FinanceReferenceTests(unittest.TestCase):
    def setup(self):return context([invoice('A','50000','100'),invoice('B','FV25-60000','1000')])
    def test_monetary_date_batch_tokens_not_numeric_document(self):
        for text in ['TOTAL 50000','INVOICES 500.00','INVOICES 50000,00','INVOICES 50000 EUR','INVOICES EUR 50000','INVOICES 50000€','BATCH 50000','RUN 50000','INVOICES 20250115']:
            self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works '+text),self.setup())['all'],set(),text)
    def test_typed_numeric_exact_and_list_suffix(self):
        c=self.setup();self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works INVOICE 50000'),c)['exact_documents'],{'A'})
        self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works FV25-60000 50000'),c)['all'],{'A','B'})
    def test_longer_id_does_not_match_shorter_and_context_stops(self):
        c=context([invoice('A','FV25-50000'),invoice('B','FV25-60000')])
        self.assertEqual(finance_reference_sets(txn('90','Alice Water Works FV25-500009'),c)['all'],set())
        self.assertEqual(finance_reference_sets(txn('90','Alice Water Works FV25-60000 TOTAL 50000'),c)['all'],{'B'})
    def test_context_not_carried_between_fields(self):
        self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works INVOICES',ref1='50000'),self.setup())['all'],set())
    def test_closed_and_credit_competitors_preserved(self):
        c=context([invoice('A','FV25-50000'),invoice('B','RE25-50000')]);c.items['B']['open_amount']=0;c.items['B']['kind']='credit'
        self.assertEqual(finance_reference_sets(txn('90','Alice Water Works INVOICE 50000'),c)['all'],{'A','B'})
    def test_bounded_po_token_not_substring(self):
        c=self.setup();c.items['B']['customer_po']='PO223344'
        self.assertEqual(finance_reference_sets(txn('90','Alice Water Works PO223344'),c)['all'],{'B'})
        self.assertEqual(finance_reference_sets(txn('90','Alice Water Works PO2233449'),c)['all'],set())
    def test_actual_numeric_dates_never_become_invoice_anchors(self):
        for number in ['20250115','15012025']:
            c=context([invoice('A',number)])
            self.assertEqual(finance_reference_sets(txn('90','Alice Water Works INVOICE '+number),c)['all'],set())
    def test_unicode_currency_and_spaced_decimal_are_not_anchors(self):
        for suffix in [' ₹',' ₺',' R$',' HK$',' US$',' ,00 EUR',' .00 EUR']:
            self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works INVOICES 50000'+suffix),self.setup())['all'],set(),suffix)
        self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works INVOICES 50000, 60000'),self.setup())['all'],{'A','B'})
    def test_complete_money_and_percent_span_matrix(self):
        for field in ['text','ref1','ref2','remittance_info','end_to_end_id']:
            for suffix in [',0000 EUR',',00000 EUR',',000000000 INR',' , 0000 EUR','.000000 EUR',' %',' PCT',' percent',' BPS',' INR',' ₿']:
                tx=txn('1100','Alice Water Works');tx[field]=('Alice Water Works ' if field=='text' else '')+'INVOICE 50000'+suffix
                self.assertEqual(finance_reference_sets(tx,self.setup())['all'],set(),(field,suffix))
        for references in ['INVOICES 50000,60000','INVOICES 50000, 60000','INVOICES 50000 ; 60000']:
            self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works '+references),self.setup())['all'],{'A','B'})
    def test_comma_run_requires_every_identity_and_closed_uniqueness(self):
        for text in ['INVOICES 50000,1234567','INVOICES 1234567,50000']:
            self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works '+text),self.setup())['all'],set(),text)
        c=context([invoice('A','50000'),invoice('B','FV25-60000'),invoice('C','RE25-60000')]);c.items['C']['open_amount']=0
        self.assertEqual(finance_reference_sets(txn('1100','Alice Water Works INVOICES 50000,60000'),c)['all'],set())
