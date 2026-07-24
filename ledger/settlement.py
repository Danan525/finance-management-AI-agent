"""结算（第二段）：应付/应收 ↔ 银行，含净额差（手续费/预扣税/折扣/舍入）。

生产化自 `设计验证/ledger_settlement_spike.py` 的已验证结算逻辑。要点：
- **差额落在借/贷哪一方是机械确定的**：diff = 票面清账额 - 实收付现金；
  diff>0（少付/少收）→ AP 记贷、AR 记借；diff<0（多付/多收）→ 反之。
  用户只需选差额**科目（原因）**，方向不用配。
- **不可自动凑平**：有差额但未指定承接科目 → 拒绝（借贷不平由 assert_balanced 兜底）。
- **单票未结额取自明细辅助账**（逐单据累计已结），不能用控制账户总额代替（控制账户 == 全部明细合计）。
- **舍入容差**：微差可入舍入差异兜底轧平，超阈值须人工查明（防兜底科目沦为垃圾桶）。
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from core import db
from . import accounts as A
from .engine import ZERO, JournalEntry, JournalLine, _dec

AP, AR = "AP", "AR"


def diff_side(direction: str, diff: Decimal) -> str:
    """差额承接方向（借/贷），由 direction + 差额符号机械决定。"""
    if diff > ZERO:                 # 少付(AP)/少收(AR)
        return "credit" if direction == AP else "debit"
    return "debit" if direction == AP else "credit"   # 多付/多收


def settlement_entry(direction: str, settle_amount: Decimal, cash_amount: Decimal,
                     diff_account: Optional[str] = None, cash_account: str = A.BANK,
                     date: str = "", memo: str = "", source_hash: str = "",
                     source_ref: str = "") -> JournalEntry:
    """生成一张结算分录。settle_amount=本次清账票面额；cash_amount=实收付现金。"""
    settle_amount = _dec(settle_amount)
    cash_amount = _dec(cash_amount)
    if settle_amount <= ZERO:
        raise ValueError("结算票面额必须为正")
    if cash_amount < ZERO:
        raise ValueError("现金金额不能为负")
    diff = settle_amount - cash_amount

    if direction == AP:
        lines = [JournalLine(A.AP, debit=settle_amount),
                 JournalLine(cash_account, credit=cash_amount)]
    else:
        lines = [JournalLine(cash_account, debit=cash_amount),
                 JournalLine(A.AR, credit=settle_amount)]

    if diff != ZERO:
        if not diff_account:
            raise ValueError(
                f"差额 {diff} 未指定承接科目（如手续费/预扣税/折扣/舍入差异），"
                f"拒绝结算——不自动凑平")
        side = diff_side(direction, diff)
        amt = abs(diff)
        lines.append(JournalLine(diff_account,
                                 debit=amt if side == "debit" else ZERO,
                                 credit=amt if side == "credit" else ZERO))

    e = JournalEntry(date=date, memo=memo or f"结算 {source_ref}".strip(),
                     lines=lines, source_kind="settlement",
                     source_hash=source_hash, source_ref=source_ref, status="Draft")
    e.assert_balanced()
    return e


# ---------- 明细辅助账：单票未结额（不走控制账户总额）----------

def _accrual(file_hash: str, conn) -> Optional[dict]:
    """取该发票已过账的应计分录（含方向/全额）。已红冲/不存在则 None。"""
    row = conn.execute(
        "SELECT * FROM journal_entries WHERE source_kind='invoice' AND source_hash=? "
        "AND status='Posted' AND reverses_id IS NULL LIMIT 1", (file_hash,)).fetchone()
    if not row:
        return None
    # 方向：应计分录里出现 AP 科目=应付，AR 科目=应收
    accts = [r["account"] for r in conn.execute(
        "SELECT account FROM journal_lines WHERE entry_id=?", (row["id"],)).fetchall()]
    direction = AP if A.AP in accts else (AR if A.AR in accts else AP)
    # 业务性质科目（费用/收入/资产）：排除往来控制、税、现金科目
    nature = next((a for a in accts
                   if a not in (A.AP, A.AR, A.INPUT_TAX, A.OUTPUT_TAX) and not A.is_cash(a)), "")
    return {"gross": _dec(row["total_debit"]), "direction": direction,
            "entry_no": row["entry_no"], "date": row["date"] or "", "nature": nature}


def infer_activity(nature_account: str) -> str:
    """由业务性质科目推断现金流活动类别：固定资产→投资；其余（费用/收入）→经营。"""
    if A.report_line(nature_account) == "BalanceSheet:PPE":
        return A.INVESTING
    return A.OPERATING


def accrual_date(file_hash: str, conn: Optional[sqlite3.Connection] = None) -> str:
    """该发票应计分录的记账日期（结算未给日期时的兜底，避免落入 0000-00 期间）。"""
    with db._conn_or(conn) as c:
        acc = _accrual(file_hash, c)
        return acc["date"] if acc else ""


def accrual_nature(file_hash: str, conn: Optional[sqlite3.Connection] = None) -> str:
    """该发票应计分录的业务性质科目（用于推断结算现金流活动类别）。"""
    with db._conn_or(conn) as c:
        acc = _accrual(file_hash, c)
        return acc["nature"] if acc else ""


def settled_amount(file_hash: str, conn) -> Decimal:
    """该发票已结票面额合计（仅 Posted 的结算分录；红冲的不计）。"""
    rows = conn.execute(
        "SELECT settle_amount FROM journal_entries WHERE source_kind='settlement' "
        "AND source_hash=? AND status='Posted'", (file_hash,)).fetchall()
    return sum((_dec(r["settle_amount"]) for r in rows), ZERO)


def open_amount(file_hash: str, conn: Optional[sqlite3.Connection] = None
                ) -> Optional[Tuple[Decimal, str, Decimal]]:
    """返回 (未结额, 方向, 全额)；无有效应计分录则 None。"""
    with db._conn_or(conn) as c:
        acc = _accrual(file_hash, c)
        if not acc:
            return None
        return acc["gross"] - settled_amount(file_hash, c), acc["direction"], acc["gross"]


def open_invoices(conn: Optional[sqlite3.Connection] = None) -> List[dict]:
    """全部有未结余额的已过账发票（待结算），逐单据取自明细辅助账。"""
    out = []
    with db._conn_or(conn) as c:
        rows = c.execute(
            "SELECT source_hash, source_ref, entry_no, total_debit, id FROM journal_entries "
            "WHERE source_kind='invoice' AND status='Posted' AND reverses_id IS NULL "
            "ORDER BY period, entry_no").fetchall()
        for r in rows:
            fh = r["source_hash"]
            gross = _dec(r["total_debit"])
            settled = settled_amount(fh, c)
            open_amt = gross - settled
            if open_amt <= ZERO:
                continue
            accts = [x["account"] for x in c.execute(
                "SELECT account FROM journal_lines WHERE entry_id=?", (r["id"],)).fetchall()]
            direction = AP if A.AP in accts else (AR if A.AR in accts else AP)
            out.append({"file_hash": fh, "invoice_no": r["source_ref"],
                        "entry_no": r["entry_no"], "direction": direction,
                        "gross": str(gross), "settled": str(settled),
                        "open": str(open_amt)})
    return out


def control_reconciliation(conn: Optional[sqlite3.Connection] = None) -> dict:
    """控制账户对账：应付/应收总账余额 == 各自明细未结合计（应恒等）。"""
    from .service import load_ledger
    led = load_ledger()
    with db._conn_or(conn) as c:
        rows = c.execute(
            "SELECT source_hash, total_debit, id FROM journal_entries "
            "WHERE source_kind='invoice' AND status='Posted' AND reverses_id IS NULL").fetchall()
        detail = {AP: ZERO, AR: ZERO}
        for r in rows:
            fh = r["source_hash"]
            open_amt = _dec(r["total_debit"]) - settled_amount(fh, c)
            accts = [x["account"] for x in c.execute(
                "SELECT account FROM journal_lines WHERE entry_id=?", (r["id"],)).fetchall()]
            d = AP if A.AP in accts else (AR if A.AR in accts else AP)
            detail[d] += open_amt
    control_ap = -led.net(A.AP)      # 负债贷方 → 取相反为正
    control_ar = led.net(A.AR)       # 资产借方
    return {
        "AP": {"control": str(control_ap), "detail": str(detail[AP]),
               "ok": control_ap == detail[AP]},
        "AR": {"control": str(control_ar), "detail": str(detail[AR]),
               "ok": control_ar == detail[AR]},
    }
