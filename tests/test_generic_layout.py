"""版式无关提取的通用行为回归（合成版面，不依赖任何私有发票文件）。

锁定针对 "Ogier 类" 发票（Invoice number / Gross Total / Net Total / Description·Value /
US$ / 尾随类别明细页）修复后的通用能力：
- Gross/Net Total 被识别为 应付总额/小计；尾随明细页的孤立 "Total" 不覆盖主发票应付额；
- "Value" 列头被认作金额列，Net/Gross Total 行不混入明细；
- US$ → USD；开票方在信笺只有域名时回退用"收款户名(Account Name)"。
"""
import unittest

from extraction.parse import generic


class _L:
    """最小行对象：extract_generic / extract_line_items 只需 .words=[(x0,x1,text),...]。"""
    def __init__(self, y, words):
        self.y = y
        self.words = words

    def text(self):
        return " ".join(w[2] for w in self.words)


def _ogier_like():
    return [
        _L(10,  [(43, 90, "ogier.com")]),
        _L(25,  [(43, 110, "Your invoice")]),
        _L(40,  [(43, 90, "Invoice to:")]),
        _L(55,  [(43, 240, "Lightforge Capital Limited")]),
        _L(70,  [(43, 120, "Invoice number:")]),
        _L(85,  [(43, 100, "6024081")]),
        _L(100, [(43, 110, "Invoice date:")]),
        _L(115, [(43, 140, "2 June 2026")]),
        # 明细表：Description | Value
        _L(253, [(43, 120, "Description"), (538, 570, "Value")]),
        _L(274, [(43, 95, "Professional"), (95, 140, "Fees"), (523, 580, "7,000.00")]),
        _L(289, [(43, 74, "Sundry"), (74, 140, "Expenses"), (531, 580, "295.00")]),
        _L(304, [(43, 140, "Disbursements"), (523, 580, "3,000.00")]),
        _L(323, [(43, 59, "Net"), (59, 90, "Total"), (518, 580, "10,295.00")]),
        _L(343, [(43, 71, "Gross"), (71, 110, "Total"), (501, 600, "US$10,295.00")]),
        # 付款块
        _L(500, [(43, 320, "Payment by wire transfer to: HSBC")]),
        _L(515, [(43, 220, "Swift Code: HSBCHKHHHKH")]),
        _L(530, [(43, 220, "Account No: 808-541700-838")]),
        _L(545, [(43, 180, "Account Name: Ogier")]),
        # 尾随"类别明细"页：按类别分组的子明细 + 每类小计 + 一个孤立 Total（不应覆盖主发票应付额）
        _L(1059, [(43, 80, "Date"), (121, 200, "Description"), (532, 570, "Value")]),
        _L(1075, [(43, 130, "Sundry Expense")]),
        _L(1103, [(43, 90, "29/05/2026"), (121, 250, "Sundry Expenses Charge"), (530, 590, "75.00")]),
        _L(1118, [(43, 90, "02/06/2026"), (121, 250, "Sundry Expenses Charge"), (525, 590, "220.00")]),
        _L(1133, [(525, 590, "295.00")]),
        _L(1150, [(43, 150, "Disbursements")]),
        _L(1165, [(43, 90, "19/03/2026"), (121, 340, "Lightforge - Application Fee"), (518, 600, "1,200.00")]),
        _L(1180, [(43, 90, "26/05/2026"), (121, 340, "Lightforge - Approval Fee"), (518, 600, "1,800.00")]),
        _L(1196, [(518, 600, "3,000.00")]),
        _L(1223, [(454, 490, "Total"), (500, 600, "US$3,295.00")]),
    ]


class GenericLayoutTest(unittest.TestCase):
    def _amt(self, raw):
        import re
        return float(re.sub(r"[^\d.]", "", (raw or "").replace(",", "")) or 0)

    def test_gross_total_beats_trailing_detail_total(self):
        g = generic.extract_generic(_ogier_like())
        # 应付总额取主发票 Gross Total(10295)，而不是明细页孤立 Total(3295)
        self.assertEqual(self._amt(g["total_due"]), 10295.0)
        # Net Total 作为小计
        self.assertEqual(self._amt(g["subtotal"]), 10295.0)

    def test_issuer_from_account_name_when_header_is_domain(self):
        g = generic.extract_generic(_ogier_like())
        self.assertEqual(g.get("issuer_name"), "Ogier")          # 收款户名=收款方=开票方
        self.assertEqual(g.get("customer_name"), "Lightforge Capital Limited")

    def test_line_items_exclude_totals_and_read_value_column(self):
        items = generic.extract_line_items(_ogier_like())
        descs = [ (it["description"] or "") for it in items ]
        self.assertIn("Professional Fees", descs)
        self.assertIn("Sundry Expenses", descs)
        self.assertIn("Disbursements", descs)
        # 合计行绝不作为明细
        self.assertFalse(any("Total" in d for d in descs))
        # Professional Fees 的金额取到 Value 列
        pf = next(it for it in items if it["description"] == "Professional Fees")
        self.assertEqual(self._amt(pf["amount"]), 7000.0)

    def test_currency_symbol_us_dollar(self):
        self.assertEqual(generic.currency_fallback("Gross Total US$10,295.00"), "USD")
        self.assertEqual(generic.currency_fallback("Amount £11,700.00"), "GBP")
        self.assertEqual(generic.currency_fallback("Total €2,806.89"), "EUR")

    def test_your_invoice_title_not_company(self):
        self.assertFalse(generic.looks_like_company("Your invoice"))
        self.assertTrue(generic.looks_like_company("Ogier"))

    def test_detail_schedule_grouped(self):
        """尾随明细附表：按类别分组解析（不把页脚含 charge 的句子/发票号前言误当表头/类别）。"""
        groups = {g["category"]: g for g in generic.extract_detail_schedule(_ogier_like())}
        self.assertIn("Sundry Expense", groups)
        self.assertIn("Disbursements", groups)
        self.assertEqual(len(groups), 2)                         # 无杂散组
        self.assertEqual(len(groups["Disbursements"]["rows"]), 2)
        self.assertEqual(self._amt(groups["Disbursements"]["subtotal"]), 3000.0)

    def test_detail_attached_and_reconciled_to_line(self):
        """明细子行按类别名归属到主发票行，并勾稽（Σ子行==行金额 → note 打勾）。"""
        from decimal import Decimal
        from core.models import Invoice, LineItem
        from extraction.parse import template_rules
        inv = Invoice()
        inv.line_items = [
            LineItem(description="Professional Fees", amount=Decimal("7000.00")),
            LineItem(description="Sundry Expenses", amount=Decimal("295.00")),
            LineItem(description="Disbursements", amount=Decimal("3000.00")),
        ]
        template_rules._attach_detail_schedules(inv, generic.extract_detail_schedule(_ogier_like()))
        by = {li.description: li for li in inv.line_items}
        self.assertEqual(len(by["Disbursements"].sub_items), 2)   # Application/Approval Fee 归属
        self.assertIn("勾稽", by["Disbursements"].note or "")       # 3000==1200+1800
        self.assertEqual(by["Professional Fees"].sub_items, [])   # 无明细的行不受影响
        self.assertFalse(any(i.code == "DETAIL_MISMATCH" for i in inv.issues))


if __name__ == "__main__":
    unittest.main()
