"""Invoice JSON 往返保真：SQLite 单一数据源依赖它（Decimal/嵌套/原文不丢）。"""
import json
import unittest
from decimal import Decimal

from core.models import Invoice, FieldValue, PaymentDetail, LineItem


class TestRoundTrip(unittest.TestCase):
    def _roundtrip(self, inv):
        return Invoice.from_jsonable(json.loads(json.dumps(inv.to_jsonable())))

    def test_decimal_preserved(self):
        inv = Invoice(file_hash="h1")
        inv.set("total_due", FieldValue(raw="$1,234.5678", value=Decimal("1234.5678")))
        out = self._roundtrip(inv)
        v = out.f("total_due").value
        self.assertIsInstance(v, Decimal)
        self.assertEqual(v, Decimal("1234.5678"))

    def test_raw_text_and_scalars_preserved(self):
        inv = Invoice(file_hash="h2", raw_pdf_text="完整原文 archive", parse_method="pdf_text",
                      risk_score=42, needs_manual_review=True)
        out = self._roundtrip(inv)
        self.assertEqual(out.raw_pdf_text, "完整原文 archive")
        self.assertEqual(out.parse_method, "pdf_text")
        self.assertEqual(out.risk_score, 42)
        self.assertTrue(out.needs_manual_review)

    def test_nested_objects_preserved(self):
        inv = Invoice(file_hash="h3")
        inv.line_items = [LineItem(description="svc", amount=Decimal("9.99"), amount_raw="$9.99")]
        inv.payments = [PaymentDetail(method="On-chain", chain="Ethereum",
                                      wallet_address="0xabc", valid_address=True)]
        inv.add_issue("DEMO", "msg", "total_due", "warning")
        out = self._roundtrip(inv)
        self.assertEqual(out.line_items[0].amount, Decimal("9.99"))
        self.assertEqual(out.payments[0].chain, "Ethereum")
        self.assertTrue(out.payments[0].valid_address)
        self.assertEqual(out.issues[0].code, "DEMO")


if __name__ == "__main__":
    unittest.main()
