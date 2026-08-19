#!/usr/bin/env python3
"""检测 Frankfurter 最新汇率、更新本地缓存，并在有效日变更时推送 Lark。

用法：
    python3 update-fx-rates.py                    # 查询 latest；适合发布窗口轮询
    python3 update-fx-rates.py 2026-08-14         # 拉指定日
    python3 update-fx-rates.py --force-notify     # 有效日未变也推送一次
    python3 update-fx-rates.py --bootstrap        # 以现有缓存初始化去重状态，不推送

ECB 通常在工作日欧洲中部时间 16:00 左右发布。部署 cron 在对应的北京时间
22:00（夏令时）/23:00（冬令时）窗口轮询；本脚本查询 ``latest``，所以不依赖
服务器 UTC 日期。只有检测到新的“有效日”才推送，重复轮询不会刷屏；失败告警
同一北京时间自然日最多推送一次。

只拉公开汇率、只发送汇率状态文本，不发送任何发票、金额、客户等内部数据。
规则见 wiki/系统知识/汇率规则.md。
"""
import sys
import argparse
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import datetime as _dt  # noqa: E402
from decimal import Decimal  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
from core import config, fx  # noqa: E402

_ALERT_FILE = Path(__file__).resolve().parent / "config" / "alert_targets.txt"
_STATE_FILE = getattr(config, "FX_UPDATE_STATE_PATH", config.DATA_ROOT / "fx-update-state.json")


def _rates_text(eff: str) -> str:
    """有效日 eff 那批各币种汇率明细（1 外币 = ? 功能货币，显示 6 位；换算用内部高精度值），供人工核对。"""
    func = getattr(config, "FUNCTIONAL_CURRENCY", "USD")
    table = fx.rates()
    rows = []
    for ccy in sorted(table):
        r = next((e["rate"] for e in table[ccy] if e["date"] == eff), None)
        if r is not None:
            rows.append(f"{ccy} {Decimal(r).quantize(Decimal('0.000001'))}")
    body = "\n".join(rows) if rows else "（无该有效日明细）"
    return f"1 外币 = ? {func}（{len(rows)} 种，仅显示 6 位）：\n{body}"


def _lark_url(target: str):
    t = (target or "").strip()
    if t.startswith("lark://"):
        return "https://open.larksuite.com/open-apis/bot/v2/hook/" + t[len("lark://"):]
    if t.startswith("http"):
        return t
    return None


def _notify(msg: str) -> bool:
    """推送全部已配置目标；返回是否全部成功。未配置目标视为成功。"""
    if not _ALERT_FILE.exists():
        return True
    targets = [l for l in _ALERT_FILE.read_text(encoding="utf-8").splitlines()
               if l.strip() and not l.strip().startswith("#")]
    if not targets:
        return True
    import requests
    all_ok = True
    for t in targets:
        url = _lark_url(t)
        if not url:
            all_ok = False
            continue
        try:
            response = requests.post(
                url, json={"msg_type": "text", "content": {"text": msg}}, timeout=10)
            response.raise_for_status()
            result = response.json() or {}
            if result.get("code") not in (None, 0):
                raise RuntimeError(f"Lark code={result.get('code')}")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"[fx] 告警推送失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    return all_ok


def _beijing_now() -> _dt.datetime:
    tz = ZoneInfo(getattr(config, "BUSINESS_TIMEZONE", "Asia/Shanghai"))
    return _dt.datetime.now(tz)


def _load_state() -> dict:
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = _STATE_FILE.with_suffix(_STATE_FILE.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(_STATE_FILE)


def _latest_cached_effective_date() -> str:
    dates = [row.get("date", "") for rows in fx.rates().values() for row in rows]
    return max((date for date in dates if date), default="")


def _bootstrap() -> int:
    """把现有缓存视为已经推送，迁移调度时避免重复通知。"""
    latest = _latest_cached_effective_date()
    if not latest:
        print("[fx] 本地尚无汇率缓存；无需初始化，首次抓到数据时会推送")
        return 0
    now = _beijing_now().isoformat(timespec="seconds")
    state = _load_state()
    state.update({"last_notified_effective_date": latest, "bootstrapped_at": now})
    _save_state(state)
    print(f"[fx] 已初始化推送状态：有效日 {latest}（不发送 Lark）")
    return 0


def _run_update(date: str | None = None, force_notify: bool = False) -> int:
    """执行一次检测；每个新有效日最多推送一次。供 CLI 与测试复用。"""
    checked_at = _beijing_now()
    request_date = date or "latest"
    count, effective_date = fx.update_rates(request_date)
    if not effective_date or effective_date == "latest":
        raise ValueError(f"数据源未返回有效日期：{effective_date!r}")

    state = _load_state()
    previous = str(state.get("last_notified_effective_date") or "")
    is_new = not previous or effective_date > previous
    checked_text = checked_at.strftime("%Y-%m-%d %H:%M:%S")

    if is_new or force_notify:
        label = "发现新汇率" if is_new else "手动复报"
        message = (
            f"✅ 财务系统·{label}（北京时间 {checked_text} 检测、有效日 {effective_date}，"
            f"{count} 币种，来源 {fx.get_provider().name}）\n{_rates_text(effective_date)}"
        )
        print(f"[fx] {message}")
        delivered = _notify(message)
        if delivered and is_new:
            state["last_notified_effective_date"] = effective_date
            state["last_notification_at"] = checked_at.isoformat(timespec="seconds")
    else:
        print(f"[fx] 暂无新汇率（北京时间 {checked_text}，最新有效日 {effective_date}）")

    state.update({
        "last_checked_at": checked_at.isoformat(timespec="seconds"),
        "last_seen_effective_date": effective_date,
    })
    _save_state(state)
    return 0


def run(date: str | None = None, force_notify: bool = False) -> int:
    """执行一次检测并对失败告警去重；返回进程退出码。"""
    try:
        return _run_update(date=date, force_notify=force_notify)
    except Exception as exc:  # noqa: BLE001
        now = _beijing_now()
        state = _load_state()
        local_date = now.date().isoformat()
        error = (f"⚠️ 财务系统·汇率拉取失败（北京时间 {now:%Y-%m-%d %H:%M:%S}）："
                 f"{type(exc).__name__}: {exc}")
        print(f"[fx] {error}", file=sys.stderr)
        if state.get("last_error_notified_date") != local_date and _notify(error):
            state["last_error_notified_date"] = local_date
        state["last_error_at"] = now.isoformat(timespec="seconds")
        _save_state(state)
        return 1


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="检测并缓存 Frankfurter 最新汇率")
    parser.add_argument("date", nargs="?", help="可选指定日期 YYYY-MM-DD；默认查询 latest")
    parser.add_argument("--force-notify", action="store_true", help="有效日未变化也强制推送一次")
    parser.add_argument("--bootstrap", action="store_true", help="用现有缓存初始化去重状态，不推送")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.bootstrap:
        sys.exit(_bootstrap())
    sys.exit(run(date=args.date, force_notify=args.force_notify))
