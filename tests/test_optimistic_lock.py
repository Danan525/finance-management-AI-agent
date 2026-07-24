"""乐观锁（并发编辑防丢改）+ 运维端点（/healthz）+ 原件页渲染缓存 的测试。

标准库 unittest，与项目其余测试一致。
"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from review import service as review
from core.models import Invoice, FieldValue


class OpsAndLockTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._db, self._data, self._up = config.DB_PATH, config.DATA_ROOT, config.UPLOAD_DIR
        config.DB_PATH = Path(self._dir) / "t.db"
        config.DATA_ROOT = Path(self._dir)             # 页面缓存落临时目录，隔离
        config.UPLOAD_DIR = Path(self._dir) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.DATA_ROOT, config.UPLOAD_DIR = self._db, self._data, self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    @staticmethod
    def _mk(h, total="100.00"):
        inv = Invoice(file_name=f"{h}.pdf", file_hash=h)
        inv.set("invoice_no", FieldValue(raw="INV", value="INV"))
        inv.set("invoice_date", FieldValue(raw="2026-06-01", value="2026-06-01"))
        inv.set("total_due", FieldValue(raw=total, value=Decimal(total)))
        db.save_invoice(inv)
        return inv

    # ---- 乐观锁 rev ----
    def test_rev_starts_zero_and_bumps_each_edit(self):
        self._mk("h1")
        self.assertEqual(db.get_invoice("h1").rev, 0)
        review.change_field("h1", "invoice_no", "X1")
        self.assertEqual(db.get_invoice("h1").rev, 1)
        review.change_field("h1", "invoice_no", "X2")
        self.assertEqual(db.get_invoice("h1").rev, 2)

    def test_detail_exposes_rev(self):
        self._mk("h1")
        self.assertEqual(review.review_detail("h1")["rev"], 0)
        review.change_field("h1", "invoice_no", "X")
        self.assertEqual(review.review_detail("h1")["rev"], 1)

    def test_api_stale_base_rev_returns_409_and_keeps_value(self):
        from fastapi.testclient import TestClient
        from gateway.main import app
        self._mk("h1", total="100.00")
        c = TestClient(app)
        # 用正确 base_rev=0 改一次 → 成功，rev 推进到 1
        r = c.post("/api/review/h1/field",
                   json={"field": "total_due", "value": "120.00", "base_rev": 0})
        self.assertEqual(r.status_code, 200)
        # 再用过期 base_rev=0 改 → 409 冲突，且值不被覆盖（仍是 120）
        r2 = c.post("/api/review/h1/field",
                    json={"field": "total_due", "value": "130.00", "base_rev": 0})
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.json()["code"], "conflict")
        self.assertEqual(review.review_detail("h1")["fields"]["total_due"]["value"], "120.00")
        # 用最新 base_rev=1 → 成功
        r3 = c.post("/api/review/h1/field",
                    json={"field": "total_due", "value": "130.00", "base_rev": 1})
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(review.review_detail("h1")["fields"]["total_due"]["value"], "130.00")

    def test_api_without_base_rev_is_backward_compatible(self):
        """不带 base_rev（脚本/批量/旧客户端）→ 跳过并发校验，照常生效。"""
        from fastapi.testclient import TestClient
        from gateway.main import app
        self._mk("h1")
        c = TestClient(app)
        r = c.post("/api/review/h1/field", json={"field": "invoice_no", "value": "Z"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(review.review_detail("h1")["fields"]["invoice_no"]["value"], "Z")

    def test_action_endpoint_respects_conflict(self):
        from fastapi.testclient import TestClient
        from gateway.main import app
        self._mk("h1")
        # 先有人改一次（rev 0→1）
        review.change_field("h1", "invoice_no", "A")
        c = TestClient(app)
        # 用过期 base_rev=0 尝试 Approve → 409，状态不变
        r = c.post("/api/review/h1/action", json={"action": "Approved", "base_rev": 0})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(review.review_detail("h1")["approve_status"], "Pending")

    # ---- /healthz ----
    def test_healthz_ok(self):
        from fastapi.testclient import TestClient
        from gateway.main import app
        c = TestClient(app)
        r = c.get("/healthz")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["db"])

    # ---- 原件页渲染缓存（ETag + 304 + 磁盘缓存）----
    def test_page_render_cache_etag_and_304(self):
        try:
            import fitz
        except Exception:
            self.skipTest("fitz 不可用")
        from fastapi.testclient import TestClient
        from gateway.main import app
        pdf_path = config.UPLOAD_DIR / "a.pdf"
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        doc.save(pdf_path)
        doc.close()
        inv = Invoice(file_name="a.pdf", file_hash="hp", file_path=str(pdf_path))
        db.save_invoice(inv)
        c = TestClient(app)
        r1 = c.get("/api/review/hp/page/0")
        self.assertEqual(r1.status_code, 200)
        etag = r1.headers.get("etag")
        self.assertTrue(etag)
        self.assertIn("max-age", r1.headers.get("cache-control", ""))
        # 磁盘缓存已写入
        self.assertTrue((config.DATA_ROOT / "cache" / "pages").exists())
        # 带 If-None-Match → 304
        r2 = c.get("/api/review/hp/page/0", headers={"If-None-Match": etag})
        self.assertEqual(r2.status_code, 304)


if __name__ == "__main__":
    unittest.main()
