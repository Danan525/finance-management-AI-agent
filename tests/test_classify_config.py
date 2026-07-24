"""分类规则可配置：JSON 覆盖 / 按币种固定资产阈值 / 损坏回退默认。"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config
from core.models import Invoice, FieldValue, LineItem
from extraction.classify import engine, rules


class ClassifyConfigTest(unittest.TestCase):
    def setUp(self):
        self._saved = config.CLASSIFY_RULES_PATH
        self._dir = Path(tempfile.mkdtemp())
        rules.reload()

    def tearDown(self):
        config.CLASSIFY_RULES_PATH = self._saved
        rules.reload()
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def _write(self, obj):
        p = self._dir / "classification.json"
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        config.CLASSIFY_RULES_PATH = p
        rules.reload()

    def _inv_desc(self, desc, total=None, ccy=None):
        inv = Invoice()
        inv.line_items = [LineItem(description=desc, amount=(Decimal(total) if total else Decimal("1")))]
        if total:
            inv.set("total_due", FieldValue(raw=total, value=Decimal(total)))
        if ccy:
            inv.set("currency_settlement", FieldValue(raw=ccy, value=ccy))
        return inv

    # ---- 缺文件=默认，行为不变 ----
    def test_missing_file_uses_defaults(self):
        config.CLASSIFY_RULES_PATH = self._dir / "nope.json"
        rules.reload()
        self.assertEqual(engine.classify(self._inv_desc("annual software subscription")).account,
                         "6110 Software & Cloud")

    # ---- JSON 覆盖科目表：改成自己的科目号 ----
    def test_override_category_account(self):
        self._write({"category_rules": [["software|saas", "IT 订阅", "MY-IT-001"]]})
        c = engine.classify(self._inv_desc("annual software subscription"))
        self.assertEqual(c.account, "MY-IT-001")
        self.assertEqual(c.category, "IT 订阅")

    # ---- JSON 增加供应商映射 ----
    def test_override_supplier_rules(self):
        self._write({"supplier_rules": [["acme\\s*cloud", "Software & Cloud", "6110 Software & Cloud"]]})
        inv = Invoice()
        inv.set("issuer_name", FieldValue(raw="ACME Cloud Ltd", value="ACME Cloud Ltd"))
        self.assertEqual(engine.classify(inv).account, "6110 Software & Cloud")

    # ---- 固定资产阈值按币种 ----
    def test_asset_threshold_per_currency(self):
        # 默认：laptop 3000 USD → 达阈值 → 固定资产候选
        c = engine.classify(self._inv_desc("laptop", total="3000", ccy="USD"))
        self.assertEqual(c.account, "1500 Fixed Assets")
        # 同样数额 3000 但币种 JPY（阈值 400000）→ 未达 → 不判固定资产
        c2 = engine.classify(self._inv_desc("laptop", total="3000", ccy="JPY"))
        self.assertNotEqual(c2.account, "1500 Fixed Assets")
        # 可配置：把 USD 阈值抬到 100000 → 3000 USD 不再判资产
        self._write({"asset_thresholds": {"default": 3000, "USD": 100000}})
        c3 = engine.classify(self._inv_desc("laptop", total="3000", ccy="USD"))
        self.assertNotEqual(c3.account, "1500 Fixed Assets")

    def test_asset_keywords_and_labels_configurable(self):
        self._write({"asset_keywords": "gpu|monitor", "asset_account": "1600 Equipment",
                     "asset_category": "设备", "asset_thresholds": {"default": 100}})
        c = engine.classify(self._inv_desc("external monitor", total="500", ccy="USD"))
        self.assertEqual(c.account, "1600 Equipment")
        self.assertEqual(c.category, "设备")

    # ---- 损坏 JSON 回退默认，不崩 ----
    def test_corrupt_file_falls_back(self):
        p = self._dir / "classification.json"
        p.write_text("{ not valid json ", encoding="utf-8")
        config.CLASSIFY_RULES_PATH = p
        rules.reload()
        self.assertEqual(engine.classify(self._inv_desc("legal fees")).account, "6420 Legal Fees")

    # ---- 下拉候选也随配置走 ----
    def test_suggestion_pairs_reflect_override(self):
        self._write({"category_rules": [["software", "IT 订阅", "MY-IT-001"]],
                     "supplier_rules": [], "seed_pairs": []})
        pairs = engine.suggestion_pairs()
        self.assertIn(("IT 订阅", "MY-IT-001"), pairs)


if __name__ == "__main__":
    unittest.main()
