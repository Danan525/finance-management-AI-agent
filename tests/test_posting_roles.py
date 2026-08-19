"""过账科目角色可配置：默认不变 + JSON 覆盖 + CONTROL_CODES 从角色推导 + 无效/坏文件回退。

隔离临时 POSTING_ROLES_PATH，tearDown 必 reload 回默认（模块级全局，勿污染其它测试）。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core import config
from ledger import accounts as A


class PostingRolesTest(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._path = config.POSTING_ROLES_PATH
        config.POSTING_ROLES_PATH = self._dir / "posting_accounts.json"
        A.reload_roles()

    def tearDown(self):
        config.POSTING_ROLES_PATH = self._path
        A.reload_roles()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _write(self, d):
        config.POSTING_ROLES_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        A.reload_roles()

    def test_default_when_no_file(self):
        self.assertEqual(A.AP, "2100 应付账款 Accounts Payable")
        self.assertEqual(A.CONTROL_CODES, ("2100", "1100"))
        self.assertEqual(A.DIFF_REASONS["fx_gain_loss"], "6607 汇兑损益 FX Gain/Loss")

    def test_override_role_updates_diff_reasons(self):
        self._write({"FEE": "6604 现金折扣 Cash Discount"})
        self.assertEqual(A.FEE, "6604 现金折扣 Cash Discount")
        self.assertEqual(A.DIFF_REASONS["fee"], "6604 现金折扣 Cash Discount")

    def test_control_codes_derived_from_roles(self):
        # 覆盖 AR 角色到不同编码 → CONTROL_CODES 自动跟随（不再手工保持一致）
        self._write({"AR": "1150 应收票据 Notes Receivable"})
        self.assertEqual(A.AR, "1150 应收票据 Notes Receivable")
        self.assertEqual(A.CONTROL_CODES, ("2100", "1150"))
        self.assertEqual(A.control_side("1150 应收票据 Notes Receivable"), "AR")
        self.assertTrue(A.is_control("1150 x"))

    def test_invalid_override_without_code_ignored(self):
        self._write({"AP": "应付账款没有编码"})        # 无编码 → 忽略、保持默认
        self.assertEqual(A.AP, "2100 应付账款 Accounts Payable")

    def test_unknown_role_key_ignored(self):
        self._write({"NOT_A_ROLE": "9999 x"})
        self.assertEqual(A.AP, "2100 应付账款 Accounts Payable")   # 未知键不影响

    def test_corrupt_file_falls_back_to_default(self):
        config.POSTING_ROLES_PATH.write_text("{ not valid json", encoding="utf-8")
        A.reload_roles()
        self.assertEqual(A.AP, "2100 应付账款 Accounts Payable")
        self.assertEqual(A.CONTROL_CODES, ("2100", "1100"))


if __name__ == "__main__":
    unittest.main()
