"""总账持久化：分录落库/读取/凭证号分配/红冲。金额以 Decimal 原文存 TEXT。

凭证号 YYYYMM-NNNN 在 `BEGIN IMMEDIATE` 事务内按期间序号分配，避免并发重号。
幂等：同一 source（kind+hash）已有未红冲的 Posted 分录时，post_entry 拒绝重复入账。
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import List, Optional

from core import config, db
from . import accounts as A
from .engine import ZERO, JournalEntry, JournalLine, _dec


def _check_activity(entry: JournalEntry, activity) -> None:
    """现金流活动类别校验：动现金必须标（经营/投资/筹资）；不动现金/内部腾挪不许标。"""
    delta = entry.cash_delta()
    if delta != ZERO:
        if activity not in A.ACTIVITIES:
            raise ValueError(
                f"该分录动用现金（净流 {delta}），必须标活动类别"
                f"（operating/investing/financing）：{entry.memo}")
    elif activity is not None:
        raise ValueError(f"分录不动用现金（或现金内部腾挪），不应指定活动类别：{entry.memo}")


def period_of(date: str) -> str:
    """从 ISO 日期取会计期间 YYYY-MM；无日期归 '0000-00'（待人工补）。"""
    if date and len(date) >= 7 and date[4] == "-":
        return date[:7]
    return "0000-00"


def existing_posted(source_kind: str, source_hash: str,
                    conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """返回该来源已存在的、未被红冲的 Posted 分录号；无则 None（幂等判据）。"""
    if not source_hash:
        return None
    with db._conn_or(conn) as c:
        row = c.execute(
            "SELECT entry_no FROM journal_entries "
            "WHERE source_kind=? AND source_hash=? AND status='Posted' "
            "AND reverses_id IS NULL LIMIT 1",
            (source_kind, source_hash)).fetchone()
    return row["entry_no"] if row else None


def post_entry(entry: JournalEntry, by: str, at: str,
               settle_amount=None, activity=None) -> str:
    """把一张分录以 Posted 落库并分配凭证号；返回 entry_no。

    平衡硬校验 + 幂等（应计来源已有 Posted 分录则抛 ValueError）都在**同一事务**内完成。
    幂等只对 source_kind='invoice'（应计，一票至多一张）生效；结算可对同一票多次（部分结算）。
    settle_amount：结算分录本次清账票面额（明细辅助账用）。
    activity：现金流活动类别——**动用现金及等价物的分录必须标**（经营/投资/筹资，直接法前提，E5）；
    不动现金或现金口径内部腾挪（cash_delta=0）则必须为 None（否则拒绝）。
    """
    entry.assert_balanced()
    _check_activity(entry, activity)
    period = period_of(entry.date)
    dr, cr = entry.totals()

    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 幂等：应计来源已有未红冲 Posted 分录 → 拒绝（结算不受此限，可多次部分结算）
        if entry.source_hash and entry.source_kind == "invoice":
            dup = conn.execute(
                "SELECT entry_no FROM journal_entries WHERE source_kind=? AND source_hash=? "
                "AND status='Posted' AND reverses_id IS NULL LIMIT 1",
                (entry.source_kind, entry.source_hash)).fetchone()
            if dup:
                conn.rollback()
                raise ValueError(f"该来源已入账（{dup['entry_no']}），拒绝重复过账")
        # 期间内顺序号
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM journal_entries WHERE period=?", (period,)
        ).fetchone()["c"]
        entry_no = "%s-%04d" % (period.replace("-", ""), n + 1)
        cur = conn.execute(
            "INSERT INTO journal_entries(entry_no, date, memo, source_kind, source_hash, "
            "source_ref, status, total_debit, total_credit, period, reverses_id, "
            "settle_amount, activity, created_by, created_at, posted_by, posted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_no, entry.date, entry.memo, entry.source_kind, entry.source_hash,
             entry.source_ref, "Posted", str(dr), str(cr), period,
             entry.reverses_id if hasattr(entry, "reverses_id") else None,
             str(settle_amount) if settle_amount is not None else None, activity,
             by, at, by, at))
        eid = cur.lastrowid
        for seq, l in enumerate(entry.lines):
            conn.execute(
                "INSERT INTO journal_lines(entry_id, seq, account, debit, credit, memo) "
                "VALUES (?,?,?,?,?,?)",
                (eid, seq, l.account, str(l.debit), str(l.credit), l.memo))
        conn.commit()
        entry.entry_no = entry_no
        entry.status = "Posted"
        return entry_no
    finally:
        conn.close()


def _row_to_entry(c, row) -> JournalEntry:
    lines = [
        JournalLine(r["account"], _dec(r["debit"]), _dec(r["credit"]), r["memo"])
        for r in c.execute(
            "SELECT account, debit, credit, memo FROM journal_lines "
            "WHERE entry_id=? ORDER BY seq", (row["id"],)).fetchall()
    ]
    e = JournalEntry(
        date=row["date"], memo=row["memo"], lines=lines,
        source_kind=row["source_kind"], source_hash=row["source_hash"] or "",
        source_ref=row["source_ref"] or "", entry_no=row["entry_no"],
        status=row["status"], created_by=row["created_by"] or "",
        created_at=row["created_at"] or "")
    e.reverses_id = row["reverses_id"]
    e._id = row["id"]
    e.activity = row["activity"] if "activity" in row.keys() else None
    return e


def list_entries(status: str = "Posted", limit: int = 500,
                 conn: Optional[sqlite3.Connection] = None) -> List[JournalEntry]:
    with db._conn_or(conn) as c:
        rows = c.execute(
            "SELECT * FROM journal_entries WHERE status=? ORDER BY period, entry_no LIMIT ?",
            (status, limit)).fetchall()
        return [_row_to_entry(c, r) for r in rows]


def entries_for_balance(limit: int = 1000000,
                        conn: Optional[sqlite3.Connection] = None) -> List[JournalEntry]:
    """所有影响科目余额的分录（Posted + Reversed）。红冲保留原分录，二者相抵。"""
    with db._conn_or(conn) as c:
        rows = c.execute(
            "SELECT * FROM journal_entries WHERE status IN ('Posted','Reversed') "
            "ORDER BY period, entry_no LIMIT ?", (limit,)).fetchall()
        return [_row_to_entry(c, r) for r in rows]


def get_entry(entry_no: str, conn: Optional[sqlite3.Connection] = None) -> Optional[JournalEntry]:
    with db._conn_or(conn) as c:
        row = c.execute("SELECT * FROM journal_entries WHERE entry_no=?", (entry_no,)).fetchone()
        return _row_to_entry(c, row) if row else None


def reverse_entry(entry_no: str, by: str, at: str) -> str:
    """红冲：为一张已 Posted 分录生成方向相反的 Reversal 分录并过账；原分录置 Reversed。"""
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM journal_entries WHERE entry_no=?", (entry_no,)).fetchone()
        if not row:
            conn.rollback()
            raise ValueError(f"分录不存在：{entry_no}")
        if row["status"] != "Posted":
            conn.rollback()
            raise ValueError(f"只有 Posted 分录可红冲，当前状态：{row['status']}")
        src = _row_to_entry(conn, row)
        period = period_of(src.date)
        n = conn.execute("SELECT COUNT(*) AS c FROM journal_entries WHERE period=?", (period,)).fetchone()["c"]
        rev_no = "%s-%04d" % (period.replace("-", ""), n + 1)
        dr, cr = src.totals()
        # 红冲现金分录：反向现金流仍归同一活动类别，使 CFS 中原流入/流出被抵消
        rev_activity = row["activity"] if "activity" in row.keys() else None
        cur = conn.execute(
            "INSERT INTO journal_entries(entry_no, date, memo, source_kind, source_hash, "
            "source_ref, status, total_debit, total_credit, period, reverses_id, "
            "activity, created_by, created_at, posted_by, posted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rev_no, src.date, "红冲 " + src.memo, "reversal", src.source_hash,
             src.source_ref, "Posted", str(cr), str(dr), period, row["id"],
             rev_activity, by, at, by, at))
        rid = cur.lastrowid
        for seq, l in enumerate(src.lines):                 # 借贷互换
            conn.execute(
                "INSERT INTO journal_lines(entry_id, seq, account, debit, credit, memo) "
                "VALUES (?,?,?,?,?,?)", (rid, seq, l.account, str(l.credit), str(l.debit), l.memo))
        conn.execute("UPDATE journal_entries SET status='Reversed' WHERE id=?", (row["id"],))
        conn.commit()
        return rev_no
    finally:
        conn.close()
