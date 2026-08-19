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

from core import config, db
from ledger import accounts as A
from ledger.engine import ZERO, _dec
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


def _figures_from(by_line: dict) -> dict:
    def amt(line):
        return by_line.get("IncomeStatement:" + line, ZERO)
    revenue = sum((amt(l) for l in mapping.IS_REVENUE), ZERO)
    cogs = amt("COGS")
    opex = amt("Opex")
    finance = amt("FinanceCosts")
    other = amt("OtherIncome")          # 其它收益(利息/营业外)——加回净利
    tax = amt("IncomeTax")              # 所得税费用——减去
    return {"Revenue": revenue, "COGS": cogs, "GrossProfit": revenue - cogs,
            "Opex": opex, "FinanceCosts": finance, "OtherIncome": other, "IncomeTax": tax,
            "NetIncome": revenue + other - cogs - opex - finance - tax}


def _income_figures() -> dict:
    """当前损益余额（含结转影响；资产负债表的"本期净利润"用它——关账后归零、由留存承接）。"""
    by_line, _unc = _collect()
    return _figures_from(by_line)


def _business_pl_figures() -> dict:
    """经营损益：**排除结转分录及其红冲**（closing / reversal→closing）——利润表用它。
    否则关账把损益清零后利润表显示 0;且重开(红冲结转)后损益被红冲分录重复计入而翻倍。
    跨全部期间的真实经营收入/费用。"""
    by_line: Dict[str, Decimal] = {}
    with db._conn_or(None) as c:
        rows = c.execute(
            "SELECT l.account AS acct, l.debit AS dr, l.credit AS cr "
            "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
            "LEFT JOIN journal_entries orig ON e.reverses_id = orig.id "
            "WHERE e.status IN ('Posted','Reversed') AND e.source_kind != 'closing' "
            "AND (orig.source_kind IS NULL OR orig.source_kind != 'closing')").fetchall()
    for r in rows:
        if A.account_type(r["acct"]) not in ("revenue", "expense"):
            continue
        token = A.report_line(r["acct"])
        if not token or not token.startswith("IncomeStatement:"):
            continue
        by_line[token] = by_line.get(token, ZERO) + _natural(r["acct"], _dec(r["dr"]) - _dec(r["cr"]))
    return _figures_from(by_line)


def income_statement() -> dict:
    computed = _business_pl_figures()
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
    # 期初现金 = 期初建账(source_kind='opening')分录对现金及等价物的净额（非本期流量，不进上面三活动）
    opening = sum((e.cash_delta() for e in led.entries
                   if getattr(e, "source_kind", "") == "opening"), ZERO)
    # 资产负债表货币资金（现金及等价物科目余额之和）
    _dr, _cr, rows = led.trial_balance()
    bs_cash = sum((dr - cr for acct, dr, cr in rows if A.is_cash(acct)), ZERO)
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


def cash_flow_indirect() -> dict:
    """间接法经营活动现金流（与直接法交叉验证，E4）。

    经营现金流 = 本期经营净利润 + 非现金折旧摊销加回 − 经营性流动资产增加 + 经营性流动负债增加。
    「增加」= 期末净额 − 期初(建账 opening)净额。E4：间接法经营现金流 == 直接法经营现金流。
    在正确记账下恒等（应计不动现金两边抵消、结算=Δ往来、折旧加回、投资/筹资不碰经营营运资金）；
    不等即暴露活动分类错、或非经营项误置进经营性流动资产/负债科目。
    """
    led = load_ledger()
    ni = _business_pl_figures()["NetIncome"]            # 本期经营净利（未结转口径，排除 closing）
    _dr, _cr, rows = led.trial_balance()
    end = {acct: dr - cr for acct, dr, cr in rows}      # 期末净额（借−贷）
    opening_net = {}                                    # 期初（建账）净额
    for e in led.entries:
        if getattr(e, "source_kind", "") == "opening":
            for l in e.lines:
                opening_net[l.account] = opening_net.get(l.account, ZERO) + l.debit - l.credit
    side = {c: s for c, _n, _t, s, _r in A.chart()}

    op_assets = op_liab = ZERO
    for a in set(end) | set(opening_net):
        d = end.get(a, ZERO) - opening_net.get(a, ZERO)          # Δ(借−贷)
        line = A.report_line(a)
        if line in ("BalanceSheet:Receivables", "BalanceSheet:OtherCurrentAssets"):
            op_assets += d                                       # 经营流动资产（借方增加 d>0 占现金）
        elif line in ("BalanceSheet:Payables", "BalanceSheet:TaxPayable"):
            op_liab += d                                         # 经营流动负债（贷方增加 d<0 增现金）

    # 非现金折旧摊销加回 = 累计折旧/摊销科目**本期贷方计提额**（排除期初余额与资产处置的借方冲销）——
    # 不能用累折科目净变动：资产处置会 Dr 累折冲销已提折旧，把净变动抵消，漏加本期折旧（自检发现）。
    dep_addback = ZERO
    for e in led.entries:
        if getattr(e, "source_kind", "") == "opening":
            continue
        for l in e.lines:
            if A.report_line(l.account) == "BalanceSheet:PPE" and side.get(A.account_code(l.account), "") == "credit":
                dep_addback += l.credit

    # 剔除计入**投资/筹资活动**分录携带的损益（如资产处置收益、投资收益）——它们进了净利润，
    # 但对应现金在投资/筹资活动，间接法经营现金流不能保留在经营 NI 里（标准间接法"剔除非经营损益"）。
    non_op_pl = ZERO
    for e in led.entries:
        if getattr(e, "activity", None) in (A.INVESTING, A.FINANCING):
            for l in e.lines:
                t = A.account_type(l.account)
                if t == "revenue":
                    non_op_pl += l.credit - l.debit             # 收益对 NI 的正贡献
                elif t == "expense":
                    non_op_pl -= l.debit - l.credit             # 费用对 NI 的负贡献

    operating = ni - non_op_pl + dep_addback - op_assets - op_liab
    direct_op = _dec(next((l["amount"] for l in cash_flow_statement()["lines"]
                           if l["key"] == A.OPERATING), "0"))
    return {
        "net_income": str(ni), "depreciation_addback": str(dep_addback),
        "non_operating_pl_excluded": str(non_op_pl),
        "working_capital": {"op_assets_increase": str(op_assets),
                            "op_liab_increase": str(-op_liab)},
        "operating": str(operating), "direct_operating": str(direct_op),
        "e4_ok": operating == direct_op, "e4_diff": str(operating - direct_op),
    }


def _closing_to_retained() -> Decimal:
    """结转活动对未分配利润(3200)的**净**贷方影响 = 当前已结转入留存的净利润。
    含结转分录**与其红冲**：重开后 closing(+) 与其 reversal(−) 相抵归零,与留存实际余额一致。"""
    with db._conn_or(None) as c:
        rows = c.execute(
            "SELECT l.debit AS dr, l.credit AS cr "
            "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
            "LEFT JOIN journal_entries orig ON e.reverses_id = orig.id "
            "WHERE e.status IN ('Posted','Reversed') AND l.account = ? "
            "AND (e.source_kind = 'closing' OR orig.source_kind = 'closing')",
            (A.RETAINED,)).fetchall()
    return sum((_dec(r["cr"]) - _dec(r["dr"]) for r in rows), ZERO)


def checks() -> dict:
    bs = balance_sheet()
    cf = cash_flow_statement()
    e1 = bs["balanced"]
    e6 = len(bs["unclassified"]) == 0
    e3 = cf["e3_ok"]
    # E2：经营净利润 == 未结转损益(资产负债表本期净利润) + 已结转入留存的部分
    biz_ni = _business_pl_figures()["NetIncome"]
    unclosed = _income_figures()["NetIncome"]
    closed = _closing_to_retained()
    e2 = biz_ni == unclosed + closed
    ind = cash_flow_indirect()
    e4 = ind["e4_ok"]
    return {
        "E1_balance_sheet_balanced": {"ok": e1, "diff": bs["diff"],
                                      "desc": "资产 = 负债 + 权益"},
        "E2_income_to_retained": {"ok": e2, "diff": str(biz_ni - unclosed - closed),
                                  "desc": "净利润 = 未结转损益 + 已结转入留存"},
        "E3_cash_tie": {"ok": e3, "diff": cf["e3_diff"],
                        "desc": "现金流量表期末现金 = 资产负债表货币资金"},
        "E4_cfo_direct_indirect": {"ok": e4, "diff": ind["e4_diff"],
                                   "desc": "经营现金流：直接法 = 间接法"},
        "E6_all_classified": {"ok": e6, "unclassified": bs["unclassified"],
                              "desc": "无未归类科目"},
        "can_issue": e1 and e2 and e3 and e4 and e6,      # 勾稽全过才允许出表
    }


def export_excel(filename: str = ""):
    """导出三张报表 Excel（封面+三表+勾稽+审计轨迹）。**勾稽不过不出表**——抛 ValueError。"""
    import datetime as _dt
    from pathlib import Path
    from core import config, maintenance
    from . import excel as _excel

    ck = checks()
    if not ck["can_issue"]:
        bad = [k for k in ("E1_balance_sheet_balanced", "E2_income_to_retained",
                           "E3_cash_tie", "E4_cfo_direct_indirect", "E6_all_classified")
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
        "cash_flow_indirect": cash_flow_indirect(),
        "checks": ck,
        "can_issue": ck["can_issue"],
        "basis": {"currency": getattr(config, "FUNCTIONAL_CURRENCY", "USD"),
                  "framework": "IFRS", "period": "本期累计（单期）"},
    }
