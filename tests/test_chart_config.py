"""科目表可配置（规则即数据）：默认加载 + JSON 覆盖(加/改科目) + 无效条目跳过 + reload。

隔离临时 CHART_PATH,不改真实配置。"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core import config
from ledger import accounts as A


class ChartConfigTest(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._path = config.CHART_PATH
        config.CHART_PATH = self._dir / "chart_of_accounts.json"
        A.reload_chart()

    def tearDown(self):
        config.CHART_PATH = self._path
        A.reload_chart()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_default_when_no_file(self):
        # 无覆盖文件 → 纯内置默认
        self.assertEqual(A.account_type("1002 银行存款 Bank"), "asset")
        self.assertEqual(A.report_line("4100 利息收入 Interest Income"), "IncomeStatement:OtherIncome")

    def _write(self, accounts):
        config.CHART_PATH.write_text(json.dumps({"accounts": accounts}, ensure_ascii=False),
                                     encoding="utf-8")
        A.reload_chart()

    def test_json_adds_new_account(self):
        self._write([{"code": "4300", "name": "咨询收入 Consulting", "type": "revenue",
                      "side": "credit", "report_line": "IncomeStatement:Revenue"}])
        self.assertEqual(A.account_type("4300 咨询收入 Consulting"), "revenue")
        self.assertEqual(A.report_line("4300 X"), "IncomeStatement:Revenue")
        self.assertIn("4300", [c for c, *_ in A.chart()])

    def test_json_overrides_existing_report_line(self):
        # 把 4100 利息收入 从 OtherIncome 改到 Revenue
        self._write([{"code": "4100", "name": "利息收入 Interest Income", "type": "revenue",
                      "side": "credit", "report_line": "IncomeStatement:Revenue"}])
        self.assertEqual(A.report_line("4100 利息收入 Interest Income"), "IncomeStatement:Revenue")

    def test_invalid_entries_skipped(self):
        self._write([
            {"code": "9001", "name": "坏类别", "type": "weird", "side": "debit",
             "report_line": "IncomeStatement:Revenue"},                    # type 非法 → 跳过
            {"code": "9002", "name": "坏损益行", "type": "revenue", "side": "credit",
             "report_line": "IncomeStatement:Nonsense"},                   # 未知损益行 → 跳过
            {"code": "4400", "name": "有效新科目", "type": "revenue", "side": "credit",
             "report_line": "IncomeStatement:Revenue"},                    # 有效
        ])
        codes = [c for c, *_ in A.chart()]
        self.assertNotIn("9001", codes)
        self.assertNotIn("9002", codes)
        self.assertIn("4400", codes)

    def test_corrupt_file_falls_back_to_default(self):
        config.CHART_PATH.write_text("{ not valid json", encoding="utf-8")
        A.reload_chart()
        self.assertEqual(A.account_type("1002 银行存款 Bank"), "asset")   # 回退默认、不崩


if __name__ == "__main__":
    unittest.main()
