"""规则分类引擎（不使用 LLM）。

优先级：历史人工记录 → 描述/供应商规则 → 关键词规则 → 金额辅助 → Unclassified。
输出：建议分类、建议会计科目、置信度、命中规则、是否需重点复核（默认 True）。

历史人工记录依赖人工审核中心（本期不做交互），此处留接口，传入空字典即可。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Dict, List, Optional

from core.models import Classification, Invoice
from . import rules as _rules

# 规则表 / 科目表 / 供应商表 / 固定资产阈值全部来自**可配置**的 `rules` 模块
# （内置默认 + 可选 `config/classification.json` 覆盖），此处不再写死。


def suggestion_pairs() -> List[tuple]:
    """规则表内置的 (分类, 会计科目) 候选对 + 常见种子——供审核界面分类下拉可选项。"""
    pairs: List[tuple] = []
    for rules in (_rules.category_rules(), _rules.supplier_rules()):
        pairs += [(cat, acct) for _pat, cat, acct in rules]
    pairs.append(_rules.asset_labels())     # (Fixed Asset 候选类别, 科目)
    pairs += list(_rules.seed_pairs())
    return pairs


def classify(inv: Invoice, history: Optional[Dict[str, dict]] = None) -> Classification:
    """对发票分类。history: {供应商/描述键 -> {category, account}}（人工确认沉淀，本期可空）。

    关键修正（避免"全文关键词轮盘"）：分类只看**结构化字段**（明细描述、供应商名），
    并按**金额加权**取主导明细项；只有在结构化字段命中时才给正常置信度。
    结构化字段为空时，退化为**低置信全文兜底**（明确标注、强制复核），而非自信地硬凑一个。
    """
    history = history or {}
    desc = _dominant_desc(inv.line_items)          # 主导明细项（按金额加权）的描述
    supplier = (inv.f("issuer_name").value or "")
    structured = f"{desc} {supplier}".strip()      # 仅结构化字段，不含整篇原文

    # 1) 历史人工记录优先（对结构化字段匹配）
    for key, rec in history.items():
        if key and structured and key.lower() in structured.lower():
            return Classification(category=rec.get("category"), account=rec.get("account"),
                                  confidence=0.95, hit_rules=[f"history:{key}"], needs_review=True)

    # 2) 描述规则 → 3) 供应商规则 → 4) 关键词规则（均只看结构化字段）
    cat_rules = _rules.category_rules()
    for rules, text, tag in ((cat_rules, desc, "desc"),
                             (_rules.supplier_rules(), supplier, "supplier"),
                             (cat_rules, structured, "keyword")):
        c = _first_match(rules, text, tag)
        if c:
            return c

    # 5) 金额辅助：高额 + 设备关键词（仅结构化字段）-> 固定资产候选。阈值**按币种**取。
    total = inv.f("total_due").value
    ccy = (inv.f("currency_settlement").value or inv.f("invoice_ccy_raw").value or "")
    if structured and _rules.asset_re().search(structured) and isinstance(total, Decimal) \
            and total >= _rules.asset_threshold(ccy):
        acat, aacct = _rules.asset_labels()
        return Classification(category=acat, account=aacct,
                              confidence=0.4, hit_rules=["amount+keyword:asset_candidate"],
                              needs_review=True)

    # 6) 结构化字段无信号：低置信全文兜底（明确标注关键词来源，强制复核），不伪装可信
    raw = inv.raw_pdf_text or inv.raw_ocr_text or ""
    for pat, category, account in cat_rules:
        if re.search(pat, raw, re.IGNORECASE):
            return Classification(category=category, account=account, confidence=0.25,
                                  hit_rules=[f"fulltext:{pat}（仅全文命中，未定位到明细/供应商，待人工确认）"],
                                  needs_review=True)

    # 7) 无法分类
    return Classification(category="Unclassified", account=None, confidence=0.0,
                          hit_rules=["fallback:unclassified"], needs_review=True)


def _dominant_desc(line_items) -> str:
    """取金额最大的明细项描述作为分类主信号；无金额则拼接全部描述。"""
    if not line_items:
        return ""
    withamt = [li for li in line_items if isinstance(li.amount, Decimal)]
    if withamt:
        top = max(withamt, key=lambda li: li.amount)
        return top.description or ""
    return " ".join(li.description or "" for li in line_items).strip()


def _first_match(rules, text: str, tag: str) -> Optional[Classification]:
    if not text:
        return None
    for pat, category, account in rules:
        if re.search(pat, text, re.IGNORECASE):
            return Classification(category=category, account=account, confidence=0.8,
                                  hit_rules=[f"{tag}:{pat}"], needs_review=True)
    return None
