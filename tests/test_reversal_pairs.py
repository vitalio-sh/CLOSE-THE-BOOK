import unittest
from recon.reversal_pairs import bank_error_pairs
class ReversalPairTests(unittest.TestCase):
    def tx(self,i,a,text='',day='2025-01-10',account='a'):
        return dict(bank_txn_id=i,amount=a,text=text,booking_date=day,account_id=account,entity='ES01',currency='EUR')
    def test_both_legs_only_with_explicit_reversal(self):
        a=self.tx('a','100');b=self.tx('b','-100','ANULACION OPERACION INDEBIDA',day='2025-01-11')
        self.assertEqual(bank_error_pairs([a,b]),{'a','b'})
        b['text']='TRANSFER';self.assertFalse(bank_error_pairs([a,b]))
    def test_ambiguous_original_or_repeated_reversal(self):
        a=self.tx('a','100');b=self.tx('b','-100','ANULACION OPERACION INDEBIDA')
        self.assertFalse(bank_error_pairs([a,b,self.tx('aa','100')]))
        self.assertFalse(bank_error_pairs([a,b,self.tx('c','-100','ANULACION OPERACION INDEBIDA')]))
    def test_wrong_account_currency_or_date(self):
        a=self.tx('a','100');b=self.tx('b','-100','ANULACION OPERACION INDEBIDA',account='other')
        self.assertFalse(bank_error_pairs([a,b]));b['account_id']='a';b['currency']='USD'
        self.assertFalse(bank_error_pairs([a,b]));b['currency']='EUR';b['booking_date']='2025-01-25'
        self.assertFalse(bank_error_pairs([a,b]))

    def test_same_day_later_id_is_not_original(self):
        self.assertFalse(bank_error_pairs([self.tx('a','-100','ANULACION OPERACION INDEBIDA'),self.tx('b','100')]))

    def test_reversal_of_erroneous_wording(self):
        a=self.tx('a','100');b=self.tx('b','-100','REVERSAL OF ERRONEOUS TRANSFER',day='2025-01-11')
        self.assertEqual(bank_error_pairs([a,b]),{'a','b'})
        b['account_id']='other';self.assertFalse(bank_error_pairs([a,b]))
