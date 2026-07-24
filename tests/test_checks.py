"""内部校验：总额关系、税率冲突、必填字段、多付款方式。"""
import unittest
from decimal import Decimal

from core.models import Invoice, FieldValue, PaymentDetail
from extraction.validate import checks


def _amt(inv, key, num):
    inv.set(key, FieldValue(raw=str(num), value=Decimal(num)))


def _codes(inv):
    return [i.code for i in inv.issues]


class TestLineItemsSum(unittest.TestCase):
    def _inv_with_lines(self, *amounts):
        from core.models import LineItem
        inv = Invoice()
        _amt(inv, "subtotal", "100.00")
        _amt(inv, "sales_tax", "10.00")
        _amt(inv, "total_due", "110.00")     # 含税
        inv.line_items = [LineItem(description=f"svc{i}", amount=Decimal(a)) for i, a in enumerate(amounts)]
        return inv

    def test_lines_equal_subtotal_not_total_no_mismatch(self):
        """含税发票：明细合计=小计(净额)，不应因 ≠含税 TOTAL 而误报 LINE_SUM_MISMATCH。"""
        inv = self._inv_with_lines("60.00", "40.00")   # 和=100=subtotal
        checks.run_checks(inv)
        self.assertNotIn("LINE_SUM_MISMATCH", _codes(inv))

    def test_lines_not_matching_subtotal_flagged(self):
        inv = self._inv_with_lines("60.00", "30.00")   # 和=90≠100
        checks.run_checks(inv)
        self.assertIn("LINE_SUM_MISMATCH", _codes(inv))


class TestTotalRelation(unittest.TestCase):
    def test_mismatch_flagged(self):
        inv = Invoice()
        _amt(inv, "subtotal", "100.00")
        _amt(inv, "sales_tax", "10.00")
        _amt(inv, "total_due", "200.00")   # 应为 110
        checks.run_checks(inv)
        self.assertIn("TOTAL_MISMATCH", _codes(inv))

    def test_consistent_ok(self):
        inv = Invoice()
        _amt(inv, "subtotal", "100.00")
        _amt(inv, "sales_tax", "10.00")
        _amt(inv, "total_due", "110.00")
        checks.run_checks(inv)
        self.assertNotIn("TOTAL_MISMATCH", _codes(inv))

    def test_discount_reconciles_no_mismatch(self):
        # 折扣行：Subtotal+Tax ≠ Total 但差额==折扣额 → 不报错 TOTAL_MISMATCH，但留 info 痕迹 TOTAL_ADJUSTED
        inv = Invoice()
        _amt(inv, "subtotal", "5000.00"); _amt(inv, "sales_tax", "360.00")
        _amt(inv, "total_due", "4860.00")          # 5000+360-500(折扣)=4860
        inv.raw_pdf_text = "Subtotal\n5000.00\nDiscount (10%)\n-500.00\nTax (8%)\n360.00\nTotal Due\n4860.00"
        checks.run_checks(inv)
        self.assertNotIn("TOTAL_MISMATCH", _codes(inv))
        self.assertIn("TOTAL_ADJUSTED", _codes(inv))                  # 不完全静默：留痕供复核
        adj = next(i for i in inv.issues if i.code == "TOTAL_ADJUSTED")
        self.assertEqual(adj.severity, "info")                        # info 级：不升风险、不强制复核

    def test_deposit_reconciles_no_mismatch(self):
        inv = Invoice()
        _amt(inv, "subtotal", "10000.00"); _amt(inv, "total_due", "7000.00")
        inv.raw_pdf_text = "Subtotal\n10000.00\nTotal\n10000.00\nLess Deposit Paid\n-3000.00\nBalance Due\n7000.00"
        checks.run_checks(inv)
        self.assertNotIn("TOTAL_MISMATCH", _codes(inv))

    def test_unexplained_gap_still_flagged(self):
        # 差额无调整行解释 → 仍报（不误吞真实错误）
        inv = Invoice()
        _amt(inv, "subtotal", "5000.00"); _amt(inv, "sales_tax", "360.00")
        _amt(inv, "total_due", "4860.00")
        inv.raw_pdf_text = "Subtotal\n5000.00\nTax\n360.00\nTotal Due\n4860.00"   # 无折扣行
        checks.run_checks(inv)
        self.assertIn("TOTAL_MISMATCH", _codes(inv))


class TestTaxRate(unittest.TestCase):
    def test_zero_rate_but_nonzero_tax(self):
        inv = Invoice()
        inv.set("tax_rate", FieldValue(raw="0.00%"))
        _amt(inv, "sales_tax", "5.00")
        checks.run_checks(inv)
        self.assertIn("TAX_RATE_CONFLICT", _codes(inv))


class TestRequiredFields(unittest.TestCase):
    def test_empty_invoice_reports_missing(self):
        inv = Invoice()
        checks.run_checks(inv)
        codes = _codes(inv)
        self.assertIn("MISSING_INVOICE_NO", codes)
        self.assertIn("MISSING_TOTAL", codes)


class TestMultiPayment(unittest.TestCase):
    def test_two_targets_flagged(self):
        inv = Invoice()
        inv.payments = [
            PaymentDetail(method="On-chain", chain="Ethereum", wallet_address="0xA"),
            PaymentDetail(method="Bank transfer", wallet_address="ACCT-1"),
        ]
        checks.run_checks(inv)
        self.assertIn("MULTI_PAYMENT_METHOD", _codes(inv))
        self.assertTrue(inv.has_multiple_payment_methods)


if __name__ == "__main__":
    unittest.main()


class TestLoyaltyCreditAdjust(unittest.TestCase):
    def test_loyalty_credit_reconciles(self):
        from core.models import Invoice, FieldValue
        from decimal import Decimal
        inv = Invoice()
        inv.set("subtotal", FieldValue(raw="2000.00", value=Decimal("2000.00")))
        inv.set("total_due", FieldValue(raw="1900.00", value=Decimal("1900.00")))
        inv.raw_pdf_text = "Consulting\n2000.00\nSubtotal\n2000.00\nLoyalty credit\n-100.00\nTotal Due\n1900.00"
        checks.run_checks(inv)
        codes = [i.code for i in inv.issues]
        self.assertNotIn("TOTAL_MISMATCH", codes)
        self.assertIn("TOTAL_ADJUSTED", codes)


class TestAddonAdjust(unittest.TestCase):
    """非税附加项（运费/手续费/保险/小费）令 sub+tax≠total 属正常，能凑平则不报 TOTAL_MISMATCH。"""
    def test_shipping_handling_insurance_reconcile(self):
        from core.models import Invoice, FieldValue
        from decimal import Decimal
        inv = Invoice()
        inv.set("subtotal", FieldValue(raw="2000.00", value=Decimal("2000.00")))
        inv.set("sales_tax", FieldValue(raw="160.00", value=Decimal("160.00")))
        inv.set("total_due", FieldValue(raw="2460.00", value=Decimal("2460.00")))
        inv.raw_pdf_text = ("Subtotal\n2000.00\nShipping\n150.00\nHandling\n50.00\n"
                            "Insurance\n100.00\nTax (8%)\n160.00\nTotal Due\n2460.00")
        checks.run_checks(inv)
        codes = [i.code for i in inv.issues]
        self.assertNotIn("TOTAL_MISMATCH", codes)
        self.assertIn("TOTAL_ADJUSTED", codes)

    def test_gratuity_reconciles(self):
        from core.models import Invoice, FieldValue
        from decimal import Decimal
        inv = Invoice()
        inv.set("subtotal", FieldValue(raw="120.00", value=Decimal("120.00")))
        inv.set("sales_tax", FieldValue(raw="12.00", value=Decimal("12.00")))
        inv.set("total_due", FieldValue(raw="150.00", value=Decimal("150.00")))
        inv.raw_pdf_text = "Subtotal\n120.00\nService charge\n12.00\nGratuity\n18.00\nTotal Due\n150.00"
        checks.run_checks(inv)
        self.assertNotIn("TOTAL_MISMATCH", [i.code for i in inv.issues])


class TestBillFeeAdjust(unittest.TestCase):
    """账单命名费用（规费/滞纳金/利息）令 sub+tax≠total 属正常，能凑平则不报 TOTAL_MISMATCH。"""
    def _run(self, sub, tax, total, raw):
        from core.models import Invoice, FieldValue
        from decimal import Decimal
        inv = Invoice()
        inv.set("subtotal", FieldValue(raw=sub, value=Decimal(sub)))
        if tax:
            inv.set("sales_tax", FieldValue(raw=tax, value=Decimal(tax)))
        inv.set("total_due", FieldValue(raw=total, value=Decimal(total)))
        inv.raw_pdf_text = raw
        checks.run_checks(inv)
        return [i.code for i in inv.issues]

    def test_telecom_regulatory_fee(self):
        codes = self._run("58.50", "3.51", "64.00",
                          "Subtotal\n58.50\nFederal tax\n3.51\nRegulatory fee\n1.99\nTotal Due\n64.00")
        self.assertNotIn("TOTAL_MISMATCH", codes)

    def test_tax_notice_penalty_interest(self):
        codes = self._run("5000.00", None, "5335.00",
                          "Income tax\n5000.00\nSubtotal\n5000.00\nLate payment penalty\n250.00\nInterest\n85.00\nTotal Payable\n5335.00")
        self.assertNotIn("TOTAL_MISMATCH", codes)
