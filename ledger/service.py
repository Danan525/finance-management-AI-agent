"""总账服务：人工触发的入账闸门 + 从已过账分录重建账套（试算平衡/科目余额）。

红线：**AI 绝不自动入账**。post_invoice 只在人工审核通过（approve_status='Approved'）后、
由人显式调用（API/CLI）时才生成并过账分录——本函数只做"闸门 + 生成 + 落库"，不在解析管道里自动跑。
MVP 单用户：审核通过即一步"Approve & Post"。
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import List, Optional, Tuple

from core import config, db
from core.models import Invoice
from . import accounts as A
from . import close, opening, posting, settlement, store
from .engine import ZERO, JournalEntry, JournalLine, Ledger, _dec


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _invoice_currency(inv) -> str:
    """发票币种规范化为大写 ISO 码；空/美元符号视为 USD。"""
    raw = (inv.f("currency_settlement").value or inv.f("invoice_ccy_raw").value or "").strip().upper()
    if raw in ("", "USD", "US$", "$", "＄"):
        return "USD"
    return raw


def post_invoice(inv: Invoice, by: str = "user",
                 direction: Optional[str] = None,
                 own_company: Optional[str] = None,
                 tax_deductible: Optional[bool] = None,
                 as_of: Optional[str] = None) -> str:
    """把一张【已审核通过】的发票过账为应计分录，返回凭证号。

    闸门：approve_status 必须为 'Approved'；否则拒绝（AI/未审核的一律不许入账）。
    **币种闸门 / 换算**：发票币种 ≠ 功能货币（USD）时——查该发票日期的汇率（`core.fx`，人工录入·固定）：
    有汇率则按入账日汇率**换算成功能货币入账**（外币原值+汇率留档在凭证摘要）；**无汇率仍拒绝**
    （防外币金额被静默当 USD 记账）。汇兑损益（结算/期末重估）属后续增量。
    tax_deductible：进项税可抵扣性（None → config 默认）；见 posting.accrual_entry。
    """
    status = (getattr(inv, "approve_status", "") or "").lower()
    if status != "approved":
        raise ValueError(f"发票未审核通过（approve_status={inv.approve_status!r}），拒绝入账")
    if (getattr(inv, "doc_type", "invoice") or "invoice") != "invoice":
        raise ValueError("只有发票可走应计入账，流水请走结算/资金匹配")
    ccy = _invoice_currency(inv)
    func = getattr(config, "FUNCTIONAL_CURRENCY", "USD")
    fx_note = ""
    if ccy != func:
        # 汇率取值时点 = **人工审核通过、录入系统的北京时间日期**；审计时间戳仍用 UTC。
        from core import fx
        as_of = as_of or fx.today()
        inv, rate, eff = _to_functional_invoice(inv, ccy, func, as_of)   # 换算副本（无汇率则抛）
        if eff == as_of:
            fx_note = f"（原币 {ccy} @ {rate} → {func}，录入日 {as_of} 汇率）"
        else:
            # 用了"≤录入日最近可用"汇率（当日官方汇率暂不可得/离线）——如实标注真实汇率日 + 陈旧天数，勿误标今天
            days = _days_between(eff, as_of)
            fx_note = f"（原币 {ccy} @ {rate} → {func}，⚠汇率日 {eff}（非录入日 {as_of}，陈旧 {days} 天））"
    entry = posting.accrual_entry(inv, direction=direction, own_company=own_company,
                                  tax_deductible=tax_deductible)
    if fx_note:
        entry.memo = (entry.memo + " " + fx_note).strip()
    return store.post_entry(entry, by=by, at=_now())


def _days_between(d1: str, d2: str) -> int:
    try:
        a = _dt.date.fromisoformat(d1); b = _dt.date.fromisoformat(d2)
        return abs((b - a).days)
    except Exception:
        return 0


def _to_functional_invoice(inv: Invoice, ccy: str, func: str, as_of: str):
    """外币发票 → 金额换算成功能货币的**副本**（原发票不改）。汇率取 `as_of`（录入系统当日）。

    经 `fx.rate_with_date`：录入日==今天时会**主动拉当天**（不静默沿用旧汇率）；返回实际生效日 `eff`
    （可能早于 as_of=用了最近可用）。换算 total_due/subtotal/sales_tax（同汇率保留分；accrual 以 total
    为准、tax=total−sub 反推、逐项舍入不破坏借贷平衡）。无任何可用汇率则拒绝入账。返回 (副本, 汇率, 生效日)。
    """
    import copy
    from core import fx
    from core.models import FieldValue
    r, eff = fx.rate_with_date(ccy, as_of)
    if r is None:
        raise ValueError(
            f"外币发票（{ccy}）无 {as_of} 的汇率：请先更新汇率（Frankfurter 按日拉取）"
            f"或在「汇率」页手工录入 {ccy}→{func} 再入账")
    conv = copy.deepcopy(inv)
    for field in ("total_due", "subtotal", "sales_tax"):
        raw = inv.f(field).value
        v = _dec(raw)
        if raw is not None and str(raw).strip() != "" and v != ZERO:
            conv.set(field, FieldValue(value=str((v * r).quantize(Decimal("0.01")))))
    return conv, r, eff


def post_invoice_by_hash(file_hash: str, by: str = "user", **kw) -> str:
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise ValueError(f"发票不存在：{file_hash}")
    if not kw.get("direction") and not kw.get("own_company"):   # 未显式给方向→按登记的我方主体自动判 AR/AP
        own = _own_company_for(inv)
        if own:
            kw["own_company"] = own
    return post_invoice(inv, by=by, **kw)


def settle_invoice(file_hash: str, cash_amount, diff_reason: Optional[str] = None,
                   diff_account: Optional[str] = None, settle_amount=None,
                   cash_account: str = A.BANK, tolerance=None, activity: Optional[str] = None,
                   date: str = "", by: str = "reviewer", cash_currency: str = "") -> str:
    """人工触发：对一张【已入账】发票做资金结算（第二段），返回结算凭证号。

    - 未结额取自明细辅助账（逐单据累计已结）；默认全额结清 open_amount。
    - 差额（票面-现金）方向机械确定；用户给 diff_reason（fee/withholding_*/discount/rounding/fx_gain_loss）
      或直接给 diff_account；无差额时不需要。
    - 舍入容差：给 tolerance 且差额在阈值内、又没给科目 → 自动入舍入差异兜底；超阈值拒绝。
    - **外币付款**：给 `cash_currency`（≠功能货币）时，`cash_amount` 按**结算日**（`date`）汇率换算成功能货币；
      与票面（入账日汇率的账面 USD）之差即**已实现汇兑损益**（diff_reason='fx_gain_loss'）。无汇率则拒。
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
    func = getattr(config, "FUNCTIONAL_CURRENCY", "USD")
    cash_ccy = (cash_currency or func).strip().upper()
    cash_note = ""
    if cash_ccy != func:                       # 外币付款 → 按结算日汇率换算成功能货币
        # 外币下"清账面额"(账面 USD)与"实付现金"是不同汇率口径，不默认全额清账——否则部分付款
        # 忘传 settle_amount 会清空整张往来、把未付本金当汇兑损益（自检发现，2026-08-11 修）。
        if _dec_or(settle_amount, None) is None:
            raise ValueError(
                "外币结算必须显式提供清账面额（settle_amount，账面口径）——"
                "外币下它与实付现金是不同汇率口径，不默认全额，避免未付本金被误当汇兑损益")
        from core import fx
        conv = fx.to_functional(cash, cash_ccy, date or "")
        if conv is None:
            raise ValueError(
                f"外币付款（{cash_ccy}）无 {date or '结算日'} 的汇率：请先在「汇率」页录入再结算"
                f"（汇率人工录入·固定，不联网取价）")
        cash_note = f"（付 {cash_ccy} {cash} @ {fx.rate(cash_ccy, date or '')} → {func} {conv}）"
        cash = conv
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
    # L1:差额科目不得指向应付/应收控制账户——否则差额静默落进控制账户、不带明细依据、致对账漂移
    if acct and A.is_control(acct):
        raise ValueError(f"差额科目不能是往来控制账户（{acct}）；请选手续费/预扣税/折扣/舍入等")

    ref = (db.get_invoice(file_hash).f("invoice_no").value if db.get_invoice(file_hash) else "") or file_hash[:8]
    memo = f"结算·{'应付' if direction == settlement.AP else '应收'} {ref}"
    use_date = date or settlement.accrual_date(file_hash)   # 未给结算日期则沿用应计日期（避免落 0000-00 期间）
    act = activity or settlement.infer_activity(settlement.accrual_nature(file_hash))  # 现金流活动：默认按发票性质推断，可覆盖
    entry = settlement.settlement_entry(
        direction=direction, settle_amount=settle_amt, cash_amount=cash,
        diff_account=acct, cash_account=cash_account, date=use_date, memo=memo + cash_note,
        source_hash=file_hash, source_ref=ref)
    return store.post_entry(entry, by=by, at=_now(), settle_amount=settle_amt, activity=act)


def _dec_or(v, default):
    if v is None or v == "":
        return default
    return v if isinstance(v, Decimal) else Decimal(str(v))


def post_manual_entry(lines: List[dict], date: str, memo: str = "",
                      by: str = "reviewer", activity: Optional[str] = None,
                      source_ref: str = "", allow_control: bool = False,
                      counterparty: str = "") -> str:
    """人工新建并过账一张任意的手工记账凭证，返回凭证号。

    通用记账原语——期初建账、期末结转、非发票流水入账、调整/纠错都在其上构建。
    lines: [{"account": str, "debit": num?, "credit": num?, "memo": str?}, …]。
    复用 `store.post_entry` 的全部闸门:借贷平硬校验(不平拒绝)、动现金必标活动、凭证号分配。
    红线:AI 绝不自动造分录——本函数只在人显式调用(API/界面)时执行。

    **往来控制账户软护栏**：应付/应收（[[is_control]]）正常应由发票应计/结算生成，手工直接动它
    容易让"控制账户 == 明细辅助账"失去依据。故手工凭证含控制账户行时要求
    `allow_control=True`（人显式确认这是期初建账/往来调整）**且**给 `counterparty`（对手方，
    否则往来无从追踪）。护栏是"软"的——确认后放行，且该笔影响会被计入控制账户对账的明细侧
    （见 `settlement.control_reconciliation`），不再产生假告警。
    """
    if not lines or len(lines) < 2:
        raise ValueError("手工凭证至少需要两行(有借有贷)")
    jlines: List[JournalLine] = []
    for i, ln in enumerate(lines):
        acct = (ln.get("account") or "").strip()
        if not acct:
            raise ValueError(f"第 {i + 1} 行缺科目")
        if A.account_type(acct) is None:
            raise ValueError(f"第 {i + 1} 行科目无法归类(编码需 1资产/2负债/3权益/4收入/5·6费用 开头):{acct}")
        dr = _dec(ln.get("debit"))
        cr = _dec(ln.get("credit"))
        if dr < ZERO or cr < ZERO:
            raise ValueError(f"第 {i + 1} 行金额不能为负")
        if (dr > ZERO) == (cr > ZERO):      # 两者都>0 或 都=0 → 非法
            raise ValueError(f"第 {i + 1} 行必须借、贷其一为正(不可同时或均为空)")
        jlines.append(JournalLine(acct, debit=dr, credit=cr, memo=(ln.get("memo") or None)))

    ctl = [l.account for l in jlines if A.is_control(l.account)]
    if ctl and not allow_control:
        raise ValueError(
            "手工凭证含往来控制账户(%s)：应付/应收正常由发票应计/结算产生。"
            "如确为期初建账或往来调整，请显式确认(allow_control)并填写对手方" % "、".join(ctl))

    entry = JournalEntry(date=date, memo=memo or "手工凭证", lines=jlines,
                         source_kind="manual", source_ref=source_ref, status="Draft")
    return store.post_entry(entry, by=by, at=_now(), activity=activity,
                            counterparty=counterparty)


def _txn_currency(txn, stmt) -> str:
    """一笔银行流水的币种规范化：本笔币种优先、回退账户头币种；空/美元符号视为 USD。"""
    raw = (getattr(txn, "currency", None) or stmt.f("currency_settlement").value or "").strip().upper()
    if raw in ("", "USD", "US$", "$", "＄"):
        return "USD"
    return raw


def statement_txn_entry_key(stmt_hash: str, index: int) -> str:
    """一笔银行流水的入账幂等键（区别于发票级 file_hash）。"""
    return f"{stmt_hash}#{index}"


def posted_statement_indices(stmt_hash: str) -> set:
    """某银行流水中已【流水入账】(source_kind='statement')的交易下标集合。

    幂等键基于下标（`{hash}#{index}`），删除/重切分这些行会让下标漂移 → 已入账笔的键失配、
    可被重复入账（现金双记）。故审核侧编辑（增删/重切分）须像"已对账"一样**锁定这些行**。
    """
    out = set()
    inv = db.get_invoice(stmt_hash)
    n = len(inv.transactions or []) if inv else 0
    for i in range(n):
        if store.existing_posted("statement", statement_txn_entry_key(stmt_hash, i)):
            out.add(i)
    return out


def post_statement_entry(stmt_hash: str, index: int, counter_account: str,
                         activity: str, date: Optional[str] = None,
                         memo: str = "", by: str = "reviewer") -> str:
    """把一笔【没有对应发票】的银行流水入账：选一个对方科目，生成 Dr/Cr 银行 两行分录，返回凭证号。

    非发票现金收支（银行手续费/利息/缴税/工资转账等）——现金流量表完整与银行余额调节的前提。
    对方【有发票】的收付款不走这里（走发票应计 + 资金结算/据对账结算）。
    **方向机械确定**：流水为支出(expense)→ 银行减少（借 对方科目、贷 银行）；
    收入(income)→ 银行增加（借 银行、贷 对方科目）。金额取自银行流水事实、人只选对方科目与活动类别。
    **红线**：人显式触发（API/界面）才执行，AI 不自动入账；金额不由 AI 估算。
    **幂等**：同一 (stmt_hash,index) 已有未红冲入账则拒绝重复（红冲后可重入）。
    """
    stmt = db.get_invoice(stmt_hash)
    if stmt is None:
        raise ValueError(f"银行流水不存在：{stmt_hash}")
    if (getattr(stmt, "doc_type", "") or "") != "statement":
        raise ValueError("该来源不是银行流水（doc_type!='statement'），不能按流水入账")
    txns = stmt.transactions or []
    if not isinstance(index, int) or index < 0 or index >= len(txns):
        raise ValueError(f"流水行号越界：{index}（共 {len(txns)} 行）")
    txn = txns[index]

    ccy = _txn_currency(txn, stmt)
    func = getattr(config, "FUNCTIONAL_CURRENCY", "USD")

    exp = _dec(txn.expense) if txn.expense is not None else ZERO
    inc = _dec(txn.income) if txn.income is not None else ZERO
    if (exp > ZERO) == (inc > ZERO):
        raise ValueError("该流水行收/支金额缺失或同时为正，无法判定方向，请人工核对")
    amount = exp if exp > ZERO else inc

    fx_note = ""
    if ccy != func:                        # 外币流水按**交易日**汇率换算成功能货币（有汇率则换、无则拒）
        from core import fx
        txn_date = (getattr(txn, "date", None) or date or "")
        conv = fx.to_functional(amount, ccy, txn_date)
        if conv is None:
            raise ValueError(
                f"外币流水（{ccy}）无 {txn_date or '该日'} 的汇率：请先在「汇率」页录入 {ccy}→{func} 再入账"
                f"（汇率人工录入·固定，不联网取价）")
        fx_note = f"（原币 {ccy} {amount} @ {fx.rate(ccy, txn_date)} → {func} {conv}）"
        amount = conv

    ca = (counter_account or "").strip()
    if not ca:
        raise ValueError("请选择对方科目")
    if A.account_type(ca) is None:
        raise ValueError(f"对方科目无法归类（编码需 1资产/2负债/3权益/4收入/5·6费用 开头）：{ca}")
    if A.is_cash(ca):
        raise ValueError("对方科目不能是现金/银行（现金内部划转请走新建凭证并标明，不动现金流量）")
    if A.is_control(ca):
        raise ValueError("对方科目是往来控制账户（应付/应收）：有发票的收付款请走发票应计+结算，不走流水直入")

    if activity not in A.ACTIVITIES:
        raise ValueError("非发票流水动用现金，必须指定现金流活动类别（operating/investing/financing）")

    # H2 护栏：已对账到发票的流水，其现金应走「据对账结算」（settle），不能在此再直入 → 否则银行双记
    if db.confirmed_txn_kinds().get((stmt_hash, index)) == "reconciled":
        raise ValueError("该流水已对账到发票：请走「据对账结算」，不要在此重复入账（避免现金双记）")
    key = statement_txn_entry_key(stmt_hash, index)
    dup = store.existing_posted("statement", key)
    if dup:
        raise ValueError(f"该流水行已入账（{dup}），拒绝重复过账（如需更正请先红冲）")

    bank = A.BANK
    if exp > ZERO:      # 支出：银行减少
        lines = [JournalLine(ca, debit=amount), JournalLine(bank, credit=amount)]
    else:               # 收入：银行增加
        lines = [JournalLine(bank, debit=amount), JournalLine(ca, credit=amount)]

    desc = (getattr(txn, "description", None) or "").strip()
    entry = JournalEntry(date=(date or txn.date or ""),
                         memo=((memo or desc or "银行流水入账") + fx_note),
                         lines=lines, source_kind="statement", source_hash=key,
                         source_ref=desc[:120], status="Draft")
    return store.post_entry(entry, by=by, at=_now(), activity=activity)


def statement_lines_view(only_open: bool = True) -> List[dict]:
    """银行流水逐笔 + 入账/对账状态，供「流水入账」页。

    `only_open=True` 只返回**待入账**行：未入账、且未匹配到发票（匹配到发票的走据对账结算，
    不在此直入，以免重复动银行）。`no_invoice`（已确认无需发票的）仍可在此入账。
    """
    kinds = db.confirmed_txn_kinds()          # {(hash,index): 'reconciled'|'no_invoice'}
    out: List[dict] = []
    for h, inv in db.load_all_invoices().items():
        if (getattr(inv, "doc_type", "") or "") != "statement":
            continue
        for i, tx in enumerate(inv.transactions or []):
            if tx.expense is None and tx.income is None:
                continue
            is_out = tx.expense is not None and _dec(tx.expense) > ZERO
            amount = _dec(tx.expense) if is_out else _dec(tx.income)
            if amount <= ZERO:
                continue
            key = statement_txn_entry_key(h, i)
            posted = store.existing_posted("statement", key)
            rec = kinds.get((h, i))
            if only_open and (posted or rec == "reconciled"):
                continue
            out.append({
                "stmt_hash": h, "index": i, "date": tx.date or "",
                "description": (getattr(tx, "description", None) or "").strip(),
                "amount": str(amount), "direction": "out" if is_out else "in",
                "currency": _txn_currency(tx, inv),
                "posted": posted or "", "reconciled": rec or "",
            })
    out.sort(key=lambda r: (r["date"], r["stmt_hash"], r["index"]))
    return out


def _dec_opt(v):
    """空/非法 → None；否则 Decimal（银行自报余额常缺失，故可空）。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return v if isinstance(v, Decimal) else Decimal(str(v).replace(",", "").strip())
    except Exception:
        return None


def bank_reconciliation_view() -> dict:
    """银行余额调节：逐张银行流水单核对「自报期末余额」与「逐笔轧差」，并汇报入账进度。

    MVP 定位为**诊断报告、不记账**（差异由人解释——真实银行调节表的常态）：
    - `self_check`：流水自洽——期初 + Σ(收入−支出) == 期末（银行自报两余额都在时才校验，
      不符多为流水解析错）。**仅提示、不阻断**（不进出表勾稽 can_issue、不拦记账），
      是数据质量**自检指标**、不是能阻断的"闸门"。
    - 入账进度：每笔流水是「已据发票结算/已流水入账/待处理」三态之一，指引去「流水入账」处理待处理项。
    - **不跨单聚合银行侧余额**（closing 是时点值、跨账户/多期不能相加；避免错误聚合）；
      总账 1002 余额单列作参考。
    """
    from extraction.parse import amount as _amt
    led = load_ledger()
    ledger_bank = str(led.net(A.BANK))
    kinds = db.confirmed_txn_kinds()          # {(hash,index): 'reconciled'|'no_invoice'}
    stmts: List[dict] = []
    for h, inv in db.load_all_invoices().items():
        if (getattr(inv, "doc_type", "") or "") != "statement":
            continue
        ccy = (inv.f("currency_settlement").value or "USD").strip().upper() or "USD"
        opening = _dec_opt(inv.f("opening_balance").value)
        closing = _dec_opt(inv.f("closing_balance").value)
        in_tot = out_tot = ZERO
        n = posted = reconciled = open_n = 0
        for i, tx in enumerate(inv.transactions or []):
            if tx.expense is None and tx.income is None:
                continue
            n += 1
            is_out = tx.expense is not None and _dec(tx.expense) > ZERO
            if is_out:
                out_tot += _dec(tx.expense)
            elif tx.income is not None:
                in_tot += _dec(tx.income)
            if store.existing_posted("statement", statement_txn_entry_key(h, i)):
                posted += 1
            elif kinds.get((h, i)) == "reconciled":
                reconciled += 1
            else:
                open_n += 1
        net = in_tot - out_tot
        tol = _amt.match_tolerance(ccy)
        self_check = None
        if opening is not None and closing is not None:
            self_check = abs(opening + net - closing) <= tol
        stmts.append({
            "stmt_hash": h, "bank_name": (inv.f("bank_name").value or "").strip(),
            "bank_account_no": str(inv.f("bank_account_no").value or "").strip(),  # 供无名时兜底显示账号（与审核列表口径一致）
            "period_start": (inv.f("statement_period_start").value or "").strip(),
            "period_end": (inv.f("statement_period_end").value or "").strip(),
            "currency": ccy,
            "opening": (str(opening) if opening is not None else ""),
            "closing": (str(closing) if closing is not None else ""),
            "in_total": str(in_tot), "out_total": str(out_tot), "net": str(net),
            "self_check": self_check,      # True/False/None(缺余额无法校验)
            "txn_count": n, "posted": posted, "reconciled_unposted": reconciled,
            "open": open_n,
        })
    stmts.sort(key=lambda s: (s["period_end"] or s["period_start"], s["bank_name"]))
    total_open = sum(s["open"] for s in stmts)
    bad_self = sum(1 for s in stmts if s["self_check"] is False)
    return {"ledger_bank": ledger_bank, "statements": stmts,
            "total_open": total_open, "self_check_failures": bad_self}


def fx_revaluation_view(as_of_date: str = "") -> dict:
    """期末外币敞口重估**报告**（诊断，不记账）。

    账面已按入账日汇率抹平为功能货币；这里**从未结发票回溯原币敞口**，用期末（as_of）汇率重估，
    与账面之差 = **未实现汇兑损益**。对 AR（资产）升值为收益(+)、AP（负债）升值为损失(−)。
    定位同银行余额调节：**先诊断展示外币敞口**；生成重估调整分录（含未实现损益的下期冲回、
    与往来明细辅助账一致性）属后续增量——此处不记账。期初往来(opening)按功能货币录入、无原币，跳过。
    """
    from core import fx
    func = getattr(config, "FUNCTIONAL_CURRENCY", "USD")
    as_of = as_of_date or fx.today()
    items = []
    agg = {}         # ccy -> 累计
    for inv in settlement.open_invoices():
        if inv.get("kind") != "invoice":
            continue
        rec = db.get_invoice(inv["file_hash"])
        if rec is None:
            continue
        ccy = _invoice_currency(rec)
        if ccy == func:
            continue
        total_ccy = _dec(rec.f("total_due").value)
        gross = _dec(inv["gross"]); book = _dec(inv["open"])
        if total_ccy <= ZERO or gross <= ZERO or book <= ZERO:
            continue
        open_ccy = (total_ccy * book / gross).quantize(Decimal("0.01"))
        rate = fx.rate(ccy, as_of)
        direction = inv["direction"]
        row = {"file_hash": inv["file_hash"], "invoice_no": inv.get("invoice_no", ""),
               "direction": direction, "currency": ccy, "open_ccy": str(open_ccy),
               "book_usd": str(book)}
        a = agg.setdefault(ccy, {"open_ccy": ZERO, "book_usd": ZERO, "reval_usd": ZERO,
                                 "unrealized_pl": ZERO, "rate": None, "missing_rate": False})
        a["open_ccy"] += open_ccy; a["book_usd"] += book
        if rate is None:
            row.update({"rate": "", "reval_usd": "", "unrealized_pl": "", "missing_rate": True})
            a["missing_rate"] = True
        else:
            reval = (open_ccy * rate).quantize(Decimal("0.01"))
            diff = reval - book
            pl = diff if direction == settlement.AR else -diff      # AR 升值=收益, AP 升值=损失
            row.update({"rate": str(rate), "reval_usd": str(reval),
                        "unrealized_pl": str(pl), "missing_rate": False})
            a["rate"] = str(rate); a["reval_usd"] += reval; a["unrealized_pl"] += pl
        items.append(row)
    by_ccy = [{"currency": c, "open_ccy": str(v["open_ccy"]), "book_usd": str(v["book_usd"]),
               "reval_usd": str(v["reval_usd"]), "unrealized_pl": str(v["unrealized_pl"]),
               "rate": v["rate"], "missing_rate": v["missing_rate"]}
              for c, v in sorted(agg.items())]
    total_pl = sum((_dec(v["unrealized_pl"]) for v in agg.values()), ZERO)
    missing = sorted([c for c, v in agg.items() if v["missing_rate"]])
    return {"as_of": as_of, "functional": func, "items": items, "by_currency": by_ccy,
            "total_unrealized_pl": str(total_pl), "missing_rates": missing}


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
            "counterparty": getattr(e, "counterparty", None) or "",
            "total": str(dr), "reverses_id": getattr(e, "reverses_id", None),
            "lines": [{"account": l.account, "debit": str(l.debit),
                       "credit": str(l.credit), "memo": l.memo or ""}
                      for l in e.lines],
        })
    return out


def unmarked_cash_entries(limit: int = 100) -> List[dict]:
    """**历史遗留体检**：已过账分录里"动了现金却没有活动类别"的（现金流量表会漏计这笔）。

    `store.post_entry` 一直要求动现金必标活动，但判定依赖 `accounts.is_cash`——2026-08-03 前
    它认不出手填的 `"1002银行存款"`（编码贴名），那类分录得以在无活动类别的情况下落库。
    判定修好后，这些历史分录会让 **E3（CFS 期末现金 = BS 货币资金）真的不平、报表拒绝出表**；
    没有本清单，用户只看到"出不了表"却无从下手。处理方式：红冲后带活动类别重记。
    """
    out = []
    for e in store.entries_for_balance(limit=100000):
        if getattr(e, "activity", None):
            continue
        if e.cash_delta() == ZERO:
            continue
        out.append({"entry_no": e.entry_no, "date": e.date, "memo": e.memo,
                    "source_kind": e.source_kind,
                    "cash_delta": str(e.cash_delta()), "status": e.status})
        if len(out) >= limit:
            break
    return out


def trial_balance_view() -> dict:
    dr, cr, rows, ok = trial_balance()
    return {
        "total_debit": str(dr), "total_credit": str(cr), "balanced": ok,
        "rows": [{"account": a, "debit": str(d), "credit": str(c),
                  "net": str(d - c)} for a, d, c in rows if d or c],
    }


def _own_company_for(inv) -> Optional[str]:
    """若发票的开票方/收票方是**已登记为 self 的我方主体**，返回该名字，供方向自动判 AR/AP。
    未登记 self（或都不是我方）则返回 None → 方向沿用稳妥缺省 AP，仍可人工在待入账卡片覆盖。
    补齐缺口：此前 own_company 全靠调用方显式传，我方开的 AR 发票默认被当 AP。"""
    try:
        from core import counterparty as cp
        for field in ("issuer_name", "customer_name"):
            raw = (inv.f(field).value or "").strip()
            if not raw:
                continue
            p = cp.resolve(raw)
            if p and cp.has_role(p, "self"):
                return raw
    except Exception:
        pass
    return None


def postable_invoices() -> List[dict]:
    """已审核通过、但尚未入账的发票（待人工触发过账）。"""
    out = []
    for inv in load_all_invoices_approved():
        if store.existing_posted("invoice", inv.file_hash):
            continue
        try:
            own = _own_company_for(inv)
            e = posting.accrual_entry(inv, own_company=own)   # 预览建议分录（不落库）
            preview = {"direction": posting.infer_direction(inv, own),
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
            "total_due": ("" if inv.f("total_due").value in (None, "") else str(inv.f("total_due").value)),  # value 可能是 Decimal，务必转字符串（否则 JSONResponse 序列化 500）
            "preview": preview,
        })
    return out


def load_all_invoices_approved() -> List[Invoice]:
    from review import service as _rev
    return [inv for inv in db.load_all_invoices().values()
            if (inv.approve_status or "") == _rev.APPROVED
            and (getattr(inv, "doc_type", "invoice") or "invoice") == "invoice"]


def chart_accounts() -> List[dict]:
    """默认科目表(供手工凭证下拉);用户仍可手填表外但编码合规的科目。"""
    return [{"account": "%s %s" % (code, name), "code": code, "type": typ}
            for code, name, typ, _side, _rep in A.chart()]


def preview_opening_import(data: bytes, filename: str) -> dict:
    """解析上传的期初 Excel/CSV → 预览 {items, other_lines, errors}（不记账）。"""
    from . import opening_import
    return opening_import.parse_opening_file(data, filename)


def commit_opening_import(data: bytes, filename: str, date: str = "", by: str = "admin") -> dict:
    """解析并过账期初 Excel/CSV。有解析错误则拒绝（返回错误、不部分导入）。"""
    parsed = preview_opening_import(data, filename)
    if parsed["errors"]:
        raise ValueError("期初表有 %d 处错误，请先修正后再导入" % len(parsed["errors"]))
    if not parsed["items"] and not parsed["other_lines"]:
        raise ValueError("期初表没有可导入的有效行")
    res = post_opening(items=parsed["items"], other_lines=parsed["other_lines"], date=date, by=by)
    res["imported"] = {"items": len(parsed["items"]), "other_lines": len(parsed["other_lines"])}
    return res


def post_opening(items=None, other_lines=None, date: str = "", by: str = "admin") -> dict:
    """录入期初余额（建账）：往来逐户可结算 + 其它科目余额，对方科目统一 3200。"""
    return opening.post_opening(items=items, other_lines=other_lines, date=date, by=by)


def periods_view() -> List[dict]:
    """会计期间 + 关账状态（供界面）。"""
    return close.list_periods()


def close_period(period: str, by: str = "admin") -> dict:
    return close.close_period(period, by=by)


def reopen_period(period: str, by: str = "admin") -> dict:
    return close.reopen_period(period, by=by)


def open_view() -> List[dict]:
    """待结算发票（有未结余额的已过账发票）。"""
    return settlement.open_invoices()


def control_view() -> dict:
    """控制账户对账：应付/应收总账余额 vs 明细未结合计。"""
    return settlement.control_reconciliation()


def summary() -> dict:
    tb = trial_balance_view()
    ctl = control_view()
    unmarked = unmarked_cash_entries()
    return {
        "unmarked_cash": len(unmarked),      # 历史遗留：动现金却无活动类别（会让 E3 不平）
        "posted": len([e for e in store.entries_for_balance() if e.status == "Posted"]),
        "reversed": len([e for e in store.entries_for_balance() if e.status == "Reversed"]),
        "postable": len(postable_invoices()),
        "statement_open": len(statement_lines_view(only_open=True)),  # 无票银行行待入账（「流水入账」页），顶栏另列，别和发票待入账混
        "open": len(open_view()),
        "balanced": tb["balanced"],
        "control_ok": ctl["AP"]["ok"] and ctl["AR"]["ok"],
        "total_debit": tb["total_debit"], "total_credit": tb["total_credit"],
    }
