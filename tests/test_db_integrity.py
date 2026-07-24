"""DB 完整性：状态+审计原子写、损坏 payload 不静默丢弃（自检修复的回归）。"""
import tempfile
import unittest
from pathlib import Path

from core import config, db
from core.models import Invoice, FieldValue


def _inv(h="hhh1", no="INV-1"):
    inv = Invoice(file_hash=h, file_name=h + ".pdf", uploaded_at="2026-07-22T00:00:00",
                  processed_at="2026-07-22T00:00:00", parse_status="ok", review_status="Pending")
    inv.set("invoice_no", FieldValue(raw=no, value=no))
    return inv


class TestDbIntegrity(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._db = config.DB_PATH
        config.DB_PATH = Path(self._dir) / "t.db"
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._db
        db._initialized = False
        import shutil
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_resave_and_log_writes_both(self):
        """resave_and_log 一次事务里同时写快照与 change_log。"""
        inv = _inv()
        db.save_invoice(inv)
        inv.set("invoice_no", FieldValue(raw="INV-2", value="INV-2"))
        db.resave_and_log(inv, "invoice_no", "INV-1", "INV-2", "reviewer", "改号")
        got = db.get_invoice("hhh1")
        self.assertEqual(got.f("invoice_no").value, "INV-2")          # 快照已更新
        changes = db.list_changes("hhh1")
        self.assertTrue(any(c["field"] == "invoice_no" and c["new_value"] == "INV-2" for c in changes))

    def test_resave_and_log_atomic_rollback(self):
        """事务中途失败 → 快照与留痕都不落库（状态与审计同生共死）。"""
        inv = _inv()
        db.save_invoice(inv)
        orig = db.log_change
        db.log_change = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # 令留痕抛错
        try:
            inv.set("invoice_no", FieldValue(raw="INV-BAD", value="INV-BAD"))
            with self.assertRaises(RuntimeError):
                db.resave_and_log(inv, "invoice_no", "INV-1", "INV-BAD", "reviewer")
        finally:
            db.log_change = orig
        # 回滚：发票号未被改、change_log 无该条
        self.assertEqual(db.get_invoice("hhh1").f("invoice_no").value, "INV-1")
        self.assertFalse(any(c["new_value"] == "INV-BAD" for c in db.list_changes("hhh1")))

    def test_corrupt_payload_logged_not_silent(self):
        """损坏快照不静默消失：load_all_invoices 跳过它但记 error（带 file_hash）。"""
        db.save_invoice(_inv("good", "OK-1"))
        with db.connect() as conn:                                    # 直插一条损坏 payload
            conn.execute("INSERT INTO invoices (file_hash, file_name, payload) VALUES (?,?,?)",
                         ("broken", "b.pdf", "{not valid json"))
        with self.assertLogs("finance.db", level="ERROR") as cm:
            allinv = db.load_all_invoices()
        self.assertIn("good", allinv)                                 # 好记录仍在
        self.assertNotIn("broken", allinv)                           # 坏记录跳过
        self.assertTrue(any("broken" in line for line in cm.output))  # 但有 error 告警带 file_hash


if __name__ == "__main__":
    unittest.main()
