"""发布窗口轮询脚本：新有效日去重、失败告警去重。全程 mock，不联网。"""
import importlib.util
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from core import config, fx


def _load_script_module():
    path = Path(__file__).resolve().parents[1] / "update-fx-rates.py"
    spec = importlib.util.spec_from_file_location("fx_update_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Provider(fx.RateProvider):
    name = "mock"

    def __init__(self):
        self.effective_date = "2026-08-18"
        self.calls = []

    def fetch(self, functional, date="latest"):
        self.calls.append((functional, date))
        return self.effective_date, {"EUR": Decimal("1.20")}


class FxUpdateScriptTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.old_rates = config.FX_RATES_PATH
        self.old_provider = fx.get_provider()
        config.FX_RATES_PATH = self.temp_dir / "rates.json"
        config.FX_RATES_PATH.write_text("{}", encoding="utf-8")
        self.provider = _Provider()
        fx.set_provider(self.provider)
        self.module = _load_script_module()
        self.module._STATE_FILE = self.temp_dir / "state.json"
        self.module._ALERT_FILE = self.temp_dir / "missing-alert-targets.txt"
        self.notify = Mock(return_value=True)
        self.module._notify = self.notify

    def tearDown(self):
        config.FX_RATES_PATH = self.old_rates
        fx.set_provider(self.old_provider)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_poll_latest_notifies_once_per_effective_date(self):
        self.assertEqual(self.module.run(), 0)
        self.assertEqual(self.module.run(), 0)
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.provider.calls, [("USD", "latest"), ("USD", "latest")])

        self.provider.effective_date = "2026-08-19"
        self.assertEqual(self.module.run(), 0)
        self.assertEqual(self.notify.call_count, 2)
        state = json.loads(self.module._STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(state["last_notified_effective_date"], "2026-08-19")

    def test_failure_alert_is_once_per_beijing_date(self):
        self.provider.fetch = Mock(side_effect=RuntimeError("offline"))
        self.assertEqual(self.module.run(), 1)
        self.assertEqual(self.module.run(), 1)
        self.assertEqual(self.notify.call_count, 1)


if __name__ == "__main__":
    unittest.main()
