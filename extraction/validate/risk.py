"""风险评分（计划第六节 15）。

综合 OCR 置信度、字段类型与业务规则计算评分；>30 触发二次识别，
二次后仍 >30 进入人工审核。本期不做交互，仅产出评分与标记。
"""
from __future__ import annotations

from core import config
from core.models import Invoice


def compute(inv: Invoice, ocr_pdf_mismatch: bool = False,
            dual_ocr_mismatch: bool = False) -> int:
    """计算并写入 inv.risk_score，返回评分。"""
    score = 0
    if inv.ocr_quality < config.OCR_QUALITY_GOOD:          # OCR < 95%
        score += config.RISK_OCR_LOW
    if inv.amount_field_confidence < config.AMOUNT_FIELD_RECHECK:  # 金额 < 98%
        score += config.RISK_AMOUNT_LOW
    if inv.decimal_confidence < config.DECIMAL_RECHECK:    # 小数点 < 95%
        score += config.RISK_DECIMAL_LOW
    if _total_check_failed(inv):                            # Total 校验失败
        score += config.RISK_TOTAL_FAIL
    # 必填身份字段缺失（提取不完整）——每缺一个加分，缺一个即超阈值进人工
    missing = [f for f in config.REQUIRED_FIELDS if inv.f(f).raw in (None, "")]
    if missing:
        score += config.RISK_FIELD_MISSING * len(missing)
    if ocr_pdf_mismatch:                                    # OCR 与 PDF 文本不一致
        score += config.RISK_OCR_PDF_MISMATCH
    if dual_ocr_mismatch:                                   # 双 OCR 不一致
        score += config.RISK_DUAL_OCR_MISMATCH

    inv.risk_score = score
    if score > config.RISK_THRESHOLD:
        inv.needs_manual_review = True
        if score >= config.RISK_TOTAL_FAIL:
            inv.critical_review = True
    # 缺失发票号/总额属严重，无论分数都置重点审核
    if "invoice_no" in missing or "total_due" in missing:
        inv.critical_review = True
    return score


def _total_check_failed(inv: Invoice) -> bool:
    return any(i.code in ("TOTAL_MISMATCH", "LINE_SUM_MISMATCH", "TAX_RATE_CONFLICT")
               for i in inv.issues)
