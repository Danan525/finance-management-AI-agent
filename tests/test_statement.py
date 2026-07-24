"""银行流水（对账单）识别：账户头 + 逐笔交易表 + 类型隔离 + 交易人工编辑。"""
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import fitz

from core import config, db
from extraction import pipeline
from extraction.parse import statement as stmt
from review import service as review


def _make_statement_pdf(path):
    doc = fitz.open(); pg = doc.new_page(width=595, height=842)
    pg.insert_text((40, 50), "Account No.: 123-456789-001")
    pg.insert_text((40, 70), "Statement Period: 01-Mar-2026 to 31-Mar-2026")
    pg.insert_text((40, 90), "Opening Balance: 10,000.00")
    pg.insert_text((40, 130), "Date"); pg.insert_text((160, 130), "Description")
    pg.insert_text((330, 130), "Debit"); pg.insert_text((410, 130), "Credit"); pg.insert_text((500, 130), "Balance")
    data = [("02-Mar-2026", "Client payment", "", "5,000.00", "15,000.00"),
            ("10-Mar-2026", "Office rent", "3,000.00", "", "12,000.00")]
    y = 150
    for dte, desc, dr, cr, bal in data:
        pg.insert_text((40, y), dte); pg.insert_text((160, y), desc)
        if dr: pg.insert_text((330, y), dr)
        if cr: pg.insert_text((410, y), cr)
        pg.insert_text((500, y), bal); y += 18
    pg.insert_text((40, y + 10), "Closing Balance: 12,000.00")
    doc.save(str(path)); doc.close()


class StatementTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._db = config.DB_PATH
        self._up = config.UPLOAD_DIR
        config.DB_PATH = Path(self._dir) / "t.db"
        config.UPLOAD_DIR = Path(self._dir) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False; db.init_db()

    def tearDown(self):
        config.DB_PATH = self._db; config.UPLOAD_DIR = self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_is_statement(self):
        self.assertTrue(stmt.is_statement("Bank Statement\nOpening Balance 100"))
        self.assertTrue(stmt.is_statement("对账单 期初余额"))
        self.assertFalse(stmt.is_statement("Invoice No INV-1 Total Due 100"))

    def test_parse_statement_header_and_txns(self):
        p = Path(self._dir) / "s.pdf"; _make_statement_pdf(p)
        inv = pipeline.process_path(p, doc_type="statement")[0]
        self.assertEqual(inv.doc_type, "statement")
        self.assertEqual(inv.f("bank_account_no").value, "123-456789-001")
        self.assertEqual(inv.f("opening_balance").value, Decimal("10000.00"))
        self.assertEqual(inv.f("statement_period_start").value, "2026-03-01")
        self.assertEqual(len(inv.transactions), 2)
        t0, t1 = inv.transactions
        self.assertEqual(t0.income, Decimal("5000.00")); self.assertIsNone(t0.expense)
        self.assertEqual(t1.expense, Decimal("3000.00")); self.assertIsNone(t1.income)
        self.assertEqual(t1.balance, Decimal("12000.00"))

    def test_layout_signed_single_amount_column(self):
        """PDF 单金额列且金额**带符号**（负=支出）→ 按符号归收/支、存正数幅度（不留负号）。"""
        p = Path(self._dir) / "sa.pdf"
        doc = fitz.open(); pg = doc.new_page(width=595, height=842)
        pg.insert_text((40, 50), "Account No.: 55-0001")
        pg.insert_text((40, 90), "Opening Balance: 10,000.00")
        pg.insert_text((40, 130), "Date"); pg.insert_text((160, 130), "Transaction")
        pg.insert_text((400, 130), "Amount"); pg.insert_text((510, 130), "Balance")
        data = [("02-Mar-2026", "Salary", "5000.00", "15,000.00"),
                ("10-Mar-2026", "Rent", "-3000.00", "12,000.00")]
        y = 150
        for dte, desc, amt, bal in data:
            pg.insert_text((40, y), dte); pg.insert_text((160, y), desc)
            pg.insert_text((400, y), amt); pg.insert_text((510, y), bal); y += 18
        doc.save(str(p)); doc.close()
        inv = pipeline.process_path(p, doc_type="statement")[0]
        self.assertEqual(len(inv.transactions), 2)
        t0, t1 = inv.transactions
        self.assertEqual(t0.income, Decimal("5000.00")); self.assertIsNone(t0.expense)
        self.assertEqual(t1.expense, Decimal("3000.00"))     # 正数幅度，不是 -3000
        self.assertIsNone(t1.income)

    def test_headerless_statement(self):
        """无表头流水（每行=日期+摘要+带符号发生额+余额，无列名行）→ 兜底按位置解析。"""
        p = Path(self._dir) / "nh.pdf"
        doc = fitz.open(); pg = doc.new_page(width=595, height=842)
        pg.insert_text((40, 50), "First National Bank")
        pg.insert_text((40, 70), "Account: 8842-119")
        rows = ["2026-01-05   Salary deposit      +5,000.00     5,000.00",
                "2026-01-08   Grocery store       -320.50       4,679.50",
                "2026-01-15   Rent payment        -1,500.00     3,179.50"]
        y = 110
        for r in rows:
            pg.insert_text((40, y), r); y += 18
        doc.save(str(p)); doc.close()
        inv = pipeline.process_path(p, doc_type="statement")[0]
        self.assertEqual(len(inv.transactions), 3)
        t0, t1, t2 = inv.transactions
        self.assertEqual(t0.income, Decimal("5000.00")); self.assertEqual(t0.balance, Decimal("5000.00"))
        self.assertEqual(t0.description, "Salary deposit")     # 描述不含金额
        self.assertEqual(t1.expense, Decimal("320.50")); self.assertIsNone(t1.income)
        self.assertEqual(t2.expense, Decimal("1500.00")); self.assertEqual(t2.balance, Decimal("3179.50"))

    def test_layout_amount_recovered_from_balance_delta(self):
        """借贷列都缺（如 OCR/水印丢了发生额）但余额在 → 按与上一笔余额之差推发生额 + 留 note。"""
        p = Path(self._dir) / "bd.pdf"
        doc = fitz.open(); pg = doc.new_page(width=595, height=842)
        pg.insert_text((40, 50), "Account No.: 55-0002")
        pg.insert_text((40, 90), "Opening Balance: 10,000.00")
        pg.insert_text((40, 130), "Date"); pg.insert_text((160, 130), "Description")
        pg.insert_text((360, 130), "Debit"); pg.insert_text((450, 130), "Credit"); pg.insert_text((530, 130), "Balance")
        # 第二行故意只有日期+余额（无借贷）——模拟发生额被丢
        rows = [("02-Mar-2026", "Salary", "", "5000.00", "15,000.00"),
                ("10-Mar-2026", "Rent", "", "", "13,000.00")]
        y = 150
        for dte, desc, deb, cred, bal in rows:
            pg.insert_text((40, y), dte); pg.insert_text((160, y), desc)
            if deb: pg.insert_text((360, y), deb)
            if cred: pg.insert_text((450, y), cred)
            pg.insert_text((530, y), bal); y += 18
        doc.save(str(p)); doc.close()
        inv = pipeline.process_path(p, doc_type="statement")[0]
        self.assertEqual(len(inv.transactions), 2)
        t1 = inv.transactions[1]
        self.assertEqual(t1.expense, Decimal("2000.00"))     # 15000→13000，推出支出 2000
        self.assertIsNone(t1.income)
        self.assertTrue(t1.note and "余额变化" in t1.note)    # 留复核痕迹

    def test_statement_format_uploaded_as_invoice_hints_type(self):
        """把流水格式(.csv)误当【发票】上传 → 落 failed 记录并提示改选「银行流水」（非笼统'不支持'）。"""
        p = Path(self._dir) / "s.csv"
        p.write_text("Date,Description,Debit,Credit,Balance\n2026-03-01,X,,100,100\n", encoding="utf-8")
        inv = pipeline.process_upload(p.read_bytes(), "s.csv", "invoice")[0]
        self.assertEqual(inv.parse_status, "failed")
        msg = " ".join(i.message for i in inv.issues if i.code == "PARSE_FAILED")
        self.assertIn("银行流水", msg)

    def test_type_isolation_in_queue(self):
        p = Path(self._dir) / "s.pdf"; _make_statement_pdf(p)
        pipeline.process_upload(p.read_bytes(), "s.pdf", "statement")
        ip = Path(self._dir) / "i.pdf"
        d = fitz.open(); d.new_page().insert_text((40, 50), "Invoice No INV-1 Total Due 100.00"); d.save(str(ip)); d.close()
        pipeline.process_upload(ip.read_bytes(), "i.pdf", "invoice")
        stmts = review.review_queue(doc_type="statement")
        invs = review.review_queue(doc_type="invoice")
        self.assertEqual([x["doc_type"] for x in stmts], ["statement"])
        self.assertEqual([x["doc_type"] for x in invs], ["invoice"])

    def test_recent_statement_transactions(self):
        """上传页「识别进度」卡片数据：最近流水交易，最新上传的流水在前、每张内末笔在前。"""
        def _csv(name, rows):
            p = Path(self._dir) / name
            p.write_text("Date,Description,Debit,Credit,Balance,Currency\n" +
                         "".join(rows), encoding="utf-8")
            return pipeline.process_upload(p.read_bytes(), name, "statement")[0]
        _csv("s1.csv", ["2026-03-01,Salary,,5000.00,15000.00,USD\n",
                        "2026-03-05,Rent,1200.00,,13800.00,USD\n"])
        _csv("s2.csv", ["2026-04-01,Refund,,150.00,4850.00,GBP\n",
                        "2026-04-03,Fee,20.00,,4830.00,GBP\n"])
        rows = review.recent_statement_transactions(limit=10)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["file_name"], "s2.csv")               # 最新上传的流水在前
        self.assertEqual(rows[0]["description"], "Fee")                # 张内末笔在前
        self.assertEqual(rows[0]["expense"], "20.00")
        self.assertEqual(rows[0]["currency"], "GBP")
        self.assertTrue(all(r["file_hash"] for r in rows))             # 每笔带 file_hash 供深链
        # limit 生效
        self.assertEqual(len(review.recent_statement_transactions(limit=3)), 3)

    def test_save_transaction_edit_add_del(self):
        p = Path(self._dir) / "s.pdf"; _make_statement_pdf(p)
        inv = pipeline.process_upload(p.read_bytes(), "s.pdf", "statement")[0]
        h = inv.file_hash
        review.save_transaction(h, 0, "description", "Client payment (edited)")
        self.assertEqual(db.get_invoice(h).transactions[0].description, "Client payment (edited)")
        review.save_transaction(h, -1, "__add__", "")
        self.assertEqual(len(db.get_invoice(h).transactions), 3)
        review.save_transaction(h, 2, "__del__", "")
        self.assertEqual(len(db.get_invoice(h).transactions), 2)


class StructuredStatementTest(unittest.TestCase):
    """银行流水结构化格式（CSV/MT940/OFX/CAMT053/QIF/HTML/定宽）确定性解析。"""

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _p(self, name, text):
        p = self._dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_csv(self):
        from extraction.parse import statement_structured as s
        p = self._p("a.csv",
            "transaction_date,counterparty_name,debit_amount,credit_amount,closing_balance,currency,statement_account_id\n"
            "2025-02-03,Greyvane,217000.00,0,173000.00,GBP,ACC-1\n"
            "2025-03-01,Client Co,0,5000.00,178000.00,GBP,ACC-1\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].expense, Decimal("217000.00")); self.assertIsNone(txns[0].income)
        self.assertEqual(txns[1].income, Decimal("5000.00")); self.assertIsNone(txns[1].expense)
        self.assertEqual(hdr["bank_account_no"], "ACC-1")
        self.assertEqual(hdr["currency_settlement"], "GBP")
        self.assertEqual(txns[0].date, "2025-02-03")

    def test_excel_serial_date_recovered(self):
        """CSV 里日期列是 Excel 序列号（5 位整数）→ 转换为日期，而非当分隔行丢弃。"""
        from extraction.parse import statement_structured as s
        p = self._p("serial.csv",
            "Date,Description,Debit,Credit,Balance\n"
            "46081,Salary,,4000.00,4000.00\n"
            "46085,Rent,1500.00,,2500.00\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)
        self.assertTrue(txns[0].date and txns[0].date.startswith("2026"))   # 46081 → 2026 年
        self.assertEqual(txns[0].income, Decimal("4000.00"))

    def test_section_divider_rows_skipped(self):
        """分节/月份分隔行（日期列是非日期文本，如 '=== March 2026 ==='）不应变成幽灵交易。"""
        from extraction.parse import statement_structured as s
        p = self._p("sec.csv",
            "Date,Description,Debit,Credit,Balance\n"
            "=== March 2026 ===,,,,\n"
            "2026-03-01,Salary,,4000.00,4000.00\n"
            "Subtotal,,,,\n"
            "2026-04-01,Rent,1500.00,,2500.00\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)                    # 只 2 笔真交易，分隔/文本行被跳过
        self.assertEqual([t.date for t in txns], ["2026-03-01", "2026-04-01"])

    def test_multi_account_flagged(self):
        """单文件含多个账户 → _rows_to_txns 传 _multi_account sentinel（pipeline 据此加警告）。"""
        from extraction.parse import statement_structured as s
        recs = [{"Date": "2026-05-01", "Account": "ACC-1", "Debit": "", "Credit": "100", "Balance": "100"},
                {"Date": "2026-05-02", "Account": "ACC-2", "Debit": "50", "Credit": "", "Balance": "50"}]
        hdr, txns = s._rows_to_txns(recs)
        self.assertEqual(hdr.get("_multi_account"), 2)
        self.assertEqual(len(txns), 2)
        # 单账户不误报
        recs1 = [{"Date": "2026-05-01", "Account": "ACC-1", "Credit": "100", "Balance": "100"}]
        hdr1, _ = s._rows_to_txns(recs1)
        self.assertNotIn("_multi_account", hdr1)

    def test_row_count_capped(self):
        """行数上限：超 MAX_STATEMENT_ROWS 只解析前 N（防 50 万行拖垮），不崩。"""
        from extraction.parse import statement_structured as s
        from core import config
        saved = config.MAX_STATEMENT_ROWS
        config.MAX_STATEMENT_ROWS = 50
        try:
            rows = [["Date", "Debit", "Credit", "Balance"]] + \
                   [["2025-01-%02d" % ((i % 27) + 1), "10.00", "", "%d.00" % (1000 - i)] for i in range(500)]
            recs = s._records_from_matrix(rows)
            self.assertLessEqual(len(recs), 50)                 # 截断到上限内（表头+数据行合计 ≤ 50）
        finally:
            config.MAX_STATEMENT_ROWS = saved

    def test_csv_column_name_synonyms(self):
        """多银行列名同义词：Paid Out/Paid In、Payments/Receipts、Bal、Withdrawal/Deposit 等。"""
        from extraction.parse import statement_structured as s
        p = self._p("b.csv",
            "Posting Date,Narrative,Paid Out,Paid In,Bal,Currency\n"
            "2026-03-02,Rent,1200.00,,8800.00,GBP\n"
            "2026-03-05,Salary,,5000.00,13800.00,GBP\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].expense, Decimal("1200.00"))
        self.assertEqual(txns[1].income, Decimal("5000.00"))
        self.assertEqual(hdr["closing_balance"], Decimal("13800.00"))    # "Bal" 也识别为余额
        # Payments/Receipts 方案
        p2 = self._p("c.csv",
            "Value Date,Particulars,Payments,Receipts,Closing Balance\n"
            "2026-04-01,Utility,300.00,,4700.00\n"
            "2026-04-03,Refund,,150.00,4850.00\n")
        _h2, t2 = s.parse_structured(p2)
        self.assertEqual(t2[0].expense, Decimal("300.00"))
        self.assertEqual(t2[1].income, Decimal("150.00"))

    def test_csv_chinese_and_receipt_columns(self):
        """中文列名 + 收款专用列名（收款金额/付款方/Amount Received/Charges）。"""
        from extraction.parse import statement_structured as s
        # 中文收款流水
        p = self._p("cn.csv",
            "收款日期,付款方,收款金额,付款金额,余额,币种\n"
            "2026-05-02,华东贸易,8000.00,,58000.00,CNY\n"
            "2026-05-06,手续费,,20.00,57980.00,CNY\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].income, Decimal("8000.00"))
        self.assertEqual(txns[1].expense, Decimal("20.00"))
        self.assertEqual(hdr["closing_balance"], Decimal("57980.00"))
        self.assertEqual(hdr["currency_settlement"], "CNY")
        # 英文收款专用列名
        p2 = self._p("recv.csv",
            "Receipt Date,Received From,Amount Received,Charges,Balance\n"
            "2026-05-01,Acme Retail,5000.00,,15000.00\n"
            "2026-05-03,Blue Ocean,2500.00,10.00,17490.00\n")
        _h2, t2 = s.parse_structured(p2)
        self.assertEqual(t2[0].income, Decimal("5000.00"))
        self.assertEqual(t2[1].income, Decimal("2500.00"))
        self.assertEqual(t2[1].expense, Decimal("10.00"))

    def test_csv_robustness_encoding_delimiter_amountforms(self):
        """鲁棒性：GBK 编码 / 分号分隔 / 金额 Cr-Dr 后缀 / 括号负数——随手上传的流水都不该出错。"""
        from extraction.parse import statement_structured as s
        # GBK 编码中文 CSV
        pg = self._dir / "gbk.csv"
        pg.write_bytes(("交易日期,摘要,支出,收入,余额\n"
                        "2026-05-01,工资,,5000.00,15000.00\n"
                        "2026-05-03,消费,1200.00,,13800.00\n").encode("gbk"))
        hdr, txns = s.parse_structured(pg)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].income, Decimal("5000.00")); self.assertEqual(txns[1].expense, Decimal("1200.00"))
        # 分号分隔 + 逗号小数（欧洲）
        p2 = self._p("semi.csv",
            "Date;Description;Debit;Credit;Balance\n"
            "2026-05-01;Salary;;5.000,00;15.000,00\n"
            "2026-05-03;Rent;1.200,00;;13.800,00\n")
        _h, t2 = s.parse_structured(p2)
        self.assertEqual(len(t2), 2)
        self.assertEqual(t2[0].income, Decimal("5000.00")); self.assertEqual(t2[1].expense, Decimal("1200.00"))
        # 金额 Cr/Dr 后缀（单金额列）
        p3 = self._p("crdr.csv",
            "Date,Narration,Amount,Balance\n"
            "2026-05-01,Salary,5000.00 Cr,15000.00\n"
            "2026-05-03,Rent,1200.00 Dr,13800.00\n")
        _h, t3 = s.parse_structured(p3)
        self.assertEqual(t3[0].income, Decimal("5000.00")); self.assertEqual(t3[1].expense, Decimal("1200.00"))
        # 括号负数（单签名列，支出=括号）
        p4 = self._p("paren.csv",
            "Date,Description,Amount,Balance\n"
            "2026-05-01,Salary,5000.00,15000.00\n"
            "2026-05-03,Rent,(1200.00),13800.00\n")
        _h, t4 = s.parse_structured(p4)
        self.assertEqual(t4[0].income, Decimal("5000.00")); self.assertEqual(t4[1].expense, Decimal("1200.00"))

    def test_html_and_multisheet_xlsx_with_preamble(self):
        """HTML 账单 + 多 sheet xlsx（交易表在非首 sheet、含 preamble 行）——都能找到表头。"""
        from extraction.parse import statement_structured as s
        ph = self._p("stmt.html",
            "<html><body><h2>Statement</h2><p>Account 123</p>"
            "<table><tr><th>Date</th><th>Description</th><th>Debit</th><th>Credit</th><th>Balance</th></tr>"
            "<tr><td>2026-05-01</td><td>Salary</td><td></td><td>5000.00</td><td>15000.00</td></tr>"
            "<tr><td>2026-05-03</td><td>Rent</td><td>1200.00</td><td></td><td>13800.00</td></tr>"
            "</table></body></html>")
        _h, th = s.parse_structured(ph)
        self.assertEqual(len(th), 2)
        self.assertEqual(th[0].income, Decimal("5000.00")); self.assertEqual(th[1].expense, Decimal("1200.00"))
        # 多 sheet xlsx：Summary 在前、Transactions（带 2 行 preamble）在后
        import openpyxl
        wb = openpyxl.Workbook(); s0 = wb.active; s0.title = "Summary"
        s0.append(["Account Statement"]); s0.append(["Account", "123"])
        s1 = wb.create_sheet("Transactions")
        s1.append(["交易明细"]); s1.append(["导出时间 2026-04-01"])
        s1.append(["Date", "Description", "Debit", "Credit", "Balance"])
        s1.append(["2026-05-01", "Salary", None, 5000.0, 15000.0])
        s1.append(["2026-05-03", "Rent", 1200.0, None, 13800.0])
        px = self._dir / "ms.xlsx"; wb.save(str(px))
        _h2, tx = s.parse_structured(px)
        self.assertEqual(len(tx), 2)
        self.assertEqual(tx[0].income, Decimal("5000.0")); self.assertEqual(tx[1].expense, Decimal("1200.0"))

    def test_csv_empty_and_headeronly_no_crash(self):
        """空文件 / 仅表头 不该崩，优雅出 0 笔。"""
        from extraction.parse import statement_structured as s
        pe = self._p("empty.csv", "")
        self.assertEqual(s.parse_structured(pe), ({}, []))
        ph = self._p("hdr.csv", "Date,Description,Debit,Credit,Balance\n")
        _h, t = s.parse_structured(ph)
        self.assertEqual(t, [])

    def test_csv_preamble_and_direction_column(self):
        """平台账单（微信/支付宝式）：前导说明行 + "收/支"方向列 + 金额(¥)列——跳过前导找表头、按方向路由。"""
        from extraction.parse import statement_structured as s
        p = self._p("wx.csv",
            "微信支付账单明细\n"
            "微信昵称：[测试]\n"
            "起始时间：[2026-03-01] 终止时间：[2026-03-31]\n"
            "----------------------微信支付交易明细列表------------------\n"
            "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号\n"
            "2026-03-04 12:30:45,商户消费,店家,商品,支出,¥100.00,零钱,支付成功,4200\n"
            "2026-03-05 09:00:00,好友转账,某人,转账,收入,¥500.00,零钱,支付成功,4201\n"
            "2026-03-06 10:00:00,零钱通转入,-,-,不计收支,¥2000.00,零钱通,成功,4202\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 3)                        # 3 笔（前导行不算）
        self.assertEqual(txns[0].expense, Decimal("100.00")); self.assertIsNone(txns[0].income)
        self.assertEqual(txns[1].income, Decimal("500.00")); self.assertIsNone(txns[1].expense)
        self.assertIsNone(txns[2].income); self.assertIsNone(txns[2].expense)   # 不计收支 → 皆空
        self.assertEqual(txns[0].date, "2026-03-04")          # 交易时间(带时刻)→ ISO 日期

    def test_csv_footer_total_row_skipped(self):
        """带"合计/Total"脚注行（无日期）不应被当成一笔交易（否则笔数+1、金额翻倍）。"""
        from extraction.parse import statement_structured as s
        p = self._p("foot.csv",
            "Date,Description,Debit,Credit,Balance\n"
            "2026-05-01,Salary,,5000.00,15000.00\n"
            "2026-05-03,Rent,1200.00,,13800.00\n"
            ",合计 / Total,1200.00,5000.00,13800.00\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)                        # 脚注行被跳过
        self.assertEqual(sum((t.income or 0) for t in txns), Decimal("5000.00"))
        self.assertEqual(sum((t.expense or 0) for t in txns), Decimal("1200.00"))

    def test_csv_balance_only_derives_direction(self):
        """只有 日期/摘要/余额（无借贷列）→ 按余额变化推收/支；首笔无锚留空。"""
        from extraction.parse import statement_structured as s
        p = self._p("bal.csv",
            "Date,Description,Balance\n"
            "2026-05-01,Open,10000.00\n"
            "2026-05-02,In,12000.00\n"
            "2026-05-03,Out,11500.00\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 3)
        self.assertIsNone(txns[0].income); self.assertIsNone(txns[0].expense)   # 首笔无锚
        self.assertEqual(txns[1].income, Decimal("2000.00"))                    # 12000-10000
        self.assertEqual(txns[2].expense, Decimal("500.00"))                    # 11500-12000

    def test_csv_ddmm_dateformat_column_inference(self):
        """UK 日/月/年（DD/MM/YYYY）：整列推断出日在前，歧义行（日≤12）不再被误当 MM/DD。"""
        from extraction.parse import statement_structured as s
        p = self._p("uk.csv",
            "Date,Description,Debit,Credit,Balance\n"
            "04/03/2026,A,,100.00,1100.00\n"      # 4 March（歧义：4 和 3 都 ≤12）
            "14/03/2026,B,50.00,,1050.00\n")      # 14 March（无歧义，锚定日在前）
        hdr, txns = s.parse_structured(p)
        self.assertEqual(txns[0].date, "2026-03-04")   # 不再是 2026-04-03
        self.assertEqual(txns[1].date, "2026-03-14")

    def test_csv_european_amount_format(self):
        """结构化流水金额兼容欧式（1.234,56）——_dec 复用 parse_amount。"""
        from extraction.parse import statement_structured as s
        p = self._p("eu.csv",
            "Date,Description,Debit,Credit,Balance,Currency\n"
            "2026-05-01,Salary,,\"5.000,00\",\"15.000,00\",EUR\n"
            "2026-05-04,Rent,\"1.200,50\",,\"13.799,50\",EUR\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(txns[0].income, Decimal("5000.00"))
        self.assertEqual(txns[1].expense, Decimal("1200.50"))
        self.assertEqual(hdr["closing_balance"], Decimal("13799.50"))

    def test_mt940_comma_decimal(self):
        from extraction.parse import statement_structured as s
        p = self._p("a.mt940",
            ":20:REF\n:25:GB29-ACC-9\n:28C:1/1\n:60F:C250217AUD520000,00\n"
            ":61:2502170217D92000,00NTRFNONREF//TXN-1\n:86:Thornfield Advisory\n"
            ":62F:C250217AUD428000,00\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].expense, Decimal("92000.00"))   # 逗号是小数点，不是千分位
        self.assertEqual(hdr["opening_balance"], Decimal("520000.00"))
        self.assertEqual(hdr["closing_balance"], Decimal("428000.00"))

    def test_ofx_signed_amount(self):
        from extraction.parse import statement_structured as s
        p = self._p("a.ofx",
            "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD</CURDEF>"
            "<BANKACCTFROM><ACCTID>ACC-9</ACCTID></BANKACCTFROM><BANKTRANLIST>"
            "<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20250203000000</DTPOSTED>"
            "<TRNAMT>-217000.00</TRNAMT><NAME>Greyvane</NAME></STMTTRN>"
            "<STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20250301000000</DTPOSTED>"
            "<TRNAMT>5000.00</TRNAMT><NAME>Client</NAME></STMTTRN>"
            "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].expense, Decimal("217000.00"))
        self.assertEqual(txns[1].income, Decimal("5000.00"))
        self.assertEqual(hdr["currency_settlement"], "USD")

    def test_camt053(self):
        from extraction.parse import statement_structured as s
        p = self._p("a.camt053.xml",
            '<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
            '<BkToCstmrStmt><Stmt><Acct><Id><IBAN>GB29ACC9</IBAN></Id></Acct>'
            '<Ntry><Amt Ccy="AUD">92000.00</Amt><CdtDbtInd>DBIT</CdtDbtInd>'
            '<BookgDt><Dt>2025-02-17</Dt></BookgDt><NtryDtls><TxDtls>'
            '<RltdPties><Cdtr><Nm>Thornfield</Nm></Cdtr></RltdPties>'
            '<RmtInf><Ustrd>INV TAL-1</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>'
            '</Stmt></BkToCstmrStmt></Document>')
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].expense, Decimal("92000.00"))
        self.assertEqual(txns[0].date, "2025-02-17")
        self.assertEqual(hdr["bank_account_no"], "GB29ACC9")
        self.assertEqual(hdr["currency_settlement"], "AUD")

    def test_qif(self):
        from extraction.parse import statement_structured as s
        p = self._p("a.qif",
            "!Type:Bank\nD02/03/2025\nT-217000.00\nPGreyvane\nMpay\n^\n"
            "D03/01/2025\nT5000.00\nPClient\n^\n")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].expense, Decimal("217000.00"))
        self.assertEqual(txns[1].income, Decimal("5000.00"))

    def test_html_xls(self):
        from extraction.parse import statement_structured as s
        p = self._p("a.xls",
            "<html><body><table>"
            "<tr><th>transaction_date</th><th>counterparty_name</th><th>debit_amount</th>"
            "<th>credit_amount</th><th>currency</th></tr>"
            "<tr><td>2025-02-03</td><td>Greyvane &amp; Co</td><td>217000.00</td><td>0</td><td>USDT</td></tr>"
            "</table></body></html>")
        self.assertTrue(s.is_structured(p))
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns[0].expense, Decimal("217000.00"))
        self.assertIn("Greyvane & Co", txns[0].description)

    def test_pipeline_routes_structured(self):
        from extraction import pipeline
        p = self._p("bank.csv",
            "transaction_date,counterparty_name,debit_amount,credit_amount,statement_account_id\n"
            "2025-02-03,Greyvane,217000.00,0,ACC-1\n")
        inv = pipeline.process_path(p, doc_type="statement")[0]
        self.assertEqual(inv.doc_type, "statement")
        self.assertEqual(inv.parse_method, "structured")
        self.assertEqual(len(inv.transactions), 1)
        self.assertEqual(inv.transactions[0].expense, Decimal("217000.00"))

    def test_no_invoice_issue_codes(self):
        """流水不得套用发票必填字段校验（invoice_no/日期/总额），也不应无端高风险。"""
        import tempfile as _tf, shutil as _sh
        from pathlib import Path as _P
        from core import config, db
        d = _tf.mkdtemp()
        _db, _up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = _P(d) / "t.db"; config.UPLOAD_DIR = _P(d) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False; db.init_db()
        try:
            from extraction import pipeline
            p = self._p("bank.csv",
                "transaction_date,counterparty_name,debit_amount,credit_amount,statement_account_id,currency\n"
                "2025-02-03,Greyvane,217000.00,0,ACC-1,GBP\n")
            inv = pipeline.process_upload(p.read_bytes(), "bank.csv", "statement")[0]
            codes = {i.code for i in inv.issues}
            self.assertNotIn("FIELD_COVERAGE_LOW", codes)
            self.assertNotIn("KEY_FIELD_LOW", codes)
            self.assertEqual(inv.risk_score, 0)
        finally:
            config.DB_PATH, config.UPLOAD_DIR = _db, _up
            db._initialized = False
            _sh.rmtree(d, ignore_errors=True)

    def test_statement_approvable_and_excluded_from_export(self):
        """流水应能通过审批（不被发票必填字段挡），且不混进发票 Excel 导出。"""
        import tempfile as _tf, shutil as _sh
        from pathlib import Path as _P
        from core import config, db
        from review import service as review
        from extraction import pipeline
        d = _tf.mkdtemp()
        _db, _up, _ex = config.DB_PATH, config.UPLOAD_DIR, config.EXPORT_DIR
        config.DB_PATH = _P(d) / "t.db"; config.UPLOAD_DIR = _P(d) / "up"; config.EXPORT_DIR = _P(d) / "ex"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True); config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False; db.init_db()
        try:
            p = self._p("s.csv",
                "transaction_date,counterparty_name,debit_amount,credit_amount,statement_account_id\n"
                "2025-02-03,Greyvane,217000.00,0,ACC-1\n")
            st = pipeline.process_upload(p.read_bytes(), "s.csv", "statement")[0]
            self.assertEqual(db.get_invoice(st.file_hash).validation_status, "passed")
            r = review.act(st.file_hash, "Approved", by="t")
            self.assertNotIn("blocked", r)                       # 不被必填字段闸门挡
            self.assertEqual(db.get_invoice(st.file_hash).approve_status, "Approved")
            # 导出发票工作底稿：应排除流水
            items = [v for v in db.load_all_invoices().values() if (v.doc_type or "invoice") != "statement"]
            self.assertEqual(items, [])
        finally:
            config.DB_PATH, config.UPLOAD_DIR, config.EXPORT_DIR = _db, _up, _ex
            db._initialized = False
            _sh.rmtree(d, ignore_errors=True)

    def test_transaction_bbox_geometry(self):
        """每笔交易应获得与规范交易表画布对齐的行级 bbox（供点行↔左侧高亮）。"""
        from extraction.extract import textrender
        from core.models import Invoice, Transaction, FieldValue
        inv = Invoice(file_name="s.csv", file_hash="h", doc_type="statement")
        inv.set("bank_account_no", FieldValue(raw="ACC-1", value="ACC-1"))
        inv.transactions = [Transaction(date="2025-02-0%d" % (i + 1), description="X",
                                        expense=Decimal("100")) for i in range(3)]
        L = textrender.statement_layout(inv)
        self.assertEqual(len(L["boxes"]), 3)
        W, H = L["width"], L["height"]
        for b in L["boxes"]:
            self.assertEqual(b[0], 0)
            self.assertTrue(0 <= b[1] < b[3] <= W and 0 <= b[2] < b[4] <= H)
        # 逐行 y 递增、行高一致
        self.assertLess(L["boxes"][0][2], L["boxes"][1][2])
        self.assertEqual(L["boxes"][1][2] - L["boxes"][0][2], L["boxes"][2][2] - L["boxes"][1][2])
        png = textrender.render_statement_png(inv)
        self.assertTrue(png[:8] == b"\x89PNG\r\n\x1a\n")   # 是 PNG

    def test_reapply_all_scoped_by_doc_type(self):
        """『全部按最新规则重新提取』可按范围：只发票 / 只流水 / 全部。"""
        import tempfile as _tf, shutil as _sh
        from pathlib import Path as _P
        from core import config, db
        from review import service as review
        from extraction import pipeline
        import fitz
        d = _tf.mkdtemp()
        _db, _up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = _P(d) / "t.db"; config.UPLOAD_DIR = _P(d) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False; db.init_db()
        try:
            ip = _P(config.UPLOAD_DIR) / "i.pdf"
            doc = fitz.open(); doc.new_page().insert_text((40, 50), "Invoice No INV-9\nInvoice Date 2025-02-01\nTotal Due 100.00")
            doc.save(str(ip)); doc.close()
            pipeline.process_upload(ip.read_bytes(), "i.pdf", "invoice")
            sp = _P(config.UPLOAD_DIR) / "s.csv"
            sp.write_text("transaction_date,counterparty_name,debit_amount,credit_amount,statement_account_id\n"
                          "2025-02-03,Greyvane,217000.00,0,ACC-1\n", encoding="utf-8")
            pipeline.process_upload(sp.read_bytes(), "s.csv", "statement")
            self.assertEqual(review.reapply_learned_all("t", doc_type="invoice")["scanned"], 1)
            self.assertEqual(review.reapply_learned_all("t", doc_type="statement")["scanned"], 1)
            self.assertEqual(review.reapply_learned_all("t", doc_type=None)["scanned"], 2)
        finally:
            config.DB_PATH, config.UPLOAD_DIR = _db, _up
            db._initialized = False
            _sh.rmtree(d, ignore_errors=True)

    def test_reapply_keeps_doc_type(self):
        """重新提取一条流水应仍是 statement（不被当作发票重新分类）。"""
        import tempfile as _tf, shutil as _sh
        from pathlib import Path as _P
        from core import config, db
        from review import service as review
        d = _tf.mkdtemp()
        _db, _up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = _P(d) / "t.db"; config.UPLOAD_DIR = _P(d) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False; db.init_db()
        try:
            from extraction import pipeline
            src = _P(config.UPLOAD_DIR) / "bank.csv"
            src.write_text("transaction_date,counterparty_name,debit_amount,credit_amount,statement_account_id\n"
                           "2025-02-03,Greyvane,217000.00,0,ACC-1\n", encoding="utf-8")
            inv = pipeline.process_upload(src.read_bytes(), "bank.csv", "statement")[0]
            r = review.reapply_learned(inv.file_hash)
            self.assertTrue(r.get("applied"))
            fresh = db.get_invoice(inv.file_hash)
            self.assertEqual(fresh.doc_type, "statement")
            self.assertEqual(len(fresh.transactions), 1)
            self.assertNotIn("FIELD_COVERAGE_LOW", {i.code for i in fresh.issues})
        finally:
            config.DB_PATH, config.UPLOAD_DIR = _db, _up
            db._initialized = False
            _sh.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class CamtStatementTest(unittest.TestCase):
    def test_camt053_bare_ext_parsed(self):
        """`.camt053` 裸扩展（在白名单里）应被路由到 CAMT 解析器，而非落 STMT_EMPTY。"""
        import tempfile, shutil
        from pathlib import Path
        from extraction.parse import statement_structured as s
        d = Path(tempfile.mkdtemp())
        try:
            p = d / "bank.camt053"
            p.write_text(
                '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>'
                '<Acct><Id><IBAN>DE89370400440532013000</IBAN></Id></Acct>'
                '<Ntry><Amt Ccy="EUR">4000.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-03-01</Dt></BookgDt></Ntry>'
                '<Ntry><Amt Ccy="EUR">1500.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-03-05</Dt></BookgDt></Ntry>'
                '</Stmt></BkToCstmrStmt></Document>', encoding="utf-8")
            res = s.parse_structured(p)
            self.assertIsNotNone(res)
            hdr, txns = res
            self.assertEqual(len(txns), 2)
            self.assertEqual(txns[0].income, __import__("decimal").Decimal("4000.00"))
            self.assertEqual(txns[1].expense, __import__("decimal").Decimal("1500.00"))
            self.assertEqual(hdr.get("bank_account_no"), "DE89370400440532013000")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class UnsignedAmountDirectionTest(unittest.TestCase):
    """无符号单金额列 + 余额列（无借贷/无方向）→ 按余额变化推收/支，不再一律记收入。"""
    def setUp(self):
        self._dir = tempfile.mkdtemp(); self._db = config.DB_PATH
        config.DB_PATH = Path(self._dir) / "t.db"; db._initialized = False; db.init_db()

    def tearDown(self):
        config.DB_PATH = self._db; db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_amount_direction_from_balance(self):
        from extraction.parse import statement_structured as s
        p = Path(self._dir) / "fx.csv"
        p.write_text("Date,Description,Currency,FX Rate,Amount,Base Amount,Balance\n"
                     "2026-03-01,Wire in,EUR,1.10,3000.00,3300.00,3300.00\n"
                     "2026-03-05,Payment,USD,1.00,1200.00,1200.00,2100.00\n", encoding="utf-8")
        hdr, txns = s.parse_structured(p)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].income, Decimal("3000.00")); self.assertIsNone(txns[0].expense)
        self.assertEqual(txns[1].expense, Decimal("1200.00"))    # 余额 3300→2100 降 → 支出（不是收入）
        self.assertIsNone(txns[1].income)


class PendingStatusTest(unittest.TestCase):
    """带 Status 列的流水：PENDING/挂起（预授权）行不计入收支（避免与结算行重复计）。"""
    def test_pending_rows_skipped(self):
        from extraction.parse import statement_structured as s
        import tempfile as _t, shutil as _sh
        d = Path(_t.mkdtemp())
        try:
            p = d / "p.csv"
            p.write_text("Date,Description,Status,Debit,Credit,Balance\n"
                         "2026-03-01,Hotel pre-auth,PENDING,500.00,,\n"
                         "2026-03-02,Grocery,POSTED,80.00,,9420.00\n"
                         "2026-03-03,Hotel settle,POSTED,450.00,,8970.00\n", encoding="utf-8")
            hdr, txns = s.parse_structured(p)
            self.assertEqual(len(txns), 2)                       # 预授权行被跳过
            self.assertEqual(sum((t.expense or 0) for t in txns), Decimal("530.00"))  # 80+450，无 500 重复
            self.assertEqual(hdr.get("_pending_skipped"), 1)
        finally:
            _sh.rmtree(d, ignore_errors=True)
