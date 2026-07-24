"""审核界面 + API 端到端测试（FastAPI TestClient）。

验证：/review 返回审核页 HTML；/api/review/* 队列→详情→改字段→动作 一条龙可达。
若环境缺 TestClient 依赖（httpx），整组跳过（不影响其余测试）。
"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from core.models import Invoice, FieldValue

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:
    _HAS_TC = False


@unittest.skipUnless(_HAS_TC, "fastapi TestClient 不可用（缺 httpx）")
class ReviewWebTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._dir) / "t.db"
        db._initialized = False
        db.init_db()
        inv = Invoice(file_name="h1.pdf", file_hash="h1")
        inv.set("invoice_no", FieldValue(raw="INV-1", value="INV-1"))
        inv.set("invoice_date", FieldValue(raw="2026-06-01", value="2026-06-01"))
        inv.set("total_due", FieldValue(raw="100.00", value=Decimal("100.00")))
        inv.raw_pdf_text = "Invoice INV-1 Total 100.00"
        inv.approve_status = "Pending"
        db.save_invoice(inv)
        from gateway.main import app
        self.c = TestClient(app)

    def tearDown(self):
        config.DB_PATH = self._orig
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_review_page_served(self):
        r = self.c.get("/review")
        self.assertEqual(r.status_code, 200)
        self.assertIn("人工审核", r.text)

    def test_upload_rejects_unsupported_ext(self):
        """扩展名白名单：真·垃圾类型（.exe）落盘前即拒，不进处理、不写盘。"""
        r = self.c.post("/api/upload",
                        files={"files": ("evil.exe", b"MZ\x90\x00binary", "application/octet-stream")})
        self.assertEqual(r.status_code, 200)
        row = r.json()["results"][0]
        self.assertIn("不支持的文件类型", row.get("error", ""))

    def test_queue_detail_field_action_flow(self):
        # 队列
        q = self.c.get("/api/review/queue").json()
        self.assertEqual(q["summary"]["Pending"], 1)
        self.assertEqual(q["queue"][0]["file_hash"], "h1")
        # 详情
        d = self.c.get("/api/review/h1").json()
        self.assertEqual(d["fields"]["total_due"]["value"], "100.00")
        self.assertIn("INV-1", d["raw_pdf_text"])
        # 改字段（留痕）
        r = self.c.post("/api/review/h1/field", json={"field": "total_due", "value": "120.00", "by": "alice"})
        self.assertEqual(r.status_code, 200)
        d2 = self.c.get("/api/review/h1").json()
        self.assertEqual(d2["fields"]["total_due"]["value"], "120.00")
        self.assertTrue(any(c["new_value"] == "120.00" for c in d2["changes"]))
        # 通过
        r = self.c.post("/api/review/h1/action", json={"action": "Approved", "by": "bob"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/api/review/h1").json()["approve_status"], "Approved")

    def test_reject_without_reason_400(self):
        r = self.c.post("/api/review/h1/action", json={"action": "Rejected"})
        self.assertEqual(r.status_code, 400)

    def test_fix_first_orders_needs_fix_to_front(self):
        # 干净发票 h1（较新上传）→ 通过校验；再造一条较旧的"需纠错"发票（缺必填 total_due）
        good = db.get_invoice("h1")
        good.parse_status = "parsed"; good.uploaded_at = "2026-06-10T10:00:00"
        db.save_invoice(good)
        bad = Invoice(file_name="bad.pdf", file_hash="bad")
        bad.set("invoice_no", FieldValue(raw="INV-9", value="INV-9"))
        bad.set("invoice_date", FieldValue(raw="2026-06-02", value="2026-06-02"))
        bad.parse_status = "parsed"; bad.approve_status = "Pending"
        bad.uploaded_at = "2026-06-01T10:00:00"      # 比 h1 旧 → 默认按新→旧排会排在后面
        db.save_invoice(bad)
        # 默认排序（无 fix_first）：较新的干净发票 h1 在前
        base = self.c.get("/api/review/queue").json()["queue"]
        self.assertEqual(base[0]["file_hash"], "h1")
        # fix_first：需纠错的 bad 被顶到最前，且 needs_fix 标记正确
        q = self.c.get("/api/review/queue?fix_first=1").json()["queue"]
        self.assertEqual(q[0]["file_hash"], "bad")
        self.assertTrue(q[0]["needs_fix"])
        flags = {x["file_hash"]: x["needs_fix"] for x in q}
        self.assertFalse(flags["h1"])                # 干净发票 needs_fix=False

    def test_detail_lists_match_block_reasons(self):
        # 缺必填 total_due 的发票 → 详情要给出"必须修正才能匹配"的具体原因（缺总金额）
        bad = Invoice(file_name="bad.pdf", file_hash="bad")
        bad.set("invoice_no", FieldValue(raw="INV-9", value="INV-9"))
        bad.set("invoice_date", FieldValue(raw="2026-06-02", value="2026-06-02"))
        bad.parse_status = "parsed"
        db.save_invoice(bad)
        d = self.c.get("/api/review/bad").json()
        blocks = d["match_blocks"]
        self.assertTrue(any(b["kind"] == "missing" and b.get("field") == "total_due" for b in blocks))
        # 干净发票（h1 补齐 parse_status）→ 无阻断项
        good = db.get_invoice("h1"); good.parse_status = "parsed"; db.save_invoice(good)
        self.assertEqual(self.c.get("/api/review/h1").json()["match_blocks"], [])


if __name__ == "__main__":
    unittest.main()
