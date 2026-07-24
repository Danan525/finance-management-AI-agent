"""总账引擎：复式内核（借贷平硬校验）+ 应计过账（AP/AR/无税退化）+ 入账闸门 + 幂等 + 红冲 + 试算平衡。

隔离临时库，绝不污染真实库。"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import fitz

from core import config, db
from core.models import Classification, FieldValue, Invoice
from extraction import pipeline
from ledger import accounts as A
from ledger import posting, service, settlement, store
from ledger.engine import JournalEntry, JournalLine, Ledger

D = Decimal


def _mk_invoice(no, total, sub=None, tax=None, account="6440 会计费 Accounting Fees",
                issuer="Vendor Co", customer="My Company Ltd", date="2026-06-01",
                file_hash="h1", approve="Approved"):
    inv = Invoice()
    inv.file_hash = file_hash
    inv.doc_type = "invoice"
    inv.approve_status = approve
    inv.set("invoice_no", FieldValue(value=no))
    inv.set("invoice_date", FieldValue(value=date))
    inv.set("total_due", FieldValue(value=total))
    if sub is not None:
        inv.set("subtotal", FieldValue(value=sub))
    if tax is not None:
        inv.set("sales_tax", FieldValue(value=tax))
    inv.set("issuer_name", FieldValue(value=issuer))
    inv.set("customer_name", FieldValue(value=customer))
    inv.classification = Classification(account=account)
    return inv


class EngineTest(unittest.TestCase):
    def test_balanced_posts_and_trial_balance(self):
        led = Ledger()
        led.post(JournalEntry("2026-06-01", "t", [
            JournalLine("6440", debit="100"), JournalLine("2100", credit="100")]))
        dr, cr, rows = led.trial_balance()
        self.assertEqual(dr, D("100"))
        self.assertEqual(dr, cr)

    def test_unbalanced_rejected(self):
        led = Ledger()
        with self.assertRaises(ValueError):
            led.post(JournalEntry("2026-06-01", "bad", [
                JournalLine("6440", debit="100"), JournalLine("2100", credit="90")]))

    def test_zero_rejected(self):
        led = Ledger()
        with self.assertRaises(ValueError):
            led.post(JournalEntry("2026-06-01", "z", [
                JournalLine("6440", debit="0"), JournalLine("2100", credit="0")]))

    def test_decimal_no_float_error(self):
        led = Ledger()
        led.post(JournalEntry("2026-06-01", "t", [
            JournalLine("x", debit="0.1"), JournalLine("x2", debit="0.2"),
            JournalLine("y", credit="0.3")]))
        dr, cr, _ = led.trial_balance()
        self.assertEqual(dr, D("0.3"))          # float 会得 0.30000000000000004


class AccrualTest(unittest.TestCase):
    def test_ap_with_tax(self):
        inv = _mk_invoice("AP-1", "110.00", sub="100.00", tax="10.00")
        e = posting.accrual_entry(inv, direction="AP")
        self.assertTrue(e.is_balanced())
        by_acct = {l.account: (l.debit, l.credit) for l in e.lines}
        self.assertEqual(by_acct["6440 会计费 Accounting Fees"], (D("100.00"), D("0")))
        self.assertEqual(by_acct[A.INPUT_TAX], (D("10.00"), D("0")))
        self.assertEqual(by_acct[A.AP], (D("0"), D("110.00")))

    def test_ap_no_breakdown(self):
        inv = _mk_invoice("AP-2", "88.00")       # 只有总额
        e = posting.accrual_entry(inv, direction="AP")
        self.assertTrue(e.is_balanced())
        self.assertEqual(len(e.lines), 2)        # 费用 + 应付
        by = {l.account: l.debit for l in e.lines if l.debit}
        self.assertEqual(by["6440 会计费 Accounting Fees"], D("88.00"))

    def test_ar_with_tax(self):
        inv = _mk_invoice("AR-1", "110.00", sub="100.00", tax="10.00")
        e = posting.accrual_entry(inv, direction="AR")
        self.assertTrue(e.is_balanced())
        by_acct = {l.account: (l.debit, l.credit) for l in e.lines}
        self.assertEqual(by_acct[A.AR], (D("110.00"), D("0")))
        self.assertEqual(by_acct[A.REVENUE], (D("0"), D("100.00")))
        self.assertEqual(by_acct[A.OUTPUT_TAX], (D("0"), D("10.00")))

    def test_direction_inference(self):
        # 本方=开票方 → AR
        inv = _mk_invoice("X", "100", issuer="My Company Ltd", customer="Client A")
        self.assertEqual(posting.infer_direction(inv, own_company="My Company"), "AR")
        # 本方=收票方 → AP
        inv2 = _mk_invoice("Y", "100", issuer="Vendor B", customer="My Company Ltd")
        self.assertEqual(posting.infer_direction(inv2, own_company="My Company"), "AP")
        # 无本方配置 → 默认 AP
        self.assertEqual(posting.infer_direction(inv2), "AP")

    def test_subtax_mismatch_uses_total(self):
        # 净额+税 对不上总额 → 以总额为准
        inv = _mk_invoice("M", "120.00", sub="100.00", tax="10.00")
        e = posting.accrual_entry(inv, direction="AP")
        self.assertTrue(e.is_balanced())
        self.assertEqual(e.totals()[0], D("120.00"))


class ServiceTest(unittest.TestCase):
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

    def test_gate_rejects_unapproved(self):
        inv = _mk_invoice("G-1", "100.00", approve="Pending")
        with self.assertRaises(ValueError):
            service.post_invoice(inv)

    def test_gate_rejects_statement(self):
        inv = _mk_invoice("G-2", "100.00")
        inv.doc_type = "statement"
        with self.assertRaises(ValueError):
            service.post_invoice(inv)

    def test_post_approved_and_trial_balance(self):
        inv = _mk_invoice("P-1", "110.00", sub="100.00", tax="10.00", file_hash="hp1")
        no = service.post_invoice(inv, by="tester")
        self.assertTrue(no.startswith("202606-"))
        dr, cr, rows, ok = service.trial_balance()
        self.assertTrue(ok)
        self.assertEqual(dr, D("110.00"))
        led = service.load_ledger()
        self.assertEqual(led.net(A.AP), D("-110.00"))          # 负债贷方 → 负净额

    def test_idempotent_duplicate_rejected(self):
        inv = _mk_invoice("D-1", "50.00", file_hash="hd1")
        service.post_invoice(inv)
        with self.assertRaises(ValueError):
            service.post_invoice(inv)                          # 同来源重复入账被拒
        self.assertEqual(len(store.list_entries()), 1)

    def test_entry_no_sequence(self):
        service.post_invoice(_mk_invoice("S-1", "10.00", file_hash="h1"))
        service.post_invoice(_mk_invoice("S-2", "20.00", file_hash="h2"))
        nos = sorted(e.entry_no for e in store.list_entries())
        self.assertEqual(nos, ["202606-0001", "202606-0002"])

    def test_reverse_restores_balance(self):
        inv = _mk_invoice("R-1", "77.00", file_hash="hr1")
        no = service.post_invoice(inv)
        rev = store.reverse_entry(no, by="tester", at="2026-06-02T00:00:00Z")
        self.assertTrue(rev.startswith("202606-"))
        self.assertEqual(store.get_entry(no).status, "Reversed")
        # 红冲后净额归零
        led = service.load_ledger()
        self.assertEqual(led.net(A.AP), D("0"))
        # 原来源被红冲后可再次入账（不再算未红冲的 Posted）
        self.assertIsNone(store.existing_posted("invoice", "hr1"))

    def test_full_pipeline_gate(self):
        # 真实 PDF 走管道 → 未审核前拒绝，审核后可入账
        doc = fitz.open()
        pg = doc.new_page()
        for i, ln in enumerate([
                "Acme Consulting LLC", "Invoice No: AC-2026-9001", "Date: 2026-06-15",
                "Subtotal: $1,000.00", "Tax: $100.00", "Total Due: $1,100.00"]):
            pg.insert_text((40, 50 + i * 22), ln)
        p = self._dir / "inv.pdf"
        doc.save(str(p)); doc.close()
        inv = pipeline.process_path(p, original_name="inv.pdf", doc_type="invoice")[0]
        with self.assertRaises(ValueError):
            service.post_invoice(inv)                          # 默认 Pending
        inv.approve_status = "Approved"
        db.resave_invoice(inv)
        no = service.post_invoice_by_hash(inv.file_hash, by="tester")
        self.assertTrue(no)
        _, _, _, ok = service.trial_balance()
        self.assertTrue(ok)


class SettlementTest(unittest.TestCase):
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

    def _accrue(self, no, total, fh, direction="AP"):
        inv = _mk_invoice(no, total, file_hash=fh)
        service.post_invoice(inv, direction=direction)
        return fh

    def test_full_payment_clears(self):
        fh = self._accrue("AP-001", "1000.00", "h1")
        service.settle_invoice(fh, cash_amount="1000.00")
        op, _, _ = settlement.open_amount(fh)
        self.assertEqual(op, D("0"))
        led = service.load_ledger()
        self.assertEqual(-led.net(A.AP), D("0"))               # 应付清零
        self.assertEqual(-led.net(A.BANK), D("1000.00"))       # 银行减 1000
        self.assertTrue(service.trial_balance()[3])

    def test_partial_leaves_open(self):
        fh = self._accrue("AP-002", "1000.00", "h1")
        service.settle_invoice(fh, cash_amount="400.00", settle_amount="400.00")
        op, _, _ = settlement.open_amount(fh)
        self.assertEqual(op, D("600.00"))
        self.assertEqual(-service.load_ledger().net(A.AP), D("600.00"))

    def test_ar_fee_diff_debit(self):
        fh = self._accrue("AR-001", "1000.00", "h1", direction="AR")
        service.settle_invoice(fh, cash_amount="980.00", diff_reason="fee")
        led = service.load_ledger()
        self.assertEqual(led.net(A.AR), D("0"))                # 应收已清
        self.assertEqual(led.net(A.FEE), D("20.00"))          # 手续费费用 20
        self.assertTrue(service.trial_balance()[3])

    def test_ar_withholding_prepaid_asset(self):
        fh = self._accrue("AR-002", "1000.00", "h1", direction="AR")
        service.settle_invoice(fh, cash_amount="900.00", diff_reason="withholding_ar")
        led = service.load_ledger()
        self.assertEqual(led.net(A.AR), D("0"))
        self.assertEqual(led.net(A.WHT_PREPAID), D("100.00"))  # 预缴所得税(资产借方)

    def test_ap_withholding_payable_credit(self):
        fh = self._accrue("AP-005", "1000.00", "h1")
        service.settle_invoice(fh, cash_amount="900.00", diff_reason="withholding_ap")
        led = service.load_ledger()
        self.assertEqual(-led.net(A.AP), D("0"))
        self.assertEqual(-led.net(A.WHT_PAYABLE), D("100.00"))  # 代扣税款(负债贷方)
        self.assertEqual(-led.net(A.BANK), D("900.00"))         # 只付净额

    def test_ap_discount_reduces_expense(self):
        # 应计费用 1000，折扣 20 贷记同一费用科目 → 净 980
        inv = _mk_invoice("AP-006", "1000.00", file_hash="h1",
                          account="6440 会计费 Accounting Fees")
        service.post_invoice(inv, direction="AP")
        service.settle_invoice("h1", cash_amount="980.00",
                               diff_account="6440 会计费 Accounting Fees")
        self.assertEqual(service.load_ledger().net("6440 会计费 Accounting Fees"), D("980.00"))

    def test_unspecified_diff_rejected(self):
        fh = self._accrue("AP-007", "1000.00", "h1")
        with self.assertRaises(ValueError):
            service.settle_invoice(fh, cash_amount="950.00")     # 差 50 未指定科目
        op, _, _ = settlement.open_amount(fh)
        self.assertEqual(op, D("1000.00"))                       # 被拒后仍全额未结

    def test_over_settle_rejected(self):
        fh = self._accrue("AP-008", "500.00", "h1")
        with self.assertRaises(ValueError):
            service.settle_invoice(fh, cash_amount="600.00", settle_amount="600.00")

    def test_rounding_tolerance(self):
        fh = self._accrue("AP-K1", "1000.00", "h1")
        service.settle_invoice(fh, cash_amount="999.98", tolerance="1.00")
        self.assertEqual(settlement.open_amount(fh)[0], D("0"))
        # AP 少付 0.02 → 舍入差记贷方（冲减费用），net = -0.02（与 spike 场景 K 一致）
        self.assertEqual(service.load_ledger().net(A.ROUNDING), D("-0.02"))
        # 超阈值拒绝
        fh2 = self._accrue("AP-K2", "1000.00", "h2")
        with self.assertRaises(ValueError):
            service.settle_invoice(fh2, cash_amount="950.00", tolerance="1.00")

    def test_control_reconciliation(self):
        self._accrue("AP-J1", "1000.00", "h1")
        self._accrue("AP-J2", "500.00", "h2")
        self._accrue("AP-J3", "800.00", "h3")
        service.settle_invoice("h1", cash_amount="1000.00")
        service.settle_invoice("h2", cash_amount="200.00", settle_amount="200.00")
        service.settle_invoice("h3", cash_amount="800.00")
        ctl = service.control_view()
        self.assertEqual(ctl["AP"]["control"], "300.00")
        self.assertEqual(ctl["AP"]["detail"], "300.00")
        self.assertTrue(ctl["AP"]["ok"])
        # 单票状态取自明细
        self.assertEqual(settlement.open_amount("h2")[0], D("300.00"))
        self.assertEqual(len(service.open_view()), 1)            # 只剩 J2 未结

    def test_reverse_settlement_restores_open(self):
        fh = self._accrue("AP-R", "1000.00", "h1")
        no = service.settle_invoice(fh, cash_amount="400.00", settle_amount="400.00")
        self.assertEqual(settlement.open_amount(fh)[0], D("600.00"))
        store.reverse_entry(no, by="t", at="2026-06-02T00:00:00Z")
        self.assertEqual(settlement.open_amount(fh)[0], D("1000.00"))  # 红冲结算后未结回到全额


if __name__ == "__main__":
    unittest.main()
