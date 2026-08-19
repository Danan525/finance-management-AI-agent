"""建账期初余额：往来逐户可结算、其它科目余额、期初现金进 CFS 期初、试算平衡、报表勾稽。

隔离临时库。"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from ledger import accounts as A
from ledger import service, settlement
from reports import service as reports

D = Decimal


class OpeningTest(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = self._dir / "t.db"
        config.UPLOAD_DIR = self._dir / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def _build(self):
        # 期初:银行10000、固定资产5000、实收资本12000;应收(客户A 2000)、应付(供应商B 1000)
        return service.post_opening(
            items=[
                {"account": A.AR, "counterparty": "客户A", "amount": "2000"},
                {"account": A.AP, "counterparty": "供应商B", "amount": "1000"},
            ],
            other_lines=[
                {"account": "1002 银行存款 Bank", "amount": "10000", "side": "debit"},
                {"account": "1500 固定资产 Fixed Assets", "amount": "5000", "side": "debit"},
                {"account": "3000 实收资本 Share Capital", "amount": "12000", "side": "credit"},
            ],
            date="2026-05-31", by="admin")

    def test_opening_balances_and_trial_balance(self):
        self._build()
        led = service.load_ledger()
        self.assertEqual(led.net("1002 银行存款 Bank"), D("10000"))
        self.assertEqual(led.net(A.AR), D("2000"))
        self.assertEqual(-led.net(A.AP), D("1000"))
        self.assertEqual(-led.net("3000 实收资本 Share Capital"), D("12000"))
        # 3200 = 净资产 - 实收资本 = (10000+5000+2000-1000) - 12000 = 4000 的年初留存
        self.assertEqual(-led.net(A.RETAINED), D("4000"))
        self.assertTrue(service.trial_balance()[3])

    def test_opening_receivable_is_settleable(self):
        self._build()
        # 期初往来进"待结算",且可像发票一样清账
        opens = {o["invoice_no"]: o for o in service.open_view()}
        self.assertTrue(any("客户A" in k for k in opens))
        fh = [o["file_hash"] for o in service.open_view() if o["direction"] == "AR"][0]
        self.assertEqual(settlement.open_amount(fh)[0], D("2000"))
        service.settle_invoice(fh, cash_amount="2000.00")     # 收回旧应收
        self.assertEqual(settlement.open_amount(fh)[0], D("0"))
        self.assertEqual(service.load_ledger().net(A.AR), D("0"))
        self.assertTrue(service.trial_balance()[3])

    def test_control_reconciliation_holds_with_opening(self):
        self._build()
        ctl = service.control_view()
        self.assertTrue(ctl["AP"]["ok"] and ctl["AR"]["ok"])   # 期初往来计入明细侧,不假告警
        # 结算一笔后仍恒等
        fh = [o["file_hash"] for o in service.open_view() if o["direction"] == "AP"][0]
        service.settle_invoice(fh, cash_amount="1000.00")
        ctl = service.control_view()
        self.assertTrue(ctl["AP"]["ok"] and ctl["AR"]["ok"])

    def test_opening_cash_is_cfs_opening_not_flow(self):
        self._build()
        cf = reports.cash_flow_statement()
        self.assertEqual(cf["opening"], "10000")               # 期初现金
        self.assertEqual(cf["net_change"], "0")                # 建账不产生本期流量
        self.assertEqual(cf["ending"], "10000")
        self.assertTrue(cf["e3_ok"])                           # 期末现金 == 货币资金

    def test_reports_balance_after_opening(self):
        self._build()
        r = reports.generate()
        self.assertTrue(r["balance_sheet"]["balanced"])
        self.assertTrue(r["checks"]["can_issue"])

    def test_opening_cash_without_activity_ok_but_with_activity_rejected(self):
        from ledger import store
        from ledger.engine import JournalEntry, JournalLine
        # 期初现金分录**无** activity 应成功(豁免)
        ok = store.post_entry(JournalEntry(
            "2026-05-31", "期初银行", [
                JournalLine("1002 银行存款 Bank", debit="100"),
                JournalLine(A.RETAINED, credit="100")], source_kind="opening"),
            by="t", at="2026-05-31T00:00:00Z")
        self.assertTrue(ok)
        # 期初分录**带** activity 应被拒
        with self.assertRaises(ValueError):
            store.post_entry(JournalEntry(
                "2026-05-31", "期初银行误标", [
                    JournalLine("1002 银行存款 Bank", debit="1"),
                    JournalLine(A.RETAINED, credit="1")], source_kind="opening"),
                by="t", at="2026-05-31T00:00:00Z", activity="operating")

    def test_opening_control_must_have_counterparty(self):
        with self.assertRaises(ValueError):
            service.post_opening(items=[{"account": A.AR, "amount": "500"}], date="2026-05-31")

    def test_atomic_no_partial_post_on_bad_row(self):
        # M1: 后置行无法归类 → 整批拒绝，前面合法行不得已落库（自检修）
        with self.assertRaises(ValueError):
            service.post_opening(other_lines=[
                {"account": "1002 银行存款 Bank", "amount": "500", "side": "debit"},
                {"account": "9999 火星科目", "amount": "300", "side": "debit"}],
                date="2026-05-31")
        dr, cr, _ = service.load_ledger().trial_balance()
        self.assertEqual(dr, D("0"))          # 无任何分录落库


if __name__ == "__main__":
    unittest.main()
