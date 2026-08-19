"""建账与期初余额（会计循环的起点，MVP 收官件）。

把接手既有账套时的**期初余额**录入总账。做法(对齐计划 §3.5):
- **往来逐单据/逐户录**:每笔期初应收/应付单独成一张分录 → 可像发票一样**被结算清账**(进"待结算"队列)。
- **其它科目期初余额**(银行/固定资产/实收资本/应交税费…)各录一张。
- **对方科目统一用未分配利润 3200**(期初建账权益):每张自平,建账后 3200 余额 = 净资产 − 实收资本 = **年初留存**。
  这是软件常用的"期初建账权益"手法(每笔 account↔3200),试算天然平衡;是否符合预期由建账人核对 3200。
- **期初现金不是本期现金流**:opening 分录豁免"动现金必标活动"(store),且现金流量表把它算作**期初现金**、不计入本期净流。

红线沿用:金额全 Decimal、借贷平硬校验、人工显式触发(AI 不自动建账)。建账应在录业务前做。
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import List, Optional

from core import config, db
from . import accounts as A
from . import store
from .engine import ZERO, JournalEntry, JournalLine, _dec

OPENING_COUNTER = A.RETAINED      # 期初对方科目：未分配利润(3200)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_seq() -> int:
    with db._conn_or(None) as c:
        n = c.execute("SELECT COUNT(*) AS c FROM journal_entries WHERE source_kind='opening'").fetchone()["c"]
    return n


def post_opening(items: Optional[List[dict]] = None,
                 other_lines: Optional[List[dict]] = None,
                 date: str = "", by: str = "admin") -> dict:
    """录入期初余额，返回 {entry_nos, opening_retained}。

    items: 期初往来(可结算)—— [{"account": 应付/应收控制账户, "counterparty", "amount", "ref"?}]，amount>0。
    other_lines: 其它期初余额 —— [{"account", "amount", "side": "debit"|"credit"}]，amount>0。
    每条各成一张 opening 分录、对方科目 3200；全部借贷自平。
    """
    items = items or []
    other_lines = other_lines or []
    if not items and not other_lines:
        raise ValueError("期初建账为空：至少给一条往来或余额")
    date = date or (_now()[:10])
    seq = _next_seq()
    nos: List[str] = []

    def _post(lines, ref, cp=None):
        nonlocal seq
        e = JournalEntry(date=date, memo="期初建账 " + ref, lines=lines,
                         source_kind="opening", source_hash="opening-%d" % seq,
                         source_ref=ref, status="Draft")
        seq += 1
        nos.append(store.post_entry(e, by=by, at=_now(), counterparty=cp))

    # 原子性：先**全量校验并组装**所有分录，全部通过后才逐条落库——
    # 否则后置行报错时前面各行已独立 commit，账套被部分写脏（自检发现，2026-08-11 修）。
    prepared = []      # [(lines, ref, cp)]

    # 往来逐户(可结算)
    for it in items:
        acct = (it.get("account") or "").strip()
        cp = (it.get("counterparty") or "").strip()
        amt = _dec(it.get("amount"))
        if amt <= ZERO:
            raise ValueError("期初往来金额必须为正：%s" % (it.get("ref") or cp))
        if not A.is_control(acct):
            raise ValueError("期初往来科目必须是应付/应收控制账户：%s" % acct)
        if not cp:
            raise ValueError("期初往来必须指定对手方")
        side = A.control_side(acct)   # 'AR' 借方常余 / 'AP' 贷方常余
        if side == "AR":
            lines = [JournalLine(acct, debit=amt), JournalLine(OPENING_COUNTER, credit=amt)]
        else:
            lines = [JournalLine(OPENING_COUNTER, debit=amt), JournalLine(acct, credit=amt)]
        prepared.append((lines, "%s %s" % (acct.split(None, 1)[0], cp), cp))

    # 其它科目期初余额
    for ln in other_lines:
        acct = (ln.get("account") or "").strip()
        amt = _dec(ln.get("amount"))
        sd = (ln.get("side") or "").strip()
        if amt <= ZERO:
            raise ValueError("期初余额金额必须为正：%s" % acct)
        if A.is_control(acct):
            raise ValueError("往来控制账户请走 items 逐户录：%s" % acct)
        if A.account_type(acct) is None:
            raise ValueError("期初科目无法归类：%s" % acct)
        if sd == "debit":
            lines = [JournalLine(acct, debit=amt), JournalLine(OPENING_COUNTER, credit=amt)]
        elif sd == "credit":
            lines = [JournalLine(OPENING_COUNTER, debit=amt), JournalLine(acct, credit=amt)]
        else:
            raise ValueError("期初余额 side 必须为 debit/credit：%s" % acct)
        prepared.append((lines, acct.split(None, 1)[0], None))

    for lines, ref, cp in prepared:      # 校验全通过后才落库
        _post(lines, ref, cp=cp)

    from .service import load_ledger
    retained = -load_ledger().net(OPENING_COUNTER)     # 3200 贷方为正
    return {"entry_nos": nos, "opening_retained": str(retained)}
