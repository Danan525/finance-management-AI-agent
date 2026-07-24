"""报表中心：利润表/资产负债表取数 + 勾稽（E1 资产=负债+权益、E6 归类完整、勾稽不过不出表）。

隔离临时库。报表从 module 6 已过账分录取数。"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from core.models import Classification, FieldValue, Invoice
from ledger import service as ledger
from ledger import store
from ledger.engine import JournalEntry, JournalLine
from reports import service as reports

D = Decimal


def _inv(no, total, fh, account="6440 会计费 Accounting Fees"):
    inv = Invoice()
    inv.file_hash = fh
    inv.doc_type = "invoice"
    inv.approve_status = "Approved"
    inv.set("invoice_no", FieldValue(value=no))
    inv.set("invoice_date", FieldValue(value="2026-06-01"))
    inv.set("total_due", FieldValue(value=total))
    inv.set("issuer_name", FieldValue(value="Vendor"))
    inv.classification = Classification(account=account)
    db.save_invoice(inv)
    return inv


class ReportsTest(unittest.TestCase):
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

    def _opening_capital(self, amount):
        # 期初实收资本：借 银行 / 贷 实收资本（动现金 → 标筹资活动）
        store.post_entry(JournalEntry(
            "2026-06-01", "期初实收资本", [
                JournalLine("1002 银行存款 Bank", debit=amount),
                JournalLine("3000 实收资本 Share Capital", credit=amount)],
            source_kind="opening"), by="t", at="2026-06-01T00:00:00Z", activity="financing")

    def test_empty_reports_balance(self):
        r = reports.generate()
        self.assertTrue(r["checks"]["E1_balance_sheet_balanced"]["ok"])
        self.assertTrue(r["can_issue"])

    def test_ar_sale_income_and_balance(self):
        # AR 开票 1000（Dr 应收 / Cr 收入）→ 利润表收入 1000、净利 1000；资产=负债+权益
        ledger.post_invoice(_inv("AR-1", "1000.00", "h1"), direction="AR")
        istmt = reports.income_statement()
        by = {l["key"]: l["amount"] for l in istmt["lines"]}
        self.assertEqual(by["Revenue"], "1000.00")
        self.assertEqual(by["NetIncome"], "1000.00")
        bs = reports.balance_sheet()
        self.assertTrue(bs["balanced"])
        # 净利润进入权益
        self.assertIn("CurrentNetIncome", [e["key"] for e in bs["equity"]])
        self.assertEqual(bs["diff"], "0.00")

    def test_ap_expense_reduces_net_income(self):
        self._opening_capital("5000.00")
        ledger.post_invoice(_inv("AP-1", "1000.00", "h1"))     # Dr 费用 / Cr 应付
        istmt = reports.income_statement()
        by = {l["key"]: l["amount"] for l in istmt["lines"]}
        self.assertEqual(by["Opex"], "1000.00")
        self.assertEqual(by["NetIncome"], "-1000.00")          # 纯费用 → 亏损
        bs = reports.balance_sheet()
        self.assertTrue(bs["balanced"])
        # 资产=银行5000；负债=应付1000；权益=实收5000 + 净利-1000 = 4000；5000==1000+4000
        self.assertEqual(bs["assets_total"], "5000.00")
        self.assertEqual(bs["liab_equity_total"], "5000.00")

    def test_full_cycle_accrue_settle_balances(self):
        self._opening_capital("5000.00")
        ledger.post_invoice(_inv("AP-1", "1000.00", "h1"))
        ledger.settle_invoice("h1", cash_amount="1000.00")     # 付清
        bs = reports.balance_sheet()
        self.assertTrue(bs["balanced"])
        # 银行 5000-1000=4000；应付 0；权益 5000 + 净利(-1000)=4000
        self.assertEqual(bs["assets_total"], "4000.00")
        self.assertEqual(bs["liab_equity_total"], "4000.00")

    def test_cash_flow_direct_and_e3(self):
        self._opening_capital("5000.00")                       # 筹资 +5000
        ledger.post_invoice(_inv("AP-1", "1000.00", "h1"))
        ledger.settle_invoice("h1", cash_amount="1000.00")     # 经营 -1000
        ledger.post_invoice(_inv("AR-1", "3000.00", "h2"), direction="AR")
        ledger.settle_invoice("h2", cash_amount="3000.00")     # 经营 +3000
        cf = reports.cash_flow_statement()
        by = {l["key"]: l["amount"] for l in cf["lines"]}
        self.assertEqual(by["financing"], "5000.00")
        self.assertEqual(by["operating"], "2000.00")
        self.assertEqual(by["investing"], "0")
        self.assertEqual(cf["net_change"], "7000.00")
        self.assertEqual(cf["ending"], "7000.00")
        self.assertEqual(cf["bs_cash"], "7000.00")
        self.assertTrue(cf["e3_ok"])                           # CFS 期末现金 == BS 货币资金
        self.assertTrue(reports.checks()["can_issue"])

    def test_investing_activity_inferred(self):
        # 固定资产发票 → 结算现金流归投资活动
        ledger.post_invoice(_inv("AP-FA", "2000.00", "h1",
                                 account="1500 固定资产 Fixed Assets"))
        ledger.settle_invoice("h1", cash_amount="2000.00")
        cf = reports.cash_flow_statement()
        by = {l["key"]: l["amount"] for l in cf["lines"]}
        self.assertEqual(by["investing"], "-2000.00")
        self.assertEqual(by["operating"], "0")

    def test_cash_entry_requires_activity(self):
        # 动现金却不标活动 → 拒绝（E5 前提）
        with self.assertRaises(ValueError):
            store.post_entry(JournalEntry("2026-06-01", "付现无分类", [
                JournalLine("6440 会计费 Accounting Fees", debit="100"),
                JournalLine("1002 银行存款 Bank", credit="100")],
                source_kind="manual"), by="t", at="2026-06-01T00:00:00Z")

    def test_internal_transfer_no_activity_not_in_cfs(self):
        self._opening_capital("5000.00")
        # 银行取现到库存现金：两边都是现金及等价物 → 净流 0 → 不标活动、不进 CFS
        store.post_entry(JournalEntry("2026-06-01", "取现", [
            JournalLine("1001 现金 Cash on Hand", debit="1000"),
            JournalLine("1002 银行存款 Bank", credit="1000")],
            source_kind="manual"), by="t", at="2026-06-01T00:00:00Z")
        # 误给内部腾挪标活动 → 拒绝
        with self.assertRaises(ValueError):
            store.post_entry(JournalEntry("2026-06-01", "取现误标", [
                JournalLine("1001 现金 Cash on Hand", debit="1"),
                JournalLine("1002 银行存款 Bank", credit="1")],
                source_kind="manual"), by="t", at="2026-06-01T00:00:00Z", activity="operating")
        cf = reports.cash_flow_statement()
        self.assertEqual(cf["net_change"], "5000.00")          # 只有筹资 5000，取现不计
        self.assertTrue(cf["e3_ok"])                           # 期末现金仍 = 银行+现金合计

    def test_unclassified_blocks_issue(self):
        # 造一个无法归类的科目（未知类别编码 9xxx）→ E6 失败、不出表
        store.post_entry(JournalEntry(
            "2026-06-01", "怪科目", [
                JournalLine("9999 未知科目 Weird", debit="100"),
                JournalLine("1002 银行存款 Bank", credit="100")],
            source_kind="manual"), by="t", at="2026-06-01T00:00:00Z", activity="operating")
        ck = reports.checks()
        self.assertFalse(ck["E6_all_classified"]["ok"])
        self.assertIn("9999 未知科目 Weird", ck["E6_all_classified"]["unclassified"])
        self.assertFalse(ck["can_issue"])                      # 勾稽不过不出表


    def test_generate_is_json_serializable(self):
        # generate() 直接进 JSONResponse，必须无 Decimal 泄漏（曾致 /api/reports 500）
        import json
        self._opening_capital("5000.00")
        ledger.post_invoice(_inv("AP-1", "1000.00", "h1"))
        ledger.settle_invoice("h1", cash_amount="1000.00")
        json.dumps(reports.generate())        # 不抛 TypeError 即通过
        json.dumps(reports.generate())        # 空前也应可序列化

    def test_export_excel_when_balanced(self):
        from openpyxl import load_workbook
        self._opening_capital("5000.00")
        ledger.post_invoice(_inv("AP-1", "1000.00", "h1"))
        ledger.settle_invoice("h1", cash_amount="1000.00")
        out = reports.export_excel()
        self.assertTrue(out.exists())
        wb = load_workbook(str(out))
        for sheet in ("封面 Cover", "利润表 Income Statement", "资产负债表 Balance Sheet",
                      "现金流量表 Cash Flow", "勾稽校验 Reconciliation",
                      "科目余额 取数轨迹", "分录明细 凭证轨迹"):
            self.assertIn(sheet, wb.sheetnames)

    def test_export_blocked_when_unbalanced(self):
        # 未归类科目 → 勾稽不过 → 拒绝导出（勾稽不通过不出表）
        store.post_entry(JournalEntry("2026-06-01", "怪科目", [
            JournalLine("9999 未知科目 Weird", debit="100"),
            JournalLine("1002 银行存款 Bank", credit="100")],
            source_kind="manual"), by="t", at="2026-06-01T00:00:00Z", activity="operating")
        with self.assertRaises(ValueError):
            reports.export_excel()


if __name__ == "__main__":
    unittest.main()
