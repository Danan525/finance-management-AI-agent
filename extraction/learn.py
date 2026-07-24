"""审核期学到的「字段定位线索」的确定性运用——**软先验，不是死模板**。

设计（回应"不要学成死规则"）：
- 学的是**标签关键词**（如某家发票的总额标签是 "Gross Total"），**不是固定坐标、也不是固定值**；
- 用时在**当前这张发票**里**现场按标签找值、按字段类型校验**（金额/日期/文本），
  找不到 / 不合法就**忽略**、回退通用提取；
- **只补"空 / 通用兜底低置信"字段**，绝不覆盖模板精确命中或人工值（加法式、非替换）；
- 作用域：优先"对手方"，并辅以"标签集合指纹"（同类版式聚类，规避开票方名抽错带偏）；
- 一律 **pending → 人工启用**、可信度随 `confirm_count` 上升（在 db 侧）。

纯规则、无 LLM。本模块只做确定性文本匹配与校验。
"""
from __future__ import annotations

import hashlib
import re
from typing import List, Optional

from .parse import amount as amt
from .parse import dates as dt
from .parse import generic as _g

# 字段类型分组（决定"按标签取到的候选值"如何校验）
_AMOUNT_FIELDS = {"subtotal", "sales_tax", "total_due", "payment_due"}
_DATE_FIELDS = {"invoice_date", "payment_due_date", "service_start", "service_end",
                "fund_valuation_date"}

# 金额 token：**复用 generic._MONEY**（美式/欧式/空格/瑞士撇号千分位全支持，避免各处正则漂移，
# 曾因本地窄正则把 "111'780.00" 截成 "111"），再补"标签锚定下的裸整数"（label 后 "950" 也是金额）。
_MONEY = re.compile("(?:" + _g._MONEY.pattern + r")|(?<![\d.,'’])\d+(?![\d.,'’])")
_DATE_SUB = re.compile(
    r"\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}[/-][A-Za-z0-9]{1,9}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}")
_LABEL_TOKEN = re.compile(r"([A-Za-z][A-Za-z /&.#]{1,28})\s*[:：]")


def fingerprint(text: str) -> str:
    """"发票类型"粗指纹：文档里出现的**标签词集合**（以冒号结尾者）排序后哈希。
    同类版式（同一批标签）→ 同指纹；比"开票方名"更鲁棒（名字抽错也能聚类）。"""
    labels = sorted({m.group(1).strip().lower() for m in _LABEL_TOKEN.finditer(text or "")})
    if not labels:
        return ""
    return hashlib.sha1("|".join(labels[:24]).encode("utf-8")).hexdigest()[:12]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip(" :：\t-–—"))


def _is_labelish(s: str) -> bool:
    s = (s or "").strip()
    if not (2 <= len(s) <= 40):
        return False
    if re.search(r"\d{3,}", s) or _MONEY.fullmatch(s.replace(" ", "")):
        return False                       # 像号码/金额 → 不是标签
    return bool(re.search(r"[A-Za-z]", s))


def derive_label(text: str, value, field: str) -> Optional[str]:
    """从原文里定位 value，取其**前置标签**（同行冒号前，或上一非空行）。取不到返回 None。
    仅用于"学习"（捕捉人工确认值旁边的标签），失败即不学。"""
    if not text or value in (None, ""):
        return None
    v = str(value).strip()
    if len(v) < 2:
        return None
    vcore = re.sub(r"[^\d.]", "", v) if field in _AMOUNT_FIELDS else None
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        hit = -1
        if v in ln:
            hit = ln.index(v)
        elif vcore and len(vcore) >= 3:            # 金额：人工填干净数字、原文带 $/, → 按数字核匹配
            for m in _MONEY.finditer(ln):
                if re.sub(r"[^\d.]", "", m.group(0)) == vcore:
                    hit = m.start()
                    break
        if hit < 0:
            continue
        pre = ln[:hit]
        # 剥掉值前粘连的货币前缀（US$ / HK$ / USD 等），避免把 "US" 误当标签
        pre = re.sub(r"\b(US|HK|SG|AU|CA|NZ|USD|HKD|SGD|AUD|CAD|GBP|EUR|CNY|JPY)?\s*[\$€£]?\s*$",
                     "", pre, flags=re.IGNORECASE)
        pre = _norm(pre)                            # 同行值前的部分 = 标签
        if _is_labelish(pre):
            return pre
        for j in range(i - 1, max(-1, i - 3), -1):  # 否则取上一非空行作标签
            p = lines[j].strip()
            if p:
                p = _norm(p)
                return p if _is_labelish(p) else None
        return None
    return None


def _typed_value(candidate: str, field: str) -> Optional[str]:
    """把标签后的候选文本按字段类型校验，返回规范 raw（不合法则 None）。"""
    s = (candidate or "").strip(" :：\t-–—")
    if not s:
        return None
    if field in _AMOUNT_FIELDS:
        m = _MONEY.search(s)
        if not m:
            return None
        val, _susp, _n = amt.parse_amount(m.group(0))
        return m.group(0).strip() if val is not None else None
    if field in _DATE_FIELDS:
        m = _DATE_SUB.search(s)
        if m and dt.normalize_date(m.group(0))[0]:
            return m.group(0).strip()
        return None
    # 文本/编号：取该行剩余（截断到分隔），要求含字母数字
    s = re.split(r"\s{2,}|[|]", s)[0].strip()
    return s if re.search(r"[A-Za-z0-9]", s) else None


def value_by_label(text: str, label: str, field: str) -> Optional[str]:
    """在**当前发票原文**里按学到的标签现场取值：标签后的同行剩余，否则下一非空行。
    按字段类型校验，取不到/不合法即 None（→ 忽略该线索，回退通用）。"""
    if not text or not label:
        return None
    lines = (text or "").splitlines()
    rx = re.compile(re.escape(label) + r"\s*[:：]?\s*(.*)", re.IGNORECASE)
    for i, ln in enumerate(lines):
        m = rx.search(ln)
        if not m:
            continue
        v = _typed_value(m.group(1), field)
        if v:
            return v
        for j in range(i + 1, min(len(lines), i + 3)):     # 值在下一非空行
            nxt = lines[j].strip()
            if nxt:
                return _typed_value(nxt, field)
    return None
