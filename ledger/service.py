"""总账服务：人工触发的入账闸门 + 从已过账分录重建账套（试算平衡/科目余额）。

红线：**AI 绝不自动入账**。post_invoice 只在人工审核通过（approve_status='Approved'）后、
由人显式调用（API/CLI）时才生成并过账分录——本函数只做"闸门 + 生成 + 落库"，不在解析管道里自动跑。
MVP 单用户：审核通过即一步"Approve & Post"。
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import List, Optional, Tuple

from core import db
from core.models import Invoice
from . import accounts as A
from . import posting, settlement, store
from .engine import ZERO, Ledger


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def post_invoice(inv: Invoice, by: str = "user",
                 direction: Optional[str] = None,
                 own_company: Optional[str] = None) -> str:
    """把一张【已审核通过】的发票过账为应计分录，返回凭证号。

    闸门：approve_status 必须为 'Approved'；否则拒绝（AI/未审核的一律不许入账）。
    幂等：同一发票已入账则抛错（由 store.post_entry 保证）。
    """
    status = (getattr(inv, "approve_status", "") or "").lower()
    if status != "approved":
        raise ValueError(f"发票未审核通过（approve_status={inv.approve_status!r}），拒绝入账")
    if (getattr(inv, "doc_type", "invoice") or "invoice") != "invoice":
        raise ValueError("只有发票可走应计入账，流水请走结算/资金匹配")
    entry = posting.accrual_entry(inv, direction=direction, own_company=own_company)
    return store.post_entry(entry, by=by, at=_now())


def post_invoice_by_hash(file_hash: str, by: str = "user", **kw) -> str:
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise ValueError(f"发票不存在：{file_hash}")
    return post_invoice(inv, by=by, **kw)


def settle_invoice(file_hash: str, cash_amount, diff_reason: Optional[str] = None,
                   diff_account: Optional[str] = None, settle_amount=None,
                   cash_account: str = A.BANK, tolerance=None, activity: Optional[str] = None,
                   date: str = "", by: str = "reviewer") -> str:
    """人工触发：对一张【已入账】发票做资金结算（第二段），返回结算凭证号。

    - 未结额取自明细辅助账（逐单据累计已结）；默认全额结清 open_amount。
    - 差额（票面-现金）方向机械确定；用户给 diff_reason（fee/withholding_*/discount/rounding）
      或直接给 diff_account；无差额时不需要。
    - 舍入容差：给 tolerance 且差额在阈值内、又没给科目 → 自动入舍入差异兜底；超阈值拒绝。
    """
    info = settlement.open_amount(file_hash)
    if info is None:
        raise ValueError("该发票尚未入账（无已过账应计分录），不能结算")
    open_amt, direction, _gross = info
    if open_amt <= ZERO:
        raise ValueError("该发票已结清，无未结余额")

    settle_amt = _dec_or(settle_amount, open_amt)
    if settle_amt <= ZERO:
        raise ValueError("结算票面额必须为正")
    if settle_amt > open_amt:
        raise ValueError(f"结算票面额 {settle_amt} 超过未结额 {open_amt}")

    cash = _dec_or(cash_amount, None)
    if cash is None:
        raise ValueError("必须提供实收/付现金金额")
    diff = settle_amt - cash

    acct = diff_account
    if diff != ZERO and not acct:
        if diff_reason:
            acct = A.DIFF_REASONS.get(diff_reason)
            if not acct:
                raise ValueError(f"未知差额原因：{diff_reason}")
        elif tolerance is not None and abs(diff) <= _dec_or(tolerance, ZERO):
            acct = A.ROUNDING          # 微差入舍入差异兜底
        else:
            raise ValueError(
                f"差额 {diff} 未指定承接科目（手续费/预扣税/折扣/舍入），拒绝结算——不自动凑平")

    ref = (db.get_invoice(file_hash).f("invoice_no").value if db.get_invoice(file_hash) else "") or file_hash[:8]
    memo = f"结算·{'应付' if direction == settlement.AP else '应收'} {ref}"
    use_date = date or settlement.accrual_date(file_hash)   # 未给结算日期则沿用应计日期（避免落 0000-00 期间）
    act = activity or settlement.infer_activity(settlement.accrual_nature(file_hash))  # 现金流活动：默认按发票性质推断，可覆盖
    entry = settlement.settlement_entry(
        direction=direction, settle_amount=settle_amt, cash_amount=cash,
        diff_account=acct, cash_account=cash_account, date=use_date, memo=memo,
        source_hash=file_hash, source_ref=ref)
    return store.post_entry(entry, by=by, at=_now(), settle_amount=settle_amt, activity=act)


def _dec_or(v, default):
    if v is None or v == "":
        return default
    return v if isinstance(v, Decimal) else Decimal(str(v))


def load_ledger() -> Ledger:
    """从所有影响余额的分录重建内存账套，用于试算平衡/科目余额。

    含 Posted 与 Reversed：红冲不删除原分录（原分录保留 + 增一张红字冲销分录，二者相抵），
    故被红冲的原分录金额仍留在账上、由其反向冲销分录抵消——只排除 Draft/Approved。
    """
    led = Ledger()
    for e in store.entries_for_balance(limit=1000000):
        led.post(e)
    return led


def trial_balance() -> Tuple[Decimal, Decimal, List[Tuple[str, Decimal, Decimal]], bool]:
    """返回 (总借, 总贷, 明细行, 是否平衡)。"""
    dr, cr, rows = load_ledger().trial_balance()
    return dr, cr, rows, dr == cr


# ---------- 视图（供 gateway/前端）----------

def entries_view(limit: int = 500) -> List[dict]:
    """已过账分录列表（含红冲），供前端展示。"""
    out = []
    for e in store.entries_for_balance(limit=limit):
        dr, cr = e.totals()
        out.append({
            "entry_no": e.entry_no, "date": e.date, "memo": e.memo,
            "source_kind": e.source_kind, "source_ref": e.source_ref,
            "source_hash": e.source_hash, "status": e.status,
            "total": str(dr), "reverses_id": getattr(e, "reverses_id", None),
            "lines": [{"account": l.account, "debit": str(l.debit),
                       "credit": str(l.credit), "memo": l.memo or ""}
                      for l in e.lines],
        })
    return out


def trial_balance_view() -> dict:
    dr, cr, rows, ok = trial_balance()
    return {
        "total_debit": str(dr), "total_credit": str(cr), "balanced": ok,
        "rows": [{"account": a, "debit": str(d), "credit": str(c),
                  "net": str(d - c)} for a, d, c in rows if d or c],
    }


def postable_invoices() -> List[dict]:
    """已审核通过、但尚未入账的发票（待人工触发过账）。"""
    out = []
    for inv in load_all_invoices_approved():
        if store.existing_posted("invoice", inv.file_hash):
            continue
        try:
            e = posting.accrual_entry(inv)          # 预览建议分录（不落库）
            preview = {"direction": posting.infer_direction(inv),
                       "total": str(e.totals()[0]),
                       "lines": [{"account": l.account, "debit": str(l.debit),
                                  "credit": str(l.credit)} for l in e.lines]}
        except Exception as ex:
            preview = {"error": str(ex)}
        out.append({
            "file_hash": inv.file_hash,
            "invoice_no": inv.f("invoice_no").value or "",
            "issuer": inv.f("issuer_name").value or "",
            "date": inv.f("invoice_date").value or "",
            "total_due": inv.f("total_due").value or "",
            "preview": preview,
        })
    return out


def load_all_invoices_approved() -> List[Invoice]:
    from review import service as _rev
    return [inv for inv in db.load_all_invoices().values()
            if (inv.approve_status or "") == _rev.APPROVED
            and (getattr(inv, "doc_type", "invoice") or "invoice") == "invoice"]


def open_view() -> List[dict]:
    """待结算发票（有未结余额的已过账发票）。"""
    return settlement.open_invoices()


def control_view() -> dict:
    """控制账户对账：应付/应收总账余额 vs 明细未结合计。"""
    return settlement.control_reconciliation()


def summary() -> dict:
    tb = trial_balance_view()
    ctl = control_view()
    return {
        "posted": len([e for e in store.entries_for_balance() if e.status == "Posted"]),
        "reversed": len([e for e in store.entries_for_balance() if e.status == "Reversed"]),
        "postable": len(postable_invoices()),
        "open": len(open_view()),
        "balanced": tb["balanced"],
        "control_ok": ctl["AP"]["ok"] and ctl["AR"]["ok"],
        "total_debit": tb["total_debit"], "total_credit": tb["total_credit"],
    }
