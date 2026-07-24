"""审核期「字段定位线索」学习：软先验、非死模板。

覆盖：纯函数(指纹/取标签/按标签取值)、审核中捕获(change_field→pending 线索)、
提取时应用(补空/弱字段、不覆盖可信值、找不到即忽略)。
"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from core.models import Invoice, FieldValue
from review import service as review
from extraction import learn, pipeline


class LearnLocatorTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = Path(self._dir) / "t.db"
        config.UPLOAD_DIR = Path(self._dir) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    _TXT = "Invoice number:\n6024081\nGross Total\nUS$10,295.00\n"

    # ---- 纯函数 ----
    def test_derive_label_amount_by_numeric_core(self):
        # 人工填干净数字，原文带 US$/千分位 → 按数字核找到，标签取上一行
        self.assertEqual(learn.derive_label(self._TXT, "10295.00", "total_due"), "Gross Total")
        # 同行标签: "Invoice number:" 后就是值
        self.assertEqual(learn.derive_label(self._TXT, "6024081", "invoice_no"), "Invoice number")

    def test_value_by_label_typed_and_validated(self):
        # 复用 generic._MONEY 后保留字母前缀币种（US$），parse_amount 仍解析为 10295.00
        self.assertEqual(learn.value_by_label(self._TXT, "Gross Total", "total_due"), "US$10,295.00")
        self.assertEqual(learn.value_by_label(self._TXT, "Invoice number", "invoice_no"), "6024081")
        # 标签不在本文 → None（忽略、回退）
        self.assertIsNone(learn.value_by_label(self._TXT, "Grand Total Due", "total_due"))

    def test_value_by_label_intl_number_formats(self):
        # 复用 generic._MONEY：瑞士撇号/欧式/空格千分位都完整取值（曾把 111'780.00 截成 111）
        self.assertEqual(learn.value_by_label("Subtotal: CHF 111'780.00", "Subtotal", "subtotal"),
                         "111'780.00")
        self.assertEqual(learn.value_by_label("Subtotal 88 400,00", "Subtotal", "subtotal"),
                         "88 400,00")
        self.assertEqual(learn.value_by_label("Subtotal 1.234,56", "Subtotal", "subtotal"),
                         "1.234,56")
        # 标签锚定下的裸整数也认
        self.assertEqual(learn.value_by_label("Total Due 950", "Total Due", "total_due"), "950")

    def test_fingerprint_stable_for_same_labels(self):
        fp1 = learn.fingerprint(self._TXT)
        fp2 = learn.fingerprint("Gross Total\nUS$1.00\nInvoice number: X\n")  # 同标签集
        self.assertTrue(fp1 and fp1 == fp2)

    # ---- 捕获 ----
    def test_change_field_captures_pending_locator(self):
        inv = Invoice(file_name="a.pdf", file_hash="h1")
        inv.set("issuer_name", FieldValue(raw="Ogier", value="Ogier"))
        inv.raw_pdf_text = self._TXT
        db.save_invoice(inv)
        review.change_field("h1", "total_due", "10295.00", "bob")   # 人工确认总额
        locs = [r for r in db.list_learned() if r["rule_type"] == "field_locator"]
        self.assertTrue(any(r["target"] == "total_due" and r["value"] == "Gross Total" for r in locs))
        self.assertTrue(all(r["status"] != "active" for r in locs))  # 先 pending

    # ---- 应用 ----
    def _seed_active_locator(self, field, label, key="ogier", fp=""):
        db.learn_field_locator(key, field, label, fp, "bob")
        rid = [r["id"] for r in db.list_learned()
               if r["rule_type"] == "field_locator" and r["target"] == field][0]
        db.enable_learned(rid)

    def _inv(self, issuer="Ogier"):
        inv = Invoice(file_name="n.pdf", file_hash="n1", raw_pdf_text=self._TXT)
        inv.set("issuer_name", FieldValue(raw=issuer, value=issuer))
        return inv

    def test_apply_fills_empty_field(self):
        self._seed_active_locator("total_due", "Gross Total")
        inv = self._inv()
        pipeline._apply_learned_locators(inv)
        self.assertEqual(inv.f("total_due").value, Decimal("10295.00"))
        self.assertEqual(inv.f("total_due").source, "learned")     # 标来源、进人工复核

    def test_apply_does_not_override_confident_value(self):
        self._seed_active_locator("total_due", "Gross Total")
        inv = self._inv()
        inv.set("total_due", FieldValue(raw="999.00", value=Decimal("999.00"),
                                        confidence=1.0, source="pdf_text"))
        pipeline._apply_learned_locators(inv)
        self.assertEqual(inv.f("total_due").value, Decimal("999.00"))  # 精确命中不被覆盖

    def test_apply_overrides_generic_lowconf(self):
        self._seed_active_locator("total_due", "Gross Total")
        inv = self._inv()
        inv.set("total_due", FieldValue(raw="3295.00", value=Decimal("3295.00"),
                                        confidence=0.90, source="pdf_text_generic"))
        pipeline._apply_learned_locators(inv)
        self.assertEqual(inv.f("total_due").value, Decimal("10295.00"))  # 通用低置信可被纠正

    def test_apply_noop_when_label_absent(self):
        self._seed_active_locator("total_due", "Amount Payable Now")   # 本文没有该标签
        inv = self._inv()
        pipeline._apply_learned_locators(inv)
        self.assertIsNone(inv.f("total_due").value)                    # 找不到→忽略、回退

    def test_apply_noop_when_no_active_rules(self):
        inv = self._inv()
        pipeline._apply_learned_locators(inv)                          # 学习表空 → 无操作
        self.assertIsNone(inv.f("total_due").value)

    def _locator_id(self, field="total_due"):
        return [r["id"] for r in db.list_learned()
                if r["rule_type"] == "field_locator" and r["target"] == field][0]

    def test_scoped_locator_not_applied_to_other_issuer(self):
        """仅此类：换一个开票方(且指纹不同)→ 不套用（软先验、不跨类）。"""
        db.learn_field_locator("ogier", "total_due", "Gross Total", "fpA", "bob")
        db.enable_learned(self._locator_id(), make_global=False)
        other = Invoice(file_name="o.pdf", file_hash="o1", raw_pdf_text=self._TXT)
        other.set("issuer_name", FieldValue(raw="Totally Different Ltd", value="Totally Different Ltd"))
        # 指纹按文本算；这里文本相同→指纹会相同，为验证"仅此类不跨开票方"，改文本使指纹不同
        other.raw_pdf_text = "Amount:\n1\nGross Total\nUS$10,295.00\nRef:\nx\n"
        pipeline._apply_learned_locators(other)
        self.assertIsNone(other.f("total_due").value)                  # 不同开票方+不同指纹 → 不套用

    def test_field_default_verify_then_fill(self):
        """对手方默认值：**核对到原件上有该值才填**（不盲填，防过拟合）。"""
        addr = "11th Floor Central Tower 28 Queen's Road Central Central Hong Kong"
        db.learn_field_default("ogier", "issuer_address", addr, "bob")
        rid = [r["id"] for r in db.list_learned() if r["rule_type"] == "field_default"][0]
        db.enable_learned(rid)
        # (a) 该 ogier 发票原件里**有**这个地址（多行）→ 核对通过、填入
        inv = Invoice(file_name="a.pdf", file_hash="a1",
                      raw_pdf_text="Ogier\n11th Floor Central Tower\n28 Queen's Road Central\nCentral\nHong Kong\n")
        inv.set("issuer_name", FieldValue(raw="Ogier", value="Ogier"))
        pipeline._apply_learned_defaults(inv)
        self.assertEqual(inv.f("issuer_address").value, addr)
        self.assertEqual(inv.f("issuer_address").source, "learned")
        # (b) 另一张 ogier 发票原件里**没有**这个地址 → 不盲填（留空交人工）
        inv2 = Invoice(file_name="b.pdf", file_hash="b1",
                       raw_pdf_text="Ogier\nNew Office 5 Some Other Street\nLondon\n")
        inv2.set("issuer_name", FieldValue(raw="Ogier", value="Ogier"))
        pipeline._apply_learned_defaults(inv2)
        self.assertIsNone(inv2.f("issuer_address").value)

    def test_field_default_verify_then_fill_is_general(self):
        """核对后再填对**所有**默认值字段通用（不只地址）：邮箱/电话/银行 SWIFT 同样先核对。"""
        for field, val, in_text, not_text in [
            ("issuer_email", "billing@ogier.com",
             "Ogier\nE billing@ogier.com\n", "Ogier\nno email here\n"),
            ("issuer_phone", "+852 3656 6000",
             "Ogier\nT +852 3656 6000\n", "Ogier\nT +1 000\n"),
            ("bank_swift", "HSBCHKHHHKH",
             "Swift Code: HSBCHKHHHKH\n", "Swift Code: OTHERXXX\n"),
        ]:
            db.learn_field_default("ogier", field, val, "bob")
            rid = [r["id"] for r in db.list_learned()
                   if r["rule_type"] == "field_default" and r["target"] == field][0]
            db.enable_learned(rid)
            hit = Invoice(file_hash="hit", raw_pdf_text=in_text)
            hit.set("issuer_name", FieldValue(raw="Ogier", value="Ogier"))
            pipeline._apply_learned_defaults(hit)
            self.assertEqual(hit.f(field).value, val, f"{field}: 原件有该值应填")
            miss = Invoice(file_hash="miss", raw_pdf_text=not_text)
            miss.set("issuer_name", FieldValue(raw="Ogier", value="Ogier"))
            pipeline._apply_learned_defaults(miss)
            self.assertIsNone(miss.f(field).value, f"{field}: 原件没有该值不应盲填")

    def _syn_pdf(self):
        import fitz
        doc = fitz.open(); page = doc.new_page(width=420, height=320)
        lines = ["ACME Consulting LLC", "Invoice number: INV-9", "Invoice date: 2 June 2026",
                 "Bill to: Client Co", "Description   Value", "Consulting fee    500.00",
                 "Gross Total   US$500.00"]
        for i, l in enumerate(lines):
            page.insert_text((40, 44 + i * 24), l)
        data = doc.tobytes(); doc.close()
        return data

    def test_reapply_reextracts_but_keeps_manual(self):
        """『按最新规则重新提取』：从原件重跑，保留人工改过的字段；已通过的跳过。"""
        from extraction import pipeline
        inv = pipeline.process_upload(self._syn_pdf(), "syn.pdf")[0]
        h = inv.file_hash
        self.assertEqual(inv.f("total_due").value, Decimal("500.00"))
        review.change_field(h, "customer_name", "手工客户", "bob")     # 人工改一个字段
        r = review.reapply_learned(h)                                   # 重新提取
        self.assertTrue(r["applied"], r)
        d = db.get_invoice(h)
        self.assertEqual(d.f("customer_name").value, "手工客户")        # 人工改的保留
        self.assertEqual(d.f("total_due").value, Decimal("500.00"))     # 其余重新提取
        d.approve_status = "Approved"; db.save_invoice(d)               # 已通过 → 跳过
        self.assertFalse(review.reapply_learned(h)["applied"])

    def test_reapply_revalidates_against_final_values(self):
        """重新提取后覆盖回人工值 → 校验须按**最终值**算：人工把总额改成与小计对不平，
        重提后仍应报 TOTAL_MISMATCH（而非按重提的原始值判过）。"""
        from extraction import pipeline
        inv = pipeline.process_upload(self._syn_pdf(), "syn.pdf")[0]
        h = inv.file_hash
        # 明细合计 500；人工把总额改成 999.00（与明细合计对不平 → LINE_SUM_MISMATCH）
        review.change_field(h, "total_due", "999.00", "bob")
        self.assertIn("LINE_SUM_MISMATCH", {i.code for i in db.get_invoice(h).issues})
        review.reapply_learned(h)                       # 重新提取（保留人工改的 999.00）
        d = db.get_invoice(h)
        self.assertEqual(d.f("total_due").value, Decimal("999.00"))       # 人工值保留
        # 未修复时：校验按重提原始值(500)算 → 无此问题；修复后按最终值(999) → 仍报不一致
        self.assertIn("LINE_SUM_MISMATCH", {i.code for i in d.issues})

    def test_global_locator_applied_to_any_issuer(self):
        """启用为全局同义词：任何开票方/版面，只要出现该标签就现场提取。"""
        db.learn_field_locator("ogier", "total_due", "Gross Total", "fpA", "bob")
        db.enable_learned(self._locator_id(), make_global=True)
        other = Invoice(file_name="o.pdf", file_hash="o1",
                        raw_pdf_text="Amount:\n1\nGross Total\nUS$10,295.00\nRef:\nx\n")
        other.set("issuer_name", FieldValue(raw="Totally Different Ltd", value="Totally Different Ltd"))
        pipeline._apply_learned_locators(other)
        self.assertEqual(other.f("total_due").value, Decimal("10295.00"))   # 全局 → 跨开票方生效
        self.assertEqual(other.f("total_due").source, "learned")


if __name__ == "__main__":
    unittest.main()
