"""金额/币种"写太死"整改回归：多币种符号、整数额/日元/3小数被识别，且不误吞数量/年份。"""
import unittest
from decimal import Decimal

from extraction.parse import amount as a, generic as g, template_rules as t


class SymbolTest(unittest.TestCase):
    def test_parse_amount_symbols(self):
        self.assertEqual(a.parse_amount("¥10,000")[0], Decimal("10000"))
        self.assertEqual(a.parse_amount("₹500.00")[0], Decimal("500.00"))
        self.assertEqual(a.parse_amount("฿1,200")[0], Decimal("1200"))
        self.assertEqual(a.parse_amount("₩50000")[0], Decimal("50000"))

    def test_currency_fallback_symbols(self):
        self.assertEqual(g.currency_fallback("Total ₹500.00"), "INR")
        self.assertEqual(g.currency_fallback("Amount ₩50000"), "KRW")
        self.assertEqual(g.currency_fallback("Fee ฿1,200.00"), "THB")
        # ¥ 在 JPY/CNY 间有歧义 → 不臆断（返回 None，留人工）
        self.assertIsNone(g.currency_fallback("Paid ¥10000"))


class DetectionTest(unittest.TestCase):
    def _m(self, s):
        m = g._MONEY.search(s)
        return m.group(0) if m else None

    def test_symbol_anchored_integers_detected(self):
        self.assertEqual(self._m("¥10000"), "¥10000")
        self.assertEqual(self._m("$500"), "$500")
        self.assertEqual(self._m("$1000"), "$1000")

    def test_three_decimals_not_truncated(self):
        self.assertEqual(self._m("100.000"), "100.000")     # KWD/BHD 等 3 位小数

    def test_precision_preserved_bare_integers_not_money(self):
        # 关键：无符号无标点的裸整数/数量/年份/参考号仍**不**被当作金额
        for s in ("1000", "Qty 5", "Year 2024", "Ref 12345", "500"):
            self.assertIsNone(self._m(s), f"{s!r} 不应被当作金额")


class TemplateTotalTest(unittest.TestCase):
    def _v(self, s, key):
        return t._match(t._TOTAL_PATTERNS, s).get(key)

    def test_whole_number_and_symbol_and_3dec(self):
        self.assertEqual(self._v("TOTAL DUE 5000", "total_due"), "5000")        # 整数额
        self.assertEqual(self._v("TOTAL DUE ¥10000", "total_due"), "10000")     # 日元符号
        self.assertEqual(self._v("Subtotal 100.000", "subtotal"), "100.000")   # 3 位小数
        self.assertEqual(self._v("Sales Tax 0", "sales_tax"), "0")             # 0 税
        self.assertEqual(self._v("TOTAL DUE $2,953.43", "total_due"), "2,953.43")  # 原西式不变


class TaxRateNotAmountTest(unittest.TestCase):
    """税率不得被当成税额（回归：Sales Tax 3% 曾被识别成税额 3）。"""
    def _m(self, s):
        return t._match(t._TOTAL_PATTERNS, s)

    def test_percent_goes_to_rate_not_amount(self):
        self.assertEqual(self._m("Sales Tax 3%"), {"tax_rate": "3%"})
        self.assertEqual(self._m("VAT 3%"), {"tax_rate": "3%"})
        self.assertEqual(self._m("Tax 5%"), {"tax_rate": "5%"})
        self.assertEqual(self._m("Tax Rate 3%"), {"tax_rate": "3%"})

    def test_no_partial_number_from_percent(self):
        # 关键：不能把 30% / 3.5% 回溯成半个数 3 当税额
        self.assertEqual(self._m("Sales Tax 30%"), {"tax_rate": "30%"})
        self.assertEqual(self._m("Sales Tax 3.5%"), {"tax_rate": "3.5%"})

    def test_real_tax_amount_still_captured(self):
        self.assertEqual(self._m("Sales Tax 3.00"), {"sales_tax": "3.00"})
        self.assertEqual(self._m("Sales Tax 150.00"), {"sales_tax": "150.00"})
        self.assertEqual(self._m("Sales Tax: 212.43"), {"sales_tax": "212.43"})

    def test_prose_amounts_survive_sentence_periods(self):
        out = g.prose_amounts("The subtotal is USD 2,605.00. Applicable tax is USD 201.89. "
                              "The total amount due is USD 2,806.89.")
        self.assertEqual(out.get("sales_tax"), "201.89")     # 句末句号不再误杀
        self.assertEqual(out.get("total_due"), "2,806.89")
        # 散文里的税率百分比不被当税额
        self.assertEqual(g.prose_amounts("The applicable tax 3% is charged."), {})


if __name__ == "__main__":
    unittest.main()
