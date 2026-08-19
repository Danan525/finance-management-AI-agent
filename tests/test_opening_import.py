"""期初余额批量导入：解析(分类/方向/校验) + CSV 读取 + 提交过账（隔离临时库）。"""
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db
from ledger import opening_import as oi
from ledger import service

D = Decimal


class ParseRowsTest(unittest.TestCase):
    def test_classify_control_vs_other(self):
        rows = [
            {"科目": "1002 银行存款 Bank", "借方": "1000", "贷方": ""},
            {"科目": "2100 应付账款 Accounts Payable", "对手方": "Globex", "贷方": "500"},
        ]
        r = oi.parse_opening_rows(rows)
        self.assertEqual(r["errors"], [])
        self.assertEqual(len(r["other_lines"]), 1)
        self.assertEqual(r["other_lines"][0]["side"], "debit")
        self.assertEqual(len(r["items"]), 1)
        self.assertEqual(r["items"][0]["counterparty"], "Globex")

    def test_side_column_and_amount(self):
        rows = [{"account": "3100 实收资本 Share Capital", "amount": "100000", "side": "credit"}]
        r = oi.parse_opening_rows(rows)
        self.assertEqual(r["other_lines"][0]["side"], "credit")
        self.assertEqual(r["other_lines"][0]["amount"], "100000")

    def test_control_missing_counterparty_errors(self):
        rows = [{"科目": "1100 应收账款 Accounts Receivable", "借方": "500"}]
        r = oi.parse_opening_rows(rows)
        self.assertTrue(r["errors"])
        self.assertFalse(r["items"])

    def test_other_missing_side_errors(self):
        rows = [{"科目": "1500 固定资产 PP&E", "金额": "5000"}]     # 无借贷方向
        r = oi.parse_opening_rows(rows)
        self.assertTrue(r["errors"])

    def test_both_debit_and_credit_errors(self):
        rows = [{"科目": "1002 银行存款 Bank", "借方": "100", "贷方": "50"}]
        r = oi.parse_opening_rows(rows)
        self.assertTrue(r["errors"])

    def test_negative_amount_errors(self):
        rows = [{"科目": "1002 银行存款 Bank", "借方": "-100"}]
        r = oi.parse_opening_rows(rows)
        self.assertTrue(r["errors"])

    def test_unclassifiable_account_errors(self):
        rows = [{"科目": "9999 火星科目", "借方": "100"}]
        r = oi.parse_opening_rows(rows)
        self.assertTrue(r["errors"])

    def test_blank_rows_skipped(self):
        rows = [{"科目": "", "借方": ""}, {"科目": "1002 银行存款 Bank", "借方": "100"}]
        r = oi.parse_opening_rows(rows)
        self.assertEqual(r["errors"], [])
        self.assertEqual(len(r["other_lines"]), 1)

    def test_thousands_separator_and_name_only(self):
        rows = [{"科目": "银行存款 Bank", "借方": "1,200.50"}]      # 只写全名 + 千分位
        r = oi.parse_opening_rows(rows)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["other_lines"][0]["account"].split()[0], "1002")
        self.assertEqual(r["other_lines"][0]["amount"], "1200.50")

    def test_partial_token_name_no_longer_mismatches(self):
        # M2: 只写 "Tax" 不得静默配到某含该 token 的科目(旧: 1180 进项税)，应无法归类报错
        r = oi.parse_opening_rows([{"科目": "Tax", "借方": "100"}])
        self.assertTrue(r["errors"])
        self.assertFalse(r["other_lines"])


class CommitTest(unittest.TestCase):
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

    def _csv(self):
        return (Path(config.BASE_DIR) / "config" / "opening_balances.example.csv").read_bytes()

    def test_example_csv_commits_and_balances(self):
        res = service.commit_opening_import(self._csv(), "opening.csv", by="tester")
        self.assertEqual(res["imported"], {"items": 2, "other_lines": 5})
        # 建账后试算平衡
        led = service.load_ledger()
        dr, cr, _ = led.trial_balance()
        self.assertEqual(dr, cr)
        # 往来进「待结算」明细
        self.assertTrue(res["entry_nos"])

    def test_commit_rejected_when_errors(self):
        bad = b"\xef\xbb\xbf" + "科目,借方\n9999 火星,100\n".encode("utf-8")
        with self.assertRaises(ValueError):
            service.commit_opening_import(bad, "bad.csv", by="tester")


if __name__ == "__main__":
    unittest.main()
