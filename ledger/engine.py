"""复式记账内核：分录行 / 分录（借贷平校验）/ 账套（过账 + 试算平衡）。

生产化自 `设计验证/ledger_settlement_spike.py` 的已验证内核。**金额一律 Decimal、绝不 float**；
借贷不平**拒绝过账**（绝不自动凑平）——对应总账计划的"借贷平衡硬校验"。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

ZERO = Decimal("0")


@dataclass
class JournalLine:
    """一条分录行：借或贷其一为正（另一为 0）。account = 科目编码+名称字符串。"""
    account: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    memo: Optional[str] = None

    def __post_init__(self):
        # 容错：传入 str/float/int 统一转 Decimal（避免 float 误差）
        self.debit = _dec(self.debit)
        self.credit = _dec(self.credit)


@dataclass
class JournalEntry:
    """一张会计分录（头 + 多行）。source_* 追溯到来源凭证；status 走生命周期状态机。"""
    date: str                        # 记账日期（ISO）
    memo: str
    lines: List[JournalLine] = field(default_factory=list)
    source_kind: str = ""            # invoice / statement / manual / opening / closing
    source_hash: str = ""            # 来源凭证 file_hash（幂等键：一来源至多一张）
    source_ref: str = ""             # 发票号 / 摘要等
    entry_no: str = ""               # 凭证字号 YYYYMM-NNNN（过账时分配）
    status: str = "Draft"            # Draft → Approved → Posted（→ Reversed 红冲）
    created_by: str = ""
    created_at: str = ""

    def totals(self) -> Tuple[Decimal, Decimal]:
        dr = sum((l.debit for l in self.lines), ZERO)
        cr = sum((l.credit for l in self.lines), ZERO)
        return dr, cr

    def is_balanced(self) -> bool:
        dr, cr = self.totals()
        return dr == cr and dr > ZERO

    def cash_delta(self) -> Decimal:
        """本分录对现金及等价物口径的净流入（>0 流入 <0 流出；0=不动现金或内部腾挪）。"""
        from . import accounts as _A
        return sum((l.debit - l.credit for l in self.lines if _A.is_cash(l.account)), ZERO)

    def assert_balanced(self) -> None:
        dr, cr = self.totals()
        if dr != cr:
            raise ValueError(f"借贷不平，拒绝过账：{self.memo} 借={dr} 贷={cr}（差额 {dr - cr}）")
        if dr <= ZERO:
            raise ValueError(f"分录金额为 0，拒绝过账：{self.memo}")


class Ledger:
    """内存账套：过账（含平衡硬校验）+ 科目余额 + 试算平衡。持久化在 ledger.service/db。"""

    def __init__(self):
        self.entries: List[JournalEntry] = []
        self._dr: Dict[str, Decimal] = defaultdict(lambda: ZERO)
        self._cr: Dict[str, Decimal] = defaultdict(lambda: ZERO)

    def post(self, entry: JournalEntry) -> None:
        entry.assert_balanced()          # 硬校验：不平不过账（绝不凑平）
        self.entries.append(entry)
        for l in entry.lines:
            self._dr[l.account] += l.debit
            self._cr[l.account] += l.credit

    def net(self, account: str) -> Decimal:
        """借-贷净额（资产/费用正常为正；负债/权益/收入取相反看）。"""
        return self._dr[account] - self._cr[account]

    def trial_balance(self) -> Tuple[Decimal, Decimal, List[Tuple[str, Decimal, Decimal]]]:
        """返回 (总借, 总贷, [(科目, 借合计, 贷合计)…])。平衡时总借==总贷。"""
        accts = sorted(set(self._dr) | set(self._cr))
        rows = [(a, self._dr[a], self._cr[a]) for a in accts]
        return sum(self._dr.values(), ZERO), sum(self._cr.values(), ZERO), rows


def _dec(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if v is None or v == "":
        return ZERO
    return Decimal(str(v))
