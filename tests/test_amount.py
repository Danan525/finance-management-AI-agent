"""金额解析：严格正则 + Decimal，保留真实精度、不自动修正可疑值。"""
import unittest
from decimal import Decimal

from extraction.parse import amount as amt


class TestParseAmount(unittest.TestCase):
    def test_standard_two_decimals(self):
        v, susp, _ = amt.parse_amount("$24,946.34")
        self.assertEqual(v, Decimal("24946.34"))
        self.assertFalse(susp)

    def test_high_precision_preserved(self):
        # 不强制两位：多位小数（加密金额）原样保留
        v, _, _ = amt.parse_amount("1,234.5678")
        self.assertEqual(v, Decimal("1234.5678"))

    def test_no_decimal(self):
        v, _, _ = amt.parse_amount("100")
        self.assertEqual(v, Decimal("100"))

    def test_negative_sign(self):
        v, _, _ = amt.parse_amount("-50.00")
        self.assertEqual(v, Decimal("-50.00"))

    def test_leading_plus_is_positive(self):
        # 流水贷方常写 "+5,000.00"：前导加号仅表正、不应导致解析失败或翻号
        self.assertEqual(amt.parse_amount("+5,000.00")[0], Decimal("5000.00"))
        self.assertEqual(amt.parse_amount("+100")[0], Decimal("100"))
        self.assertEqual(amt.parse_amount("+1,234.56")[0], Decimal("1234.56"))

    def test_indian_lakh_grouping(self):
        # 印度记数法（个/千后每两位一逗号），末组 3 位；非法分组仍拒绝
        self.assertEqual(amt.parse_amount("1,23,456.78")[0], Decimal("123456.78"))
        self.assertEqual(amt.parse_amount("12,34,567.00")[0], Decimal("1234567.00"))
        self.assertIsNone(amt.parse_amount("1,00.00")[0])       # 末组 2 位=非法
        self.assertIsNone(amt.parse_amount("1,2345.00")[0])     # 组 4 位=非法

    def test_trailing_minus_negative(self):
        # 尾部负号（SAP/德式）
        self.assertEqual(amt.parse_amount("1234.56-")[0], Decimal("-1234.56"))
        self.assertEqual(amt.parse_amount("1.234,56-")[0], Decimal("-1234.56"))

    def test_accounting_parentheses_negative(self):
        # 会计式括号 = 负数（银行流水支出常用）
        self.assertEqual(amt.parse_amount("(1,234.56)")[0], Decimal("-1234.56"))
        self.assertEqual(amt.parse_amount("(500.00)")[0], Decimal("-500.00"))
        self.assertEqual(amt.parse_amount("1,234.56")[0], Decimal("1234.56"))   # 无括号不变

    def test_eu_decimal_comma(self):
        # 欧式：点千分位、逗号小数 1.234,56 → 1234.56
        v, _, _ = amt.parse_amount("€ 1.234,56")
        self.assertEqual(v, Decimal("1234.56"))
        v2, _, _ = amt.parse_amount("4.118.000,00")
        self.assertEqual(v2, Decimal("4118000.00"))

    def test_space_thousands(self):
        # 空格千分位（普通/不断行空格）+ 逗号或点小数
        self.assertEqual(amt.parse_amount("88 400,00")[0], Decimal("88400.00"))
        self.assertEqual(amt.parse_amount("1 234.56")[0], Decimal("1234.56"))
        self.assertEqual(amt.parse_amount("1 234 567,89")[0], Decimal("1234567.89"))

    def test_swiss_apostrophe_thousands(self):
        # 瑞士撇号千分位（ASCII ' 与 U+2019 均支持）
        self.assertEqual(amt.parse_amount("1'234.56")[0], Decimal("1234.56"))
        self.assertEqual(amt.parse_amount("2'500'000.00")[0], Decimal("2500000.00"))
        self.assertEqual(amt.parse_amount("CHF 1\u2019234.56")[0], Decimal("1234.56"))
        self.assertEqual(amt.decimal_places("1'234.56"), 2)

    def test_gulf_three_decimals(self):
        # 海湾币种 3 位小数（BHD/KWD/OMR）——值与小数位都保真
        self.assertEqual(amt.parse_amount("BHD 1,234.567")[0], Decimal("1234.567"))
        self.assertEqual(amt.decimal_places("1,234.567"), 3)

    def test_letter_confusable_is_suspicious(self):
        v, susp, _ = amt.parse_amount("1O0.00")   # 含字母 O
        self.assertIsNone(v)
        self.assertTrue(susp)

    def test_malformed_thousands_rejected(self):
        v, susp, _ = amt.parse_amount("1,00.00")
        self.assertIsNone(v)
        self.assertTrue(susp)

    def test_multi_dot_rejected(self):
        v, susp, _ = amt.parse_amount("1.2.3")
        self.assertIsNone(v)
        self.assertTrue(susp)

    def test_none_and_empty(self):
        self.assertIsNone(amt.parse_amount(None)[0])
        self.assertIsNone(amt.parse_amount("")[0])


class TestDecimalHelpers(unittest.TestCase):
    def test_decimal_places(self):
        self.assertEqual(amt.decimal_places("$1,234.56"), 2)
        self.assertEqual(amt.decimal_places("1,234.5678"), 4)
        self.assertIsNone(amt.decimal_places("100"))

    def test_decimal_places_eu_and_space(self):
        # 欧式/空格千分位：小数位判定须与 parse_amount 归一后一致（否则误报 DECIMAL_NONSTANDARD/低置信）
        self.assertEqual(amt.decimal_places("59.400,00"), 2)     # 欧式（曾误判为 5）
        self.assertEqual(amt.decimal_places("€ 64.152,00"), 2)
        self.assertEqual(amt.decimal_places("88 400,00"), 2)     # 空格千分位（曾判为 None）
        self.assertEqual(amt.decimal_places("690.5"), 1)

    def test_normalize_for_match(self):
        self.assertEqual(amt.normalize_for_match("$1,234.56"), "1234.56")


class TestAmountBroadened(unittest.TestCase):
    def _v(self, s):
        return amt.parse_amount(s)[0]

    def test_any_iso_currency_code(self):
        # 不再限固定表：NZD/THB/AED… 前导码都能去掉
        for s in ("USD 660.00", "NZD 1,200.50", "THB 990.00", "AED 100.00"):
            self.assertIsNotNone(self._v(s), s)
        self.assertEqual(self._v("NZD 1,200.50"), Decimal("1200.50"))

    def test_symbol_letter_prefix(self):
        self.assertEqual(self._v("US$10,295.00"), Decimal("10295.00"))
        self.assertEqual(self._v("HK$1,200.00"), Decimal("1200.00"))

    def test_european_decimals(self):
        self.assertEqual(self._v("1.234,56"), Decimal("1234.56"))     # 点千分位、逗号小数
        self.assertEqual(self._v("12,50"), Decimal("12.50"))
        self.assertEqual(self._v("1.234.567,89"), Decimal("1234567.89"))

    def test_us_format_unchanged(self):
        # 美式不受欧式归一影响
        self.assertEqual(self._v("1,234.56"), Decimal("1234.56"))
        self.assertEqual(self._v("10,295.00"), Decimal("10295.00"))
        self.assertEqual(self._v("1,200"), Decimal("1200"))          # 逗号千分位
        self.assertEqual(self._v("-1,234.56"), Decimal("-1234.56"))


if __name__ == "__main__":
    unittest.main()


class TestNativeDigits(unittest.TestCase):
    def test_arabic_indic_and_persian(self):
        self.assertEqual(amt.parse_amount("١٢٣٤٫٥٦")[0], Decimal("1234.56"))   # 阿拉伯-印度
        self.assertEqual(amt.parse_amount("۱۵۰۰۰")[0], Decimal("15000"))       # 波斯
        self.assertEqual(amt.parse_amount("١٬٢٣٤٫٥٦")[0], Decimal("1234.56"))  # 带阿拉伯千分位


class TestCurrencySuffix(unittest.TestCase):
    def test_trailing_iso_code_stripped(self):
        self.assertEqual(amt.parse_amount("4000.00 USD")[0], Decimal("4000.00"))
        self.assertEqual(amt.parse_amount("1,500.00 EUR")[0], Decimal("1500.00"))
        self.assertEqual(amt.parse_amount("USD 4000.00")[0], Decimal("4000.00"))   # 前缀仍可
