"""提取质量门控测试：覆盖率/置信度、风险缺失维度、分类收口。"""
import unittest
from decimal import Decimal

from core import config
from core.models import Invoice, FieldValue, LineItem
from extraction.validate import confidence, risk
from extraction.classify import engine as classifier


def _inv(**fields):
    inv = Invoice()
    for k, v in fields.items():
        inv.set(k, FieldValue(raw=str(v), value=v, confidence=1.0))
    return inv


class TestCoverage(unittest.TestCase):
    def test_missing_required_not_excellent(self):
        """缺必填字段：覆盖率<1、不得评 Excellent、关键置信度=0、强制复核。"""
        inv = _inv(total_due="100")          # 缺 invoice_no / invoice_date
        confidence.assess(inv)
        self.assertLess(inv.field_coverage, 1.0)
        self.assertNotEqual(inv.ocr_quality_level, "Excellent")
        self.assertEqual(inv.key_field_confidence, 0.0)
        self.assertTrue(inv.needs_manual_review)

    def test_full_required_high_conf_excellent(self):
        """必填齐全且高置信（模板精确命中）→ Excellent。"""
        inv = _inv(invoice_no="X1", invoice_date="2025-01-01", total_due="100",
                   subtotal="100", currency_settlement="USD", sales_tax="0")
        confidence.assess(inv)
        self.assertEqual(inv.field_coverage, 1.0)
        self.assertEqual(inv.ocr_quality_level, "Excellent")

    def test_heuristic_fields_downgrade(self):
        """启发式抽取（置信度 0.90）→ 不评 Excellent，触发复核。"""
        inv = Invoice()
        for k in ("invoice_no", "invoice_date", "total_due"):
            inv.set(k, FieldValue(raw="x", value="x", confidence=config.GENERIC_FIELD_CONFIDENCE))
        confidence.assess(inv)
        self.assertEqual(inv.field_coverage, 1.0)
        self.assertNotEqual(inv.ocr_quality_level, "Excellent")
        self.assertTrue(inv.needs_manual_review)


class TestRiskMissing(unittest.TestCase):
    def test_missing_required_adds_risk_and_critical(self):
        inv = _inv(invoice_date="2025-01-01")    # 缺 invoice_no 与 total_due
        confidence.assess(inv)
        risk.compute(inv)
        self.assertGreaterEqual(inv.risk_score, config.RISK_FIELD_MISSING * 2)
        self.assertTrue(inv.critical_review)

    def test_complete_no_missing_risk(self):
        inv = _inv(invoice_no="X1", invoice_date="2025-01-01", total_due="100")
        confidence.assess(inv)
        base = inv.risk_score
        # 不应包含缺失项加分
        self.assertLess(base, config.RISK_FIELD_MISSING)


class TestClassifyScope(unittest.TestCase):
    def test_no_fulltext_roulette(self):
        """全文出现 'legal' 但无明细/供应商 → 低置信兜底，而非自信 0.8。"""
        inv = Invoice(raw_pdf_text="Halcyon ... Legal Document Review ... bank details")
        c = classifier.classify(inv)
        self.assertLessEqual(c.confidence, 0.25)
        self.assertTrue(c.needs_review)

    def test_dominant_lineitem_by_amount(self):
        """按金额加权取主导明细项分类。"""
        inv = Invoice()
        inv.line_items = [
            LineItem(description="taxi ride", amount=Decimal("10")),
            LineItem(description="consulting advisory", amount=Decimal("5000")),
        ]
        c = classifier.classify(inv)
        self.assertEqual(c.account, "6410 Professional Fees")
        self.assertGreater(c.confidence, 0.25)

    def test_unclassified_when_no_signal(self):
        inv = Invoice(raw_pdf_text="random text with no category keywords at all")
        c = classifier.classify(inv)
        self.assertEqual(c.category, "Unclassified")


if __name__ == "__main__":
    unittest.main()
