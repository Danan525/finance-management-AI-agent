"""过账规则：从一张（已审核的）发票生成**应计分录**（权责发生制，第一段）。

对齐总账计划：发票阶段一律挂应付/应收，**不直接记银行**（资金结算是第二段、后续增量）。
借贷强制相等：AP 场景 借费用+借进项税 = 贷应付(全额)；AR 场景 借应收(全额) = 贷收入+贷销项税。
金额缺税额明细时退化为单行费用/收入 = 全额，仍恒平。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from . import accounts as A
from .engine import ZERO, JournalEntry, JournalLine, _dec

# 方向：AP=我方收票（应付/费用）  AR=我方开票（应收/收入）
AP, AR = "AP", "AR"


def infer_direction(inv, own_company: Optional[str] = None) -> str:
    """判断发票方向。配了本方公司名则按开票/收票方匹配；否则默认 AP（收到的账单=费用）。

    返回 "AP"/"AR"。默认 AP 是本工具主流程（处理收到的发票/账单）的稳妥缺省，
    与总账计划"方向不明确则人工确认"一致——此处给出可被人工覆盖的建议值。
    """
    if own_company:
        own = own_company.strip().lower()
        issuer = (inv.f("issuer_name").value or "").strip().lower()
        customer = (inv.f("customer_name").value or "").strip().lower()
        if own and own in issuer:
            return AR      # 本方是开票方 → 应收
        if own and own in customer:
            return AP      # 本方是收票方 → 应付
    return AP


def _amounts(inv):
    """取 (subtotal, tax, total)。缺失以 Decimal 归零/回填，保证可平。"""
    total = _dec(inv.f("total_due").value)
    sub = _dec(inv.f("subtotal").value)
    tax = _dec(inv.f("sales_tax").value)
    if total <= ZERO:                       # 没有总额，退回 净额+税
        total = sub + tax
    if sub <= ZERO:                         # 没有净额：全额记费用/收入，税并入
        sub, tax = total, ZERO
    elif sub + tax != total:                # 净额+税 对不上总额：以总额为准，税=差额（可正可负→夹到0）
        tax = total - sub
        if tax < ZERO:
            sub, tax = total, ZERO
    return sub, tax, total


def accrual_entry(inv, direction: Optional[str] = None,
                  own_company: Optional[str] = None) -> JournalEntry:
    """生成一张应计分录（Draft）。direction 未指定则推断。不落库、不过账。"""
    direction = direction or infer_direction(inv, own_company)
    sub, tax, total = _amounts(inv)
    date = inv.f("invoice_date").value or inv.f("payment_due_date").value or ""
    no = inv.f("invoice_no").value or ""
    party = (inv.f("issuer_name").value if direction == AP
             else inv.f("customer_name").value) or ""

    if direction == AP:
        lines = [JournalLine(A.expense_account(inv), debit=sub, memo=party)]
        if tax > ZERO:
            lines.append(JournalLine(A.INPUT_TAX, debit=tax))
        lines.append(JournalLine(A.AP, credit=total, memo=party))
        memo = f"应计·应付 {no} {party}".strip()
    else:
        lines = [JournalLine(A.AR, debit=total, memo=party)]
        lines.append(JournalLine(A.REVENUE, credit=sub, memo=party))
        if tax > ZERO:
            lines.append(JournalLine(A.OUTPUT_TAX, credit=tax))
        memo = f"应计·应收 {no} {party}".strip()

    e = JournalEntry(
        date=date, memo=memo, lines=lines,
        source_kind="invoice", source_hash=getattr(inv, "file_hash", "") or "",
        source_ref=no, status="Draft",
    )
    e.assert_balanced()      # 生成即自检，不平立即暴露（不落库）
    return e
