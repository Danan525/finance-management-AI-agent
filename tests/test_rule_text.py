"""自由文本 → 结构化规则的本地解析（纯规则，无模型）。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core import config, db
from review import rule_text


class RuleTextTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._db = config.DB_PATH
        config.DB_PATH = Path(self._dir) / "t.db"
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._db
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_classification_issuer_and_account(self):
        r = rule_text.parse("以后老王家的发票都记 6400 咨询费",
                            "classification", {"match_key": "旧值"})
        self.assertEqual(r["fields"]["match_key"], "老王")
        self.assertIn("6400", r["fields"]["account"])
        self.assertEqual(r["fields"]["note"], "以后老王家的发票都记 6400 咨询费")
        self.assertNotIn("开票方", r["missing"])

    def test_classification_issuer_missing_falls_back(self):
        r = rule_text.parse("记成办公费", "classification", {"match_key": "Acme"})
        # 认不出开票方 → 沿用原值，不算 missing
        self.assertEqual(r["fields"]["match_key"], "Acme")

    def test_field_default_target_and_value(self):
        r = rule_text.parse("以后 Acme 的发票，结算币种填为「EUR」",
                            "field_default", {"match_key": "Acme"})
        self.assertEqual(r["fields"]["target"], "currency_settlement")
        self.assertEqual(r["fields"]["value"], "EUR")
        self.assertEqual(r["fields"]["match_key"], "Acme")

    def test_field_locator_label_and_target(self):
        r = rule_text.parse("遇到标签「Gross Total」时填进总金额", "field_locator", {})
        self.assertEqual(r["fields"]["value"], "Gross Total")
        self.assertEqual(r["fields"]["target"], "total_due")

    def test_multi_invoice_single_vs_multi(self):
        self.assertEqual(rule_text.parse("这种当作单张、别拆", "multi_invoice", {})["fields"]["value"], "single")
        self.assertEqual(rule_text.parse("要拆开成多张", "multi_invoice", {})["fields"]["value"], "multi")
        self.assertIn("倾向（写“单张/不拆”或“多张/拆开”）",
                      rule_text.parse("说不清", "multi_invoice", {})["missing"])

    def test_missing_reported_not_faked(self):
        r = rule_text.parse("随便一句没要素的话", "field_default", {})
        self.assertIn("填入哪个字段", r["missing"])
        self.assertIn("默认值", r["missing"])


if __name__ == "__main__":
    unittest.main()
