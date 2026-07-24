"""规则即数据：人工确认 → 学习规则 → 后续相似情况自动带出。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core import config, db
from core.models import Invoice, FieldValue
from review import service as review


class LearningTest(unittest.TestCase):
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

    def _mk(self, h, issuer="Acme Labs LLC", currency=None):
        inv = Invoice(file_name=f"{h}.pdf", file_hash=h)
        inv.set("invoice_no", FieldValue(raw="INV-1", value="INV-1"))
        inv.set("invoice_date", FieldValue(raw="2026-01-01", value="2026-01-01"))
        inv.set("total_due", FieldValue(raw="100", value="100"))
        inv.set("issuer_name", FieldValue(raw=issuer, value=issuer))
        if currency:
            inv.set("currency_settlement", FieldValue(raw=currency, value=currency))
        db.save_invoice(inv)
        return inv

    def _key(self):
        return db.norm_key("Acme Labs LLC")

    def test_rule_pending_until_enabled(self):
        """所有学习先 pending、不直接生效；启用后才被 lookup/应用。"""
        self._mk("h1")
        review.set_classification("h1", "Consulting Expense", "6400 Consulting", "bob")
        # 待确认：lookup（仅 active）取不到
        self.assertIsNone(db.lookup_classification(self._key()))
        rid = db.list_learned()[0]["id"]
        self.assertEqual(db.list_learned()[0]["status"], "pending")
        # 启用后才生效
        self.assertTrue(db.enable_learned(rid))
        rule = db.lookup_classification(self._key())
        self.assertEqual(rule["account"], "6400 Consulting")

    def test_update_learned_before_enable(self):
        """启用前人工修正规则：只允许改白名单字段，改后再启用按新值生效。"""
        self._mk("h1")
        review.set_classification("h1", "Consulting Expense", "6400 Consulting", "bob")
        rid = db.list_learned()[0]["id"]
        # 改科目 + 尝试改一个不在白名单的字段（应被忽略，不报错也不写入）
        self.assertTrue(db.update_learned(rid, {"account": "6500 Advisory", "status": "active"}))
        row = db.list_learned()[0]
        self.assertEqual(row["account"], "6500 Advisory")
        self.assertEqual(row["status"], "pending")   # status 非白名单，未被改
        # 空/全非法字段 → 不更新
        self.assertFalse(db.update_learned(rid, {"status": "active"}))
        # 整段自由说明 note：任意文字，只作显示、不影响行为
        self.assertTrue(db.update_learned(rid, {"note": "老王家的发票\n一律记咨询费"}))
        self.assertEqual(db.list_learned()[0]["note"], "老王家的发票\n一律记咨询费")
        # 启用后按修正值生效（note 不影响 lookup/行为）
        db.enable_learned(rid)
        self.assertEqual(db.lookup_classification(self._key())["account"], "6500 Advisory")

    def test_classification_applied_after_enable(self):
        self._mk("h1")
        review.set_classification("h1", "Consulting Expense", "6400 Consulting", "bob")
        from extraction import pipeline
        inv2 = Invoice(file_hash="h2")
        inv2.set("issuer_name", FieldValue(raw="Acme Labs LLC", value="Acme Labs LLC"))
        # 未启用：不带出
        self.assertNotEqual(pipeline._classify_with_learned(inv2).account, "6400 Consulting")
        db.enable_learned(db.list_learned()[0]["id"])
        cls = pipeline._classify_with_learned(inv2)
        self.assertEqual(cls.account, "6400 Consulting")
        self.assertIn("learned", cls.hit_rules[0])

    def test_field_default_fills_only_after_enable(self):
        self._mk("h1", currency="USD")
        review.change_field("h1", "currency_settlement", "EUR", "bob", "该供应商恒为EUR")
        from extraction import pipeline
        # 原件上确实出现该值（核对后才填的前提）；pending 时仍不填
        txt = "Acme Labs LLC\nInvoice Ccy: EUR\nTotal 5.00\n"
        inv2 = Invoice(file_hash="h2", raw_pdf_text=txt)
        inv2.set("issuer_name", FieldValue(raw="Acme Labs LLC", value="Acme Labs LLC"))
        pipeline._apply_learned_defaults(inv2)          # 未启用 → 不填
        self.assertIsNone(inv2.f("currency_settlement").value)
        db.enable_learned(db.list_learned()[0]["id"])
        inv3 = Invoice(file_hash="h3", raw_pdf_text=txt)
        inv3.set("issuer_name", FieldValue(raw="Acme Labs LLC", value="Acme Labs LLC"))
        pipeline._apply_learned_defaults(inv3)          # 启用 + 原件核对到 EUR → 填空
        self.assertEqual(inv3.f("currency_settlement").value, "EUR")
        self.assertEqual(inv3.f("currency_settlement").source, "learned")

    def test_learned_default_does_not_override_extracted(self):
        self._mk("h1", currency="USD")
        review.change_field("h1", "currency_settlement", "EUR", "bob")
        db.enable_learned(db.list_learned()[0]["id"])
        from extraction import pipeline
        inv2 = Invoice(file_hash="h2")
        inv2.set("issuer_name", FieldValue(raw="Acme Labs LLC", value="Acme Labs LLC"))
        inv2.set("currency_settlement", FieldValue(raw="GBP", value="GBP"))  # 已抽到 GBP
        pipeline._apply_learned_defaults(inv2)
        self.assertEqual(inv2.f("currency_settlement").value, "GBP")  # 不覆盖已抽到的

    def test_content_suggestion_pending_then_enabled(self):
        from core.models import LineItem
        from decimal import Decimal
        db.learn_content_class("Market Research Report Southeast Asia fintech", "Research Exp", "6500", "bob")
        inv = Invoice()
        inv.line_items = [LineItem(description="Market Research Report on Asia fintech", amount=Decimal("9000"))]
        self.assertEqual(review.classification_suggestions(inv), [])     # pending → 无建议
        db.enable_learned(db.list_learned()[0]["id"])
        sugg = review.classification_suggestions(inv)
        self.assertEqual(len(sugg), 1)
        self.assertEqual(sugg[0]["account"], "6500")
        # 不相似内容无建议
        inv2 = Invoice()
        inv2.line_items = [LineItem(description="Legal contract drafting", amount=Decimal("100"))]
        self.assertEqual(review.classification_suggestions(inv2), [])

    def test_set_classification_learns_content_rule(self):
        from core.models import LineItem
        from decimal import Decimal
        inv = self._mk("h1")
        inv.line_items = [LineItem(description="Cloud platform subscription monthly", amount=Decimal("500"))]
        db.save_invoice(inv)
        review.set_classification("h1", "Software Service", "6110 Software", "bob")
        kinds = {r["rule_type"] for r in db.list_learned()}
        self.assertIn("content_class", kinds)   # 确认分类时也沉淀了内容规则（pending）
        self.assertIn("classification", kinds)

    def test_region_fill_sets_bbox_and_normal_edit_keeps_it(self):
        """框选填入字段 → 按归一化框算出 bbox（能在原件高亮）；普通编辑保留原 bbox。"""
        inv = self._mk("h1")
        inv.page_sizes = [(200.0, 400.0)]            # 一页：200pt × 400pt
        db.save_invoice(inv)
        # 框选页顶一带填到 issuer_address
        review.change_field("h1", "issuer_address", "12 Main St",
                            region={"page": 0, "x0": 0.1, "y0": 0.05, "x1": 0.6, "y1": 0.15})
        bbox = db.get_invoice("h1").f("issuer_address").bbox
        self.assertEqual(bbox[0], 0)
        self.assertAlmostEqual(bbox[1], 20.0)        # 0.1 * 200
        self.assertAlmostEqual(bbox[2], 20.0)        # 0.05 * 400
        self.assertAlmostEqual(bbox[3], 120.0)       # 0.6 * 200
        self.assertAlmostEqual(bbox[4], 60.0)        # 0.15 * 400
        # 普通编辑（无 region）→ bbox 不丢
        review.change_field("h1", "issuer_address", "12 Main Street")
        self.assertEqual(db.get_invoice("h1").f("issuer_address").bbox, bbox)

    def test_infer_split_pattern(self):
        # 换行
        self.assertEqual(review.infer_split_pattern("A\nB\nC", ["A", "B", "C"]), "newline")
        # 分号
        self.assertEqual(review.infer_split_pattern("A; B; C", ["A", "B", "C"]), "semicolon")
        # 编号
        self.assertEqual(review.infer_split_pattern("1. Setup 2. Build 3. Ship",
                                                    ["Setup", "Build", "Ship"]), "numbered")
        # 项目符号
        self.assertEqual(review.infer_split_pattern("• Alpha • Beta", ["Alpha", "Beta"]), "bullet")
        # 对不上 → 不学
        self.assertIsNone(review.infer_split_pattern("A B C", ["A", "B"]))
        self.assertIsNone(review.infer_split_pattern("only one", ["only one"]))

    def test_split_line_item_learns_pending_and_replaces(self):
        from core.models import LineItem
        from decimal import Decimal
        inv = self._mk("h1")
        inv.line_items = [LineItem(description="Design; Develop; Deploy", amount=Decimal("900"))]
        db.save_invoice(inv)
        r = review.split_line_item("h1", 0, ["Design", "Develop", "Deploy"], "bob")
        self.assertEqual(r["segments"], 3)
        self.assertEqual(r["learned_pattern"], "semicolon")
        inv2 = db.get_invoice("h1")
        self.assertEqual(len(inv2.line_items), 3)
        self.assertEqual(inv2.line_items[0].description, "Design")
        self.assertEqual(inv2.line_items[0].amount, Decimal("900"))   # 原金额留在首段
        self.assertIsNone(inv2.line_items[1].amount)
        # 学到的是 pending，未启用 → 无拆分建议
        rule = [x for x in db.list_learned() if x["rule_type"] == "line_split"][0]
        self.assertEqual(rule["status"], "pending")

    def test_split_suggestion_appears_only_after_enable(self):
        from core.models import LineItem
        from decimal import Decimal
        inv = self._mk("h1")
        inv.line_items = [LineItem(description="Design; Develop; Deploy", amount=Decimal("900"))]
        db.save_invoice(inv)
        review.split_line_item("h1", 0, ["Design", "Develop", "Deploy"], "bob")
        # 新发票（同对手方）有一条大段、同样可按分号拆
        inv2 = self._mk("h2")
        inv2.line_items = [LineItem(description="Audit; Review; Report quarterly figures",
                                    amount=Decimal("500"))]
        db.save_invoice(inv2)
        self.assertEqual(review.line_split_suggestions(inv2), [])    # pending → 无建议
        rid = [x for x in db.list_learned() if x["rule_type"] == "line_split"][0]["id"]
        db.enable_learned(rid)
        sugg = review.line_split_suggestions(db.get_invoice("h2"))
        self.assertEqual(len(sugg), 1)
        self.assertEqual(sugg[0]["index"], 0)
        self.assertEqual(sugg[0]["pieces"], ["Audit", "Review", "Report quarterly figures"])

    def test_confirm_count_increments_and_delete(self):
        self._mk("h1")
        review.set_classification("h1", "X", "6000", "bob")
        review.set_classification("h1", "X", "6000", "bob")   # 再确认 → +1（仍 pending）
        row = db.list_learned()[0]
        self.assertEqual(row["confirm_count"], 2)
        self.assertEqual(row["status"], "pending")
        self.assertTrue(db.delete_learned(row["id"]))
        self.assertEqual(db.list_learned(), [])

    def test_change_field_returns_newly_learned_once(self):
        """改字段的返回带上"本次新学到的待启用规则"（供审核页当场弹窗）；重复改同字段不再冒。"""
        self._mk("h1")
        inv = db.get_invoice("h1")
        inv.raw_pdf_text = "Acme Labs LLC\nEmail billing@acme.com\n"
        db.save_invoice(inv)
        r = review.change_field("h1", "issuer_email", "billing@acme.com", "bob")
        types = {x["rule_type"] for x in r.get("learned", [])}
        self.assertIn("field_default", types)               # 学到对手方默认值
        self.assertTrue(all(x["id"] and "rule_type" in x for x in r["learned"]))
        # field_locator 可选全局
        self.assertTrue(any(x["rule_type"] == "field_locator" and x["can_global"] for x in r["learned"]))
        r2 = review.change_field("h1", "issuer_email", "billing@acme.com", "bob")
        self.assertEqual(r2.get("learned"), [])             # 已存在 → 不再重复弹

    def test_classification_returns_learned(self):
        self._mk("h1")
        r = review.set_classification("h1", "Consulting Expense", "6400 Consulting", "bob")
        self.assertTrue(any(x["rule_type"] == "classification" for x in r.get("learned", [])))


if __name__ == "__main__":
    unittest.main()


class LearnedLocatorValidatesTypeTest(unittest.TestCase):
    """已学字段线索注入值须按字段类型校验：坏规则抓来的散文（如"by email to"）不得成为发票号。"""
    def setUp(self):
        import tempfile
        from core import config
        self._d = tempfile.mkdtemp(); self._old = config.DB_PATH
        config.DB_PATH = __import__("pathlib").Path(self._d) / "t.db"
        db._initialized = False; db.init_db()

    def tearDown(self):
        from core import config
        import shutil
        config.DB_PATH = self._old; db._initialized = False
        shutil.rmtree(self._d, ignore_errors=True)

    def test_prose_not_accepted_as_invoice_no(self):
        from extraction.pipeline import _apply_learned_locators
        from extraction import learn
        from core.models import Invoice, FieldValue
        text = "Invoice No.\nGDN-2026-1\nplease send invoice number by email to x@y.com"
        fp = learn.fingerprint(text)
        # 学一条坏的 field_locator：标签 "invoice number" → invoice_no，并启用
        db.learn_field_locator(db.norm_key("Acme"), "invoice_no", "invoice number", fp, by="t")
        db.enable_learned(db.list_learned()[0]["id"])
        inv = Invoice(file_hash="h", raw_pdf_text=text)
        inv.set("issuer_name", FieldValue(raw="Acme", value="Acme"))
        inv.set("invoice_no", FieldValue(raw="GDN-2026-1", value="GDN-2026-1",
                                         source="pdf_text_generic", confidence=0.7))
        _apply_learned_locators(inv)
        # 坏线索抓到的散文（无数字）→ 被 id 类型校验拒绝，保留正确的 generic 值
        self.assertEqual(inv.f("invoice_no").value, "GDN-2026-1")
