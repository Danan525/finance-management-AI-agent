"""期末软关账 + 结转损益：损益归零→留存收益、试算平衡、已关账拒过账、重开红冲。

隔离临时库。业务分录用手工凭证造(避开发票 setup)。"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from ledger import accounts as A
from ledger import close, service, store
from reports import service as reports

D = Decimal
P = "2026-06"


def _seed(rev="5000", exp="2000", cap="10000"):
    """期初出资 + 一笔现金收入 + 一笔现金费用（均 2026-06）。net = rev - exp。"""
    service.post_manual_entry([
        {"account": "1002 银行存款 Bank", "debit": cap},
        {"account": "3000 实收资本 Share Capital", "credit": cap}],
        date="2026-06-01", memo="期初出资", activity="financing")
    service.post_manual_entry([
        {"account": "1002 银行存款 Bank", "debit": rev},
        {"account": "4000 营业收入 Sales Revenue", "credit": rev}],
        date="2026-06-05", memo="收入收现", activity="operating")
    service.post_manual_entry([
        {"account": "6440 管理费用 Admin Expense", "debit": exp},
        {"account": "1002 银行存款 Bank", "credit": exp}],
        date="2026-06-08", memo="付费用", activity="operating")


class CloseTest(unittest.TestCase):
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

    def test_close_transfers_pl_to_retained(self):
        _seed()
        r = close.close_period(P, by="admin")
        self.assertEqual(r["net_income"], "3000")
        led = service.load_ledger()
        self.assertEqual(led.net("4000 营业收入 Sales Revenue"), D("0"))     # 收入归零
        self.assertEqual(led.net("6440 管理费用 Admin Expense"), D("0"))     # 费用归零
        self.assertEqual(led.net(A.CY_PROFIT), D("0"))                       # 本年利润归零
        self.assertEqual(-led.net(A.RETAINED), D("3000"))                    # 未分配利润(贷)=净利
        self.assertTrue(service.trial_balance()[3])                          # 试算仍平

    def test_period_status(self):
        _seed()
        self.assertEqual(close.period_status(P), "open")
        close.close_period(P)
        self.assertEqual(close.period_status(P), "closed")

    def test_reject_post_into_closed(self):
        _seed()
        close.close_period(P)
        with self.assertRaises(ValueError):     # 向已关账期过账被拒
            service.post_manual_entry([
                {"account": "6440 管理费用", "debit": "10"},
                {"account": "2100 应付账款 Accounts Payable", "credit": "10"}],
                date="2026-06-20", memo="补记", allow_control=True, counterparty="ACME Inc")

    def test_open_period_still_postable(self):
        _seed()
        close.close_period(P)
        # 别的开放期(7月)不受影响
        no = service.post_manual_entry([
            {"account": "6440 管理费用", "debit": "10"},
            {"account": "2100 应付账款 Accounts Payable", "credit": "10"}],
            date="2026-07-03", memo="7月费用", allow_control=True, counterparty="ACME Inc")
        self.assertTrue(no.startswith("202607-"))

    def test_reject_double_close(self):
        _seed()
        close.close_period(P)
        with self.assertRaises(ValueError):
            close.close_period(P)

    def test_loss_decreases_retained(self):
        _seed(rev="1000", exp="3000")           # 亏损 2000
        r = close.close_period(P)
        self.assertEqual(r["net_income"], "-2000")
        led = service.load_ledger()
        self.assertEqual(led.net(A.CY_PROFIT), D("0"))
        self.assertEqual(-led.net(A.RETAINED), D("-2000"))   # 留存收益减少
        self.assertTrue(service.trial_balance()[3])

    def test_reopen_reverses_closing(self):
        _seed()
        close.close_period(P)
        close.reopen_period(P)
        self.assertEqual(close.period_status(P), "open")
        led = service.load_ledger()
        # 红冲后损益回到结转前、留存归零
        self.assertEqual(led.net("4000 营业收入 Sales Revenue"), D("5000") * -1)  # 贷方 5000 → net -5000
        self.assertEqual(-led.net(A.RETAINED), D("0"))
        self.assertTrue(service.trial_balance()[3])
        # 重开后可再过账
        no = service.post_manual_entry([
            {"account": "6440 管理费用", "debit": "10"},
            {"account": "2100 应付账款 Accounts Payable", "credit": "10"}],
            date="2026-06-20", memo="重开后补记", allow_control=True, counterparty="ACME Inc")
        self.assertTrue(no)


    def test_reports_correct_after_close(self):
        _seed()   # 净利 3000
        # 关账前:利润表净利=3000、E2 成立
        self.assertEqual(reports.income_statement()["net_income"], "3000")
        self.assertTrue(reports.checks()["E2_income_to_retained"]["ok"])
        close.close_period(P)
        # 关账后:利润表**仍**显示经营净利 3000(排除结转分录),不因损益清零而变 0
        self.assertEqual(reports.income_statement()["net_income"], "3000")
        r = reports.generate()
        self.assertEqual(r["income_statement"]["net_income"], "3000")
        self.assertTrue(r["balance_sheet"]["balanced"])            # 资产=负债+权益
        ck = r["checks"]
        self.assertTrue(ck["E1_balance_sheet_balanced"]["ok"])
        self.assertTrue(ck["E2_income_to_retained"]["ok"])         # 净利=未结转+已结转入留存
        self.assertTrue(ck["can_issue"])
        # 未分配利润(3200)已承接净利 3000
        self.assertEqual(-service.load_ledger().net(A.RETAINED), D("3000"))


    def test_reopen_then_reports_not_doubled(self):
        # H1 回归:重开(红冲结转)后,利润表不因红冲分录翻倍;再关账不写坏留存/不漏清损益
        _seed()   # 净利 3000
        close.close_period(P)
        close.reopen_period(P)
        self.assertEqual(reports.income_statement()["net_income"], "3000")   # 不是 6000
        ck = reports.checks()
        self.assertTrue(ck["E2_income_to_retained"]["ok"])
        r = close.close_period(P)                # 再关账
        self.assertEqual(r["net_income"], "3000")                            # 不是 6000
        led = service.load_ledger()
        self.assertEqual(led.net("4000 营业收入 Sales Revenue"), D("0"))     # 收入确被清零
        self.assertEqual(-led.net(A.RETAINED), D("3000"))                    # 留存 3000,不是 6000
        self.assertTrue(service.trial_balance()[3])

    def test_closing_date_is_month_end(self):
        _seed()
        close.close_period(P)   # 2026-06 → 结转分录日期应为 2026-06-30
        nos = [e for e in store.list_entries() if e.source_kind == "closing"]
        self.assertTrue(nos and all(e.date == "2026-06-30" for e in nos))


if __name__ == "__main__":
    unittest.main()
