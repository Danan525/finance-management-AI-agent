"""sync-demo.py 子路径补丁的回归测试：确保补丁锚点仍能命中主目录当前源码、幂等、
缺锚点会报错中止。

价值：主目录一旦重构了被打补丁的那几行（gateway/main.py、web/*.html），本测试立刻
失败，提醒同步更新 PATCHES —— 避免"演示副本悄悄坏掉"。
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_sync_demo():
    spec = importlib.util.spec_from_file_location("sync_demo", ROOT / "sync-demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)      # __name__ != "__main__" → 不会执行 main()
    return mod


class SyncDemoPatchTest(unittest.TestCase):
    def setUp(self):
        self.sd = _load_sync_demo()

    def test_all_patched_files_exist(self):
        for rel in self.sd.PATCHES:
            self.assertTrue((ROOT / rel).is_file(), f"补丁目标文件不存在：{rel}")

    def test_anchors_hit_current_source_and_patch_is_idempotent(self):
        """对主目录当前源码套补丁：每条锚点都命中（否则 old 缺失会抛 SystemExit），
        且套两次结果不变（幂等）。"""
        for rel in self.sd.PATCHES:
            src = (ROOT / rel).read_text(encoding="utf-8")
            once = self.sd.patch_text(rel, src)      # 命中锚点才不会抛错
            self.assertNotEqual(once, src, f"{rel}: 补丁未产生改动（锚点可能已失效）")
            twice = self.sd.patch_text(rel, once)
            self.assertEqual(once, twice, f"{rel}: 补丁不幂等")

    def test_missing_anchor_raises(self):
        """锚点缺失 → 报错中止（而非静默产出坏站）。"""
        with self.assertRaises(SystemExit):
            self.sd.patch_text("gateway/main.py", "def unrelated():\n    return 1\n")

    def test_base_path_wiring_present(self):
        """补丁后的 main.py 应带子路径注入（读 APP_BASE_PATH + fetch 前缀包装）。"""
        src = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
        patched = self.sd.patch_text("gateway/main.py", src)
        self.assertIn("APP_BASE_PATH", patched)
        self.assertIn("window.fetch", patched)
        self.assertIn("def _html(", patched)


if __name__ == "__main__":
    unittest.main()
