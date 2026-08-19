"""多币种汇率表 + 可插拔汇率数据源。

**汇率规则（改动请同步 wiki `系统知识/汇率规则.md`）**：
- **来源**：默认 `FrankfurterProvider`（api.frankfurter.dev，欧洲央行公开数据）**按日拉取**并更新本地汇率表；
  provider 是**可插拔**的——`set_provider()` 换一个实现即可整体替换数据源，上层无需改动。
- **数据不出机（红线不变）**：provider **只 GET 公开汇率**（URL 仅含 base/日期这类公开参数），
  **绝不发送任何发票/金额/客户/内部数据**。唯一出站流量就是"拉汇率"这一只读请求。拉回的汇率写入本地
  `config/fx_rates.json`，之后查询全部走本地（快、可离线复用已拉的）。
- **取值时点**：见各调用方——发票入账用**人工审核通过、录入系统的当日**汇率（`ledger.post_invoice`）；
  外币结算用**结算日**、期末重估用**期末日**（各业务时点）。查询语义：取 **≤该日期的最近一条**（生效日），
  空日期或查不到 → `None`（fail-closed，不静默用最新）。
- **兜底**：`update_rates()` 可由定时任务/手动调用；`rate()` 若本地缺当日且 `FX_AUTO_FETCH` 开启会自动拉一次，
  拉取失败不崩、返回 `None`。手工 `add_rate()` 仍保留（离线或修正用）。
"""
from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from core import config

ZERO = Decimal("0")


# ============ 可插拔汇率数据源接口 ============
class RateProvider:
    """汇率数据源接口。实现只做一件事：**拉取公开汇率**，绝不发送任何内部数据。

    换数据源 = 写一个新的 RateProvider 子类 + `fx.set_provider(YourProvider())`，上层不变。
    """
    name = "abstract"

    def fetch(self, functional: str, date: str = "latest"):
        """拉取 date（'YYYY-MM-DD' 或 'latest'）各币种对 functional 的汇率。

        返回 **(实际生效日期 ISO, {币种: Decimal(1 币种 = ? functional)})**。
        **实际生效日期可能早于请求日**——如请求当天但官方尚未发布/周末节假日，数据源会返回最近工作日，
        provider **必须如实回传该真实日期**（不得伪装成请求日），否则会把旧汇率当"今天"静默错记
        （2026-08-17 自检发现）。拉取失败应抛异常。
        """
        raise NotImplementedError


class FrankfurterProvider(RateProvider):
    """Frankfurter（api.frankfurter.app）：欧洲央行公开汇率，免费、无需 key。

    **只 GET 公开端点**，参数仅 base（功能货币）——不含任何内部数据。
    """
    name = "frankfurter"
    BASE_URL = "https://api.frankfurter.dev/v1"   # 官方现行域名（.app 会 301 重定向到 .dev、徒增延迟/超时）

    def fetch(self, functional: str, date: str = "latest"):
        import requests
        # URL 仅含日期与 base 币种（公开信息）；绝不带发票/金额/客户等内部数据
        url = f"{self.BASE_URL}/{date}"
        resp = requests.get(url, params={"base": functional}, timeout=15)
        resp.raise_for_status()
        data = resp.json() or {}
        eff = str(data.get("date") or "").strip()      # 数据实际生效日期（可能早于请求日）
        out: Dict[str, Decimal] = {}
        for ccy, r in (data.get("rates") or {}).items():
            try:
                rd = Decimal(str(r))
            except (InvalidOperation, ValueError):
                continue
            if rd > ZERO:
                out[str(ccy).strip().upper()] = (Decimal(1) / rd)   # 1 func = r ccy → 1 ccy = 1/r func
        return eff, out


_provider: RateProvider = FrankfurterProvider()


def set_provider(provider: RateProvider) -> None:
    """替换汇率数据源（换 API/测试注入）。"""
    global _provider
    _provider = provider


def get_provider() -> RateProvider:
    return _provider


# ============ 本地汇率表（provider 拉回的结果落这里，查询全走本地）============
def _functional() -> str:
    return getattr(config, "FUNCTIONAL_CURRENCY", "USD").upper()


def today() -> str:
    """当前业务日期（北京时间），与汇率调度和默认入账日期保持一致。"""
    tz = ZoneInfo(getattr(config, "BUSINESS_TIMEZONE", "Asia/Shanghai"))
    return _dt.datetime.now(tz).date().isoformat()


def _today() -> str:
    """兼容既有内部调用；新代码优先使用公开的 :func:`today`。"""
    return today()


def _load() -> dict:
    try:
        with open(config.FX_RATES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for ccy, rows in data.items():
        if not isinstance(rows, list):
            continue
        clean = []
        for r in rows:
            d = str((r or {}).get("date", "")).strip()
            rt = (r or {}).get("rate", None)
            if not d or rt is None:
                continue
            try:
                rtd = Decimal(str(rt))
            except (InvalidOperation, ValueError):
                continue
            if rtd > ZERO:
                clean.append({"date": d, "rate": str(rtd)})
        if clean:
            out[str(ccy).strip().upper()] = sorted(clean, key=lambda x: x["date"])
    return out


def _write_rate(ccy: str, date: str, rate_value) -> None:
    """把一条汇率写入本地 JSON（同币种同日期覆盖）。内部用，不做人工校验。"""
    ccy = ccy.strip().upper()
    try:
        rtd = Decimal(str(rate_value))
    except (InvalidOperation, ValueError):
        return
    if rtd <= ZERO or ccy == _functional():
        return
    path = config.FX_RATES_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    rows = [r for r in data.get(ccy, []) if str((r or {}).get("date", "")).strip() != date]
    rows.append({"date": date, "rate": str(rtd)})
    data[ccy] = sorted(rows, key=lambda x: str(x.get("date", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_rates(date: Optional[str] = None) -> int:
    """**按日拉取并更新**本地汇率表：调 provider 拉 date（默认今天）各币种对功能货币的汇率、写本地。

    只拉公开汇率、只写本地；绝不上传任何内部数据。返回 **(更新币种数, 实际有效日期)**——供调用方区分
    "抓取日"（请求日/今天）与"有效日"（数据实际生效日，可能早于抓取日）。拉取失败抛异常（调用方决定吞否）。
    **按数据实际生效日期存**（provider 回传的 eff，可能早于请求日）——不把"当天未发布时返回的旧汇率"
    伪装成当天，从而不制造"看似今天、实为昨日"的假条目（2026-08-17 自检修）。
    """
    func = _functional()
    d = date or _today()
    eff, fetched = _provider.fetch(func, d)
    eff = (eff or d).strip() or d
    n = 0
    for ccy, r in fetched.items():
        if ccy == func:
            continue
        _write_rate(ccy, eff, r)        # 用实际生效日期，非请求日
        n += 1
    return n, eff                       # (更新币种数, 实际有效日期)——供区分"抓取日"与"有效日"


def _lookup_row(ccy: str, d: str) -> Optional[dict]:
    """本地表里 ≤d 的最近一条 {date, rate}；无则 None。"""
    entries = _load().get(ccy, [])
    cands = [e for e in entries if e["date"] <= d]
    return cands[-1] if cands else None


def _lookup(ccy: str, d: str) -> Optional[Decimal]:
    row = _lookup_row(ccy, d)
    return Decimal(row["rate"]) if row else None


def _has_exact(ccy: str, d: str) -> bool:
    return any(e["date"] == d for e in _load().get(ccy, []))


def rate_with_date(ccy: str, date: str):
    """返回 (汇率, 实际生效日期)。生效日可能早于查询日（用了"≤该日最近可用"汇率），供凭证如实标注。

    **当天业务的关键**：若 `date == 今天` 且本地**没有当天精确汇率**（哪怕有更旧的），会**主动拉当天**
    （即使 `_lookup` 已能返回旧值）——否则发布窗口轮询中断/拉取失败时，当天录入会静默沿用陈旧汇率
    而不更新到当天（2026-08-17 自检发现）。历史日期（`date<今天`，如补录/期末重估）**不强拉**：历史汇率不变、
    缓存有就用，保持生效日语义。空 date / 完全无汇率 → (None, None)（fail-closed）。拉取失败则回退最近可用。
    """
    ccy = (ccy or "").strip().upper()
    if not ccy or ccy == _functional():
        return Decimal("1"), (date or "").strip()
    d = (date or "").strip()
    if not d:
        return None, None
    row = _lookup_row(ccy, d)
    stale_today = (d == _today() and not _has_exact(ccy, d))    # 当天但无当天精确汇率 → 需主动刷新
    if (row is None or stale_today) and getattr(config, "FX_AUTO_FETCH", True):
        try:
            update_rates(d)
        except Exception:
            pass                     # 离线/API 故障 → 回退最近可用；仍无则 None（fail-closed）
        row = _lookup_row(ccy, d)
    if row is None:
        return None, None
    return Decimal(row["rate"]), row["date"]


def rate(ccy: str, date: str) -> Optional[Decimal]:
    """币种 ccy 在 date 的汇率（≤该日最近、当天会主动刷新、fail-closed）；见 `rate_with_date`。"""
    return rate_with_date(ccy, date)[0]


def to_functional(amount, ccy: str, date: str) -> Optional[Decimal]:
    """把外币金额按 date 汇率换算为功能货币（保留分）；无汇率返回 None。"""
    r = rate(ccy, date)
    if r is None:
        return None
    amt = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return (amt * r).quantize(Decimal("0.01"))


def rates() -> dict:
    """全部本地汇率表（已归一排序），供展示。"""
    return _load()


def add_rate(ccy: str, date: str, rate_value) -> dict:
    """**人工**录入/更新一条汇率（离线兜底或修正用；同币种同日期覆盖），写回 JSON。"""
    ccy = (ccy or "").strip().upper()
    date = (date or "").strip()
    if not ccy:
        raise ValueError("请指定币种")
    if ccy == _functional():
        raise ValueError("功能货币（%s）汇率恒为 1，无需录入" % _functional())
    if not date or len(date) < 8:
        raise ValueError("请指定生效日期（YYYY-MM-DD）")
    try:
        rtd = Decimal(str(rate_value))
    except (InvalidOperation, ValueError):
        raise ValueError("汇率必须是数字")
    if rtd <= ZERO:
        raise ValueError("汇率必须为正")
    _write_rate(ccy, date, rtd)
    return {"currency": ccy, "rates": _load().get(ccy, [])}
