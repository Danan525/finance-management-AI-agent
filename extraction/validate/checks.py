"""发票内部校验（计划第六节 1~10、对应异常写入 Validation Issues）。

金额与小数点为最高风险字段。校验容忍字段缺失（如样例2无 Subtotal/Tax）。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import List, Optional

from core.models import Invoice
from ..parse import amount as amt
from ..parse import generic as g

ZERO = Decimal("0.00")

# 折扣 / 押金 / 预付 / 抵扣 / 舍入等**调整行**：这些会让 Subtotal+Tax ≠ Total（属正常，非错误）
_ADJ_RE = re.compile(
    r"(discount|rebate|\bless\b|deposit|prepaid|pre[\s-]*payment|advance|paid\s*(?:to\s*date|so\s*far)?|"
    r"credit|rebate|loyalty|adjustment|rounding|round\s*off|"
    # 非税附加项（增项）：运费/手续费/保险/小费/规费/滞纳金/利息等——sub+tax≠total 属正常，能凑平则不报错
    r"shipping|freight|delivery|handling|insurance|postage|gratuity|\btip\b|surcharge|"
    r"\bfee\b|\blevy\b|\bduty\b|regulatory|penalty|interest|\blate\b|"
    r"折扣|优惠|已付|预付|定金|押金|抵扣|尾差|舍入|积分|返现|运费|邮费|快递费|手续费|保险费|小费|附加费|"
    r"规费|滞纳金|滞纳|利息|附加税|服务费)",
    re.IGNORECASE)


def _dec(inv: Invoice, key: str) -> Optional[Decimal]:
    v = inv.f(key).value
    return v if isinstance(v, Decimal) else None


def _adjustment_amounts(inv: Invoice) -> List[Decimal]:
    """从原文里找折扣/押金/预付/舍入等**调整行**上的金额（取绝对值）——用于 Total 关系调平。

    金额可能与标签同行，也可能（PDF 抽取常见）在下一行（"Discount (10%)"↓"-$500.00"）；先剔除
    百分比再找钱数，避免把税率 "10%" 当调整额。每个调整标签只取一个金额。
    """
    text = inv.raw_pdf_text or inv.raw_ocr_text or ""
    lines = text.splitlines()
    vals: List[Decimal] = []
    for i, ln in enumerate(lines):
        if not _ADJ_RE.search(ln):
            continue
        cands = [ln] + ([lines[i + 1]] if i + 1 < len(lines) else [])
        for c in cands:
            c = re.sub(r"\d+(?:\.\d+)?\s*%", "", c)             # 去掉 "(10%)" 之类
            m = g._MONEY.search(c)
            if m:
                v = amt.parse_amount(m.group(0))[0]
                if v is not None and v != ZERO:
                    vals.append(abs(v))
                    break
    return vals


def run_checks(inv: Invoice, duplicate_of: Optional[str] = None) -> None:
    """执行全部内部校验，结果写入 inv.issues，并设定 validation_status。"""
    _check_amount_format(inv)
    _check_decimal_places(inv)
    _check_total_relation(inv)
    _check_tax_rate(inv)
    _check_payment_due(inv)
    _check_line_items_sum(inv)
    _check_currency(inv)
    _check_dates(inv)
    _check_duplicate(inv, duplicate_of)
    _check_required_fields(inv)
    _check_wallet_presence(inv)
    _check_multiple_payments(inv)

    has_err = any(i.severity in ("error", "critical") for i in inv.issues)
    inv.validation_status = "has_issues" if inv.issues else "passed"
    if has_err:
        inv.validation_status = "has_issues"


# 1. 金额格式校验
def _check_amount_format(inv: Invoice) -> None:
    for key in ("subtotal", "sales_tax", "total_due", "payment_due"):
        fv = inv.f(key)
        if fv.raw and fv.value is None:
            inv.add_issue("AMOUNT_FORMAT", f"{key} 金额格式异常: {fv.raw}", key, "error")
        elif fv.suspicious:
            inv.add_issue("AMOUNT_SUSPICIOUS", f"{key} 金额含可疑字符: {fv.raw} ({fv.note})", key, "warning")


# 2. 小数位校验（不强制两位；非两位时验证是否提取错误）
def _check_decimal_places(inv: Invoice) -> None:
    import re
    cross = re.sub(r"[\s$,€£]", "", inv.cross_engine_text or "")
    for key in ("subtotal", "sales_tax", "total_due", "payment_due"):
        fv = inv.f(key)
        if not fv.raw:
            continue
        dp = amt.decimal_places(fv.raw)
        if dp == 2:
            continue  # 标准两位，直接通过

        core = amt.normalize_for_match(fv.raw)
        # 双引擎一致 = 第二引擎文本里能找到完全相同的数字串 -> 大概率是真实值而非误识别
        agrees = bool(core) and bool(cross) and core in cross

        if dp is None:
            inv.add_issue("DECIMAL_MISSING",
                          f"{key} 金额无小数点: {fv.raw}，请确认是否漏识别小数位", key, "warning")
        elif dp == 1:
            sev = "warning" if agrees else "error"
            tail = "（双引擎一致，可能确为一位小数）" if agrees else "（双引擎不一致/无法核对，疑似漏位，需复核）"
            inv.add_issue("DECIMAL_NONSTANDARD",
                          f"{key} 仅 1 位小数: {fv.raw}{tail}", key, sev)
        else:  # dp >= 3
            sev = "info" if agrees else "warning"
            tail = ("（双引擎一致，判定为真实高精度金额）" if agrees
                    else "（请确认为高精度金额还是小数识别错误）")
            inv.add_issue("DECIMAL_NONSTANDARD",
                          f"{key} 为 {dp} 位小数: {fv.raw}{tail}", key, sev)


# 3. 总额关系: Subtotal + Sales Tax == TOTAL DUE
def _check_total_relation(inv: Invoice) -> None:
    sub = _dec(inv, "subtotal")
    tax = _dec(inv, "sales_tax")
    total = _dec(inv, "total_due")
    if total is None:
        return
    if sub is not None:
        expect = sub + (tax or ZERO)
        # 容忍 ±0.01 税额四舍五入（如 Subtotal 422.75 × 6% = 25.365，税额印 25.36 而总额按未舍入算
        # → 差 0.01，属发票正常舍入，非错误）。与明细合计校验的容差一致。
        gap = abs(expect - total)
        if gap > Decimal("0.01"):
            # 折扣/押金/预付/舍入等调整行会让 Subtotal+Tax ≠ Total（正常）：若这些调整额能凑出差额则不报错
            adj = _adjustment_amounts(inv)
            reconciled = adj and (
                any(abs(a - gap) <= Decimal("0.01") for a in adj)          # 单个调整项 == 差额
                or abs(sum(adj) - gap) <= Decimal("0.01"))                 # 多个调整项之和 == 差额
            if not reconciled:
                inv.add_issue("TOTAL_MISMATCH",
                              f"Subtotal({sub})+Tax({tax or ZERO})={expect} ≠ TOTAL DUE({total})",
                              "total_due", "error")
            else:
                # **不完全静默**：差额虽被调整行解释，仍留一条 info 痕迹——万一那笔"调整额"其实是
                # 无关金额的巧合、掩盖了真实提取错误，审核人可据此复核（不升风险、不触发强制复核）。
                inv.add_issue("TOTAL_ADJUSTED",
                              f"Subtotal({sub})+Tax({tax or ZERO})={expect} ≠ TOTAL DUE({total})，"
                              f"差额 {gap} 已由折扣/押金/预付等调整行解释，请核对",
                              "total_due", "info")
    # sub 缺失（如样例2只有 TOTAL DUE）-> 不报错，留待人工确认


# 4. 税率关系: Tax Rate 0.00% -> Sales Tax 必须为 0
def _check_tax_rate(inv: Invoice) -> None:
    rate_raw = inv.f("tax_rate").raw
    tax = _dec(inv, "sales_tax")
    if rate_raw and rate_raw.replace(" ", "").startswith("0.00%"):
        if tax is not None and tax != ZERO:
            inv.add_issue("TAX_RATE_CONFLICT",
                          f"Tax Rate 为 0.00% 但 Sales Tax={tax} 非零", "sales_tax", "error")


# 5. Payment Due 与 TOTAL DUE 一致
def _check_payment_due(inv: Invoice) -> None:
    pd = _dec(inv, "payment_due")
    total = _dec(inv, "total_due")
    if pd is not None and total is not None and pd != total:
        # 仅单明细时二者应一致
        if len(inv.line_items) <= 1:
            inv.add_issue("PAYMENT_DUE_MISMATCH",
                          f"明细 Payment Due({pd}) ≠ TOTAL DUE({total})", "payment_due", "warning")


# 6. 明细合计校验
def _check_line_items_sum(inv: Invoice) -> None:
    # 明细行缺金额：不丢弃该行，但提示人工复核
    missing = [li for li in inv.line_items if li.amount is None]
    if missing:
        inv.add_issue("LINE_NO_AMOUNT",
                      f"{len(missing)} 条明细行未识别到金额，已保留描述待人工复核",
                      "line_items", "warning")
    # 明细合计应等于**净额（小计）**，而非含税的 TOTAL DUE（含税差异由 #3 总额关系另行校验）。
    # 净额目标优先级：subtotal → (total − tax) → total。
    subtotal = _dec(inv, "subtotal")
    total = _dec(inv, "total_due")
    tax = _dec(inv, "sales_tax")
    if subtotal is not None:
        target, name = subtotal, "Subtotal"
    elif total is not None:
        target, name = total - (tax or ZERO), ("TOTAL DUE−税" if tax else "TOTAL DUE")
    else:
        return
    if not inv.line_items:
        return
    s = ZERO
    for li in inv.line_items:
        if li.amount is None:
            return                       # 有缺金额明细，跳过合计校验（已由 LINE_NO_AMOUNT 提示）
        s += li.amount
    if abs(s - target) > Decimal("0.01"):
        inv.add_issue("LINE_SUM_MISMATCH",
                      f"明细合计({s}) ≠ {name}({target})", "subtotal", "error")


# 7. 币种一致性 + 显示符号/结算币种拆分
def _check_currency(inv: Invoice) -> None:
    symbol = inv.f("currency_display_symbol").value
    settle = inv.f("currency_settlement").value
    if symbol and settle and symbol == "$" and settle.upper() not in ("USD",):
        inv.add_issue("CURRENCY_SPLIT",
                      f"显示符号为 {symbol} 但结算币种为 {settle}，已拆分，不按美元结算（待确认）",
                      "currency_settlement", "warning")
    if settle and "/" in settle:
        inv.add_issue("CURRENCY_AMBIGUOUS",
                      f"结算币种存在多选 {settle}，需人工确认实际结算币种",
                      "currency_settlement", "warning")


# 8. 重复文件校验（哈希 / 发票号）
# 7b. 日期 日/月 歧义（05/06 这类）——已按默认解读但提示人工核对，避免月/日悄悄记反
def _check_dates(inv: Invoice) -> None:
    for key in ("invoice_date", "payment_due_date", "service_start", "service_end"):
        fv = inv.f(key)
        if fv.value and fv.suspicious and "歧义" in (fv.note or ""):
            inv.add_issue("DATE_AMBIGUOUS",
                          f"{key} 日/月有歧义（{fv.raw}），已按默认解读为 {fv.value}，请核对",
                          key, "warning")


def _check_duplicate(inv: Invoice, duplicate_of: Optional[str]) -> None:
    if duplicate_of:
        inv.add_issue("DUPLICATE", f"疑似重复发票，已存在记录: {duplicate_of}", None, "error")


# 9. 关键字段完整性
def _check_required_fields(inv: Invoice) -> None:
    if not inv.f("invoice_no").value:
        inv.add_issue("MISSING_INVOICE_NO", "缺少发票号", "invoice_no", "error")
    if not inv.f("invoice_date").value:
        inv.add_issue("MISSING_DATE", "缺少或无法识别发票日期", "invoice_date", "error")
    if not inv.f("issuer_name").value and not inv.f("customer_name").value:
        inv.add_issue("MISSING_PARTY", "缺少开票方与收票方", None, "error")
    if _dec(inv, "total_due") is None:
        inv.add_issue("MISSING_TOTAL", "缺少或无法识别总金额", "total_due", "error")


# 10. 钱包地址存在性（格式校验在抽取阶段已做）
def _check_wallet_presence(inv: Invoice) -> None:
    if not inv.payments:
        inv.add_issue("NO_PAYMENT_INFO", "未识别到付款钱包地址", "wallet_address", "info")


# 11. 多付款方式：存在两个及以上收款去向时，付款前须重点核对应付给哪一方
def _check_multiple_payments(inv: Invoice) -> None:
    targets = inv.distinct_payment_targets()
    if len(targets) >= 2:
        methods = "；".join(f"{label}: {addr or '（无地址）'}" for label, addr in targets)
        inv.add_issue("MULTI_PAYMENT_METHOD",
                      f"检测到 {len(targets)} 个付款方式/收款去向，付款前须重点核对应支付给哪一方 → {methods}",
                      "payments", "warning")
