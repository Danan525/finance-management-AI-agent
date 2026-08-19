"""期末软关账 + 结转损益（会计循环收口）。

生产化自 `设计验证/period_close_spike.py`。做两件事：
1. **结转损益**：把某会计期间的损益类科目余额结转 → `本年利润` → `未分配利润`（留存收益），
   使损益归零、净利润进入权益。三张结转分录各自借贷平衡，走 `post_entry` 全部闸门。
2. **软关账**：置该期 `periods.status='closed'`；此后 `post_entry` 拒绝向该期过账（见 store 护栏），
   调整只能落开放期或先 `reopen_period` 重开。

结转分录 `source_kind='closing'`——报表侧据此**把结转分录排除出利润表**（否则损益被清零、利润表显示 0）。
红线：关账是**人工显式动作**，AI 不自动关账。金额全 Decimal。
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from core import config, db
from . import accounts as A
from .engine import ZERO, JournalEntry, JournalLine, _dec
from . import store


def period_status(period: str, conn: Optional[sqlite3.Connection] = None) -> str:
    """返回 'open' / 'closed'（无记录视为 open）。"""
    with db._conn_or(conn) as c:
        row = c.execute("SELECT status FROM periods WHERE period=?", (period,)).fetchone()
        return row["status"] if row else "open"


def pl_balances(period: str, conn: Optional[sqlite3.Connection] = None
                ) -> Tuple[Dict[str, Decimal], Dict[str, Decimal]]:
    """该期损益类科目的（收入自然额, 费用自然额）字典，**排除结转分录本身**。

    收入取贷-借（正常贷方），费用取借-贷（正常借方）。只统计该 period、非 closing 的分录。
    """
    revenue: Dict[str, Decimal] = {}
    expense: Dict[str, Decimal] = {}
    with db._conn_or(conn) as c:
        rows = c.execute(
            "SELECT l.account AS acct, l.debit AS dr, l.credit AS cr "
            "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
            "LEFT JOIN journal_entries orig ON e.reverses_id = orig.id "
            "WHERE e.period=? AND e.status IN ('Posted','Reversed') "
            "AND e.source_kind != 'closing' "
            "AND (orig.source_kind IS NULL OR orig.source_kind != 'closing')",  # 排除结转分录及其红冲
            (period,)).fetchall()
    for r in rows:
        typ = A.account_type(r["acct"])
        if typ == "revenue":
            revenue[r["acct"]] = revenue.get(r["acct"], ZERO) + (_dec(r["cr"]) - _dec(r["dr"]))
        elif typ == "expense":
            expense[r["acct"]] = expense.get(r["acct"], ZERO) + (_dec(r["dr"]) - _dec(r["cr"]))
    # 去掉净额为 0 的科目
    revenue = {k: v for k, v in revenue.items() if v != ZERO}
    expense = {k: v for k, v in expense.items() if v != ZERO}
    return revenue, expense


def closing_entries(period: str, date: str,
                    conn: Optional[sqlite3.Connection] = None) -> List[JournalEntry]:
    """生成该期的三张结转分录（Draft）：结转收入 / 结转费用 / 本年利润→未分配利润。"""
    revenue, expense = pl_balances(period, conn)
    rev_total = sum(revenue.values(), ZERO)
    exp_total = sum(expense.values(), ZERO)
    entries: List[JournalEntry] = []

    if rev_total > ZERO:
        lines = [JournalLine(a, debit=v) for a, v in revenue.items()]
        lines.append(JournalLine(A.CY_PROFIT, credit=rev_total))
        entries.append(JournalEntry(date=date, memo=f"结转收入 {period}", lines=lines,
                                    source_kind="closing", status="Draft"))
    if exp_total > ZERO:
        lines = [JournalLine(A.CY_PROFIT, debit=exp_total)]
        lines += [JournalLine(a, credit=v) for a, v in expense.items()]
        entries.append(JournalEntry(date=date, memo=f"结转费用 {period}", lines=lines,
                                    source_kind="closing", status="Draft"))
    net = rev_total - exp_total                 # 本年利润净额（>0 盈利 <0 亏损）
    if net > ZERO:
        entries.append(JournalEntry(date=date, memo=f"结转本年利润→未分配利润 {period}", lines=[
            JournalLine(A.CY_PROFIT, debit=net), JournalLine(A.RETAINED, credit=net)],
            source_kind="closing", status="Draft"))
    elif net < ZERO:
        entries.append(JournalEntry(date=date, memo=f"结转本年亏损→未分配利润 {period}", lines=[
            JournalLine(A.RETAINED, debit=-net), JournalLine(A.CY_PROFIT, credit=-net)],
            source_kind="closing", status="Draft"))
    return entries


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _period_end(period: str) -> str:
    """会计期间 'YYYY-MM' 的真实最后一天，如 2026-02 → 2026-02-28。"""
    import calendar
    if len(period) == 7 and period[4] == "-":
        y, m = int(period[:4]), int(period[5:7])
        return "%s-%02d" % (period, calendar.monthrange(y, m)[1])
    return _now()[:10]


def close_period(period: str, by: str = "admin") -> dict:
    """人工关账：生成并过账结转分录 → 置该期 closed。返回 {entry_nos, net_income}。

    闸门：期未关过；试算平衡成立（不平拒绝）。结转分录在该期仍开放时过账，之后才置 closed。
    """
    if period_status(period) == "closed":
        raise ValueError(f"会计期间 {period} 已关账，勿重复")
    # 试算平衡前提（全账套）
    from .service import trial_balance
    dr, cr, _rows, ok = trial_balance()
    if not ok:
        raise ValueError(f"账套试算不平衡（借 {dr} ≠ 贷 {cr}），拒绝关账")

    date = _period_end(period)                                    # 期末日 = 该月真实最后一天
    entries = closing_entries(period, date)
    revenue, expense = pl_balances(period)
    net = sum(revenue.values(), ZERO) - sum(expense.values(), ZERO)

    nos = [store.post_entry(e, by=by, at=_now()) for e in entries]   # 结转分录（此时期仍 open）
    # 置 closed（幂等 UPSERT）
    with db.connect() as c:
        c.execute("INSERT INTO periods(period, status, net_income, closed_by, closed_at) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(period) DO UPDATE SET "
                  "status='closed', net_income=excluded.net_income, "
                  "closed_by=excluded.closed_by, closed_at=excluded.closed_at",
                  (period, "closed", str(net), by, _now()))
    return {"period": period, "entry_nos": nos, "net_income": str(net)}


def list_periods(conn: Optional[sqlite3.Connection] = None) -> List[dict]:
    """账上出现过的会计期间 + 关账状态（供界面列出、关账/重开）。"""
    with db._conn_or(conn) as c:
        rows = c.execute(
            "SELECT DISTINCT period FROM journal_entries WHERE period != '' ORDER BY period").fetchall()
        out = []
        for r in rows:
            p = r["period"]
            pr = c.execute("SELECT status, net_income, closed_at, closed_by FROM periods WHERE period=?",
                           (p,)).fetchone()
            out.append({"period": p,
                        "status": pr["status"] if pr else "open",
                        "net_income": (pr["net_income"] if pr else None),
                        "closed_at": (pr["closed_at"] if pr else None),
                        "closed_by": (pr["closed_by"] if pr else None)})
        return out


def reopen_period(period: str, by: str = "admin") -> dict:
    """重开已关账期：先解锁 → 红冲该期所有结转分录 → 置回 open。返回红冲凭证号。"""
    if period_status(period) != "closed":
        raise ValueError(f"会计期间 {period} 未关账，无需重开")
    with db.connect() as c:
        c.execute("UPDATE periods SET status='open' WHERE period=?", (period,))  # 先解锁,红冲才能过账
        rows = c.execute(
            "SELECT entry_no FROM journal_entries WHERE period=? AND source_kind='closing' "
            "AND status='Posted' AND reverses_id IS NULL", (period,)).fetchall()
    revs = [store.reverse_entry(r["entry_no"], by=by, at=_now()) for r in rows]
    return {"period": period, "reversed": revs}
