"""对账匹配：匹配引擎(1:1/1:N/N:1/差额/未匹配) + 服务(提取闸门/建池/确认/幂等)。"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import fitz

from core import config, db
from extraction import pipeline
from reconcile import matcher
from reconcile import service as rec


def _inv(h, no, vendor, ccy, amt, date, due=None):
    return {"hash": h, "invoice_no": no, "vendor": vendor, "currency": ccy,
            "amount": amt, "date": date, "due_date": due}


def _txn(idx, desc, ccy, amt, date):
    return {"stmt_hash": "s", "index": idx, "date": date, "description": desc,
            "currency": ccy, "amount": amt, "direction": "out"}


class MatcherTest(unittest.TestCase):
    def test_1to1_exact_is_auto(self):
        invs = [_inv("A", "GP-2025-1001", "Greyvane Partners", "GBP", "217000.0", "2025-01-12")]
        txns = [_txn(0, "PAY GP-2025-1001 Greyvane Partners", "GBP", "217000.0", "2025-02-03")]
        ps = matcher.match(invs, txns)
        self.assertEqual(len(ps), 1)
        self.assertEqual((ps[0]["match_type"], ps[0]["category"]), ("1:1", "auto"))
        self.assertGreaterEqual(ps[0]["match_score"], matcher.AUTO)

    def test_currency_mismatch_not_amount_scored(self):
        invs = [_inv("A", "TAL-2025-1000", "Thornfield", "AUD", "92000.0", "2025-02-14")]
        txns = [_txn(0, "INV TAL-2025-1000 SETTLEMENT", "USD", "92000.0", "2025-02-17")]
        ps = matcher.match(invs, txns)
        # 币种不同→金额不加分，但发票号命中仍成对，需人工确认（非 auto）
        self.assertEqual(ps[0]["match_type"], "1:1")
        self.assertNotEqual(ps[0]["category"], "auto")

    def test_unknown_currency_still_amount_matches(self):
        """交易币种未标注(None)不应跳过金额比对——金额+供应商+日期对上仍应成候选(非 unmatched)。"""
        invs = [_inv("A", "NW-2025-1", "Northwind Trading", "USD", "2500.0", "2025-02-14", "2025-03-14")]
        txns = [_txn(0, "Northwind Trading settlement", None, "2500.0", "2025-02-17")]  # 币种 None
        ps = matcher.match(invs, txns)
        paired = [p for p in ps if p["invoices"] == ["A"] and p["txns"]]
        self.assertTrue(paired, "币种未知但金额/供应商/日期一致，应成对而非 unmatched")
        self.assertNotEqual(paired[0]["category"], "unmatched")

    def test_ambiguous_two_invoices_flagged_multi(self):
        """两张同额同供应商发票对一笔付款(无发票号)→ 应判「多候选」交人工，而非静默自动确认其一。"""
        invs = [_inv("A", "AAA-1", "Acme", "USD", "1000.0", "2025-01-05", "2025-02-05"),
                _inv("B", "BBB-2", "Acme", "USD", "1000.0", "2025-01-06", "2025-02-06")]
        txns = [_txn(0, "Acme payment", "USD", "1000.0", "2025-01-20")]     # 无发票号，两张都像
        ps = matcher.match(invs, txns)
        self.assertTrue(any(p["category"] == "multi" for p in ps), "歧义匹配应进 multi 桶")
        self.assertFalse(any(p["category"] == "auto" for p in ps), "歧义时不得自动确认")

    def test_1toN_split_payment(self):
        invs = [_inv("A", "AAA-2025-111", "Acme", "USD", "300", "2025-01-01", "2025-02-01")]
        txns = [_txn(0, "PAY AAA-2025-111 Acme", "USD", "100", "2025-01-10"),
                _txn(1, "PAY AAA-2025-111 Acme", "USD", "200", "2025-01-20")]
        ps = matcher.match(invs, txns)
        one_n = [p for p in ps if p["match_type"] == "1:N"]
        self.assertEqual(len(one_n), 1)
        self.assertEqual(len(one_n[0]["txns"]), 2)

    def test_Nto1_consolidated_payment(self):
        invs = [_inv("B", "BBB-2025-1", "Beta", "EUR", "150", "2025-03-01"),
                _inv("C", "CCC-2025-2", "Beta", "EUR", "250", "2025-03-02")]
        txns = [_txn(0, "PAY BBB-2025-1 CCC-2025-2 Beta", "EUR", "400", "2025-03-10")]
        ps = matcher.match(invs, txns)
        n1 = [p for p in ps if p["match_type"] == "N:1"]
        self.assertEqual(len(n1), 1)
        self.assertEqual(len(n1[0]["invoices"]), 2)

    def test_fee_delta_is_confirm(self):
        invs = [_inv("D", "DDD-2025-9", "Delta", "USD", "1000", "2025-05-01")]
        txns = [_txn(0, "PAY DDD-2025-9 Delta", "USD", "999", "2025-05-05")]
        ps = matcher.match(invs, txns)
        self.assertEqual(ps[0]["match_type"], "1:1")
        self.assertEqual(ps[0]["category"], "confirm")
        self.assertEqual(ps[0]["amount_delta"], "1")

    def test_amount_mismatch_flagged_confirm(self):
        invs = [_inv("E", "EEE-2025-5", "Echo", "USD", "777", "2025-06-01")]
        txns = [_txn(0, "PAY EEE-2025-5 Echo", "USD", "500", "2025-06-05")]
        ps = matcher.match(invs, txns)
        self.assertEqual(ps[0]["category"], "confirm")
        self.assertTrue(any("金额对不上" in b for b in ps[0]["basis"]))

    def test_direction_mismatch_flagged_not_auto(self):
        """收付方向存疑：供应商发票对应到"收款(income)"→ 标记存疑且不自动通过。"""
        invs = [{"hash": "A", "invoice_no": "GP-2025-1001", "vendor": "Greyvane Partners",
                 "customer": "Acme Corp", "currency": "GBP", "amount": "217000.0",
                 "date": "2025-01-12", "due_date": None}]
        # 一笔"收入"，对手方是供应商 Greyvane（本应是我们付款给供应商）
        txns = [{"stmt_hash": "s", "index": 0, "date": "2025-02-03",
                 "description": "INV GP-2025-1001 Greyvane Partners refund", "currency": "GBP",
                 "amount": "217000.0", "direction": "in"}]
        ps = matcher.match(invs, txns)
        self.assertEqual(ps[0]["match_type"], "1:1")
        self.assertNotEqual(ps[0]["category"], "auto")     # 方向存疑 → 不自动
        self.assertTrue(any("收付方向存疑" in b for b in ps[0]["basis"]))

    def test_normal_ap_direction_stays_auto(self):
        """正常应付：供应商发票对应到"付款(expense)"→ 方向一致，仍可 auto。"""
        invs = [{"hash": "A", "invoice_no": "GP-2025-1001", "vendor": "Greyvane Partners",
                 "customer": "Acme Corp", "currency": "GBP", "amount": "217000.0",
                 "date": "2025-01-12", "due_date": None}]
        txns = [{"stmt_hash": "s", "index": 0, "date": "2025-02-03",
                 "description": "PAY GP-2025-1001 Greyvane Partners", "currency": "GBP",
                 "amount": "217000.0", "direction": "out"}]
        ps = matcher.match(invs, txns)
        self.assertEqual(ps[0]["category"], "auto")
        self.assertFalse(any("收付方向存疑" in b for b in ps[0]["basis"]))

    def test_classify_txn_types(self):
        from reconcile import classify
        self.assertEqual(classify.classify_txn("Monthly bank service fee", "out")["type"], "bank_fee")
        self.assertEqual(classify.classify_txn("PAYROLL Feb salaries", "out")["type"], "payroll")
        self.assertEqual(classify.classify_txn("VAT tax payment", "out")["type"], "tax")
        self.assertEqual(classify.classify_txn("Internal transfer to savings", "out")["type"], "internal_transfer")
        self.assertEqual(classify.classify_txn("PAY GP-2025-1001 Greyvane", "out")["type"], "vendor_payment")
        self.assertEqual(classify.classify_txn("Client receipt", "in")["type"], "customer_receipt")
        self.assertTrue(classify.classify_txn("bank service fee", "out")["no_match_ok"])
        self.assertFalse(classify.classify_txn("PAY VEND-1 x", "out")["no_match_ok"])

    def test_no_match_needed_vs_unmatched(self):
        """未匹配≠异常：无需发票的类型(手续费)归『无需匹配』；需发票却缺的归『未匹配』带原因。"""
        txns = [{"stmt_hash": "s", "index": 0, "description": "Monthly bank fee", "currency": "USD",
                 "amount": "5", "direction": "out", "txn_type": "bank_fee", "txn_label": "手续费利息",
                 "no_match_ok": True, "auto_no_match": True},                    # 清晰小额 → 自动无需匹配
                {"stmt_hash": "s", "index": 1, "description": "handling fee huge", "currency": "USD",
                 "amount": "80000", "direction": "out", "txn_type": "bank_fee", "txn_label": "手续费利息",
                 "no_match_ok": True, "auto_no_match": False, "hold_why": "大额"},   # 大额疑似 → 不自动，转待确认
                {"stmt_hash": "s", "index": 2, "description": "PAY VEND-2025-9 Acme", "currency": "USD",
                 "amount": "500", "direction": "out", "txn_type": "vendor_payment", "txn_label": "供应商付款",
                 "no_match_ok": False}]
        ps = matcher.match([], txns)
        bycat = {p["txns"][0][1]: p["category"] for p in ps}
        self.assertEqual(bycat[0], "no_match_needed")   # 小额清晰手续费 → 无需匹配
        self.assertEqual(bycat[1], "unmatched")          # 大额疑似 → 不自动跳过，进待确认（防漏票）
        self.assertEqual(bycat[2], "unmatched")          # 需发票缺票 → 未匹配

    def test_unmatched(self):
        ps = matcher.match([_inv("Z", "ZZZ-2025-1", "Zeta", "USD", "10", "2025-01-01")], [])
        self.assertEqual(ps[0]["category"], "unmatched")


def _make_invoice_pdf(path, no, date, vendor, ccy, total):
    doc = fitz.open(); pg = doc.new_page()
    for i, ln in enumerate([f"Invoice No {no}", f"Invoice Date {date}",
                            f"From {vendor}", f"Currency {ccy}", f"Total Due {total}"]):
        pg.insert_text((40, 50 + i * 20), ln)
    doc.save(str(path)); doc.close()


class ReconcileServiceTest(unittest.TestCase):
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

    def _seed(self):
        csv = ("transaction_date,counterparty_name,bank_reference,currency,debit_amount,credit_amount,statement_account_id\n"
               "2025-02-03,Greyvane Partners,PAY GP-2025-1001 Greyvane,GBP,217000.00,0,ACC-1\n"
               "2025-02-17,Thornfield Advisory,INV TAL-2025-1000 SETTLEMENT,AUD,92000.00,0,ACC-2\n")
        (self._dir / "bank.csv").write_text(csv, encoding="utf-8")
        pipeline.process_upload((self._dir / "bank.csv").read_bytes(), "bank.csv", "statement")
        for no, date, vendor, ccy, tot in [("GP-2025-1001", "2025-01-12", "Greyvane Partners", "GBP", "217000.00"),
                                           ("TAL-2025-1000", "2025-02-14", "Thornfield Advisory", "AUD", "92000.00")]:
            p = self._dir / (no + ".pdf")
            _make_invoice_pdf(p, no, date, vendor, ccy, tot)
            pipeline.process_upload(p.read_bytes(), no + ".pdf", "invoice")

    def _approve_invoices(self):
        """把所有发票置为已审核(Approved)——对账确认前的前置（应计确认）。"""
        for i in db.load_all_invoices().values():
            if (i.doc_type or "invoice") != "statement":
                i.approve_status = "Approved"; db.resave_invoice(i)

    def test_extraction_gate(self):
        # 完整发票通过；缺 total_due 的不通过
        p = self._dir / "ok.pdf"; _make_invoice_pdf(p, "OK-2025-1", "2025-01-01", "Acme", "USD", "100.00")
        ok = pipeline.process_upload(p.read_bytes(), "ok.pdf", "invoice")[0]
        self.assertTrue(rec.extraction_passed(db.get_invoice(ok.file_hash)))
        bad = fitz.open(); bad.new_page().insert_text((40, 50), "Hello world no fields here")
        bp = self._dir / "bad.pdf"; bad.save(str(bp)); bad.close()
        b = pipeline.process_upload(bp.read_bytes(), "bad.pdf", "invoice")[0]
        self.assertFalse(rec.extraction_passed(db.get_invoice(b.file_hash)))

    def test_run_confirm_idempotent(self):
        self._seed()
        r = rec.run_matching()
        self.assertEqual(r["pool_invoices"], 2)
        self.assertEqual(r["counts"]["auto"], 2)      # 两张不同币种均凭发票号+金额+币种 → auto
        self._approve_invoices()                      # 应计确认（对账结算的前置）
        cb = rec.confirm_batch("auto")
        self.assertEqual(cb["confirmed"], 2)
        # 确认后：发票保持 Approved，且标记为已对账结算
        for m in db.list_matches(status="confirmed"):
            for h in m["invoices"]:
                self.assertEqual(db.get_invoice(h).approve_status, "Approved")
                self.assertEqual(db.get_invoice(h).review_status, "Reconciled")
        # 重跑：已确认成员移出池，不再重复产生
        r2 = rec.run_matching()
        self.assertEqual(r2["pool_invoices"], 0)
        self.assertEqual(db.match_counts()["confirmed"], 2)

    def test_unconfirm_match_reverses_reconciliation(self):
        """撤销对账(反做)：已确认对账 → unconfirm 退回待确认、释放占用、发票 review_status 复原为 Approved，
        且成员重新可参与对账（不再被"已确认"占用）。"""
        self._seed()
        rec.run_matching()
        self._approve_invoices()
        rec.confirm_batch("auto")
        confirmed = db.list_matches(status="confirmed")
        self.assertTrue(confirmed)
        mid = confirmed[0]["id"]
        inv_hash = confirmed[0]["invoices"][0]
        self.assertEqual(db.get_invoice(inv_hash).review_status, "Reconciled")
        used_inv0, _ = db.confirmed_member_refs()
        self.assertIn(inv_hash, used_inv0)
        # 反做
        r = rec.unconfirm_match(mid)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(db.get_match(mid)["status"], "proposed")     # 退回待确认
        self.assertEqual(db.get_invoice(inv_hash).approve_status, "Approved")  # 应计仍在
        self.assertEqual(db.get_invoice(inv_hash).review_status, "Approved")   # 结算标记撤销
        used_inv1, _ = db.confirmed_member_refs()
        self.assertNotIn(inv_hash, used_inv1)                         # 占用已释放 → 解锁
        # 反做后可重新参与对账
        r2 = rec.run_matching()
        self.assertGreaterEqual(r2["pool_invoices"], 1)

    def test_confirm_requires_invoice_reviewed_first(self):
        """应计先于结算：发票未审核通过时，确认对账不代盖章，而是要求先审核；审核通过后可确认。"""
        self._seed()
        rec.run_matching()
        m = db.list_matches(category="auto", status="proposed")[0]
        inv_hash = m["invoices"][0]
        r = rec.confirm_match(m["id"])                 # 发票未 Approved
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("needs_invoice_review"))
        self.assertEqual(r.get("invoice_hash"), inv_hash)
        self.assertNotEqual(db.get_match(m["id"])["status"], "confirmed")  # 未确认、未代盖章
        self.assertNotEqual(db.get_invoice(inv_hash).approve_status, "Approved")
        # 审核通过后再确认 → 成功
        self._approve_invoices()
        r2 = rec.confirm_match(m["id"])
        self.assertTrue(r2.get("ok"))
        self.assertEqual(db.get_match(m["id"])["status"], "confirmed")

    def test_no_double_reconciliation(self):
        """防重复对账：已入账的发票/流水不能被再确认到另一笔匹配。"""
        self._seed()
        rec.run_matching()
        self._approve_invoices()
        m = db.list_matches(category="auto", status="proposed")[0]
        inv_hash = m["invoices"][0]
        txn_ref = m["txns"][0]
        r1 = rec.confirm_match(m["id"])
        self.assertTrue(r1.get("ok"))
        # 手工造一条"复用同一发票+流水"的提案，直接确认应被硬闸门挡下
        import core.db as _db
        now = "2026-07-10T00:00:00"
        mid2 = _db.save_match({"category": "confirm", "match_type": "1:1", "match_score": 60,
                               "currency": "GBP", "status": "proposed", "created_at": now,
                               "invoices": [inv_hash], "txns": [txn_ref], "basis": []})
        # save_match 按成员集合去重：同组合已存在(已确认那条) → 返回 0，本身就防了重复
        if mid2:
            r2 = rec.confirm_match(mid2)
            self.assertFalse(r2.get("ok"))
            self.assertEqual(r2.get("blocked"), "duplicate_member")
        # 复用已入账发票 + 换一笔"不同"流水的提案，也应被防重复挡下（发票已入账）
        other_txn = [(h, i) for (h, i) in [(txn_ref[0], txn_ref[1] + 1)]]   # 同流水另一笔（未对账）
        mid3 = _db.save_match({"category": "confirm", "match_type": "1:1", "match_score": 60,
                               "currency": "GBP", "status": "proposed", "created_at": now,
                               "invoices": [inv_hash], "txns": other_txn, "basis": []})
        r3 = rec.confirm_match(mid3)
        self.assertFalse(r3.get("ok"))
        self.assertEqual(r3.get("blocked"), "duplicate_member")

    def test_rejected_record_excluded_from_pool(self):
        """已人工拒绝(Rejected)的发票不进匹配池，不会被对账入账。"""
        from review import service as review
        self._seed()
        invs0, _, _ = rec.build_pool()
        self.assertEqual(len(invs0), 2)
        # 拒绝其中一张发票
        h = [i.file_hash for i in db.load_all_invoices().values() if i.doc_type != "statement"][0]
        review.act(h, "Rejected", by="t", reason="废单")
        invs1, _, _ = rec.build_pool()
        self.assertEqual(len(invs1), 1)
        self.assertNotIn(h, [x["hash"] for x in invs1])

    def test_reconciled_record_protected_from_reextract(self):
        """已对账入账的记录，重新提取被拒绝（否则重排下标会破坏已确认匹配）。"""
        from review import service as review
        self._seed()
        rec.run_matching()
        self._approve_invoices()
        m = db.list_matches(category="auto", status="proposed")[0]
        inv_hash = m["invoices"][0]
        stmt_hash = m["txns"][0][0]
        rec.confirm_match(m["id"])
        r_inv = review.reapply_learned(inv_hash)       # 发票 Approved → 本就受保护（不改动）
        self.assertFalse(r_inv.get("applied"))
        r_stmt = review.reapply_learned(stmt_hash)     # 流水没被标 Approved，但因含已确认交易也应被挡
        self.assertFalse(r_stmt.get("applied"))
        self.assertIn("对账", r_stmt.get("reason", ""))

    def test_reconciled_record_locked_from_edits(self):
        """已对账入账的记录：改字段/增删交易/删除 都被锁定，避免破坏已确认对应。"""
        from review import service as review
        self._seed()
        rec.run_matching()
        self._approve_invoices()
        m = db.list_matches(category="auto", status="proposed")[0]
        inv_hash = m["invoices"][0]
        stmt_hash = m["txns"][0][0]
        rec.confirm_match(m["id"])
        # 改已对账发票字段 → 挡
        with self.assertRaises(ValueError):
            review.change_field(inv_hash, "total_due", "999")
        # 增/删已对账流水的交易 → 挡（会重排下标）
        with self.assertRaises(ValueError):
            review.save_transaction(stmt_hash, -1, "__add__", "")
        with self.assertRaises(ValueError):
            review.save_transaction(stmt_hash, 0, "__del__", "")
        # 删除已对账流水 → 挡（流水未标 Approved，靠 reconciled 锁）
        with self.assertRaises(ValueError):
            review.delete_invoice(stmt_hash)

    def test_confirm_atomic_reservation_blocks_race(self):
        """并发防重复：DB 唯一约束保证同一成员只能被一笔已确认匹配预留（绕过应用层检查也挡住）。"""
        self._seed()
        rec.run_matching()
        m = db.list_matches(category="auto", status="proposed")[0]
        keys = rec._member_keys(m)
        self.assertTrue(keys)
        self.assertIsNone(db.confirm_match_tx(m["id"], keys, "t", "2026-07-10T00:00:00"))
        # 另一笔匹配复用同一成员键再预留 → 撞唯一约束，返回冲突键（并回滚）
        c = db.confirm_match_tx(99999, keys[:1], "t", "2026-07-10T00:00:00")
        self.assertEqual(c, keys[0])
        # 释放后可再次预留
        db.release_members(m["id"])
        self.assertIsNone(db.confirm_match_tx(m["id"], keys[:1], "t", "2026-07-10T00:00:00"))

    def test_reject_then_members_unmatched_not_vanish(self):
        """点『不成立』后：该匹配标记 rejected；重跑不再把这一对配在一起，两成员各自落回未匹配（不消失）。"""
        self._seed()
        rec.run_matching()
        m = db.list_matches(category="auto", status="proposed")[0]
        inv_hash = m["invoices"][0]; txn_ref = tuple(m["txns"][0])
        rec.reject_match(m["id"])
        self.assertEqual(db.get_match(m["id"])["status"], "rejected")
        rec.run_matching()
        # 同一对不再被配上；发票与交易各自作为未匹配出现（不消失）
        proposed = db.list_matches(status="proposed")
        paired_again = any(inv_hash in mm["invoices"] and txn_ref in [tuple(t) for t in mm["txns"]] for mm in proposed)
        self.assertFalse(paired_again)
        inv_seen = any(inv_hash in mm["invoices"] for mm in proposed)
        txn_seen = any(txn_ref in [tuple(t) for t in mm["txns"]] for mm in proposed)
        self.assertTrue(inv_seen and txn_seen)

    def test_ack_no_match_and_unack(self):
        """确认无需发票：无需匹配交易 ack 后计入已处理（流水移出队列、锁定）；unack 退回待核。"""
        from review import service as review
        (self._dir / "f.csv").write_text(
            "transaction_date,counterparty_name,bank_reference,currency,debit_amount,credit_amount,statement_account_id\n"
            "2025-01-05,Bank,monthly wire fee,USD,20.00,0,A\n", encoding="utf-8")
        sh = pipeline.process_upload((self._dir / "f.csv").read_bytes(), "f.csv", "statement")[0].file_hash
        rec.run_matching()
        m = db.list_matches(category="no_match_needed", status="proposed")[0]
        self.assertEqual(len(review.review_queue(doc_type="statement")), 1)
        r = rec.ack_no_match(m["id"])
        self.assertTrue(r.get("ok"))
        self.assertEqual(db.match_counts()["no_match_needed"], 0)
        self.assertEqual(len(review.review_queue(doc_type="statement")), 0)      # 全处理完 → 移出
        self.assertTrue(review.review_detail(sh)["transactions"][0]["reconciled"])
        rec.unack_no_match(m["id"])
        self.assertEqual(db.match_counts()["no_match_needed"], 1)
        self.assertEqual(len(review.review_queue(doc_type="statement")), 1)      # 退回

    def test_cannot_reject_one_sided(self):
        """单边项（未匹配/无需匹配）不能标『不成立』——否则重跑会致该成员消失。"""
        (self._dir / "b.csv").write_text(
            "transaction_date,counterparty_name,bank_reference,currency,debit_amount,credit_amount,statement_account_id\n"
            "2025-02-03,Acme,PAY VEND-2025-9 Acme,USD,500.00,0,A\n", encoding="utf-8")
        pipeline.process_upload((self._dir / "b.csv").read_bytes(), "b.csv", "statement")
        rec.run_matching()
        m = db.list_matches(category="unmatched", status="proposed")[0]
        r = rec.reject_match(m["id"])
        self.assertFalse(r.get("ok"))
        rec.run_matching()
        self.assertEqual(db.match_counts()["unmatched"], 1)   # 仍在，未消失
        self.assertEqual(db.match_counts()["rejected"], 0)

    def test_unreject_restores_match(self):
        """撤销「不成立」：移出黑名单+重跑，这一对重新被自动配上。"""
        self._seed()
        rec.run_matching()
        m = db.list_matches(category="auto", status="proposed")[0]
        mid, inv_hash, txn_ref = m["id"], m["invoices"][0], tuple(m["txns"][0])
        rec.reject_match(mid); rec.run_matching()
        self.assertEqual(db.match_counts()["rejected"], 1)
        r = rec.unreject_match(mid)
        self.assertTrue(r.get("ok"))
        self.assertEqual(db.match_counts()["rejected"], 0)
        # 该对重新成对出现
        again = any(inv_hash in mm["invoices"] and txn_ref in [tuple(t) for t in mm["txns"]]
                    for mm in db.list_matches(status="proposed"))
        self.assertTrue(again)


if __name__ == "__main__":
    unittest.main()
