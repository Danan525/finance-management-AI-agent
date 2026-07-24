"""置信度分级（计划第六节 11~14）。

整体 OCR 质量、关键字段、金额字段、小数点字符各有独立阈值。
文本型 PDF 直抽时置信度视为 1.0（高可信），分级自然落在最优档。
"""
from __future__ import annotations

from typing import List, Tuple

from core import config
from core.models import Invoice


def grade_overall(quality: float) -> Tuple[str, str]:
    """返回 (质量等级, review_level)。"""
    if quality >= config.OCR_QUALITY_EXCELLENT:
        return "Excellent", "normal"
    if quality >= config.OCR_QUALITY_GOOD:
        return "Good", "normal"
    if quality >= config.OCR_QUALITY_WARNING:
        return "Warning", "recheck_key_fields"
    return "HighRisk", "needs_review"


def assess_statement(inv: Invoice) -> None:
    """银行流水的置信度评估——**不套用发票必填字段**（invoice_no/日期/总额对流水无意义）。
    改按「有无逐笔交易 + 有无账户头」度量完整性，结构化直解视为高可信。
    """
    if inv.ocr_used:
        legibility = inv.ocr_quality
    else:                                    # 结构化/文本/Excel 直解：文本层清晰
        legibility = config.PDF_TEXT_CONFIDENCE
        inv.ocr_quality = legibility

    has_txn = bool(inv.transactions)
    acct_keys = ("bank_name", "bank_account_no", "statement_period_start",
                 "statement_period_end", "opening_balance", "closing_balance", "currency_settlement")
    has_acct = any(inv.f(k).value for k in acct_keys)

    inv.field_coverage = 1.0 if (has_txn and has_acct) else (0.7 if has_txn else 0.0)
    inv.key_field_confidence = legibility if has_txn else 0.0
    inv.amount_field_confidence = legibility if has_txn else 0.0
    inv.decimal_confidence = legibility if inv.ocr_used else config.PDF_TEXT_CONFIDENCE

    effective = min(legibility, inv.field_coverage)
    inv.ocr_quality_level = grade_overall(effective)[0]

    # 只在真正缺内容时给提示（STMT_EMPTY 另有专门提示，这里补账户头缺失的温和提醒）
    if inv.ocr_used and legibility < config.OCR_QUALITY_WARNING:
        inv.needs_manual_review = True
        inv.add_issue("OCR_QUALITY_LOW", f"整体 OCR 质量 {legibility:.2%} 偏低(High Risk)", None, "error")
    if has_txn and not has_acct:
        inv.add_issue("STMT_HEADER_MISSING",
                      "已识别逐笔交易，但未取到账户信息（银行/账号/期间/余额），建议人工补录", None, "warning")
    # 校验状态：流水不跑发票 checks，这里据是否有 error/critical 问题直接置位（否则永远停在 pending）
    inv.validation_status = "has_issues" if any(
        i.severity in ("error", "critical") for i in inv.issues) else "passed"


def assess(inv: Invoice) -> None:
    """计算并写入各档置信度与等级；标记需二次识别/人工审核的字段。

    关键修正（避免"清晰=读懂"的假高分）：
    - **字符清晰度**（legibility）与 **字段覆盖率**（coverage）分开度量；
    - 覆盖率把"没抽到的必填字段记 0、不剔除"——抽不到就拉低分，绝不虚高；
    - 整体等级 = min(清晰度, 覆盖率)：清晰但没读懂 ≠ Excellent。
    """
    # 字符清晰度：OCR 路径用 OCR 平均置信度；PDF 文本直抽=1.0（只代表"文本层清晰"）
    if inv.ocr_used:
        legibility = inv.ocr_quality
    else:
        legibility = config.PDF_TEXT_CONFIDENCE
        inv.ocr_quality = legibility

    # 字段覆盖率：必填身份字段实际抓到的比例（缺失记 0、不剔除）
    req = config.REQUIRED_FIELDS
    missing_req = [k for k in req if inv.f(k).raw in (None, "")]
    coverage = (len(req) - len(missing_req)) / len(req) if req else 1.0
    inv.field_coverage = coverage

    # 关键字段置信度：必填字段缺失即记 0；否则取已抽字段置信度最小值
    # （启发式兜底抽到的字段置信度 0.90 会落在此，自然触发复核——不再虚高 100%）
    present_confs: List[float] = [inv.f(k).confidence for k in config.KEY_FIELDS if inv.f(k).raw]
    inv.key_field_confidence = 0.0 if missing_req else (min(present_confs) if present_confs else legibility)

    # 金额字段置信度
    amt_confs: List[float] = [inv.f(k).confidence for k in config.AMOUNT_FIELDS if inv.f(k).raw]
    inv.amount_field_confidence = min(amt_confs) if amt_confs else legibility

    # 小数点字符置信度（OCR 路径才有意义；PDF 文本=1.0）
    inv.decimal_confidence = legibility if inv.ocr_used else config.PDF_TEXT_CONFIDENCE

    # 整体等级 = min(清晰度, 覆盖率, 关键字段置信度)：清晰但没读懂/靠启发式 ≠ Excellent
    effective = min(legibility, coverage, inv.key_field_confidence)
    level, review_level = grade_overall(effective)
    inv.ocr_quality_level = level

    # ---- 据各维度产出问题与复核标记 ----
    if missing_req:
        inv.needs_manual_review = True
        inv.add_issue("FIELD_COVERAGE_LOW",
                      f"必填字段缺失 {missing_req}，覆盖率 {coverage:.0%}——提取不完整，不可视为高可信",
                      None, "error")
    if inv.ocr_used and legibility < config.OCR_QUALITY_WARNING:
        inv.needs_manual_review = True
        inv.add_issue("OCR_QUALITY_LOW", f"整体 OCR 质量 {legibility:.2%} 偏低(High Risk)", None, "error")
    elif inv.ocr_used and legibility < config.OCR_QUALITY_GOOD:
        inv.add_issue("OCR_QUALITY_WARN", f"整体 OCR 质量 {legibility:.2%} 触发关键区域二次识别", None, "warning")

    if inv.key_field_confidence < config.KEY_FIELD_RECHECK:
        inv.needs_manual_review = True
        inv.add_issue("KEY_FIELD_LOW",
                      f"关键字段置信度 {inv.key_field_confidence:.2%} 偏低（缺失或启发式抽取），需复核", None, "error")
    elif inv.key_field_confidence < config.KEY_FIELD_WARNING:
        inv.add_issue("KEY_FIELD_RECHECK",
                      f"关键字段置信度 {inv.key_field_confidence:.2%} 触发区域重识别", None, "warning")

    # 金额置信度低：仅在"真异常"（含易混字符 / 极低置信）时升级为 Critical；
    # 单纯因启发式抽取（如 0.90）只标 warning + 需复核，不滥用 Critical。
    if inv.amount_field_confidence < config.AMOUNT_FIELD_RECHECK:
        inv.needs_manual_review = True
        amt_suspicious = any(inv.f(k).suspicious for k in config.AMOUNT_FIELDS)
        if amt_suspicious or inv.amount_field_confidence < 0.6:
            inv.critical_review = True
            inv.add_issue("AMOUNT_CONF_LOW",
                          f"金额字段置信度 {inv.amount_field_confidence:.2%} 偏低且可疑，重点人工审核", None, "critical")
        else:
            inv.add_issue("AMOUNT_CONF_RECHECK",
                          f"金额字段置信度 {inv.amount_field_confidence:.2%}（启发式抽取），请人工核对金额", None, "warning")

    if inv.decimal_confidence < config.DECIMAL_RECHECK:
        inv.critical_review = True
        inv.add_issue("DECIMAL_CONF_LOW",
                      f"小数点字符置信度 {inv.decimal_confidence:.2%} 偏低，Critical Review", None, "critical")
