import re
import unittest
import unicodedata
from recon.reference_spans import classify_numeric_reference_span, numeric_reference_span_safe


class ReferenceSpanTests(unittest.TestCase):
    def check(self, raw, expected, known=None):
        match = re.search('50000', raw)
        self.assertIsNotNone(match)
        self.assertEqual(numeric_reference_span_safe(raw, *match.span(), known_reference_tokens=known), expected, raw)

    def test_unicode_currency_symbols(self):
        symbols = [chr(i) for i in range(0x110000) if unicodedata.category(chr(i)) == 'Sc']
        for symbol in symbols:
            for space in ('', ' ', '\u00a0', '\u202f'):
                self.check('INVOICES 50000'+space+symbol, False)
                self.check('INVOICES '+symbol+space+'50000', False)

    def test_monetary_runs_and_units(self):
        for suffix in (' ₹', ' ₺', ' R$', ' HK$', ' US$', ' COP', ' XAF', ' KRW',
                       ' Kč', ' zł', ' kr', ' cents', ' céntimos', ' centavos', ' centimes', ' pennies', ' pence', ' gross', ' netto', ' bruto', ' euros', ' dollars', ' pesos', ' рублей',
                       ' (EUR)', '[EUR]', ':EUR', ' - EUR', '/EUR', ';EUR', ',EUR', ' — EUR', ',0000 EUR', ',000000000INR',
                       ' ,00 EUR', ' . 00 EUR', ',1234567 %', ' ‰', ' ‱', ' BPS',
                       "'000.00", '\u202f000,00', '٫٠٠', '．００', ',٠٠', '.００', ' ０００ EUR'):
            self.check('INVOICES 50000'+suffix, False, {'50000','0000','1234567'})
        for prefix in ('EUR ', '(EUR) ', 'euros ', 'dólares ', '₹ '):
            self.check('INVOICES '+prefix+'50000', False)

    def test_whole_list_identity_proof_and_member_consistency(self):
        for separator in (',', ', ', ';', '; ', '/', ' / ', ' '):
            raw = 'INVOICES 50000'+separator+'60000'
            members = [m for m in re.finditer(r'\d+', raw)]
            spans = [classify_numeric_reference_span(raw, *m.span()) for m in members]
            self.assertEqual(spans[0], spans[1], raw)
            self.assertEqual(spans[0].kind, 'reference_list', raw)
            self.check(raw, False)
            self.check(raw, False, {'50000'})
            self.check(raw, True, {'50000','60000'})
            self.check(raw+' EUR', False, {'50000','60000'})
        self.check('INVOICES 50000,1234567', False, {'50000'})
        self.check('INVOICES 50000,1234567', True, {'50000','1234567'})

    def test_dates_and_invalid_original_offsets(self):
        for value in ('20250112','12012025','01122025','12/01/2025','2025-1-12','2025'):
            raw = 'INVOICES '+value
            for member in re.finditer(r'\d+', raw):
                self.assertFalse(numeric_reference_span_safe(raw, *member.span()), raw)
        for start,end in ((-1,5),(0,99),(0,0),(1,5),(0,4),(False,5)):
            self.assertFalse(numeric_reference_span_safe('50000',start,end))
        self.assertTrue(numeric_reference_span_safe('50000',0,5))
        for raw in ('INVOICES 50000 /REF BANK123','INVOICES 50000; INV 60000'):
            self.check(raw, True)
        self.assertFalse(numeric_reference_span_safe('FV25-50000',5,10))
