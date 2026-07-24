"""报表中心（module 7）第一增量：从总账/试算平衡取数 → 利润表 + 资产负债表 + 勾稽校验。

要点（对齐 `计划/财务报表中心计划_V1.md`）：
- **复用第六模块数据、不重复建账**：从 `ledger.service.load_ledger()` 的已过账分录取科目余额。
- **可配置映射**：科目→报表行由 `ledger.accounts.report_line` 决定；**未归类科目不静默忽略**——
  列入 unclassified 并**阻止出表**（E6）。
- **勾稽不过不出表**：E1 资产 = 负债 + 权益（本期净利润在报表期并入权益）。
- 金额全 Decimal。**本增量不含**：现金流量表（需现金流活动打标）、Excel/PDF 导出、E2/E3/E4（依赖期末结转与现金分类）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Tuple

from ledger import accounts as A
from ledger.engine import ZERO
from ledger.service import load_ledger
from . import mapping


def _natural(account: str, net: Decimal) -> Decimal:
    """把 借-贷净额 转成该科目**正常方向的正数**（资产/费用取 net；负债/权益/收入取 -net）。"""
    typ = A.account_type(account)
    if typ in ("liability", "equity", "revenue"):
        return -net
    return net


def _collect() -> Tuple[Dict[str, Decimal], List[str]]:
    """按报表行归集自然金额，返回 ({line_token: 金额}, [未归类科目])。"""
    led = load_ledger()
    _dr, _cr, rows = led.trial_balance()
    by_line: Dict[str, Decimal] = {}
    unclassified: List[str] = []
    for account, dr, cr in rows:
        net = dr - cr
        if net == ZERO:
            continue
        token = A.report_line(account)
        if not token:
            unclassified.append(account)
            continue
        by_line[token] = by_line.get(token, ZERO) + _natural(account, net)
    return by_line, unclassified


def _income_figures() -> dict:
    """利润表各行 Decimal 金额（内部用；对外 JSON 一律 str 化）。"""
    by_line, _unc = _collect()

    def amt(line):
        return by_line.get("IncomeStatement:" + line, ZERO)

    revenue = sum((amt(l) for l in mapping.IS_REVENUE), ZERO)
    cogs = amt("COGS")
    opex = amt("Opex")
    finance = amt("FinanceCosts")
    return {"Revenue": revenue, "COGS": cogs, "GrossProfit": revenue - cogs,
            "Opex": opex, "FinanceCosts": finance,
            "NetIncome": revenue - cogs - opex - finance}


def income_statement() -> dict:
    computed = _income_figures()
    lines = [{"key": k, "label": lbl, "kind": kind, "amount": str(computed[k])}
             for k, lbl, kind in mapping.IS_LINES]
    return {"lines": lines, "net_income": str(computed["NetIncome"])}


def balance_sheet() -> dict:
    by_line, unclassified = _collect()
    net_income = _income_figures()["NetIncome"]      # Decimal（内部算术）

    def section(defs, extra=None):
        seen, out, total = set(), [], ZERO
        for key, label in defs:
            if key in seen:               # BS_EQUITY 有 Equity/ShareCapital 同名标签，去重
                continue
            seen.add(key)
            v = by_line.get("BalanceSheet:" + key, ZERO)
            if v == ZERO and key not in ("RetainedEarnings",):
                continue
            out.append({"key": key, "label": label, "amount": str(v)})
            total += v
        if extra is not None:
            k, lbl, v = extra
            out.append({"key": k, "label": lbl, "amount": str(v)})
            total += v
        return out, total

    assets, assets_total = section(mapping.BS_ASSETS)
    liabs, liabs_total = section(mapping.BS_LIABILITIES)
    ek, el = mapping.BS_CURRENT_NET_INCOME
    equity, equity_total = section(mapping.BS_EQUITY, extra=(ek, el, net_income))

    balanced = assets_total == (liabs_total + equity_total)
    return {
        "assets": assets, "assets_total": str(assets_total),
        "liabilities": liabs, "liabilities_total": str(liabs_total),
        "equity": equity, "equity_total": str(equity_total),
        "liab_equity_total": str(liabs_total + equity_total),
        "balanced": balanced,
        "diff": str(assets_total - (liabs_total + equity_total)),
        "unclassified": unclassified,
    }


def cash_flow_statement() -> dict:
    """直接法现金流量表：按活动类别（经营/投资/筹资）汇总已过账分录的现金净流。

    单期起步：期初现金=0，期末现金=本期净流。E3 校验：期末现金 == 资产负债表货币资金。
    活动类别在记账（结算）阶段即打标（`store.post_entry` 强制，E5），此处只汇总。
    """
    led = load_ledger()
    totals = {A.OPERATING: ZERO, A.INVESTING: ZERO, A.FINANCING: ZERO}
    for e in led.entries:
        delta = e.cash_delta()
        if delta == ZERO:
            continue
        act = getattr(e, "activity", None)
        if act in totals:
            totals[act] += delta
    net_change = sum(totals.values(), ZERO)
    # 资产负债表货币资金（现金及等价物科目余额之和）
    _dr, _cr, rows = led.trial_balance()
    bs_cash = sum((dr - cr for acct, dr, cr in rows if A.is_cash(acct)), ZERO)
    opening = ZERO                      # 单期：期初 0
    ending = opening + net_change
    lines = [{"key": k, "label": A.ACTIVITY_LABEL[k], "amount": str(totals[k])}
             for k in (A.OPERATING, A.INVESTING, A.FINANCING)]
    return {
        "lines": lines,
        "net_change": str(net_change),
        "opening": str(opening), "ending": str(ending),
        "bs_cash": str(bs_cash),
        "e3_ok": ending == bs_cash,      # 三表勾稽：CFS 期末现金 == BS 货币资金
        "e3_diff": str(ending - bs_cash),
    }


def checks() -> dict:
    bs = balance_sheet()
    cf = cash_flow_statement()
    e1 = bs["balanced"]
    e6 = len(bs["unclassified"]) == 0
    e3 = cf["e3_ok"]
    return {
        "E1_balance_sheet_balanced": {"ok": e1, "diff": bs["diff"],
                                      "desc": "资产 = 负债 + 权益"},
        "E3_cash_tie": {"ok": e3, "diff": cf["e3_diff"],
                        "desc": "现金流量表期末现金 = 资产负债表货币资金"},
        "E6_all_classified": {"ok": e6, "unclassified": bs["unclassified"],
                              "desc": "无未归类科目"},
        "can_issue": e1 and e3 and e6,      # 勾稽全过才允许出表
    }


def export_excel(filename: str = ""):
    """导出三张报表 Excel（封面+三表+勾稽+审计轨迹）。**勾稽不过不出表**——抛 ValueError。"""
    import datetime as _dt
    from pathlib import Path
    from core import config, maintenance
    from . import excel as _excel

    ck = checks()
    if not ck["can_issue"]:
        bad = [k for k in ("E1_balance_sheet_balanced", "E3_cash_tie", "E6_all_classified")
               if not ck[k]["ok"]]
        raise ValueError("勾稽未通过，不出表（" + "、".join(bad) + "）")
    now = _dt.datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    out = Path(config.EXPORT_DIR) / (filename or f"financial_statements_{stamp}.xlsx")
    _excel.build_reports_workbook(out, generated_at=now.strftime("%Y-%m-%d %H:%M:%S"))
    maintenance.prune_exports()
    return out


def generate() -> dict:
    """完整报表包：利润表 + 资产负债表 + 现金流量表 + 勾稽结论。勾稽不过 can_issue=False。"""
    ck = checks()
    return {
        "income_statement": income_statement(),
        "balance_sheet": balance_sheet(),
        "cash_flow_statement": cash_flow_statement(),
        "checks": ck,
        "can_issue": ck["can_issue"],
        "basis": {"currency": "USD", "framework": "IFRS", "period": "本期累计（单期）"},
    }
