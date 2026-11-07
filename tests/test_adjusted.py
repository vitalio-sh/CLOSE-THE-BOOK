import unittest
from decimal import Decimal as D
from test_matching import invoice, txn, context
from recon.adjusted import single_adjusted, explain_set
from recon.matching import reconcile

TERMS = {'D2': {'early_payment_discount_pct': '2', 'discount_days': '10'}}


def ctx(items, country='ES'):
    c = context(items)
    c.payment_terms = TERMS
    c.customers = {'C1': {'name': 'Alice Water Works', 'country': country, 'entity': 'ES01'}}
    return c


class SingleAdjustedTests(unittest.TestCase):
    def test_late_discount_is_partial_for_the_cash(self):
        # POLICIES 3 PARTIAL: discount taken after the deadline; booked 14 days after issue.
        c = ctx([invoice(amount='5682.25', payment_terms='D2')])
        rows = single_adjusted(txn('5568.60', 'Alice Water Works PAYMENT'), c)
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows], [('I1', '5568.60', 'PARTIAL')])

    def test_discount_in_terms_clears_full_amount(self):
        c = ctx([invoice(amount='100', payment_terms='D2')])
        t = txn('98', 'Alice Water Works PAYMENT'); t['booking_date'] = '2025-01-12'
        self.assertEqual([(r['amount'], r['exception']) for r in single_adjusted(t, c)], [('100.00', 'SHORT_DISCOUNT')])

    def test_withholding_only_for_withholding_countries(self):
        t = txn('90', 'Alice Water Works PAYMENT')
        self.assertEqual(single_adjusted(t, ctx([invoice()], 'MX'))[0]['exception'], 'SHORT_WITHHOLDING')
        self.assertIsNone(single_adjusted(t, ctx([invoice()], 'ES')))

    def test_fx_with_withholding_is_fx_difference(self):
        t = txn('1500', 'Alice Water Works PAYMENT', orig_amount='90', orig_currency='EUR'); t['currency'] = 'MXN'
        rows = single_adjusted(t, ctx([invoice()], 'MX'))
        self.assertEqual([(r['amount'], r['exception']) for r in rows], [('100.00', 'FX_DIFFERENCE')])

    def test_ambiguous_or_unidentified_abstains(self):
        c = ctx([invoice(payment_terms='D2'), invoice('I2', 'FV25-00002', payment_terms='D2')])
        self.assertIsNone(single_adjusted(txn('98', 'Alice Water Works PAYMENT'), c))
        self.assertIsNone(single_adjusted(txn('98', 'Unknown payer'), ctx([invoice(payment_terms='D2')])))

    def test_touched_invoice_has_no_discount_signature(self):
        c = ctx([invoice(payment_terms='D2')]); c.items['I1']['open_amount'] = D('99')
        self.assertIsNone(single_adjusted(txn('98', 'Alice Water Works PAYMENT'), c))


class NamesakeTests(unittest.TestCase):
    def ctx(self, count):
        items = []
        c = None
        for k in range(count):
            item = invoice('I%02d' % k, 'F%02d' % k, str(100 + k), payment_terms='D2')
            item['party_id'] = 'P%02d' % k
            item['issue_date'] = '2025-01-10'
            items.append(item)
        c = context(items)
        c.payment_terms = TERMS
        c.customers = {'P%02d' % k: {'name': 'Port Ellery Pool Builders Pty Ltd', 'country': 'AU', 'entity': 'ES01'}
                       for k in range(count)}
        return c

    def test_unique_discount_among_many_namesakes(self):
        # 20 same-named customers; only P03's invoice (103.00) net of 2 % gives 100.94.
        t = txn('100.94', 'DIRECT CREDIT PORT ELLERY POOL BUI', counterparty_name='Port Ellery Pool Builders Pty Ltd')
        rows = single_adjusted(t, self.ctx(20))
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows], [('I03', '103.00', 'SHORT_DISCOUNT')])

    def test_namesake_paid_in_full_competes(self):
        c = self.ctx(20)
        extra = invoice('X', 'FX1', '100.94'); extra['party_id'] = 'P19'; extra['issue_date'] = '2025-01-10'
        c.items['X'] = extra; c.by_party.setdefault('P19', []).append('X')
        t = txn('100.94', 'DIRECT CREDIT PORT ELLERY POOL BUI', counterparty_name='Port Ellery Pool Builders Pty Ltd')
        self.assertIsNone(single_adjusted(t, c))


class DiscountedRestTests(unittest.TestCase):
    def test_quoted_invoice_plus_unquoted_discounted_newest(self):
        a = invoice('A', 'FV25-00001', '100'); b = invoice('B', 'FV25-00002', '50', payment_terms='D2')
        b['issue_date'] = '2025-01-10'
        rows = reconcile(txn('149', 'Alice Water Works FV25-00001'), ctx([a, b]))
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('100.00', ''), 'B': ('50.00', 'SHORT_DISCOUNT')})

    def test_two_discounted_candidates_abstain_from_the_fill(self):
        a = invoice('A', 'FV25-00001', '100'); b = invoice('B', 'FV25-00002', '50', payment_terms='D2')
        c = invoice('C', 'FV25-00003', '50', payment_terms='D2')
        b['issue_date'] = c['issue_date'] = '2025-01-10'
        rows = reconcile(txn('149', 'Alice Water Works FV25-00001'), ctx([a, b, c])) or []
        self.assertNotIn('SHORT_DISCOUNT', {r['exception'] for r in rows})


class WithheldRestTests(unittest.TestCase):
    def test_anchors_and_rest_all_net_of_one_rate(self):
        from recon.adjusted import withheld_rest
        a = invoice('A', 'F1', '1000'); b = invoice('B', 'F2', '500'); c = invoice('C', 'F3', '333.33')
        cx = ctx([a, b, c], 'BR')
        # 10 %: 900.00 + 450.00 = 1350.00 (C not paid)
        self.assertEqual([i['item_id'] for i in withheld_rest(txn('1350'), cx, [a], [b, c], D('1350'), False)], ['B'])

    def test_non_withholding_country_or_ambiguous_abstains(self):
        from recon.adjusted import withheld_rest
        a = invoice('A', 'F1', '1000'); b = invoice('B', 'F2', '500'); c = invoice('C', 'F3', '500')
        self.assertIsNone(withheld_rest(txn('1350'), ctx([a, b], 'ES'), [a], [b], D('1350'), False))
        self.assertIsNone(withheld_rest(txn('1350'), ctx([a, b, c], 'BR'), [a], [b, c], D('1350'), False))


class QuotedSetTests(unittest.TestCase):
    def test_quoted_fx_invoices_sum_to_original_amount(self):
        items = [invoice('A', 'FV25-00001', '60'), invoice('B', 'FV25-00002', '40'), invoice('C', 'FV25-00003', '17')]
        t = txn('90', 'Alice Water Works FV25-00001 FV25-00002', orig_amount='100', orig_currency='EUR'); t['currency'] = 'USD'
        rows = reconcile(t, ctx(items))
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('60.00', 'FX_DIFFERENCE'), 'B': ('40.00', 'FX_DIFFERENCE')})

    def test_quoted_invoices_with_one_late_discount(self):
        items = [invoice('A', 'FV25-00001', '100', payment_terms='D2'), invoice('B', 'FV25-00002', '50')]
        items[0]['issue_date'] = '2024-12-20'   # older than B: no group grace
        rows = reconcile(txn('148', 'Alice Water Works FV25-00001 FV25-00002'), ctx(items))
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('98.00', 'PARTIAL'), 'B': ('50.00', '')})

    def test_newest_invoice_of_a_group_keeps_discount_ten_more_days(self):
        items = [invoice('A', 'FV25-00001', '100', payment_terms='D2'), invoice('B', 'FV25-00002', '50')]
        items[1]['issue_date'] = '2024-12-20'
        rows = reconcile(txn('148', 'Alice Water Works FV25-00001 FV25-00002'), ctx(items))   # 14 days: deadline + 1
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('100.00', 'SHORT_DISCOUNT'), 'B': ('50.00', '')})
        t = txn('148', 'Alice Water Works FV25-00001 FV25-00002'); t['booking_date'] = '2025-01-25'
        self.assertEqual({r['item_id']: r['exception'] for r in reconcile(t, ctx(items))}, {'A': 'PARTIAL', 'B': ''})

    def test_quoted_withholding_with_netted_credit(self):
        a = invoice('A', 'FV25-00001', '100'); cr = invoice('CR', 'AB25-00001', '20'); cr['kind'] = 'credit'
        rows = explain_set(txn('70'), ctx([a, cr], 'MX'), [a], [cr])
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('100.00', 'SHORT_WITHHOLDING'), 'CR': ('20.00', '')})

    def test_fx_gap_fill(self):
        items = [invoice('A', 'FV25-00001', '60'), invoice('B', 'FV25-00002', '40'), invoice('C', 'FV25-00003', '17')]
        items[1]['issue_date'] = '2024-12-01'
        t = txn('90', 'Alice Water Works FV25-00001', orig_amount='100', orig_currency='EUR'); t['currency'] = 'USD'
        rows = reconcile(t, ctx(items))
        self.assertEqual({r['item_id']: r['exception'] for r in rows}, {'A': 'FX_DIFFERENCE', 'B': 'FX_DIFFERENCE'})


if __name__ == '__main__':
    unittest.main()


class TruncatedReferenceTests(unittest.TestCase):
    def test_cut_number_with_short_cash_is_partial(self):
        from recon.adjusted import truncated_reference
        items = [invoice('A', 'INV24-0115652', '500'), invoice('B', 'INV24-0221000', '300')]
        rows = truncated_reference(txn('200', 'Alice Water Works INV INV24-0115'), ctx(items))
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows], [('A', '200.00', 'PARTIAL')])

    def test_bridging_credit_note_is_netted(self):
        from recon.adjusted import truncated_reference
        cr = invoice('CR', 'CRN24-0100001', '50'); cr['kind'] = 'credit'
        rows = truncated_reference(txn('450', 'Alice Water Works INV INV24-0115'), ctx([invoice('A', 'INV24-0115652', '500'), cr]))
        self.assertEqual({r['item_id']: r['amount'] for r in rows}, {'A': '500.00', 'CR': '50.00'})

    def test_ambiguous_prefix_or_full_number_abstains(self):
        from recon.adjusted import truncated_reference
        items = [invoice('A', 'INV24-0115652', '500'), invoice('B', 'INV24-0115999', '300')]
        self.assertIsNone(truncated_reference(txn('200', 'Alice Water Works INV INV24-0115'), ctx(items)))
        items = [invoice('A', 'INV24-0115652', '500'), invoice('B', 'INV24-0221000', '300')]
        self.assertIsNone(truncated_reference(txn('200', 'Alice Water Works INV24-0221000 INV24-0115'), ctx(items)))


class AdjustedSetTests(unittest.TestCase):
    def test_one_late_discount_among_full_invoices(self):
        from recon.adjusted import adjusted_sets
        items = [invoice('A', 'F1', '100', payment_terms='D2'), invoice('B', 'F2', '55.55'), invoice('C', 'F3', '31.17')]
        items[0]['issue_date'] = '2024-12-20'
        rows = adjusted_sets(txn('153.55', 'Alice Water Works PAYMENT'), ctx(items))
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('98.00', 'PARTIAL'), 'B': ('55.55', '')})

    def test_withholding_on_every_invoice(self):
        from recon.adjusted import adjusted_sets
        items = [invoice('A', 'F1', '100'), invoice('B', 'F2', '50'), invoice('C', 'F3', '31.17')]
        rows = adjusted_sets(txn('135', 'Alice Water Works PAYMENT'), ctx(items, 'BR'))
        self.assertEqual({r['item_id']: r['exception'] for r in rows}, {'A': 'SHORT_WITHHOLDING', 'B': 'SHORT_WITHHOLDING'})

    def test_stale_invoices_are_not_set_members(self):
        from recon.adjusted import adjusted_sets
        items = [invoice('A', 'F1', '100', payment_terms='D2'), invoice('B', 'F2', '55.55')]
        items[0]['issue_date'] = '2024-12-20'; items[1]['issue_date'] = '2021-06-01'
        self.assertIsNone(adjusted_sets(txn('153.55', 'Alice Water Works PAYMENT'), ctx(items)))

    def test_large_pool_searches_its_last_180_days(self):
        from recon.adjusted import adjusted_sets
        old = [invoice('O%02d' % k, 'G%02d' % k, str(1000 + 7 * k)) for k in range(30)]
        for item in old:
            item['issue_date'] = '2024-03-01'
        items = old + [invoice('A', 'F1', '100', payment_terms='D2'), invoice('B', 'F2', '55.55'), invoice('C', 'F3', '31.17')]
        items[-3]['issue_date'] = '2024-12-20'
        rows = adjusted_sets(txn('153.55', 'Alice Water Works PAYMENT'), ctx(items))
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('98.00', 'PARTIAL'), 'B': ('55.55', '')})

    def test_plain_subset_or_two_patterns_abstain(self):
        from recon.adjusted import adjusted_sets
        items = [invoice('A', 'F1', '100', payment_terms='D2'), invoice('B', 'F2', '50'), invoice('C', 'F3', '98')]
        self.assertIsNone(adjusted_sets(txn('148', 'Alice Water Works PAYMENT'), ctx(items)))


class MultiDiscountSetTests(unittest.TestCase):
    def items(self):
        items = [invoice('A', 'F1', '100', payment_terms='D2'), invoice('B', 'F2', '250', payment_terms='D2'),
                 invoice('C', 'F3', '55.55'), invoice('D', 'F4', '31.17', payment_terms='D2')]
        items[0]['issue_date'] = '2024-11-20'; items[1]['issue_date'] = '2024-12-01'
        items[2]['issue_date'] = '2024-12-02'; items[3]['issue_date'] = '2024-12-03'
        return items

    def test_two_late_discounts_and_a_full_invoice(self):
        from recon.adjusted import adjusted_sets
        # 98.00 + 245.00 + 55.55: A and B both net of 2 %, both past issue + 10 + 3 days.
        rows = adjusted_sets(txn('398.55', 'Alice Water Works PAYMENT'), ctx(self.items()))
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('98.00', 'PARTIAL'), 'B': ('245.00', 'PARTIAL'), 'C': ('55.55', '')})

    def test_newest_discount_in_terms_older_one_late(self):
        from recon.adjusted import adjusted_sets
        items = self.items()
        items[1]['issue_date'] = '2025-01-08'
        rows = adjusted_sets(txn('343', 'Alice Water Works PAYMENT'), ctx(items))
        self.assertEqual({r['item_id']: (r['amount'], r['exception']) for r in rows},
                         {'A': ('98.00', 'PARTIAL'), 'B': ('250.00', 'SHORT_DISCOUNT')})

    def test_second_combination_abstains(self):
        from recon.adjusted import adjusted_sets
        # Three identical discounted invoices: any two of them net of 2 % make 196.00.
        items = [invoice('A', 'F1', '100', payment_terms='D2'), invoice('B', 'F2', '100', payment_terms='D2'),
                 invoice('C', 'F3', '100', payment_terms='D2')]
        for item in items:
            item['issue_date'] = '2024-11-20'
        self.assertIsNone(adjusted_sets(txn('196', 'Alice Water Works PAYMENT'), ctx(items)))


class FractionPartialTests(unittest.TestCase):
    def test_round_fraction_of_one_invoice_is_partial(self):
        from recon.adjusted import fraction_partial
        items = [invoice('A', 'F1', '20920.57'), invoice('B', 'F2', '500')]
        rows = fraction_partial(txn('14644.39', 'Alice Water Works'), ctx(items))   # 70 %, truncated
        self.assertEqual([(r['item_id'], r['amount'], r['exception']) for r in rows], [('A', '14644.39', 'PARTIAL')])

    def test_two_candidates_or_touched_invoice_abstain(self):
        from recon.adjusted import fraction_partial
        self.assertIsNone(fraction_partial(txn('50', 'Alice Water Works'), ctx([invoice('A', 'F1', '100'), invoice('B', 'F2', '200')])))
        c = ctx([invoice('A', 'F1', '100')]); c.items['A']['open_amount'] = D('80')
        self.assertIsNone(fraction_partial(txn('40', 'Alice Water Works'), c))

    def test_bridging_credit_note_wins(self):
        from recon.adjusted import fraction_partial
        cr = invoice('CR', 'AB1', '50'); cr['kind'] = 'credit'
        rows = fraction_partial(txn('50', 'Alice Water Works'), ctx([invoice('A', 'F1', '100'), cr]))
        self.assertEqual({r['item_id']: r['amount'] for r in rows}, {'A': '100.00', 'CR': '50.00'})


class RetentionTests(unittest.TestCase):
    class Facts:
        def __init__(self, facts): self.facts = facts
        def get(self, item_id): return dict(self.facts.get(item_id, {}))
        def resolve(self, item_id): return item_id

    def test_pdf_retention_is_partial_not_withholding(self):
        from recon.adjusted import single_adjusted, fraction_partial
        c = ctx([invoice('A', 'FV24-00001', '382109.54')], 'MX')
        t = txn('343898.59', 'Alice Water Works PAYMENT')
        self.assertEqual(single_adjusted(t, c)[0]['exception'], 'SHORT_WITHHOLDING')
        c.pdf_facts = self.Facts({'A': {'retention_pct': '10'}})
        self.assertIsNone(single_adjusted(t, c))
        rows = fraction_partial(t, c)
        self.assertEqual([(r['amount'], r['exception']) for r in rows], [('343898.59', 'PARTIAL')])
        rows = reconcile(txn('343898.59', 'Alice Water Works FV24-00001'), c)
        self.assertEqual([(r['amount'], r['exception']) for r in rows], [('343898.59', 'PARTIAL')])
