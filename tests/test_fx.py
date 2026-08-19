"""多币种汇率表 + 外币发票按入账日汇率换算入账（隔离临时库与临时汇率文件）。"""
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core import config, db, fx
from core.models import Classification, FieldValue, Invoice
from ledger import accounts as A
from ledger import service

D = Decimal


def _eur_invoice(no, total, fh, ccy="EUR", date="2026-06-10"):
    inv = Invoice()
    inv.file_hash = fh
    inv.doc_type = "invoice"
    inv.approve_status = "Approved"
    inv.set("invoice_no", FieldValue(value=no))
    inv.set("invoice_date", FieldValue(value=date))
    inv.set("total_due", FieldValue(value=total))
    inv.set("currency_settlement", FieldValue(value=ccy))
    inv.set("issuer_name", FieldValue(value="Euro Vendor"))
    inv.set("customer_name", FieldValue(value="My Co"))
    inv.classification = Classification(account="6440 会计费 Accounting Fees")
    db.save_invoice(inv)
    return inv


class FxTableTest(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._p, self._af = config.FX_RATES_PATH, config.FX_AUTO_FETCH
        config.FX_RATES_PATH = self._dir / "fx.json"
        config.FX_AUTO_FETCH = False        # 纯本地、不联网（测试隔离）
        config.FX_RATES_PATH.write_text(json.dumps({
            "EUR": [{"date": "2026-01-01", "rate": "1.08"},
                    {"date": "2026-06-01", "rate": "1.10"}]}), encoding="utf-8")

    def tearDown(self):
        config.FX_RATES_PATH, config.FX_AUTO_FETCH = self._p, self._af
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_functional_is_one(self):
        self.assertEqual(fx.rate("USD", "2026-06-10"), D("1"))

    def test_rate_picks_latest_on_or_before(self):
        self.assertEqual(fx.rate("EUR", "2026-06-10"), D("1.10"))
        self.assertEqual(fx.rate("EUR", "2026-03-01"), D("1.08"))

    def test_rate_none_before_first_and_unknown(self):
        self.assertIsNone(fx.rate("EUR", "2025-12-31"))
        self.assertIsNone(fx.rate("CHF", "2026-06-10"))

    def test_empty_date_fail_closed(self):
        # 空生效日 → None（不退回最新一条），与"无汇率拒绝"一致
        self.assertIsNone(fx.rate("EUR", ""))
        self.assertIsNone(fx.to_functional(D("100"), "EUR", ""))

    def test_to_functional_quantizes(self):
        self.assertEqual(fx.to_functional(D("1000"), "EUR", "2026-06-10"), D("1100.00"))
        self.assertIsNone(fx.to_functional(D("100"), "CHF", "2026-06-10"))

    def test_low_unit_currency_high_precision_conversion(self):
        # IDR/KRW 等低单位币种：汇率内部须高精度存储、换算须用高精度值（非 6 位显示值），
        # 否则大额换算明显误差。锁定此保证、防以后误把 quantize 加到汇率上。
        config.FX_RATES_PATH.write_text(json.dumps({
            "IDR": [{"date": "2026-01-01", "rate": "0.00005622083544161466239388317310"}]}))
        r = fx.rate("IDR", "2026-06-01")
        self.assertGreater(len(str(r).split(".")[-1]), 12)          # 返回值保留 >12 位小数
        conv = fx.to_functional(D("10000000"), "IDR", "2026-06-01")  # 1000万 IDR
        self.assertEqual(conv, D("562.21"))                         # 高精度换算（正确）
        self.assertNotEqual(conv, D("560.00"))                      # ≠ 用 6 位显示值换算的结果

    def test_add_rate_upsert_and_guards(self):
        fx.add_rate("EUR", "2026-06-01", "1.12")           # 覆盖同日
        self.assertEqual(fx.rate("EUR", "2026-06-05"), D("1.12"))
        fx.add_rate("GBP", "2026-01-01", "1.27")           # 新币种
        self.assertEqual(fx.rate("GBP", "2026-02-01"), D("1.27"))
        for bad in (lambda: fx.add_rate("USD", "2026-01-01", "1"),      # 功能货币
                    lambda: fx.add_rate("EUR", "2026-01-01", "-1"),     # 负
                    lambda: fx.add_rate("EUR", "2026-01-01", "x"),      # 非数
                    lambda: fx.add_rate("", "2026-01-01", "1")):        # 缺币种
            with self.assertRaises(ValueError):
                bad()


class FxInvoicePostingTest(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db, self._up, self._fx = config.DB_PATH, config.UPLOAD_DIR, config.FX_RATES_PATH
        self._af = config.FX_AUTO_FETCH
        config.DB_PATH = self._dir / "t.db"; config.UPLOAD_DIR = self._dir / "up"
        config.FX_RATES_PATH = self._dir / "fx.json"
        config.FX_AUTO_FETCH = False        # 纯本地、不联网（测试隔离）
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        # 早日期汇率(≤录入日今天)，供发票入账按【录入系统当日】汇率命中本地
        config.FX_RATES_PATH.write_text(json.dumps({
            "EUR": [{"date": "2026-01-01", "rate": "1.10"}]}), encoding="utf-8")
        db._initialized = False; db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR, config.FX_RATES_PATH = self._db, self._up, self._fx
        config.FX_AUTO_FETCH = self._af
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_foreign_invoice_converted_and_balanced(self):
        _eur_invoice("EU-1", "1000.00", "hf1", ccy="EUR", date="2026-06-10")
        no = service.post_invoice_by_hash("hf1", by="t", direction="AP")
        self.assertTrue(no)
        led = service.load_ledger()
        # 1000 EUR @1.10 → 1100 USD 记应付
        self.assertEqual(led.net(A.AP), D("-1100.00"))
        dr, cr, _ = led.trial_balance()
        self.assertEqual(dr, cr)                              # 换算后仍借贷平
        # 原币+汇率留档在凭证摘要
        e = [x for x in led.entries if x.source_hash == "hf1"][0]
        self.assertIn("EUR", e.memo); self.assertIn("1.10", e.memo)

    def test_foreign_invoice_without_rate_rejected(self):
        # 未知币种（本地无、FX_AUTO_FETCH=False 不联网）→ 无录入日汇率 → 拒
        _eur_invoice("CHF-1", "500.00", "hf3", ccy="CHF", date="2026-06-10")
        with self.assertRaises(ValueError):
            service.post_invoice_by_hash("hf3", by="t", direction="AP")

    def test_foreign_settlement_realizes_fx_gain_loss(self):
        # 入账 EUR 1000 @1.10 → AP 1100 USD；结算日 EUR 升到 1.15，付 1000 EUR = 1150 USD → 汇兑损失 50
        _eur_invoice("EU-S", "1000.00", "hs1", ccy="EUR", date="2026-01-05")
        service.post_invoice_by_hash("hs1", by="t", direction="AP")
        fx.add_rate("EUR", "2026-03-01", "1.15")
        no = service.settle_invoice("hs1", cash_amount="1000", cash_currency="EUR",
                                    settle_amount="1100", date="2026-03-05",
                                    diff_reason="fx_gain_loss", by="t")
        self.assertTrue(no)
        led = service.load_ledger()
        self.assertEqual(led.net(A.AP), D("0"))              # 应付清零
        self.assertEqual(led.net(A.BANK), D("-1150.00"))     # 付 1000 EUR @1.15
        self.assertEqual(led.net(A.EXCHANGE_GL), D("50.00")) # 汇兑损失 50（借方）
        dr, cr, _ = led.trial_balance()
        self.assertEqual(dr, cr)

    def test_foreign_settlement_without_rate_rejected(self):
        _eur_invoice("EU-S2", "1000.00", "hs2", ccy="EUR", date="2026-01-05")
        service.post_invoice_by_hash("hs2", by="t", direction="AP")
        with self.assertRaises(ValueError):                       # CHF 无汇率 → 拒
            service.settle_invoice("hs2", cash_amount="1000", cash_currency="CHF",
                                   settle_amount="1100", date="2026-03-05",
                                   diff_reason="fx_gain_loss", by="t")

    def test_rate_uses_entry_date_not_invoice_date(self):
        # 汇率取【录入系统当日】：发票交易日缺失/很旧都不影响，只要录入日(今天)有汇率即可入账
        _eur_invoice("EU-ND", "1000.00", "hnd", ccy="EUR", date="")   # 无发票日期
        no = service.post_invoice_by_hash("hnd", by="t", direction="AP")
        self.assertTrue(no)
        led = service.load_ledger()
        self.assertEqual(led.net(A.AP), D("-1100.00"))                # 按录入日汇率 1.10 换算
        e = [x for x in led.entries if x.source_hash == "hnd"][0]
        self.assertIn("录入日", e.memo)

    def test_foreign_settlement_requires_explicit_settle_amount(self):
        # MED-2: 外币结算未显式给 settle_amount → 拒（避免部分付款清空整张往来）
        _eur_invoice("EU-RS", "1000.00", "hrs", ccy="EUR", date="2026-01-05")
        service.post_invoice_by_hash("hrs", by="t", direction="AP")
        fx.add_rate("EUR", "2026-03-01", "1.20")
        with self.assertRaises(ValueError):
            service.settle_invoice("hrs", cash_amount="500", cash_currency="EUR",
                                   date="2026-03-05", diff_reason="fx_gain_loss", by="t")
        # 显式给清账面额 → 通过（部分清账 550）
        no = service.settle_invoice("hrs", cash_amount="500", cash_currency="EUR",
                                    settle_amount="550", date="2026-03-05",
                                    diff_reason="fx_gain_loss", by="t")
        self.assertTrue(no)

    def test_revaluation_report_unrealized_pl(self):
        # 外币未结 AP：EUR 1000 @1.10 入账(AP 1100)；期末 EUR 升到 1.20 → 敞口 1000×1.20=1200，未实现损失 100
        _eur_invoice("EU-R", "1000.00", "hr1", ccy="EUR", date="2026-01-05")
        service.post_invoice_by_hash("hr1", by="t", direction="AP")
        fx.add_rate("EUR", "2026-12-01", "1.20")
        v = service.fx_revaluation_view("2026-12-31")
        row = [c for c in v["by_currency"] if c["currency"] == "EUR"][0]
        self.assertEqual(D(row["open_ccy"]), D("1000.00"))
        self.assertEqual(D(row["book_usd"]), D("1100.00"))
        self.assertEqual(D(row["reval_usd"]), D("1200.00"))
        self.assertEqual(D(row["unrealized_pl"]), D("-100.00"))   # AP 升值 → 未实现损失
        self.assertEqual(D(v["total_unrealized_pl"]), D("-100.00"))

    def test_revaluation_flags_missing_rate(self):
        _eur_invoice("EU-R2", "1000.00", "hr2", ccy="EUR", date="2026-01-05")
        service.post_invoice_by_hash("hr2", by="t", direction="AP")
        v = service.fx_revaluation_view("2025-06-30")            # 早于任何 EUR 汇率
        self.assertIn("EUR", v["missing_rates"])

    def _save_stmt(self, fh, ccy, txns):
        from core.models import Transaction
        inv = Invoice(); inv.file_hash = fh; inv.doc_type = "statement"
        inv.set("currency_settlement", FieldValue(value=ccy))
        inv.transactions = list(txns)
        db.save_invoice(inv)

    def test_foreign_statement_line_converted(self):
        from core.models import Transaction
        # EUR 100 手续费 @交易日(2026-01-10→1.10) → 110 USD 支出
        self._save_stmt("hst1", "EUR", [Transaction(date="2026-01-10", description="EUR fee",
                                                    expense=D("100"), currency="EUR")])
        no = service.post_statement_entry("hst1", 0, A.FEE, activity="operating")
        self.assertTrue(no)
        led = service.load_ledger()
        self.assertEqual(led.net(A.BANK), D("-110.00"))
        e = [x for x in led.entries if x.source_hash == "hst1#0"][0]
        self.assertIn("EUR", e.memo); self.assertIn("1.10", e.memo)

    def test_foreign_statement_line_without_rate_rejected(self):
        from core.models import Transaction
        self._save_stmt("hst2", "CHF", [Transaction(date="2026-01-10", description="CHF",
                                                    expense=D("100"), currency="CHF")])
        with self.assertRaises(ValueError):
            service.post_statement_entry("hst2", 0, A.FEE, activity="operating")

    def test_usd_invoice_unchanged(self):
        _eur_invoice("US-1", "200.00", "hf4", ccy="USD", date="2026-06-10")
        no = service.post_invoice_by_hash("hf4", by="t", direction="AP")
        led = service.load_ledger()
        self.assertEqual(led.net(A.AP), D("-200.00"))         # USD 不换算
        e = [x for x in led.entries if x.source_hash == "hf4"][0]
        self.assertNotIn("原币", e.memo)


class _MockProvider(fx.RateProvider):
    """离线 mock：不联网，记录被调用的参数以验证「只传公开信息(功能货币/日期)」。"""
    name = "mock"

    def __init__(self, table=None):
        self.calls = []
        self.table = table or {"EUR": D("1.10"), "GBP": D("1.27")}

    def fetch(self, functional, date="latest"):
        self.calls.append((functional, date))
        eff = getattr(self, "eff", None) or date     # 默认模拟"当天已发布"(实际日=请求日)；可设 self.eff 模拟未发布
        return eff, dict(self.table)


class FxProviderTest(unittest.TestCase):
    """可插拔数据源 + 按日拉取更新 + 缺则自动拉（全程 mock、不真联网）。"""

    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._p, self._af, self._prov = config.FX_RATES_PATH, config.FX_AUTO_FETCH, fx.get_provider()
        config.FX_RATES_PATH = self._dir / "fx.json"; config.FX_RATES_PATH.write_text("{}")
        self.mock = _MockProvider()
        fx.set_provider(self.mock)

    def tearDown(self):
        config.FX_RATES_PATH, config.FX_AUTO_FETCH = self._p, self._af
        fx.set_provider(self._prov)
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_update_rates_writes_local(self):
        n, eff = fx.update_rates("2026-08-01")
        self.assertEqual(n, 2)
        self.assertEqual(eff, "2026-08-01")                        # 回传有效日（mock 模拟当日已发布）
        self.assertEqual(fx.rate("EUR", "2026-08-05"), D("1.10"))   # 生效日 ≤ 命中
        self.assertEqual(fx.rate("GBP", "2026-08-01"), D("1.27"))

    def test_provider_receives_only_functional_and_date(self):
        # 出站只带功能货币 + 日期这类公开信息，绝无内部数据（接口签名即保证）
        fx.update_rates("2026-08-01")
        self.assertEqual(self.mock.calls, [("USD", "2026-08-01")])

    def test_rate_auto_fetches_when_missing(self):
        config.FX_AUTO_FETCH = True
        self.assertEqual(fx.rate("EUR", "2026-08-01"), D("1.10"))   # 本地空 → 自动拉一次
        self.assertTrue(self.mock.calls)

    def test_no_auto_fetch_when_disabled(self):
        config.FX_AUTO_FETCH = False
        self.assertIsNone(fx.rate("EUR", "2026-08-01"))             # 不联网 → None
        self.assertFalse(self.mock.calls)

    def test_provider_swappable(self):
        fx.set_provider(_MockProvider({"EUR": D("2.00")}))
        fx.update_rates("2026-08-01")
        self.assertEqual(fx.rate("EUR", "2026-08-02"), D("2.00"))   # 换 provider 即换源

    # ---- 自检修复回归：当天录入不能静默沿用陈旧汇率（2026-08-17） ----
    def test_today_entry_refetches_over_stale_cache(self):
        import datetime as dt
        today_date = dt.date.fromisoformat(fx.today())
        today = today_date.isoformat()
        yest = (today_date - dt.timedelta(days=1)).isoformat()
        config.FX_AUTO_FETCH = True
        config.FX_RATES_PATH.write_text(json.dumps({"EUR": [{"date": yest, "rate": "1.10"}]}))
        self.mock.table = {"EUR": D("1.20")}
        self.assertEqual(fx.rate("EUR", today), D("1.20"))     # 主动拉当天，不用昨天旧值
        self.assertEqual(self.mock.calls, [("USD", today)])    # 确实向 provider 拉了当天

    def test_historical_backfill_uses_cache_no_refetch(self):
        import datetime as dt
        today_date = dt.date.fromisoformat(fx.today())
        yest = (today_date - dt.timedelta(days=1)).isoformat()
        week = (today_date - dt.timedelta(days=7)).isoformat()
        config.FX_AUTO_FETCH = True
        config.FX_RATES_PATH.write_text(json.dumps({"EUR": [{"date": week, "rate": "1.05"}]}))
        self.assertEqual(fx.rate("EUR", yest), D("1.05"))      # 补录历史用当时汇率、不强拉今天
        self.assertFalse(self.mock.calls)

    def test_offline_fallback_reports_true_effective_date(self):
        import datetime as dt
        today_date = dt.date.fromisoformat(fx.today())
        today = today_date.isoformat()
        yest = (today_date - dt.timedelta(days=1)).isoformat()
        config.FX_AUTO_FETCH = True
        config.FX_RATES_PATH.write_text(json.dumps({"EUR": [{"date": yest, "rate": "1.10"}]}))

        class _Fail(fx.RateProvider):
            name = "fail"
            def fetch(self, functional, date="latest"):
                raise RuntimeError("offline")
        fx.set_provider(_Fail())
        r, eff = fx.rate_with_date("EUR", today)
        self.assertEqual(r, D("1.10"))                         # 拉不到 → 回退最近可用
        self.assertEqual(eff, yest)                            # 生效日如实=昨天（不误标今天）

    def test_unpublished_today_stored_as_actual_date(self):
        # 风险F回归：请求今天但当天官方未发布 → provider 回传实际日(昨日)；须按实际日存、
        # 不制造"今天条目"(否则会把旧汇率伪装成今天、抑制后续当天刷新)
        import datetime as dt
        today_date = dt.date.fromisoformat(fx.today())
        today = today_date.isoformat()
        yest = (today_date - dt.timedelta(days=1)).isoformat()
        config.FX_AUTO_FETCH = True
        config.FX_RATES_PATH.write_text("{}")
        self.mock.eff = yest                                   # 模拟当天未发布 → 实际日=昨日
        self.mock.table = {"EUR": D("1.10")}
        r, eff = fx.rate_with_date("EUR", today)
        self.assertEqual((r, eff), (D("1.10"), yest))          # 用昨日值、生效日如实=昨日
        self.assertFalse(fx._has_exact("EUR", today))          # 关键：没制造"今天条目"
        self.assertTrue(fx._has_exact("EUR", yest))            # 按实际日存

    def test_today_uses_beijing_business_date(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        expected = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        self.assertEqual(fx.today(), expected)


if __name__ == "__main__":
    unittest.main()
