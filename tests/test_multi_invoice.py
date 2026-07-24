"""多发票文件：物理拆成"一文件一发票"后走单张路径。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core import config, db


def _has_fitz():
    try:
        import fitz  # noqa
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_fitz(), "需要 PyMuPDF")
class TestMultiInvoiceSplit(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = Path(self._dir) / "t.db"
        config.UPLOAD_DIR = Path(self._dir) / "uploads"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def _page(self, doc, *lines):
        import fitz
        pg = doc.new_page(width=400, height=560)
        y = 40
        for ln in lines:
            pg.insert_text((40, y), ln, fontsize=11)
            y += 20

    def test_page_ranges_group_continuation_pages(self):
        """发票1跨两页（续页无 INVOICE 标题）、发票2单页 → 2 段 [(0,1),(2,2)]。"""
        import fitz
        from extraction import pipeline
        doc = fitz.open()
        self._page(doc, "INVOICE", "INVOICE NO. A-1", "TOTAL DUE 100.00")
        self._page(doc, "BANK DETAILS", "Account No. 123")            # 续页：无 INVOICE 标题
        self._page(doc, "INVOICE", "INVOICE NO. B-2", "TOTAL DUE 200.00")
        p = Path(self._dir) / "multi.pdf"
        doc.save(p)
        doc.close()
        ranges = pipeline._invoice_page_ranges(p, n_inv=2)
        self.assertEqual(ranges, [(0, 1), (2, 2)])

    def test_completeness_rejects_bad_boundary(self):
        """完整性校验：某段不含恰好一个 TOTAL DUE（边界不可信）→ 不拆（返回 None）。"""
        import fitz
        from extraction import pipeline
        doc = fitz.open()
        self._page(doc, "INVOICE", "INVOICE NO. A-1")               # 无 TOTAL DUE 的段
        self._page(doc, "INVOICE", "INVOICE NO. B-2", "TOTAL DUE 100.00", "TOTAL DUE 200.00")
        p = Path(self._dir) / "bad.pdf"
        doc.save(p)
        doc.close()
        # n_inv=2（两个 TOTAL DUE），但第 1 段无 total、第 2 段有 2 个 → 校验不过
        self.assertIsNone(pipeline._invoice_page_ranges(p, n_inv=2))

    def test_process_splits_into_separate_records(self):
        """端到端：多发票文件 process 后入库为多条独立记录、各自有发票号。"""
        import fitz
        from extraction import pipeline
        filler = "Service rendered under the master agreement dated the period start date herein."
        doc = fitz.open()
        self._page(doc, "INVOICE", "INVOICE NO. A-1", "ISSUE DATE 1 January 2026",
                   "DESCRIPTION QTY AMOUNT", "Consulting 1 100.00", filler, "TOTAL DUE 100.00")
        self._page(doc, "INVOICE", "INVOICE NO. B-2", "ISSUE DATE 2 January 2026",
                   "DESCRIPTION QTY AMOUNT", "Advisory 1 200.00", filler, "TOTAL DUE 200.00")
        p = Path(self._dir) / "two.pdf"
        doc.save(p)
        doc.close()
        invs = pipeline.process_local(p)
        self.assertEqual(len(invs), 2)
        nos = sorted(i.f("invoice_no").value for i in invs)
        self.assertEqual(nos, ["A-1", "B-2"])
        # 拆成了独立文件、各自一条记录
        self.assertEqual(len(db.load_all_invoices()), 2)
        self.assertTrue(all("发票" in i.file_name for i in invs))

    def test_same_invoice_number_not_split(self):
        """一张发票 + 尾随明细附表（2 个合计标记、附表续页**重复同一发票号**）→ 不拆、单条。

        防 Ogier 式 2^n 递归拆分爆炸：主 Gross Total + 附表 total 凑成 2 个标记、续页重复发票号被
        误当第二张 → 反复重扫每轮翻倍。同号 = 同一张，绝不拆。"""
        import fitz
        from extraction import pipeline
        filler = "Service rendered under the master agreement dated the period start date herein."
        doc = fitz.open()
        self._page(doc, "INVOICE", "Invoice No. INV-777", "ISSUE DATE 1 January 2026",
                   "DESCRIPTION QTY AMOUNT", "Consulting 1 300.00", filler, "GROSS TOTAL 300.00")
        # 续页 = 类别明细附表，重复同一发票号 INV-777 + 自己的一个 total
        self._page(doc, "Invoice No. INV-777", "Breakdown of charges",
                   "Sundry 1 300.00", filler, "TOTAL 300.00")
        p = Path(self._dir) / "same_no.pdf"
        doc.save(p)
        doc.close()
        invs = pipeline.process_local(p)
        self.assertEqual(len(invs), 1)                    # 同号续页 → 单条，不拆
        self.assertEqual(invs[0].f("invoice_no").value, "INV-777")


@unittest.skipUnless(_has_fitz(), "需要 PyMuPDF")
class TestMultiInvoiceCollection(unittest.TestCase):
    """合集关联：源文件打标、同组查询、单张非合集。"""
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = Path(self._dir) / "t.db"
        config.UPLOAD_DIR = Path(self._dir) / "uploads"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def _pdf(self, n):
        import fitz
        doc = fitz.open()
        for k in range(n):
            pg = doc.new_page(width=420, height=340)
            for i, l in enumerate(["TAX INVOICE", f"Invoice number: INV-{k+1}",
                                    f"Invoice date: {k+1} June 2026", "Bill to: Client Co",
                                    "Description   Value", "Consulting fee   100.00",
                                    "TOTAL DUE   US$100.00"]):
                pg.insert_text((40, 44 + i * 22), l)
        data = doc.tobytes()
        doc.close()
        return data

    def test_multi_shares_source_and_segments(self):
        from extraction import pipeline
        out = pipeline.process_upload(self._pdf(3), "three.pdf")
        self.assertEqual(len(out), 3)
        self.assertEqual(len({i.source_file_hash for i in out}), 1)
        self.assertTrue(all(i.source_file_name == "three.pdf" for i in out))
        self.assertEqual(sorted(i.segment_index for i in out), [1, 2, 3])
        self.assertTrue(all(i.segment_total == 3 for i in out))
        self.assertTrue(all(Path(i.source_file_path).exists() for i in out))

    def test_single_not_a_collection(self):
        from extraction import pipeline
        out = pipeline.process_upload(self._pdf(1), "one.pdf")
        self.assertEqual(out[0].segment_total, 1)

    def test_siblings_by_source(self):
        from extraction import pipeline
        out = pipeline.process_upload(self._pdf(2), "two.pdf")
        sibs = db.siblings_by_source(out[0].source_file_hash)
        self.assertEqual([s["segment_index"] for s in sibs], [1, 2])
        self.assertEqual(db.siblings_by_source("nope"), [])

    def test_delete_member_refreshes_collection_counts(self):
        """删掉合集里一张 → 其余成员的"共几张/第几张"重算；删到剩 1 张 → 不再当合集。"""
        from extraction import pipeline
        from review import service as review
        out = pipeline.process_upload(self._pdf(3), "three.pdf")
        src = out[0].source_file_hash
        review.delete_invoice(out[1].file_hash)
        sibs = db.siblings_by_source(src)
        self.assertEqual([s["segment_total"] for s in sibs], [2, 2])   # 张数重算
        self.assertEqual([s["segment_index"] for s in sibs], [1, 2])   # 序号连续
        review.delete_invoice(sibs[0]["file_hash"])
        self.assertEqual(db.siblings_by_source(src)[0]["segment_total"], 1)  # 剩 1 张不再折叠

    # ---- Phase C: 合并 / 重新切分 / 守卫 ----
    def test_resplit_single_then_auto_roundtrip(self):
        from extraction import pipeline
        from review import service as review
        out = pipeline.process_upload(self._pdf(2), "two.pdf")
        src = out[0].source_file_hash
        # 「不是多张」→ 合并回单张
        r = review.resplit(out[0].file_hash, "single")
        self.assertTrue(r["resplit"])
        self.assertEqual(r["count"], 1)
        merged = db.siblings_by_source(src)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["segment_total"], 1)
        self.assertEqual(len(db.load_all_invoices()), 1)         # 旧 2 条已被替换
        # 「其实是多张」→ 自动重新切回 2 张
        r2 = review.resplit(merged[0]["file_hash"], "auto")
        self.assertTrue(r2["resplit"], r2)
        self.assertEqual(r2["count"], 2)
        self.assertEqual(len(db.siblings_by_source(src)), 2)

    def test_resplit_blocked_when_approved(self):
        from extraction import pipeline
        from review import service as review
        out = pipeline.process_upload(self._pdf(2), "two.pdf")
        inv = db.get_invoice(out[0].file_hash)
        inv.approve_status = "Approved"
        db.save_invoice(inv)
        with self.assertRaises(ValueError):
            review.resplit(out[1].file_hash, "single")           # 组内有已入账 → 拒绝

    def test_resplit_auto_no_boundary_reports(self):
        from extraction import pipeline
        from review import service as review
        out = pipeline.process_upload(self._pdf(1), "one.pdf")    # 真的只有一张
        r = review.resplit(out[0].file_hash, "auto")
        self.assertFalse(r["resplit"])
        self.assertEqual(r["reason"], "auto_no_boundary")

    # ---- Phase D: 单张/多张 软先验 ----
    def _fp_of(self, src_path):
        from extraction import learn
        from extraction.extract import pdf_text
        return learn.fingerprint(pdf_text.extract_pdf(Path(src_path)).full_text)

    def test_resplit_single_learns_pending_prior(self):
        from extraction import pipeline
        from review import service as review
        out = pipeline.process_upload(self._pdf(2), "two.pdf")
        review.resplit(out[0].file_hash, "single")               # 人工判定为单张
        rules = [r for r in db.list_learned() if r["rule_type"] == "multi_invoice"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["value"], "single")
        self.assertEqual(rules[0]["status"], "pending")          # 待启用，不写死
        # 未启用 → 先验不生效（按该版面指纹查不到 active 规则）
        merged = db.siblings_by_source(out[0].source_file_hash)[0]
        fp = self._fp_of(db.get_invoice(merged["file_hash"]).source_file_path)
        self.assertIsNone(db.multi_invoice_prior(fp))

    def test_enabled_single_prior_suppresses_split(self):
        from extraction import pipeline
        from review import service as review
        data = self._pdf(2)
        out = pipeline.process_upload(data, "a.pdf")
        self.assertEqual(len(out), 2)                            # 默认会拆成 2
        fp = self._fp_of(out[0].source_file_path)
        db.learn_multi_invoice(fp, "single", "ACME", "bob")
        rid = [r["id"] for r in db.list_learned() if r["rule_type"] == "multi_invoice"][0]
        db.enable_learned(rid)
        self.assertEqual(db.multi_invoice_prior(fp), "single")
        out2 = pipeline.process_upload(data, "a.pdf")            # 同版面 → 被先验抑制
        self.assertEqual(len(out2), 1)
        self.assertEqual(out2[0].segment_total, 1)

    # ---- Phase E: 人工画线切割 ----
    def _pdf_two_on_one_page(self):
        """一页里上下各一张发票（自动难切）——用于验证人工画线。"""
        import fitz
        doc = fitz.open()
        pg = doc.new_page(width=420, height=360)
        top = ["TAX INVOICE", "Invoice number: TOP-1", "Consulting fee 100.00", "TOTAL DUE US$100.00"]
        bot = ["TAX INVOICE", "Invoice number: BOT-2", "Advisory fee 200.00", "TOTAL DUE US$200.00"]
        for i, l in enumerate(top):
            pg.insert_text((40, 40 + i * 20), l)
        for i, l in enumerate(bot):
            pg.insert_text((40, 210 + i * 20), l)     # 下半页
        data = doc.tobytes()
        doc.close()
        return data

    def test_manual_cut_splits_one_page_into_two(self):
        from extraction import pipeline
        from review import service as review
        out = pipeline.process_upload(self._pdf_two_on_one_page(), "stack.pdf")
        first = out[0].file_hash
        # 在页面中部(pos=0.5)画一条线 → 切成两张
        r = review.resplit(first, "manual", cuts=[{"page": 0, "pos": 0.5}])
        self.assertTrue(r["resplit"], r)
        self.assertEqual(r["count"], 2)
        nos = sorted(x["invoice_no"] for x in r["records"] if x["invoice_no"])
        self.assertEqual(nos, ["BOT-2", "TOP-1"])

    def test_manual_cut_requires_cuts(self):
        from extraction import pipeline
        from review import service as review
        out = pipeline.process_upload(self._pdf(1), "one.pdf")
        with self.assertRaises(ValueError):
            review.resplit(out[0].file_hash, "manual", cuts=[])

    def test_prior_flip_resets_to_pending(self):
        db.learn_multi_invoice("FP1", "single", "X", "bob")
        rid = [r["id"] for r in db.list_learned() if r["rule_type"] == "multi_invoice"][0]
        db.enable_learned(rid)
        db.learn_multi_invoice("FP1", "multi", "X", "bob")      # 倾向翻转
        r = [x for x in db.list_learned() if x["rule_type"] == "multi_invoice"][0]
        self.assertEqual(r["value"], "multi")
        self.assertEqual(r["status"], "pending")               # 翻转后重新待确认
        self.assertIsNone(db.multi_invoice_prior("FP1"))


try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:
    _HAS_TC = False


@unittest.skipUnless(_has_fitz() and _HAS_TC, "需要 PyMuPDF + TestClient")
class TestCollectionEndpoints(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._saved = {k: getattr(config, k) for k in
                       ("DB_PATH", "UPLOAD_DIR", "BACKUP_DIR", "PAGE_CACHE_DIR", "EXPORT_DIR")}
        config.DB_PATH = Path(self._dir) / "t.db"
        config.UPLOAD_DIR = Path(self._dir) / "uploads"
        config.BACKUP_DIR = Path(self._dir) / "backups"
        config.PAGE_CACHE_DIR = Path(self._dir) / "cache"
        config.EXPORT_DIR = Path(self._dir) / "exports"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()
        from extraction import pipeline
        import fitz
        doc = fitz.open()
        for k in range(2):
            pg = doc.new_page(width=420, height=340)
            for i, l in enumerate(["TAX INVOICE", f"Invoice number: INV-{k+1}",
                                   f"Invoice date: {k+1} June 2026", "Bill to: Client Co",
                                   "Description   Value", "Consulting fee   100.00",
                                   "TOTAL DUE   US$100.00"]):
                pg.insert_text((40, 44 + i * 22), l)
        out = pipeline.process_upload(doc.tobytes(), "two.pdf")
        doc.close()
        self.src = out[0].source_file_hash
        from gateway.main import app
        self.c = TestClient(app)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_collection_endpoint(self):
        r = self.c.get(f"/api/review/collection/{self.src}")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["count"], 2)
        self.assertEqual([m["segment_index"] for m in d["members"]], [1, 2])
        self.assertEqual(self.c.get("/api/review/collection/nope").status_code, 404)

    def test_collection_fallback_for_record_without_source_link(self):
        """旧记录(source_file_hash 为空)：用记录自身当单条合集，不再报"未找到合集"。"""
        from review import service as review
        inv = db.get_invoice(db.siblings_by_source(self.src)[0]["file_hash"])
        inv.source_file_hash = ""       # 模拟功能上线前的旧记录（无源链接）
        inv.source_file_path = ""
        db.save_invoice(inv)
        h = inv.file_hash
        # 前端会用 file_hash 兜底查合集；应返回单条合集而非 None
        cd = review.collection_detail(h)
        self.assertIsNotNone(cd)
        self.assertEqual(cd["count"], 1)
        self.assertGreaterEqual(cd["page_count"], 1)     # 能数出页数（用自身 file_path 兜底）
        # 对应端点也应 200（可加载源文件做画线切割）
        self.assertEqual(self.c.get(f"/api/review/collection/{h}").status_code, 200)
        self.assertEqual(self.c.get(f"/api/collection/{h}/page/0").status_code, 200)

    def test_collection_original_page_renders(self):
        r = self.c.get(f"/api/collection/{self.src}/page/0")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(self.c.get(f"/api/collection/{self.src}/page/9").status_code, 404)

    def test_queue_carries_collection_fields(self):
        r = self.c.get("/api/review/queue")
        self.assertEqual(r.status_code, 200)
        items = r.json()["queue"]
        self.assertTrue(all(it.get("segment_total") == 2 for it in items))


if __name__ == "__main__":
    unittest.main()
