"""第五模块·人工审核 后端核心测试：队列 / 详情 / 改字段留痕 / 状态机 / 硬校验。

标准库 unittest，无额外依赖（与项目其余测试一致）。
"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from review import service as review
from core.models import Invoice, FieldValue, ValidationIssue


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._dir) / "t.db"   # 每个用例独立临时库
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._orig_db
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    @staticmethod
    def _mk(file_hash, no="INV-1", date="2026-06-01", total="100.00", approve="Pending"):
        inv = Invoice(file_name=f"{file_hash}.pdf", file_hash=file_hash)
        if no is not None:
            inv.set("invoice_no", FieldValue(raw=no, value=no))
        if date is not None:
            inv.set("invoice_date", FieldValue(raw=date, value=date))
        if total is not None:
            inv.set("total_due", FieldValue(raw=total, value=Decimal(total)))
        inv.approve_status = approve
        db.save_invoice(inv)
        return inv

    def test_queue_and_summary(self):
        self._mk("h1")
        self._mk("h2", approve="Approved")
        self.assertEqual(len(review.review_queue()), 2)
        self.assertEqual(review.queue_summary()["Pending"], 1)
        self.assertEqual(review.queue_summary()["Approved"], 1)
        pend = review.review_queue("Pending")
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["file_hash"], "h1")

    def test_upload_accepts_docx_extension(self):
        """Word .docx 不再被上传白名单拒绝（前端 accept 仅提示，后端白名单才是关口）。"""
        from fastapi.testclient import TestClient
        from gateway.main import app
        config.UPLOAD_DIR = Path(self._dir) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.assertIn(".docx", config.ALLOWED_UPLOAD_EXTS)
        c = TestClient(app)
        # 传一个 .docx（内容非法 → 会有"处理失败"类错误，但**不应**是"不支持的文件类型"）
        r = c.post("/api/upload",
                   files=[("files", ("x.docx", b"not a real docx", "application/octet-stream"))])
        self.assertEqual(r.status_code, 200)
        msg = (r.json()["results"][0].get("error") or "")
        self.assertNotIn("不支持的文件类型", msg)

    def test_failed_extraction_becomes_fillable_queued_record(self):
        """提取失败的文件也入库、进队列、详情展示全部规范字段（空）可人工录入。"""
        from extraction import pipeline
        from core.models import CANONICAL_FIELDS
        config.UPLOAD_DIR = Path(self._dir) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        invs = pipeline.process_upload(b"this is not a valid xlsx", "broken.xlsx")
        self.assertEqual(len(invs), 1)
        h = invs[0].file_hash
        self.assertEqual(invs[0].parse_status, "failed")
        # 进队列且标记失败
        row = next(r for r in review.review_queue() if r["file_hash"] == h)
        self.assertTrue(row["parse_failed"])
        # 详情：全部规范字段（空）可填
        d = review.review_detail(h)
        self.assertEqual(d["parse_status"], "failed")
        self.assertTrue(set(CANONICAL_FIELDS).issubset(set(d["fields"].keys())))
        self.assertIsNone(d["fields"]["invoice_no"]["value"])
        # 人工填入生效
        review.change_field(h, "invoice_no", "MANUAL-1")
        self.assertEqual(review.review_detail(h)["fields"]["invoice_no"]["value"], "MANUAL-1")

    def test_unknown_format_becomes_failed_queued_record(self):
        """未知/不支持自动提取的格式（如 .xyz）：不被拒，入库为 failed、进队列、可人工录入。"""
        from extraction import pipeline
        config.UPLOAD_DIR = Path(self._dir) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        invs = pipeline.process_upload(b"random unsupported bytes", "mystery.xyz")
        self.assertEqual(len(invs), 1)
        self.assertEqual(invs[0].parse_status, "failed")
        row = next(r for r in review.review_queue() if r["file_hash"] == invs[0].file_hash)
        self.assertTrue(row["parse_failed"])                     # 进队列且标失败（会置顶）
        review.change_field(invs[0].file_hash, "invoice_no", "XYZ-1")   # 可人工录入
        self.assertEqual(review.review_detail(invs[0].file_hash)["fields"]["invoice_no"]["value"], "XYZ-1")

    def test_unpreviewable_original_downloadable_page_404(self):
        """无法在线预览的原件（.doc）：/page/0 返回 404（前端降级），/original 可下载原文件。"""
        from fastapi.testclient import TestClient
        from gateway.main import app
        from extraction import pipeline
        config.UPLOAD_DIR = Path(self._dir) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        h = pipeline.process_upload(b"random unsupported bytes", "mystery.xyz")[0].file_hash
        c = TestClient(app)
        self.assertEqual(c.get(f"/api/review/{h}/page/0").status_code, 404)   # 预览降级
        r = c.get(f"/api/review/{h}/original")
        self.assertEqual(r.status_code, 200)                                   # 原件可下载
        self.assertIn("mystery.xyz", r.headers.get("content-disposition", ""))

    def test_queue_sorted_failed_first_then_newest(self):
        """队列排序：提取失败在最前，其次最新上传在前。"""
        def mkinv(h, uploaded, failed=False):
            inv = Invoice(file_name=f"{h}.pdf", file_hash=h)
            inv.set("invoice_no", FieldValue(raw=h, value=h))
            inv.uploaded_at = uploaded
            if failed:
                inv.parse_status = "failed"
            db.save_invoice(inv)
        mkinv("old", "2026-06-01T10:00:00Z")
        mkinv("new", "2026-06-30T10:00:00Z")
        mkinv("bad", "2026-06-15T10:00:00Z", failed=True)
        order = [r["file_hash"] for r in review.review_queue()]
        self.assertEqual(order[0], "bad")            # 失败置顶
        self.assertEqual(order[1:], ["new", "old"])  # 再按上传时间倒序

    def test_export_all_vs_approved_only(self):
        """导出默认含全部记录；approved_only=true 只导出已通过的（审核作入账闸门）。"""
        from fastapi.testclient import TestClient
        from gateway.main import app
        orig_exp = config.EXPORT_DIR
        config.EXPORT_DIR = Path(self._dir) / "exports"
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self._mk("h1", approve="Pending")
            self._mk("h2", approve="Approved")
            c = TestClient(app)
            allr = c.post("/api/export").json()
            self.assertEqual(allr["count"], 2)            # 全部
            self.assertFalse(allr["approved_only"])
            appr = c.post("/api/export?approved_only=true").json()
            self.assertEqual(appr["count"], 1)            # 仅 Approved
            self.assertTrue(appr["approved_only"])
            self.assertIn("approved", appr["file"])       # 文件名可区分
        finally:
            config.EXPORT_DIR = orig_exp

    def test_export_approved_only_empty_errors(self):
        """没有已通过记录时，仅导出已通过应报错而非导空表。"""
        from fastapi.testclient import TestClient
        from gateway.main import app
        orig_exp = config.EXPORT_DIR
        config.EXPORT_DIR = Path(self._dir) / "exports"
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self._mk("h1", approve="Pending")
            c = TestClient(app)
            r = c.post("/api/export?approved_only=true")
            self.assertEqual(r.status_code, 400)
            self.assertIn("已通过", r.json()["error"])
        finally:
            config.EXPORT_DIR = orig_exp

    def test_change_field_logs_old_value(self):
        self._mk("h1", total="100.00")
        review.change_field("h1", "total_due", "120.00", "alice", "OCR 读错")
        d = review.review_detail("h1")
        self.assertEqual(d["fields"]["total_due"]["value"], "120.00")
        self.assertEqual(d["fields"]["total_due"]["source"], "manual_review")
        self.assertEqual(len(d["changes"]), 1)
        chg = d["changes"][0]
        self.assertEqual(chg["old_value"], "100.00")
        self.assertEqual(chg["new_value"], "120.00")
        self.assertEqual(chg["changed_by"], "alice")
        self.assertEqual(chg["reason"], "OCR 读错")

    def test_clear_field_locate(self):
        """清除定位框：识别错位置时去掉高亮——bbox 置空、字段值不变、留痕；二次清除为 no-op。"""
        inv = self._mk("h1")
        inv.set("invoice_no", FieldValue(raw="INV-1", value="INV-1", bbox=[0, 10, 10, 50, 20]))
        db.save_invoice(inv)
        self.assertIsNotNone(review.review_detail("h1")["fields"]["invoice_no"]["bbox"])
        r = review.clear_field_locate("h1", "invoice_no", "bob", "定位错")
        self.assertTrue(r["cleared"])
        d = review.review_detail("h1")
        self.assertIsNone(d["fields"]["invoice_no"]["bbox"])          # 高亮框已清
        self.assertEqual(d["fields"]["invoice_no"]["value"], "INV-1")  # 值不变
        self.assertTrue(any("定位" in (c["new_value"] or "") for c in db.list_changes("h1")))  # 留痕
        self.assertFalse(review.clear_field_locate("h1", "invoice_no")["cleared"])  # 已无框 → no-op

    def test_change_amount_field_accepts_currency_symbols(self):
        """框选带货币符号的金额填总额不再报错，自动取数：US$/HK$/€ 等。"""
        self._mk("h1")
        for raw, want in [("US$10,295.00", "10295.00"), ("HK$500.00", "500.00"),
                          ("$1,200.00", "1200.00"), ("10,295.00", "10295.00")]:
            r = review.change_field("h1", "total_due", raw)
            self.assertEqual(r["new"], want, f"{raw!r} 应解析为 {want}")
        # 子明细金额、明细金额同样容忍符号
        from core.models import LineItem
        inv = db.get_invoice("h1")
        inv.line_items = [LineItem(description="A", amount=Decimal("1"))]
        db.save_invoice(inv)
        review.change_line_item("h1", 0, "amount", "US$ 7,000.00")
        self.assertEqual(str(db.get_invoice("h1").line_items[0].amount), "7000.00")

    def test_dedupe_keeps_one_deletes_rest_skips_approved(self):
        """重复去重：只保留 keep、删其余；已 Approved 的跳过不删。"""
        self._mk("d1")
        self._mk("d2")
        self._mk("d3", approve="Approved")
        r = review.dedupe("d1", ["d1", "d2", "d3"])
        self.assertEqual(r["kept"], "d1")
        self.assertEqual(r["deleted"], ["d2"])
        self.assertEqual(r["skipped"], ["d3"])          # 已入账不删
        self.assertIsNone(db.get_invoice("d2"))
        self.assertIsNotNone(db.get_invoice("d1"))
        self.assertIsNotNone(db.get_invoice("d3"))

    def test_approve_blocked_when_duplicate_of_approved(self):
        """入账时仍疑似重复、且已有入账副本 → 阻断强制比对；force 后放行并清重复标记。"""
        self._mk("A", no="DUP-9", approve="Approved")
        inv = self._mk("B", no="DUP-9")
        inv.issues.append(ValidationIssue("DUPLICATE", "疑似重复", None, "error"))
        db.save_invoice(inv)
        r = review.act("B", "Approved")
        self.assertEqual(r.get("blocked"), "duplicate")
        self.assertEqual([x["file_hash"] for x in r["approved_dups"]], ["A"])
        self.assertNotEqual(review.review_detail("B")["approve_status"], "Approved")  # 没入账
        r2 = review.act("B", "Approved", force=True)
        self.assertEqual(r2["approve_status"], "Approved")
        self.assertFalse(any(i.code == "DUPLICATE" for i in db.get_invoice("B").issues))

    def test_approve_not_blocked_when_dup_not_approved(self):
        """疑似重复，但重复副本尚未入账 → 不拦（正常通过）。"""
        self._mk("A", no="DUP-8")            # Pending，未入账
        inv = self._mk("B", no="DUP-8")
        inv.issues.append(ValidationIssue("DUPLICATE", "疑似重复", None, "error"))
        db.save_invoice(inv)
        r = review.act("B", "Approved")
        self.assertEqual(r.get("approve_status"), "Approved")
        self.assertNotIn("blocked", r)

    def test_candidates_exclude_self_and_selfheal(self):
        """比对候选不含自身；只剩一张时候选为空且过期重复标记自愈清除。"""
        a = self._mk("a", no="INV-9")
        a.add_issue("DUPLICATE", "疑似重复", None, "error"); db.save_invoice(a)
        b = self._mk("b", no="INV-9")
        b.add_issue("DUPLICATE", "疑似重复", None, "error"); db.save_invoice(b)
        review.dedupe("a", ["a", "a", "b"])          # 前端 GROUP 含自身，dedupe 去重
        meta = review.duplicate_candidates("a")
        self.assertEqual(meta["candidates"], [])     # 只剩一张 → 无候选（不含自身）
        self.assertFalse(any(i.code == "DUPLICATE" for i in db.get_invoice("a").issues))  # 自愈清除

    def test_stale_flag_selfheals_on_open(self):
        """孤记录带过期 DUPLICATE 标记：打开比对时自愈清除（不再误跳/误报）。"""
        inv = self._mk("solo", no="ZZ-1")
        inv.add_issue("DUPLICATE", "存量旧标记", None, "error"); db.save_invoice(inv)
        review.duplicate_candidates("solo")
        self.assertFalse(any(i.code == "DUPLICATE" for i in db.get_invoice("solo").issues))

    def test_dedupe_clears_stale_duplicate_flag_on_survivor(self):
        """去重到只剩一张后，保留那张的过期"重复"标记应被清除（不再被当重复/自动跳比对）。"""
        a = self._mk("a", no="DUP-7")
        a.issues.append(ValidationIssue("DUPLICATE", "疑似重复", None, "error")); db.save_invoice(a)
        b = self._mk("b", no="DUP-7")
        b.issues.append(ValidationIssue("DUPLICATE", "疑似重复", None, "error")); db.save_invoice(b)
        review.dedupe("a", ["a", "b"])
        self.assertFalse(any(i.code == "DUPLICATE" for i in db.get_invoice("a").issues))
        # 队列摘要 is_duplicate 也应为 False（前端据此不再自动跳转）
        qa = [it for it in review.review_queue() if it["file_hash"] == "a"][0]
        self.assertFalse(qa["is_duplicate"])

    def test_drop_unapproved_keeps_all_approved(self):
        """和已入账重复：删除所有未入账重复、保留全部已入账。"""
        self._mk("A", approve="Approved")
        self._mk("p1")
        self._mk("p2")
        self._mk("B", approve="Approved")
        r = review.drop_unapproved(["A", "p1", "p2", "B"])
        self.assertEqual(sorted(r["deleted"]), ["p1", "p2"])
        self.assertEqual(sorted(r["kept"]), ["A", "B"])
        self.assertEqual(sorted(db.load_all_invoices().keys()), ["A", "B"])

    def test_manual_date_normalized_and_currency_normalized(self):
        """手工编辑也走字段类型归一化：日期→ISO(不可解析则标待复核)，币种→代码/大写。"""
        self._mk("h1")
        # 日期：多种写法归一化到 ISO
        review.change_field("h1", "invoice_date", "2 June 2026")
        self.assertEqual(review.review_detail("h1")["fields"]["invoice_date"]["value"], "2026-06-02")
        review.change_field("h1", "payment_due_date", "16 March 2025")
        self.assertEqual(review.review_detail("h1")["fields"]["payment_due_date"]["value"], "2025-03-16")
        # 不可解析的日期：保留原文 + 标待复核
        review.change_field("h1", "service_start", "sometime Q2")
        d = review.review_detail("h1")["fields"]["service_start"]
        self.assertEqual(d["value"], "sometime Q2")
        self.assertIn("待复核", d.get("note") or "")
        # 币种：符号→代码、小写→大写
        review.change_field("h1", "currency_settlement", "US$")
        self.assertEqual(review.review_detail("h1")["fields"]["currency_settlement"]["value"], "USD")
        review.change_field("h1", "invoice_ccy_raw", "eur")
        self.assertEqual(review.review_detail("h1")["fields"]["invoice_ccy_raw"]["value"], "EUR")

    def _codes(self, h):
        return {i["code"] for i in review.review_detail(h)["issues"]}

    def test_edit_revalidates_and_clears_stale_mismatch(self):
        """改字段后就地重算：修正总额 → 旧的『总额不一致』消失、风险回落。"""
        inv = self._mk("r1", total="100.00")
        inv.set("subtotal", FieldValue(raw="90.00", value=Decimal("90.00")))
        inv.set("sales_tax", FieldValue(raw="0.00", value=Decimal("0.00")))
        db.save_invoice(inv)
        review.change_field("r1", "total_due", "100.00")   # 触发重算：90+0≠100 → 不一致
        self.assertIn("TOTAL_MISMATCH", self._codes("r1"))
        review.change_field("r1", "total_due", "90.00")    # 修正为对平 → 不一致应消失
        self.assertNotIn("TOTAL_MISMATCH", self._codes("r1"))

    def test_edit_revalidates_flags_new_mismatch(self):
        """本来对平，手改成对不上 → 重算即报『总额不一致』。"""
        inv = self._mk("r2", total="90.00")
        inv.set("subtotal", FieldValue(raw="90.00", value=Decimal("90.00")))
        inv.set("sales_tax", FieldValue(raw="0.00", value=Decimal("0.00")))
        db.save_invoice(inv)
        review.change_field("r2", "total_due", "90.00")
        self.assertNotIn("TOTAL_MISMATCH", self._codes("r2"))
        review.change_field("r2", "total_due", "500.00")   # 改错
        self.assertIn("TOTAL_MISMATCH", self._codes("r2"))

    def test_approve_blocked_on_mismatch_unless_reason(self):
        """Approve 前重算勾稽：账对不上时挡住；填原因显式放行。"""
        inv = self._mk("r3", total="500.00")
        inv.set("subtotal", FieldValue(raw="90.00", value=Decimal("90.00")))
        inv.set("sales_tax", FieldValue(raw="0.00", value=Decimal("0.00")))
        db.save_invoice(inv)
        with self.assertRaises(ValueError):
            review.act("r3", "Approved")
        # 填原因显式放行
        r = review.act("r3", "Approved", reason="尾差已核对，供应商确认")
        self.assertEqual(r["approve_status"], "Approved")

    def test_approve_blocked_on_unparseable_required_date(self):
        """必填日期不可解析(待复核) → 挡住直接通过。"""
        self._mk("r4")
        review.change_field("r4", "invoice_date", "sometime Q2")   # 存原文 + 待复核
        with self.assertRaises(ValueError):
            review.act("r4", "Approved")

    def test_sub_item_date_normalized(self):
        from core.models import LineItem
        inv = self._mk("r5")
        inv.line_items.append(LineItem(description="svc", amount=Decimal("10.00"),
                                       sub_items=[{"date": None, "description": None, "amount": None}]))
        db.save_invoice(inv)
        review.change_sub_item("r5", 0, 0, "date", "16 March 2025")
        d = review.review_detail("r5")
        self.assertEqual(d["line_items"][0]["sub_items"][0]["date"], "2025-03-16")

    def test_tax_rate_normalized(self):
        self._mk("r6")
        review.change_field("r6", "tax_rate", "15")
        self.assertEqual(review.review_detail("r6")["fields"]["tax_rate"]["value"], "15%")
        review.change_field("r6", "tax_rate", "0%")
        self.assertEqual(review.review_detail("r6")["fields"]["tax_rate"]["value"], "0%")

    def test_change_amount_field_rejects_non_number(self):
        self._mk("h1")
        with self.assertRaises(ValueError):
            review.change_field("h1", "total_due", "abc")     # 真的没数字才报错

    def test_delete_invoice_removes_record_and_logs(self):
        self._mk("hD", no="INV-D")
        review.delete_invoice("hD", "bob", "测试脏数据")
        self.assertIsNone(db.get_invoice("hD"))                 # 记录已不存在
        self.assertFalse(any(q["file_hash"] == "hD" for q in review.review_queue()))
        self.assertTrue(any(c["field"] == "_deleted" for c in db.list_changes("hD")))  # 留痕

    def test_delete_approved_blocked(self):
        self._mk("hE", no="INV-E", approve="Approved")
        with self.assertRaises(ValueError):
            review.delete_invoice("hE")
        self.assertIsNotNone(db.get_invoice("hE"))              # 仍在

    def test_find_duplicate_semantics(self):
        """相同文件=重复上传（默认检测）；same_file=False 时跳过（供内部重处理）；
        相同发票号但异文件=重复。"""
        self._mk("hA", no="INV-9")
        # 用户重复上传同一文件 → 重复
        self.assertIn("相同文件", db.find_duplicate("hA", "INV-9") or "")
        # 系统内部重处理同一文件 → 跳过相同文件判定
        self.assertIsNone(db.find_duplicate("hA", "INV-9", same_file=False))
        # 另一文件用相同发票号 → 重复
        self.assertIn("相同发票号", db.find_duplicate("hB", "INV-9", same_file=False) or "")
        # 不同文件、不同发票号 → 非重复
        self.assertIsNone(db.find_duplicate("hB", "INV-OTHER", same_file=False))

    def test_line_item_edit_and_delete(self):
        from core.models import LineItem
        from decimal import Decimal
        inv = self._mk("h1")
        inv.line_items = [LineItem(description="A", amount=Decimal("10")),
                          LineItem(description="B", amount=Decimal("20")),
                          LineItem(description="多识别", amount=None)]
        db.save_invoice(inv)
        review.change_line_item("h1", 0, "description", "改过", "bob")
        review.change_line_item("h1", 0, "amount", "99.50", "bob")
        d = review.review_detail("h1")
        self.assertEqual(d["line_items"][0]["description"], "改过")
        self.assertEqual(d["line_items"][0]["amount"], "99.50")
        review.delete_line_item("h1", 2, "bob", "多识别")     # 删最后一行
        d2 = review.review_detail("h1")
        self.assertEqual(len(d2["line_items"]), 2)
        self.assertTrue(any(c["field"].startswith("line_item[") for c in d2["changes"]))

    def test_line_item_add(self):
        inv = self._mk("h1")
        inv.line_items = []
        db.save_invoice(inv)
        r = review.add_line_item("h1", "bob")
        self.assertEqual(r["index"], 0)
        review.change_line_item("h1", 0, "description", "漏掉的服务", "bob")
        d = review.review_detail("h1")
        self.assertEqual(len(d["line_items"]), 1)
        self.assertEqual(d["line_items"][0]["description"], "漏掉的服务")

    def test_edit_subitem_and_reconcile(self):
        """勾稽子明细可改/加/删，改金额时勾稽状态实时翻转（对上/不平）。"""
        from core.models import LineItem
        inv = self._mk("h1")
        inv.line_items = [LineItem(description="Disbursements", amount=Decimal("3000.00"),
                          sub_items=[{"date": None, "description": "App", "amount": "1200.00"},
                                     {"date": None, "description": "Apr", "amount": "1800.00"}])]
        db.save_invoice(inv)
        self.assertTrue(review.review_detail("h1")["line_items"][0]["reconcile"]["matched"])   # 1200+1800=3000
        r = review.change_sub_item("h1", 0, 0, "amount", "1000.00", "bob")                    # 改子行→不平
        self.assertFalse(r["reconcile"]["matched"])
        self.assertEqual(review.review_detail("h1")["line_items"][0]["sub_items"][0]["amount"], "1000.00")
        self.assertTrue(review.change_sub_item("h1", 0, 0, "amount", "1200.00")["reconcile"]["matched"])  # 改回→对上
        self.assertFalse(review.delete_sub_item("h1", 0, 1, "bob")["reconcile"]["matched"])   # 删一条→只剩1200，不平
        self.assertTrue(review.change_line_item("h1", 0, "amount", "1200.00")["reconcile"]["matched"])   # 改行金额→对上
        review.add_sub_item("h1", 0, "bob")                                                   # 加空子行（金额空跳过）
        self.assertTrue(review.review_detail("h1")["line_items"][0]["reconcile"]["matched"])
        with self.assertRaises(ValueError):
            review.change_sub_item("h1", 0, 0, "amount", "abc")                               # 非法金额
        with self.assertRaises(ValueError):
            review.change_sub_item("h1", 0, 9, "amount", "1")                                 # 越界

    def test_add_line_item_from_region(self):
        """框选加明细：带描述/金额/区域 → 新明细预填值 + region 转 bbox（可高亮）。"""
        inv = self._mk("h1")
        inv.page_sizes = [[600, 800]]
        db.save_invoice(inv)
        r = review.add_line_item("h1", "bob", "", description="Consulting fee",
                                 amount="500.00", region={"page": 0, "x0": 0.1, "y0": 0.2, "x1": 0.5, "y1": 0.25})
        li = review.review_detail("h1")["line_items"][r["index"]]
        self.assertEqual(li["description"], "Consulting fee")
        self.assertEqual(li["amount"], "500.00")
        self.assertIsNotNone(li["bbox"])                 # 框选区域 → bbox，可在原件高亮
        self.assertEqual(li["bbox"][0], 0)               # page

    def test_resolve_line_item_bbox(self):
        """明细按描述文本定位到原件坐标（与金额框取并集覆盖整行）。"""
        from extraction import locate
        from core.models import Invoice, LineItem
        inv = Invoice()
        inv.line_items = [LineItem(description="Professional Fees", amount_raw="7,000.00")]
        words = [(0, 43, 274, 140, 284, "Professional"), (0, 95, 274, 140, 284, "Fees"),
                 (0, 523, 274, 580, 284, "7,000.00")]
        locate.resolve_line_item_bboxes(inv, words)
        b = inv.line_items[0].bbox
        self.assertIsNotNone(b)
        self.assertEqual(b[0], 0)
        self.assertLessEqual(b[3], 581)                  # 右边界含金额列

    def test_resolve_sub_item_bbox(self):
        """勾稽子明细各自按金额行定位到原件（供紫色高亮）。"""
        from extraction import locate
        from core.models import Invoice, LineItem
        inv = Invoice()
        inv.line_items = [LineItem(description="Disbursements", amount_raw="3,000.00",
                          sub_items=[{"date": "19/03", "description": "App Fee", "amount": "1,200.00"}])]
        words = [(1, 43, 300, 120, 310, "App"), (1, 125, 300, 300, 310, "Fee"),
                 (1, 520, 300, 580, 310, "1,200.00")]
        locate.resolve_line_item_bboxes(inv, words)
        b = inv.line_items[0].sub_items[0].get("bbox")
        self.assertIsNotNone(b)
        self.assertEqual(b[0], 1)          # 页码
        self.assertEqual(b[1], 43)         # 行左缘（金额行扩成整行）

    def test_line_item_bbox_amount_row_fallback(self):
        """长/花描述文本匹配不到时，用金额所在整行兜底覆盖该明细（左右缘含整行）。"""
        from extraction import locate
        from core.models import Invoice, LineItem
        inv = Invoice()
        inv.line_items = [LineItem(description="unmatchable long narrative XYZ", amount_raw="500.00")]
        words = [(0, 43, 100, 120, 110, "Totally"), (0, 125, 100, 300, 110, "different"),
                 (0, 520, 100, 580, 110, "500.00")]
        locate.resolve_line_item_bboxes(inv, words)
        b = inv.line_items[0].bbox
        self.assertIsNotNone(b)
        self.assertEqual(b[1], 43)      # 行左缘（扩成整行）
        self.assertEqual(b[3], 580)     # 行右缘（含金额）

    def test_line_item_bad_index_and_bad_number(self):
        from core.models import LineItem
        inv = self._mk("h1")
        inv.line_items = [LineItem(description="A")]
        db.save_invoice(inv)
        with self.assertRaises(ValueError):
            review.delete_line_item("h1", 5)                  # 越界
        with self.assertRaises(ValueError):
            review.change_line_item("h1", 0, "amount", "abc")  # 非数字

    def test_fields_in_canonical_order(self):
        inv = self._mk("h1")
        inv.set("customer_name", FieldValue(raw="Acme", value="Acme"))
        inv.set("customer_address", FieldValue(raw="1 St", value="1 St"))
        db.save_invoice(inv)
        keys = list(review.review_detail("h1")["fields"].keys())
        self.assertEqual(keys[keys.index("customer_name") + 1], "customer_address")

    def test_duplicate_candidates_and_resolve(self):
        """疑似重复候选列出 + 人工确认重复 → 本件被拒绝并留痕。"""
        self._mk("hA", no="INV-9")
        self._mk("hB", no="INV-9")          # 不同文件、相同发票号
        meta = review.duplicate_candidates("hB")
        hashes = [c["file_hash"] for c in meta["candidates"]]
        self.assertIn("hA", hashes)         # 异文件同号候选
        self.assertNotIn("hB", hashes)      # **不把自身列成候选**（否则只剩一张仍显示"还有重复"）
        r = review.resolve_duplicate("hB", "hA", True, "bob", "同一张发票")
        self.assertEqual(r["resolution"], "confirmed_duplicate")
        d = review.review_detail("hB")
        self.assertEqual(d["approve_status"], "Rejected")
        self.assertTrue(any(c["field"] == "_duplicate_check" for c in d["changes"]))

    def test_resolve_not_duplicate_clears_flag(self):
        """确认非重复：清除 DUPLICATE 标记、保持待审、队列不再标重复。"""
        inv = self._mk("hX", no="INV-7")
        inv.add_issue("DUPLICATE", "疑似重复", None, "error")
        db.save_invoice(inv)
        review.resolve_duplicate("hX", "hY", False, "bob", "不是同一张")
        q = next(x for x in review.review_queue() if x["file_hash"] == "hX")
        self.assertFalse(q["is_duplicate"])               # 重复标记已清
        self.assertEqual(q["approve_status"], "Pending")  # 仍待审、可见

    def test_line_items_in_detail(self):
        from core.models import LineItem
        inv = self._mk("h1")
        inv.line_items = [
            LineItem(description="Fund Administration", quantity=Decimal("3"),
                     unit_price=Decimal("44500"), amount=Decimal("133500")),
            LineItem(description="Tax Advisory", quantity=Decimal("2"),
                     unit_price=Decimal("32500"), amount=Decimal("65000")),
        ]
        db.save_invoice(inv)
        d = review.review_detail("h1")
        self.assertEqual(len(d["line_items"]), 2)
        self.assertEqual(d["line_items"][1]["description"], "Tax Advisory")
        self.assertEqual(d["line_items"][1]["quantity"], "2")
        self.assertEqual(d["line_items"][0]["amount"], "133500")

    def test_classify_comprehensive_keywords(self):
        """分类关键词全面覆盖：各常见类别的同义词都能命中对应科目（不只补单一情况）。"""
        from extraction.classify import engine
        from core.models import Invoice, LineItem
        cases = {
            "Professional Fees": "6410 Professional Fees",
            "Consulting advisory": "6410 Professional Fees",
            "Legal services rendered": "6420 Legal Fees",
            "Audit fee": "6430 Audit Fees",
            "Bookkeeping services": "6440 Accounting Fees",
            "Tax advisory": "6470 Tax Advisory",
            "Company registration fee": "6460 Government & Registration Fees",
            "Disbursements": "6910 Disbursements",
            "Sundry Expenses": "6900 Sundry Expenses",
            "Annual software subscription": "6110 Software & Cloud",
            "Bank wire fee": "6310 Bank Charges",
            "Flight to London": "6220 Travel & Lodging",
            "Taxi ride": "6210 Transportation",
            "Team dinner catering": "6230 Meals & Entertainment",
            "Office rent": "6510 Rent",
            "Electricity bill": "6520 Utilities",
            "Internet broadband": "6530 Telecom & Internet",
            "Professional indemnity insurance": "6540 Insurance",
            "Google Ads campaign": "6610 Marketing & Advertising",
            "Training workshop": "6620 Training & Development",
            "Recruitment placement fee": "6720 Recruitment",
            "DHL courier": "6320 Shipping & Courier",
            "Loan interest": "7010 Interest Expense",
        }
        for desc, acct in cases.items():
            inv = Invoice()
            inv.line_items = [LineItem(description=desc, amount=Decimal("100"))]
            self.assertEqual(engine.classify(inv).account, acct, f"{desc!r} 应分到 {acct}")

    def test_classification_options(self):
        """分类下拉候选 = 规则种子 + 已学规则，去重；review_detail 带出。"""
        opts = review.classification_options()
        cats = {o["category"] for o in opts}
        self.assertIn("Professional Service", cats)          # 规则种子
        self.assertIn("Management Fee Expense", cats)        # 描述规则
        # 人工学一条新分类 → 出现在候选里
        db.learn_classification(db.norm_key("Ogier"), "Legal Advisory", "6420 Legal", "bob")
        opts2 = review.classification_options()
        self.assertTrue(any(o["category"] == "Legal Advisory" and o["account"] == "6420 Legal" for o in opts2))
        # 去重：无重复 (category, account) 对
        keys = [(o["category"], o["account"]) for o in opts2]
        self.assertEqual(len(keys), len(set(keys)))
        # review_detail 带出
        self._mk("h1")
        self.assertIn("category_options", review.review_detail("h1"))

    def test_set_classification_logs_and_confirms(self):
        self._mk("h1")
        review.set_classification("h1", "Fund Administration Expense", "6050 Fund Admin", "bob")
        d = review.review_detail("h1")
        self.assertEqual(d["classification"]["category"], "Fund Administration Expense")
        self.assertEqual(d["classification"]["account"], "6050 Fund Admin")
        self.assertFalse(d["classification"]["needs_review"])
        self.assertTrue(any(c["field"] == "_classification" for c in d["changes"]))

    def test_approve_blocked_when_required_missing(self):
        self._mk("h3", date=None, total=None)   # 缺 invoice_date / total_due
        with self.assertRaises(ValueError):
            review.act("h3", "Approved")
        self.assertEqual(review.review_detail("h3")["approve_status"], "Pending")

    def test_approve_flow(self):
        self._mk("h1")
        r = review.act("h1", "Approved", "bob")
        self.assertEqual(r["approve_status"], "Approved")
        self.assertEqual(review.review_detail("h1")["approve_status"], "Approved")

    def test_reject_needs_reason(self):
        self._mk("h1")
        with self.assertRaises(ValueError):
            review.act("h1", "Rejected")          # 无原因 → 拒绝
        review.act("h1", "Rejected", "bob", "重复发票")
        d = review.review_detail("h1")
        self.assertEqual(d["approve_status"], "Rejected")
        self.assertTrue(any("重复发票" in (c["reason"] or "") for c in d["changes"]))

    def test_hold_keeps_in_queue(self):
        self._mk("h1")
        review.act("h1", "Hold", "bob", "待补合同")
        self.assertEqual(review.queue_summary()["Hold"], 1)

    def test_unknown_action_rejected(self):
        self._mk("h1")
        with self.assertRaises(ValueError):
            review.act("h1", "Whatever")

    def test_detail_not_found(self):
        self.assertIsNone(review.review_detail("nope"))


if __name__ == "__main__":
    unittest.main()
