"""同类失败模式加固：金额多词标签、服务期间锚点、估值日同义词、邮箱不丢。"""
import unittest

from extraction.parse import generic as g, dates as d


class AmountLabelTest(unittest.TestCase):
    def test_multiword_and_ccy_suffix_labels(self):
        for cell in ["Total Amount Due", "Balance Payable", "Total Amount Due (USD)",
                     "Total Due", "Amount Payable", "Grand Total"]:
            lm = g._label_match(cell)
            self.assertIsNotNone(lm, cell)
            self.assertEqual(lm[0], "total_due", cell)

    def test_non_total_not_matched(self):
        for cell in ["Total Items", "Total Quantity", "Item Description"]:
            lm = g._label_match(cell)
            self.assertFalse(lm and lm[0] == "total_due", cell)


class PeriodAnchorTest(unittest.TestCase):
    def test_broadened_period_phrasings(self):
        cases = {
            "Billing period: 1 June 2026 to 30 June 2026": ("2026-06-01", "2026-06-30"),
            "Service dates 06/01/2026 - 06/30/2026": ("2026-06-01", "2026-06-30"),
            "For the period 1 Jan 2026 through 31 Jan 2026": ("2026-01-01", "2026-01-31"),
            "from 1 June 2026 to 30 June 2026": ("2026-06-01", "2026-06-30"),
        }
        for s, exp in cases.items():
            self.assertEqual(d.extract_period(s), exp, s)


class ValuationDateTest(unittest.TestCase):
    def test_valuation_synonyms(self):
        for cell in ["Fund Valuation Date", "Valuation Date", "NAV Date"]:
            lm = g._label_match(cell)
            self.assertIsNotNone(lm, cell)
            self.assertEqual(lm[0], "fund_valuation_date", cell)


class EmailNotLostTest(unittest.TestCase):
    def _L(self, y, *w):
        from extraction.extract.pdf_text import Line
        return Line(y, list(w))

    def test_footer_email_captured_somewhere(self):
        lines = [self._L(10, (40, 200, "Invoice number: INV-1")),
                 self._L(30, (40, 300, "Consulting services")),
                 self._L(200, (40, 400, "Questions? contact accounts@vendor.com"))]
        out = g.extract_generic(lines)
        emails = [out.get("contact_email"), out.get("issuer_email")]
        self.assertIn("accounts@vendor.com", emails)   # 邮箱不丢（落到某个邮箱字段）


class QuantityTest(unittest.TestCase):
    def _L(self, y, *w):
        from extraction.extract.pdf_text import Line
        return Line(y, list(w))

    def _items(self, *datarows):
        header = self._L(10, (40, 120, "Description"), (300, 340, "Qty"), (440, 490, "Amount"))
        return g.extract_line_items([header] + list(datarows))

    def test_large_integer_quantity(self):
        it = self._items(self._L(30, (40, 200, "Widgets"), (300, 340, "10000"), (440, 490, "5,000.00")))
        self.assertEqual(it[0]["quantity"], "10000")     # 原 \d{1,3} 会漏
        self.assertEqual(it[0]["amount"], "5,000.00")

    def test_decimal_quantity_hours(self):
        it = self._items(self._L(30, (40, 200, "Consulting"), (300, 340, "2.5"), (440, 490, "250.00")))
        self.assertEqual(it[0]["quantity"], "2.5")       # 工时小数不再漏
        self.assertEqual(it[0]["amount"], "250.00")

    def test_qty_unit_amount_three_columns(self):
        it = self._items(self._L(30, (40, 200, "Svc"), (300, 340, "2"),
                                 (380, 430, "500.00"), (440, 490, "1,000.00")))
        self.assertEqual(it[0]["quantity"], "2")
        self.assertEqual(it[0]["unit_price"], "500.00")
        self.assertEqual(it[0]["amount"], "1,000.00")

    def test_integer_amount_not_taken_as_qty(self):
        header = self._L(10, (40, 120, "Description"), (440, 490, "Amount"))
        it = g.extract_line_items([header, self._L(30, (40, 200, "Item A"), (440, 490, "1,000"))])
        self.assertEqual(it[0]["amount"], "1,000")       # 整数金额仍是金额、不被当数量
        self.assertIsNone(it[0]["quantity"])


if __name__ == "__main__":
    unittest.main()
