"""列表 / 待审队列的分页 + 「只读紧凑摘要、不重建完整对象」的测试。"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from review import service as review
from core.models import Invoice, FieldValue


class PaginationTest(unittest.TestCase):
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

    def _mk(self, h, uploaded, approve="Pending"):
        inv = Invoice(file_name=f"{h}.pdf", file_hash=h)
        inv.set("invoice_no", FieldValue(raw=h, value=h))
        inv.set("invoice_date", FieldValue(raw="2026-06-01", value="2026-06-01"))
        inv.set("total_due", FieldValue(raw="10", value=Decimal("10")))
        inv.uploaded_at = uploaded
        inv.approve_status = approve
        db.save_invoice(inv)

    def _seed(self, n):
        for i in range(n):
            self._mk(f"h{i:02d}", f"2026-06-{(i % 28) + 1:02d}T00:00:00Z")

    def test_summary_column_populated_on_save(self):
        self._mk("h1", "2026-06-01T00:00:00Z")
        rows = db.load_summaries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["invoice_no"], "h1")
        self.assertIn("issues", rows[0])          # 摘要含 issues（列表页需要）

    def test_load_summaries_limit_offset_and_count(self):
        self._seed(5)
        self.assertEqual(db.count_invoices(), 5)
        page1 = db.load_summaries(limit=2, offset=0)
        page2 = db.load_summaries(limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        # 两页不重叠
        self.assertFalse({r["file_hash"] for r in page1} & {r["file_hash"] for r in page2})

    def test_api_invoices_pagination(self):
        from fastapi.testclient import TestClient
        from gateway.main import app
        self._seed(3)
        c = TestClient(app)
        r = c.get("/api/invoices?limit=2&offset=0").json()
        self.assertEqual(r["count"], 3)           # 总数
        self.assertEqual(len(r["results"]), 2)    # 当页
        self.assertTrue(r["has_more"])
        r2 = c.get("/api/invoices?limit=2&offset=2").json()
        self.assertEqual(len(r2["results"]), 1)
        self.assertFalse(r2["has_more"])

    def test_api_queue_pagination_and_total(self):
        from fastapi.testclient import TestClient
        from gateway.main import app
        self._seed(3)
        c = TestClient(app)
        d = c.get("/api/review/queue?limit=1&offset=0").json()
        self.assertEqual(d["total"], 3)
        self.assertEqual(len(d["queue"]), 1)
        self.assertTrue(d["has_more"])
        self.assertEqual(d["summary"]["Pending"], 3)

    def test_queue_status_filter_count(self):
        self._mk("a", "2026-06-01T00:00:00Z", approve="Pending")
        self._mk("b", "2026-06-02T00:00:00Z", approve="Approved")
        self.assertEqual(review.queue_count("Pending"), 1)
        self.assertEqual(review.queue_count(), 2)
        self.assertEqual([x["file_hash"] for x in review.review_queue("Approved")], ["b"])

    def test_ordering_failed_first_then_newest(self):
        self._mk("old", "2026-06-01T00:00:00Z")
        self._mk("new", "2026-06-28T00:00:00Z")
        bad = Invoice(file_name="bad.pdf", file_hash="bad")
        bad.uploaded_at = "2026-06-15T00:00:00Z"
        bad.parse_status = "failed"
        db.save_invoice(bad)
        order = [r["file_hash"] for r in db.load_summaries()]
        self.assertEqual(order[0], "bad")             # 失败置顶
        self.assertEqual(order[1:], ["new", "old"])   # 再按上传时间倒序


if __name__ == "__main__":
    unittest.main()
