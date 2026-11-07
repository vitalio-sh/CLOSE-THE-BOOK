import unittest
from decimal import Decimal as D
from tests.test_matching import context, invoice, txn
from recon.matching import fragment_partial, _unwrapped, _fragment_tokens


def book():
    # One payer with many open invoices; numbers share prefixes like a real ledger.
    items = [invoice(f'I{n}', f'10624{n:05d}', str(100 + n)) for n in range(1, 40)]
    for n, item in enumerate(items):
        item['issue_date'] = f'2024-08-{1 + n % 28:02d}'
    return items


class FragmentPartialTests(unittest.TestCase):
    def test_unique_fragments_anchor_partial_set(self):
        # '400012' and '400027' each occur in one document number; '10624' and '00' are ambiguous.
        items = book()
        t = txn('5000', 'Alice Water Works INVOICE 10624 400012 400027 10624000 2400031')
        rows = fragment_partial(t, context(items))
        self.assertEqual({(r['item_id'], r['amount'], r['exception']) for r in rows},
                         {('I12', '112.00', ''), ('I27', '127.00', ''), ('I31', '131.00', '')})

    def test_requires_identified_payer(self):
        t = txn('5000', 'INVOICE 400012 400027 2400031')
        self.assertIsNone(fragment_partial(t, context(book())))

    def test_requires_a_printed_list(self):
        # A single printed fragment is a reference, not a list: other rules own it.
        self.assertIsNone(fragment_partial(txn('5000', 'Alice Water Works INVOICE 400012'), context(book())))
        # A list with one unique anchor and ambiguous rest: the anchor alone.
        rows = fragment_partial(txn('5000', 'Alice Water Works INV 400012 10624000 106240'), context(book()))
        self.assertEqual([r['item_id'] for r in rows], ['I12'])

    def test_fragment_left_after_anchors_is_anchored(self):
        items = book()
        # '1062400012' and '1062400013' are printed in full; '106240001' fits I10..I19,
        # '10624000' also I1..I9; with I10..I19 all quoted except I11, '106240001' names I11.
        quoted = ' '.join(f'106240001{n}' for n in range(0, 10) if n != 1)
        rows = fragment_partial(txn('5000', f'Alice Water Works INV {quoted} 106240001'), context(items))
        self.assertIn('I11', {r['item_id'] for r in rows})
        # The same list without the ambiguous fragment does not name I11.
        rows = fragment_partial(txn('5000', f'Alice Water Works INV {quoted}'), context(items))
        self.assertNotIn('I11', {r['item_id'] for r in rows})

    def test_cancelled_number_resolves_to_replacement(self):
        from types import SimpleNamespace
        items = book()
        c = context(items)
        c.pdf_facts = SimpleNamespace(resolve=lambda iid: 'I30' if iid == 'I12' else iid, get=lambda iid: {})
        rows = fragment_partial(txn('5000', 'Alice Water Works INVOICE 400012 400027'), c)
        self.assertEqual({r['item_id'] for r in rows}, {'I30', 'I27'})

    def test_anchors_must_not_exceed_cash(self):
        t = txn('200', 'Alice Water Works INVOICE 400012 400027 2400031')
        self.assertIsNone(fragment_partial(t, context(book())))

    def test_anchors_equal_to_cash_are_the_full_set(self):
        t = txn(str(112 + 127 + 131), 'Alice Water Works INVOICE 400012 400027 2400031')
        rows = fragment_partial(t, context(book()))
        self.assertEqual({r['item_id'] for r in rows}, {'I12', 'I27', 'I31'})

    def test_instructed_amount_and_rate_are_not_fragments(self):
        tokens = _fragment_tokens({'text': 'TT IN ACME /OCMT/USD12056,31/EXCH/6,941995 INV 108627'})
        self.assertEqual(tokens, ['108627'])

    def test_anchors_plus_fifo_remainder_give_full_set(self):
        items = book()
        # Cash = I12 + I27 + the oldest other open invoice I1 (101).
        t = txn(str(112 + 127 + 101), 'Alice Water Works INVOICE 400012 400027 10624')
        rows = fragment_partial(t, context(items))
        self.assertEqual({r['item_id'] for r in rows}, {'I12', 'I27', 'I1'})

    def test_closed_and_other_currency_documents_are_not_anchors(self):
        items = book()
        items[11]['open_amount'] = D(0)          # I12 already settled
        items[26]['currency'] = 'USD'            # I27 in another currency
        t = txn('5000', 'Alice Water Works INVOICE 400012 400027 2400031 400035 400038')
        rows = fragment_partial(t, context(items))
        self.assertEqual({r['item_id'] for r in rows}, {'I31', 'I35', 'I38'})

    def test_returns_and_outgoing_lines_ignored(self):
        self.assertIsNone(fragment_partial(txn('-5000', 'Alice Water Works 400012 400027 2400031'), context(book())))
        self.assertIsNone(fragment_partial(txn('5000', 'DEVOLUCION RECIBO Alice Water Works 400012 400027 2400031'), context(book())))

    def test_fx_receipt_uses_instructed_currency(self):
        items = book()
        for item in items:
            item['currency'] = 'USD'
        t = txn('90000', 'Alice Water Works INVOICE 400012 400027 2400031', orig_amount='5000', orig_currency='USD')
        t['currency'] = 'MXN'
        rows = fragment_partial(t, context(items))
        self.assertEqual({(r['item_id'], r['exception']) for r in rows},
                         {('I12', 'FX_DIFFERENCE'), ('I27', 'FX_DIFFERENCE'), ('I31', 'FX_DIFFERENCE')})
        # Without the instructed amount the USD documents are not this receipt's currency.
        t = txn('90000', 'Alice Water Works INVOICE 400012 400027 2400031'); t['currency'] = 'MXN'
        self.assertIsNone(fragment_partial(t, context(items)))

    def test_norma43_wrapped_concept_is_rejoined(self):
        text = 'TRANSFERENCIA DE FERRETERIA AZUL S.COO | P. | CONCEPTO: FACTURA FV24-05 FV24-05 FV24 | -050 FV24-0507608 FV24-050 FV24-050563 | 6 FV24-05'
        self.assertIn('S.COOP. CONCEPTO', _unwrapped(text))
        tokens = _fragment_tokens({'text': text})
        self.assertIn('FV240505636', tokens)
        self.assertNotIn('FV24050563', tokens)


if __name__ == '__main__':
    unittest.main()


class FragmentAdjustedCompletionTests(unittest.TestCase):
    def small_book(self):
        items = [invoice('A', '1062400012', '1000'), invoice('B', '1062400027', '500'), invoice('C', '1062400031', '300'),
                 invoice('E', '1062400044', '777.77'), invoice('G', '1062400058', '1234.56')]
        for n, item in enumerate(items):
            item['issue_date'] = f'2025-01-0{n + 1}'
        return items

    def test_withholding_completes_quoted_list(self):
        # POLICIES 3 SHORT_WITHHOLDING: (A + B + unquoted C) x 0.90 = 1620.00, one reading only.
        ctx = context(self.small_book())
        ctx.customers['C1']['country'] = 'MX'
        rows = fragment_partial(txn('1620.00', 'Alice Water Works FRA 1062400012 1062400027'), ctx)
        self.assertEqual({(r['item_id'], r['amount'], r['exception']) for r in rows},
                         {('A', '1000.00', 'SHORT_WITHHOLDING'), ('B', '500.00', 'SHORT_WITHHOLDING'),
                          ('C', '300.00', 'SHORT_WITHHOLDING')})
        # Outside MX/BR/MA the same cash is no withholding: the anchors stay a partial set.
        ctx.customers['C1']['country'] = 'ES'
        rows = fragment_partial(txn('1620.00', 'Alice Water Works FRA 1062400012 1062400027'), ctx)
        self.assertEqual({(r['item_id'], r['exception']) for r in rows}, {('A', ''), ('B', '')})

    def test_discount_completes_quoted_list(self):
        # POLICIES 3 SHORT_DISCOUNT: A + B in full plus C net of 2 % (300 - 6.00) within the window.
        items = self.small_book()
        items[2].update(payment_terms='D2', issue_date='2025-01-10')
        ctx = context(items)
        ctx.payment_terms = {'D2': {'early_payment_discount_pct': '2', 'discount_days': '10'}}
        rows = fragment_partial(txn('1794.00', 'Alice Water Works FRA 1062400012 1062400027'), ctx)
        self.assertEqual({(r['item_id'], r['amount'], r['exception']) for r in rows},
                         {('A', '1000.00', ''), ('B', '500.00', ''), ('C', '300.00', 'SHORT_DISCOUNT')})
