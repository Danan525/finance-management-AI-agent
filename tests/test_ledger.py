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

    def test_self_party_auto_direction_ar_ap(self):
        # 回归：登记为 self 的我方主体 → 我方开票(issuer=self)自动判 AR、收票(customer=self)自动判 AP。
        # 此前 own_company 全靠显式传，AR 发票默认被当 AP（样例自测暴露：AR 被记成 AP、银行少 16400）。
        from core import counterparty as cp
        cp.register("Starlan Design Studio", kind="self", by="t", force=True)
        ar = _mk_invoice("AR-1", "5000.00", issuer="Starlan Design Studio",
                         customer="Meridian Corp", file_hash="ar1")
        ap = _mk_invoice("AP-1", "600.00", issuer="Adobe Systems Inc",
                         customer="Starlan Design Studio", file_hash="ap1")
        db.save_invoice(ar); db.save_invoice(ap)
        prev = {x["invoice_no"]: x["preview"]["direction"] for x in service.postable_invoices()}
        self.assertEqual(prev.get("AR-1"), "AR")      # 我方开票 → 应收
        self.assertEqual(prev.get("AP-1"), "AP")      # 我方收票 → 应付
        service.post_invoice_by_hash("ar1")           # 不给方向，靠自动判
        service.post_invoice_by_hash("ap1")
        led = service.load_ledger()
        self.assertEqual(led.net(A.AR), Decimal("5000.00"))   # 记进应收（不是应付）
        self.assertEqual(led.net(A.AP), Decimal("-600.00"))

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


class ManualEntryTest(unittest.TestCase):
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

    def test_balanced_manual_entry_posts(self):
        no = service.post_manual_entry([
            {"account": "6440 会计费 Accounting Fees", "debit": "100.00"},
            {"account": "2100 应付账款 Accounts Payable", "credit": "100.00"}],
            date="2026-06-01", memo="计提会计费", by="tester",
            allow_control=True, counterparty="ACME Inc")
        self.assertTrue(no.startswith("202606-"))
        led = service.load_ledger()
        self.assertEqual(led.net("6440 会计费 Accounting Fees"), D("100.00"))
        self.assertTrue(service.trial_balance()[3])
        e = store.get_entry(no)
        self.assertEqual(e.source_kind, "manual")

    def test_unbalanced_rejected(self):
        with self.assertRaises(ValueError):
            service.post_manual_entry([
                {"account": "6440 会计费", "debit": "100"},
                {"account": "2100 应付账款", "credit": "90"}], date="2026-06-01",
                allow_control=True, counterparty="ACME Inc")

    def test_min_two_lines(self):
        with self.assertRaises(ValueError):
            service.post_manual_entry([{"account": "6440 会计费", "debit": "100"}],
                                      date="2026-06-01")

    def test_line_one_side_only(self):
        with self.assertRaises(ValueError):    # 同一行借贷都填
            service.post_manual_entry([
                {"account": "6440 会计费", "debit": "100", "credit": "100"},
                {"account": "2100 应付账款", "credit": "100"}], date="2026-06-01",
                allow_control=True, counterparty="ACME Inc")

    def test_invalid_account_rejected(self):
        with self.assertRaises(ValueError):    # 编码非 1-6 开头 → 无法归类
            service.post_manual_entry([
                {"account": "X 乱码", "debit": "100"},
                {"account": "2100 应付账款", "credit": "100"}], date="2026-06-01",
                allow_control=True, counterparty="ACME Inc")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            service.post_manual_entry([
                {"account": "6440 会计费", "debit": "-100"},
                {"account": "2100 应付账款", "credit": "-100"}], date="2026-06-01",
                allow_control=True, counterparty="ACME Inc")

    def test_cash_line_requires_activity(self):
        # 动用现金(银行)必须标活动类别
        with self.assertRaises(ValueError):
            service.post_manual_entry([
                {"account": "6603 财务费用-手续费 Bank/Platform Fees", "debit": "30"},
                {"account": "1002 银行存款 Bank", "credit": "30"}], date="2026-06-01")
        no = service.post_manual_entry([
            {"account": "6603 财务费用-手续费 Bank/Platform Fees", "debit": "30"},
            {"account": "1002 银行存款 Bank", "credit": "30"}],
            date="2026-06-01", activity="operating")
        self.assertTrue(no)
        self.assertEqual(-service.load_ledger().net("1002 银行存款 Bank"), D("30"))

    def test_control_account_needs_explicit_confirm(self):
        # 软护栏：手工凭证动应付/应收，未显式确认 → 拒绝（提示应经发票应计/结算）
        lines = [{"account": "6440 会计费", "debit": "100"},
                 {"account": "2100 应付账款 Accounts Payable", "credit": "100"}]
        with self.assertRaises(ValueError) as cm:
            service.post_manual_entry(lines, date="2026-06-01")
        self.assertIn("往来控制账户", str(cm.exception))
        # 确认了但没填对手方 → 仍拒绝（往来无从追踪）
        with self.assertRaises(ValueError):
            service.post_manual_entry(lines, date="2026-06-01", allow_control=True)
        # 确认 + 对手方 → 放行
        no = service.post_manual_entry(lines, date="2026-06-01",
                                       allow_control=True, counterparty="ACME Inc")
        self.assertTrue(no)
        self.assertEqual(store.get_entry(no).counterparty, "ACME Inc")

    def test_control_reconciliation_counts_manual(self):
        # 手工往来计入控制账户对账的明细侧 → 不产生假告警
        service.post_manual_entry([
            {"account": "6440 会计费", "debit": "100"},
            {"account": "2100 应付账款 Accounts Payable", "credit": "100"}],
            date="2026-06-01", allow_control=True, counterparty="ACME Inc")
        ctl = service.control_view()
        self.assertTrue(ctl["AP"]["ok"], ctl)
        self.assertEqual(ctl["AP"]["control"], "100")
        self.assertEqual(ctl["AP"]["invoice_detail"], "0")
        self.assertEqual(ctl["AP"]["other"], "100")
        self.assertTrue(ctl["AR"]["ok"])

    def test_control_reconciliation_after_reversal(self):
        # 红冲手工往来凭证：控制账户与明细一起归零（红冲沿用对手方）
        no = service.post_manual_entry([
            {"account": "1100 应收账款 Accounts Receivable", "debit": "80"},
            {"account": "4000 营业收入 Sales Revenue", "credit": "80"}],
            date="2026-06-02", allow_control=True, counterparty="Client X")
        store.reverse_entry(no, by="tester", at="2026-06-03T00:00:00Z")
        ctl = service.control_view()
        self.assertEqual(ctl["AR"]["control"], "0")
        self.assertEqual(ctl["AR"]["detail"], "0")
        self.assertTrue(ctl["AR"]["ok"])

    def test_account_code_and_canonical_name(self):
        # 2026-08-03 自检：编码贴着名字（用户手填）不能让科目判定失效
        self.assertEqual(A.account_code("1002银行存款"), "1002")
        self.assertEqual(A.account_code("1002 银行存款 Bank"), "1002")
        self.assertEqual(A.account_code("乱码"), "")
        self.assertTrue(A.is_cash("1002银行存款"))
        self.assertTrue(A.is_control("2100应付账款"))
        self.assertEqual(A.control_side("2100 应付账款"), "AP")     # 写法不同也按编码归属
        self.assertEqual(A.control_side("1100xxx"), "AR")
        self.assertIsNone(A.control_side("6440 会计费"))
        # 过账时科目名规范化 → 不产生"影子科目"（余额键统一）
        self.assertEqual(A.canonical_account("1002银行存款"), A.BANK)
        self.assertEqual(A.canonical_account("6440 会计费"), "6440 会计费")   # 表外编码原样

    def test_shadow_cash_account_still_needs_activity(self):
        # 手填 "1002银行存款" 曾绕过"动现金必标活动"→ 现金流量表漏计而 E1/E3 假通过
        with self.assertRaises(ValueError):
            service.post_manual_entry([
                {"account": "6603 手续费", "debit": "30"},
                {"account": "1002银行存款", "credit": "30"}], date="2026-06-01")
        no = service.post_manual_entry([
            {"account": "6603 手续费", "debit": "30"},
            {"account": "1002银行存款", "credit": "30"}],
            date="2026-06-01", activity="operating")
        # 规范化后与常规写法合并为同一科目（试算平衡不多出一行）
        accts = [a for a, _d, _c in service.load_ledger().trial_balance()[2]]
        self.assertIn(A.BANK, accts)
        self.assertNotIn("1002银行存款", accts)
        self.assertTrue(store.get_entry(no))

    def test_control_account_variant_spelling_not_miscounted(self):
        # 曾把"编码相同但写法不同"的应付误算进应收侧 → 假告警
        service.post_manual_entry([
            {"account": "6440 会计费", "debit": "100"},
            {"account": "2100 应付账款", "credit": "100"}],
            date="2026-06-01", allow_control=True, counterparty="ACME Inc.")
        ctl = service.control_view()
        self.assertTrue(ctl["AP"]["ok"], ctl)
        self.assertTrue(ctl["AR"]["ok"], ctl)
        self.assertEqual(ctl["AP"]["control"], "100")
        self.assertEqual(ctl["AR"]["detail"], "0")

    def test_opening_capital_via_manual(self):
        # 期初建账即一张手工凭证:借银行/贷实收资本(动现金→标筹资)
        service.post_manual_entry([
            {"account": "1002 银行存款 Bank", "debit": "5000"},
            {"account": "3000 实收资本 Share Capital", "credit": "5000"}],
            date="2026-06-01", memo="期初出资", activity="financing", by="admin")
        self.assertTrue(service.trial_balance()[3])
        self.assertEqual(service.load_ledger().net("1002 银行存款 Bank"), D("5000"))


class AuditFixesTest(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        self._fx, self._af = config.FX_RATES_PATH, config.FX_AUTO_FETCH
        config.DB_PATH = self._dir / "t.db"
        config.UPLOAD_DIR = self._dir / "up"
        config.FX_RATES_PATH = self._dir / "fx.json"      # 隔离汇率文件，绝不写真实 config/
        config.FX_AUTO_FETCH = False                      # 测试不联网
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        config.FX_RATES_PATH, config.FX_AUTO_FETCH = self._fx, self._af
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_M1_negative_line_rejected_at_engine(self):
        # 恒平但含负数行,内核层就该拒(不止 post_manual_entry)
        e = JournalEntry("2026-06-01", "负数行", [
            JournalLine("6440 会计费", debit="200"),
            JournalLine("6441 其它", debit="-100"),
            JournalLine("2100 应付账款 Accounts Payable", credit="100")])
        with self.assertRaises(ValueError):
            e.assert_balanced()
        with self.assertRaises(ValueError):
            store.post_entry(e, by="t", at="2026-06-01T00:00:00Z")

    def test_M2_invoice_empty_hash_rejected(self):
        inv = _mk_invoice("NO-HASH", "100.00", file_hash="")
        with self.assertRaises(ValueError):
            service.post_invoice(inv)

    def test_M3_atomic_over_settle_rejected(self):
        inv = _mk_invoice("AP-1", "1000.00", file_hash="h1")
        service.post_invoice(inv)
        service.settle_invoice("h1", cash_amount="600.00", settle_amount="600.00")
        # 再结 600(累计 1200 > 1000)应被写锁内的原子防护拒绝
        with self.assertRaises(ValueError):
            service.settle_invoice("h1", cash_amount="600.00", settle_amount="600.00")

    def test_L1_diff_account_cannot_be_control(self):
        inv = _mk_invoice("AR-1", "1000.00", file_hash="h1")
        service.post_invoice(inv, direction="AR")
        with self.assertRaises(ValueError):
            service.settle_invoice("h1", cash_amount="980.00",
                                   diff_account="2100 应付账款 Accounts Payable")

    def test_F1_tax_deductible_vs_into_cost(self):
        # 可抵扣(默认/VAT):税记进项税、费用记净额
        inv = _mk_invoice("AP-VAT", "1190.00", sub="1000.00", tax="190.00", file_hash="hv")
        service.post_invoice(inv, direction="AP", tax_deductible=True)
        led = service.load_ledger()
        self.assertEqual(led.net(A.INPUT_TAX), D("190.00"))
        self.assertEqual(led.net("6440 会计费 Accounting Fees"), D("1000.00"))
        # 不可抵扣(美国销售税):税并入成本、不生成进项税资产
        inv2 = _mk_invoice("AP-US", "1080.00", sub="1000.00", tax="80.00", file_hash="hu")
        service.post_invoice(inv2, direction="AP", tax_deductible=False)
        led = service.load_ledger()
        self.assertEqual(led.net(A.INPUT_TAX), D("190.00"))          # 未新增进项税
        self.assertEqual(led.net("6440 会计费 Accounting Fees"), D("2080.00"))  # 1000 + 1080(含税)
        self.assertTrue(service.trial_balance()[3])

    def test_M1_missing_total_rejected(self):
        # 缺合计金额 → 拒绝入账(防拆分税只抓一档时靠 sub+tax 兜底静默丢税)
        inv = _mk_invoice("NOTOTAL", "0", sub="1000", tax="90", file_hash="hm1")
        # 清掉 total_due(_mk_invoice 会设,这里显式置空模拟提取缺失)
        inv.set("total_due", FieldValue(value=None))
        with self.assertRaises(ValueError):
            service.post_invoice(inv, direction="AP")

    def test_F2_foreign_currency_without_rate_rejected(self):
        # 外币发票【无录入日汇率】时仍拒（fail-closed，不静默当 USD）；有汇率则换算入账见 test_fx。
        af = config.FX_AUTO_FETCH
        config.FX_AUTO_FETCH = False        # 不联网：本地无 JPY 汇率 → 拒
        try:
            inv = _mk_invoice("JP-1", "110000", file_hash="hj")
            inv.set("currency_settlement", FieldValue(value="JPY"))
            with self.assertRaises(ValueError):
                service.post_invoice(inv, direction="AR")
            # USD(或空/美元符号)正常入账
            inv2 = _mk_invoice("US-OK", "1000.00", file_hash="hk")
            inv2.set("currency_settlement", FieldValue(value="USD"))
            self.assertTrue(service.post_invoice(inv2, direction="AR"))
        finally:
            config.FX_AUTO_FETCH = af


def _mk_statement(file_hash="s1", ccy="USD", txns=None):
    from core.models import Transaction
    inv = Invoice()
    inv.file_hash = file_hash
    inv.doc_type = "statement"
    inv.set("currency_settlement", FieldValue(value=ccy))
    inv.transactions = list(txns or [])
    return inv


def _txn(date, desc, expense=None, income=None, ccy=None):
    from core.models import Transaction
    return Transaction(date=date, description=desc,
                       expense=(D(expense) if expense is not None else None),
                       income=(D(income) if income is not None else None), currency=ccy)


class StatementEntryTest(unittest.TestCase):
    """非发票银行流水入账：选对方科目 → Dr/Cr 银行；方向机械、幂等、币种/科目护栏。"""

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        self._fx, self._af = config.FX_RATES_PATH, config.FX_AUTO_FETCH
        config.DB_PATH = self._dir / "t.db"; config.UPLOAD_DIR = self._dir / "up"
        config.FX_RATES_PATH = self._dir / "fx.json"      # 隔离汇率文件，绝不写真实 config/
        config.FX_AUTO_FETCH = False                      # 测试不联网
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False; db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        config.FX_RATES_PATH, config.FX_AUTO_FETCH = self._fx, self._af
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def _bank_net(self):
        led = service.load_ledger()
        return led.net(A.BANK)

    def test_expense_credits_bank(self):
        db.save_invoice(_mk_statement("s1", txns=[_txn("2026-06-05", "Bank fee", expense="25.00")]))
        no = service.post_statement_entry("s1", 0, A.FEE, activity="operating")
        self.assertTrue(no.startswith("202606-"))
        self.assertEqual(self._bank_net(), D("-25.00"))     # 银行减少
        led = service.load_ledger()
        self.assertEqual(led.net("6603 财务费用-手续费 Bank/Platform Fees"), D("25.00"))

    def test_income_debits_bank(self):
        db.save_invoice(_mk_statement("s2", txns=[_txn("2026-06-06", "Interest", income="100.00")]))
        no = service.post_statement_entry("s2", 0, "4100 利息收入 Interest Income", activity="operating")
        self.assertTrue(no)
        self.assertEqual(self._bank_net(), D("100.00"))     # 银行增加

    def test_idempotent_reject_double_post(self):
        db.save_invoice(_mk_statement("s3", txns=[_txn("2026-06-07", "Fee", expense="10.00")]))
        service.post_statement_entry("s3", 0, A.FEE, activity="operating")
        with self.assertRaises(ValueError):
            service.post_statement_entry("s3", 0, A.FEE, activity="operating")

    def test_foreign_currency_without_rate_rejected(self):
        # 外币流水【无交易日汇率】时仍拒（fail-closed）；有汇率则换算入账见 test_fx。
        af = config.FX_AUTO_FETCH
        config.FX_AUTO_FETCH = False        # 不联网：本地无 JPY 汇率 → 拒
        try:
            db.save_invoice(_mk_statement("s4", ccy="JPY", txns=[_txn("2026-06-08", "Fee", expense="500")]))
            with self.assertRaises(ValueError):
                service.post_statement_entry("s4", 0, A.FEE, activity="operating")
        finally:
            config.FX_AUTO_FETCH = af

    def test_counter_cash_rejected(self):
        db.save_invoice(_mk_statement("s5", txns=[_txn("2026-06-09", "Transfer", expense="50.00")]))
        with self.assertRaises(ValueError):
            service.post_statement_entry("s5", 0, A.BANK, activity="operating")

    def test_counter_control_rejected(self):
        db.save_invoice(_mk_statement("s6", txns=[_txn("2026-06-10", "Pay", expense="50.00")]))
        with self.assertRaises(ValueError):
            service.post_statement_entry("s6", 0, A.AP, activity="operating")

    def test_activity_required(self):
        db.save_invoice(_mk_statement("s7", txns=[_txn("2026-06-11", "Fee", expense="5.00")]))
        with self.assertRaises(ValueError):
            service.post_statement_entry("s7", 0, A.FEE, activity="")

    def test_posted_row_locked_from_edit_and_delete(self):
        # H1: 入账某笔后，删除它前面的行会重排下标、致重复入账 → 编辑侧须锁定已入账行（自检修）
        from review import service as review
        db.save_invoice(_mk_statement("sL", txns=[
            _txn("2026-06-01", "A", expense="10.00"),
            _txn("2026-06-02", "B", expense="20.00"),
            _txn("2026-06-03", "C", expense="30.00")]))
        service.post_statement_entry("sL", 1, A.FEE, activity="operating")   # 入账 B(idx1)
        self.assertEqual(service.posted_statement_indices("sL"), {1})
        with self.assertRaises(ValueError):
            review.save_transaction("sL", 0, "__del__", "")      # 删前面行 → 拒
        with self.assertRaises(ValueError):
            review.save_transaction("sL", 1, "expense", "99")  # 改已入账笔 → 拒

    def test_view_lists_open_and_excludes_posted(self):
        db.save_invoice(_mk_statement("s8", txns=[
            _txn("2026-06-12", "Fee", expense="10.00"),
            _txn("2026-06-13", "Interest", income="20.00")]))
        rows = service.statement_lines_view()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["direction"] for r in rows}, {"out", "in"})
        service.post_statement_entry("s8", 0, A.FEE, activity="operating")
        rows2 = service.statement_lines_view()
        self.assertEqual(len(rows2), 1)                      # 已入账那笔移出待入账
        self.assertEqual(rows2[0]["index"], 1)

    def test_summary_statement_open_separate_from_invoice_postable(self):
        # 回归：顶栏「待入账」曾只数发票，无票银行流水落在「流水入账」却不进汇总，
        # 造成"顶栏有数、待入账(发票)tab 却空"的错位。summary 须单列 statement_open。
        db.save_invoice(_mk_statement("sm", txns=[
            _txn("2026-06-14", "Fee A", expense="5.00"),
            _txn("2026-06-15", "Fee B", expense="6.00"),
            _txn("2026-06-16", "Interest", income="7.00")]))
        s = service.summary()
        self.assertEqual(s["postable"], 0)                   # 无待计提发票
        self.assertEqual(s["statement_open"], 3)             # 3 笔无票银行行待入账
        service.post_statement_entry("sm", 0, A.FEE, activity="operating")
        self.assertEqual(service.summary()["statement_open"], 2)  # 入账一笔后随之减少


class BankReconTest(unittest.TestCase):
    """银行余额调节诊断：流水自洽校验(opening+净额==closing) + 入账进度三态。"""

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = self._dir / "t.db"; config.UPLOAD_DIR = self._dir / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False; db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def _save_stmt(self, fh, opening, closing, txns):
        inv = _mk_statement(fh, txns=txns)
        inv.set("opening_balance", FieldValue(value=opening))
        inv.set("closing_balance", FieldValue(value=closing))
        db.save_invoice(inv)

    def test_self_check_ok_and_progress(self):
        # 期初 1000 + 收 200 − 支 50 = 期末 1150 → 自洽
        self._save_stmt("s1", "1000.00", "1150.00", [
            _txn("2026-06-02", "In", income="200.00"),
            _txn("2026-06-03", "Fee", expense="50.00")])
        v = service.bank_reconciliation_view()
        s = v["statements"][0]
        self.assertTrue(s["self_check"])
        self.assertEqual(s["net"], "150.00")
        self.assertEqual(s["open"], 2)          # 都未入账
        self.assertEqual(v["total_open"], 2)
        # 入账一笔后 open 减一、posted 加一
        service.post_statement_entry("s1", 1, A.FEE, activity="operating")
        s2 = service.bank_reconciliation_view()["statements"][0]
        self.assertEqual(s2["posted"], 1)
        self.assertEqual(s2["open"], 1)

    def test_self_check_fails_on_bad_balances(self):
        # 期初 1000 + 收 200 = 1200 ≠ 期末 999 → 自洽失败(数据质量闸门)
        self._save_stmt("s2", "1000.00", "999.00", [_txn("2026-06-02", "In", income="200.00")])
        v = service.bank_reconciliation_view()
        self.assertFalse(v["statements"][0]["self_check"])
        self.assertEqual(v["self_check_failures"], 1)

    def test_self_check_none_when_balance_missing(self):
        # 银行未自报期末余额 → 无法校验(None)，但进度仍在
        self._save_stmt("s3", "", "", [_txn("2026-06-02", "Fee", expense="30.00")])
        s = service.bank_reconciliation_view()["statements"][0]
        self.assertIsNone(s["self_check"])
        self.assertEqual(s["open"], 1)

    def test_ledger_bank_balance_reflected(self):
        self._save_stmt("s4", "0.00", "-30.00", [_txn("2026-06-02", "Fee", expense="30.00")])
        service.post_statement_entry("s4", 0, A.FEE, activity="operating")
        v = service.bank_reconciliation_view()
        self.assertEqual(v["ledger_bank"], "-30.00")     # 总账银行余额随入账变动


if __name__ == "__main__":
    unittest.main()
