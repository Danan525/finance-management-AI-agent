"""固定格式发票模板解析（不使用 LLM，纯规则）。

基于样例双栏版式：右栏=表头/Bill to/合计，左栏=开票方/付款地址。
对非固定格式凭证可退化为对线性文本跑同一套标签正则。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Dict, List, Optional

from core import config
from ..extract.pdf_text import PdfDoc
from core.models import FieldValue, Invoice, LineItem
from . import amount as amt
from . import dates as dt
from . import wallet as wl
from . import generic

# ---- 表头字段标签正则（按行匹配）---------------------------------------
# 值一律捕获**到列间隙(≥2 空格)或行尾** `(.+?)(?=\s{2,}|$)`，不再用会切碎多词值的窄字符类
# （旧版 [A-Za-z0-9\-]+ 会把 "INV/2026/001"→"INV"、"28 December 2025"→"28"，还挡住 generic 兜底）。
# 清洗交给类型解析器（normalize_date / 文本 setter），这里只负责"取到完整原值"。
_VAL = r"(.+?)(?=\s{2,}|$)"
_HEADER_PATTERNS = {
    "invoice_no": r"Invoice\s*#\s*[:：]\s*" + _VAL,
    "invoice_date": r"Invoice\s*date\s*[:：]\s*" + _VAL,
    "payment_due_date": r"Payment\s*Due\s*date\s*[:：]\s*" + _VAL,
    "invoice_ccy_raw": r"Invoice\s*Ccy\s*[:：]\s*" + _VAL,
    "fund_valuation_date": r"Fund\s*Valuation\s*Date\s*[:：]?\s*" + _VAL,
    "customer_name": r"Bill\s*to\s*[:：]\s*(.+)",
    "contact_email": r"Emails?\s*Contacts?\s*[:：]\s*([^\s]+@[^\s]+)",
}

# 金额值：标签锚定，故小数可选、允许各种币种符号（认整数额/日元 0 小数/3 小数币种，不再卡死 .\d{2}）。
# 用**原子组**吃整个数（不回溯出半个，防 "30%"→"3"）；`(?!\s*%)` 排除**百分比**——
# "Sales Tax 3%" 里的 3 是**税率**不是税额，绝不能当成金额抓走（句末的 "201.89." 句号不受影响）。
_SYMC = r"[$€£¥₹₩฿₱₦₫₨₪]"
# 数字吃到最大再排除百分比（不用原子组以兼容 Python 3.9）。分组顺序：美式 1,234.56 →
# 欧式 1.234,56 → 空格千分位 1 234,56（含普通/不断行空格）→ 裸数/单小数；欧式/空格须在裸数之前。
_NUM = (r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,3}(?:\.\d{3})+(?:,\d+)?|"
        r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+[.,]\d{1,3}|\d{1,3}(?:['\u2019]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(?![\d,]|\.\d)(?!\s*%)")
_TOTAL_PATTERNS = {
    "subtotal": r"Subtotal\s*[:：]?\s*" + _SYMC + r"?\s*" + _NUM,
    "sales_tax": r"Sales\s*Tax\s*[:：]?\s*" + _SYMC + r"?\s*" + _NUM,
    "total_due": r"TOTAL\s*DUE\s*[:：]?\s*" + _SYMC + r"?\s*" + _NUM,
    # 税率：任一税标签后的 X%（Sales Tax 3% / VAT 3% / Tax 3% / Tax Rate 3% 都归税率，不进税额）
    "tax_rate": r"(?:Sales\s*Tax|Tax\s*Rate|\bVAT\b|\bGST\b|\bTax\b)\s*[:：]?\s*([\d.]+\s*%)",
}

_MONEY = re.compile(r"" + _SYMC + r"?\s*\(?-?[\d,]+\.\d{2,3}\)?")
# 明细表列分界（PDF 点坐标）：行号列 / 描述列 / 金额列
_ITEM_X = 160.0
_AMOUNT_X = 470.0

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
# 当事方地址块的终止关键字（遇到即认为地址块结束）
_PARTY_STOP = re.compile(
    r"Item\s*#|Description|Subtotal|TOTAL\s*DUE|Sales\s*Tax|Tax\s*Rate|Please\s+make|Payment\s+Due",
    re.IGNORECASE)
_PARTY_GAP = 25.0   # 行间 y 间隔超过此值视为版块分隔


def _set_text_field(inv: Invoice, key: str, raw: Optional[str], conf: float, source: str) -> None:
    if raw is None:
        inv.set(key, FieldValue(raw=None, value=None, confidence=conf, source=source))
    else:
        inv.set(key, FieldValue(raw=raw.strip(), value=raw.strip(), confidence=conf, source=source))


def _set_date_field(inv: Invoice, key: str, raw: Optional[str], conf: float, source: str) -> None:
    iso, need_review = (None, False)
    if raw:
        iso, need_review = dt.normalize_date(raw)
    fv = FieldValue(raw=raw.strip() if raw else None, value=iso, confidence=conf, source=source)
    if need_review:
        if iso:                                        # 解析到了但 日/月 有歧义
            fv.suspicious = True
            fv.note = "日/月有歧义(如 05/06)，已按默认解读，请核对是几月几日"
        else:
            fv.note = "日期格式无法识别，待复核"
    inv.set(key, fv)


def _correct_negative_summaries(inv: Invoice, is_credit: bool) -> None:
    """非贷记单：把小计/税额/合计的**负号**取正（发票汇总本不该为负）。

    负号来源有二：① OCR 件上印章椭圆弧线 / 货币符号被误读成紧贴数字的 "-"（实测红章压在小计旁
    → "-3,562.00"）；② 文本件里总额写成句中括号重述 "Total Due: ... ($2,500.00)"，括号被当会计
    负数 → -2500。两者对**非贷记单发票**都是错的（汇总必为非负）。真·负数（贷记单/退款）由
    `is_credit` 关键字守卫保留。"""
    if is_credit:
        return
    for k in ("subtotal", "sales_tax", "total_due"):
        fv = inv.f(k)
        if fv.value is not None and fv.value < 0:
            fv.value = -fv.value
            if fv.raw:
                fv.raw = fv.raw.lstrip("-").strip()
            inv.set(k, fv)


def _recover_obscured_totals(inv: Invoice, full: str) -> None:
    """仅 OCR 件：水印/印章糊掉了 Total/Tax 的**标签**、但其**数值**作为孤立金额行幸存时，
    按算术一致性（小计 + 税 = 合计）把幸存值归位到 total_due / sales_tax。

    恢复的是**真实 OCR 读到的值**（非凭空推导）——仅在 total_due 缺失、且孤立值与小计算术自洽时
    才赋值，留 note 供人工复核。总额若非"小计(+税)"（含折扣/运费/预付）则无匹配、不赋值，安全。
    """
    if not inv.ocr_used:
        return
    sub_fv = inv.f("subtotal")
    if sub_fv.value is None or inv.f("total_due").value is not None:
        return
    S = sub_fv.value
    lines = [ln.strip() for ln in (full or "").splitlines() if ln.strip()]
    sub_idx = max((i for i, ln in enumerate(lines)
                   if re.search(r"sub\s*total|小\s*计", ln, re.I)), default=-1)
    if sub_idx < 0:
        return
    orphans = []                                     # 小计行之后的"纯金额"孤立行（标签被糊）
    for ln in lines[sub_idx + 1:]:
        if generic.is_watermark(ln) or not generic._is_amount_cell(ln):
            continue
        v = amt.parse_amount(ln)[0]
        if v is not None and v > 0:
            orphans.append((ln, v))
    if not orphans:
        return
    cent = Decimal("0.01")
    taxv = inv.f("sales_tax").value
    tax_raw = total_raw = None
    if taxv is not None:                             # 税已知：找 == 小计+税 的孤立值当合计
        total_raw = next((raw for raw, v in orphans if abs(v - (S + taxv)) <= cent), None)
    else:                                            # 税未知：优先找一对 (税, 合计) 满足 小计+税=合计
        for ra, va in orphans:
            for rb, vb in orphans:
                if ra is not rb and abs((S + va) - vb) <= cent:
                    tax_raw, total_raw = ra, rb
                    break
            if total_raw:
                break
        if not total_raw:                            # 否则单个 == 小计 的孤立值当合计（无税）
            total_raw = next((raw for raw, v in orphans if abs(v - S) <= cent), None)
    note = "标签被水印/印章遮盖，按算术一致性(小计+税=合计)从幸存金额恢复，请人工复核"
    if tax_raw and _empty(inv, "sales_tax"):
        _set_amount_field(inv, "sales_tax", tax_raw, config.GENERIC_FIELD_CONFIDENCE, "ocr_recover")
        inv.f("sales_tax").note = note
    if total_raw:
        _set_amount_field(inv, "total_due", total_raw, config.GENERIC_FIELD_CONFIDENCE, "ocr_recover")
        inv.f("total_due").note = note


def _set_amount_field(inv: Invoice, key: str, raw: Optional[str], conf: float, source: str) -> None:
    val, suspicious, note = amt.parse_amount(raw)
    inv.set(key, FieldValue(raw=raw.strip() if raw else None, value=val,
                            confidence=conf, source=source, suspicious=suspicious, note=note))


def _match(patterns: Dict[str, str], blob: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, pat in patterns.items():
        m = re.search(pat, blob, re.IGNORECASE)
        if m:
            out[key] = m.group(1).strip()
    return out


# 发票起始标记：版式无关——发票号标签（Invoice # / Invoice No / INVOICE NO. / Bill No / Invoice Number）
_INVOICE_START_RE = re.compile(r"invoice\s*#|invoice\s*(no\.?|number)\b|bill\s*no\.?\b", re.IGNORECASE)
# "发票起始线索"（比上面更宽，供按页切分时判断某页是否为一张发票的开头，而非续页）：
# 发票号/INVOICE 标题/裸 No.·# 后跟编号。要求 No. 带点或 # 带值，避免误命中 Notes/November 等。
_START_HINT = re.compile(
    r"(?:tax\s+)?invoice\b|invoice\s*#|inv\.?\s*no|bill\s*no|\bno\.\s*[A-Za-z0-9]|#\s*[A-Za-z0-9]",
    re.IGNORECASE)
# 合计标记：与字段提取**共用同一张表**（generic._TOTAL_STRONG），避免"数张数"与"取总额"用词不一致
# （旧版这里更窄，漏 total payable / amount payable / net total 等，导致多发票检测漏判）。
_TOTAL_MARK_RE = generic._TOTAL_STRONG


def count_total_markers(full_text: str) -> int:
    """文档里"含税总额"标记的数量（≈发票张数的可靠信号，与发票号标签格式无关）。"""
    return len(_TOTAL_MARK_RE.findall(full_text or ""))


def _mk_segment(seg_lines: List[Line], page_size=None) -> PdfDoc:
    seg_text = "\n".join(ln.text() for ln in seg_lines)
    return PdfDoc(full_text=seg_text, plumber_text="", lines=seg_lines,
                  char_count=len(seg_text.strip()),
                  page_sizes=[page_size] if page_size else [])


def _split_by_pages(doc: PdfDoc, n_totals: int) -> Optional[List[PdfDoc]]:
    """每页一张发票时按页切分（最可靠：与发票号标签格式无关）。

    页数 == 合计数 且每页都有内容时启用；否则返回 None 交由标记法/不切。
    依据 extract_pdf 的 y 偏移（每页 += 页高 + 20）把行归到各页。
    """
    pages = doc.page_sizes or []
    if len(pages) != n_totals or len(pages) < 2:
        return None
    # 各页起始 y 阈值：offset_k = Σ_{i<k}(h_i + 20)
    starts_y, acc = [], 0.0
    for w, h in pages:
        starts_y.append(acc)
        acc += h + 20.0
    buckets: List[List[Line]] = [[] for _ in pages]
    for ln in doc.lines:
        pi = 0
        for k in range(len(pages)):
            if ln.y >= starts_y[k]:
                pi = k
        buckets[pi].append(ln)
    if any(not b for b in buckets):
        return None                      # 有空页 → 放弃页切分
    # 每页都须含"发票起始线索"（发票号/INVOICE 标题/No.·# 编号），否则那页是**续页**而非新发票——
    # 防"一张两页、续页重复写合计(Amount Payable)"被误当第二张按页拆开。
    if not all(any(_START_HINT.search(ln.text()) for ln in b) for b in buckets):
        return None
    return [_mk_segment(b, pages[i]) for i, b in enumerate(buckets)]


def split_invoice_segments(doc: PdfDoc) -> List[PdfDoc]:
    """把含多张发票的文档切分为多个子 PdfDoc，仅在高置信时才拆（绝不误拆）。

    切分信号优先级：
    1) **按页**：页数 == 合计标记数 且 ≥2（多发票文件最常见=一张一页，与标签格式无关，最可靠）；
    2) **按发票号/标题标记**：标记数 == 合计数 且 ≥2（同页多张的兜底）。
    都不满足 → 原样返回单文档（退回"按首张解析 + MULTI_INVOICE 提示"，绝不误拆）。
    """
    n_totals = len(_TOTAL_MARK_RE.findall(doc.full_text))
    if n_totals < 2:
        return [doc]
    by_page = _split_by_pages(doc, n_totals)
    if by_page is not None:
        return _reject_same_invoice(by_page, doc)
    # 兜底：发票号标签 / "INVOICE" 标题（取与合计数一致的那组标记）
    num_starts = [i for i, ln in enumerate(doc.lines) if _INVOICE_START_RE.search(ln.text())]
    title_starts = [i for i, ln in enumerate(doc.lines)
                    if re.fullmatch(r"(tax\s+)?invoice", ln.text().strip(), re.IGNORECASE)]
    starts = num_starts if len(num_starts) == n_totals else (
        title_starts if len(title_starts) == n_totals else None)
    if not starts or len(starts) < 2:
        return [doc]
    bounds = [0] + starts[1:] + [len(doc.lines)]
    segs = [_mk_segment(doc.lines[a:b]) for a, b in zip(bounds, bounds[1:])]
    return _reject_same_invoice(segs, doc)


# 段的发票号（去空白/大小写）——用于判"多段其实是同一张发票的续页"
def _seg_invoice_no(seg: PdfDoc) -> Optional[str]:
    try:
        no = generic.extract_generic(seg.lines).get("invoice_no")
    except Exception:
        no = None
    return re.sub(r"\s+", "", no).upper() if no else None


def _reject_same_invoice(segs: List[PdfDoc], doc: PdfDoc) -> List[PdfDoc]:
    """若切出的多段其实指向**同一张发票**（续页/尾随明细附表重复了同一发票号）→ 不拆，返回整篇。

    这是 2^n 递归拆分爆炸的根因：一张发票带"尾随类别明细附表"时，主发票 Gross Total 与附表
    的 total 凑成 2 个合计标记，且附表续页常**重复印同一发票号** → 被误当第二张；反复重扫（用存储的
    段文本再跑拆分，段里仍有 2 个合计标记）→ 每轮翻倍。判据保守：**各段都取到发票号且完全相同** →
    同一张（不同发票号几乎不可能相同，故不会误合并真·多发票 bundle）。取不到号则维持原判（照拆）。
    """
    if len(segs) < 2:
        return segs
    nos = [_seg_invoice_no(s) for s in segs]
    if all(nos) and len(set(nos)) == 1:
        return [doc]
    return segs


def parse_pdfdoc(inv: Invoice, doc: PdfDoc, source: str = "pdf_text") -> None:
    """从结构化 PdfDoc 解析所有字段。"""
    conf = config.PDF_TEXT_CONFIDENCE
    right = doc.right_block()
    left = doc.left_block()
    full = doc.full_text

    # --- 表头字段 ---
    header = _match(_HEADER_PATTERNS, right)
    # 部分发票可能整行没进右栏，兜底再扫全文
    header_full = _match(_HEADER_PATTERNS, full)
    for k, v in header_full.items():
        header.setdefault(k, v)

    _set_text_field(inv, "invoice_no", header.get("invoice_no"), conf, source)
    _set_date_field(inv, "invoice_date", header.get("invoice_date"), conf, source)
    _set_date_field(inv, "payment_due_date", header.get("payment_due_date"), conf, source)
    _set_date_field(inv, "fund_valuation_date", header.get("fund_valuation_date"), conf, source)
    _set_text_field(inv, "customer_name", header.get("customer_name"), conf, source)
    _set_text_field(inv, "contact_email", header.get("contact_email"), conf, source)

    # --- 币种拆分：显示符号 vs 结算币种 ---
    ccy_raw = header.get("invoice_ccy_raw")
    _set_text_field(inv, "invoice_ccy_raw", ccy_raw, conf, source)
    display_symbol = "$" if "$" in full else None
    _set_text_field(inv, "currency_display_symbol", display_symbol, conf, source)
    _set_text_field(inv, "currency_settlement", ccy_raw, conf, source)

    # --- 服务期间（右栏标签 + 续行，或描述中提取）---
    _parse_service_period(inv, doc, source, conf)

    # --- 开票方 / 收票方完整地址块 ---
    _extract_party_blocks(inv, doc, source, conf)

    # --- 合计 ---
    totals = _match(_TOTAL_PATTERNS, right)
    totals_full = _match(_TOTAL_PATTERNS, full)
    for k, v in totals_full.items():
        totals.setdefault(k, v)
    _set_amount_field(inv, "subtotal", totals.get("subtotal"), conf, source)
    _set_amount_field(inv, "sales_tax", totals.get("sales_tax"), conf, source)
    _set_amount_field(inv, "total_due", totals.get("total_due"), conf, source)
    _set_text_field(inv, "tax_rate", totals.get("tax_rate"), conf, source)

    # --- 明细行 ---
    items = _parse_line_items(doc, inv.file_name)
    inv.line_items = items
    # 明细金额（Payment Due 列第一行）作为字段
    if items:
        _set_amount_field(inv, "payment_due", items[0].amount_raw, conf, source)
    else:
        _set_amount_field(inv, "payment_due", None, conf, source)

    # --- 通用兜底：模板没抽到的字段，用版式无关的标签锚点补齐（多供应商/多版式）---
    gres = generic.extract_generic(doc.lines)
    _fill_from_generic(inv, gres, conf, source)

    # 分组小计（多个 "subtotal"）：模板的正则 re.search 取到**首个**（可能是 section 小计）；
    # generic 会择"与 total 自洽"的总小计。若模板小计与 total 不自洽、而 generic 的自洽 → 用 generic 的。
    _sub_t, _tax_t, _tot_t = inv.f("subtotal").value, inv.f("sales_tax").value, inv.f("total_due").value
    if isinstance(_sub_t, Decimal) and isinstance(_tot_t, Decimal) and gres.get("subtotal"):
        _tax_t = _tax_t if isinstance(_tax_t, Decimal) else None
        _sub_g = amt.parse_amount(gres["subtotal"])[0]
        tol = Decimal("0.01")
        tmpl_ok = abs(_sub_t + (_tax_t or Decimal(0)) - _tot_t) <= tol
        gen_ok = _sub_g is not None and abs(_sub_g + (_tax_t or Decimal(0)) - _tot_t) <= tol
        if not tmpl_ok and gen_ok:
            _set_amount_field(inv, "subtotal", gres["subtotal"], conf, source + "_generic")

    # 明细行兜底：模板没抽到（无 "Item #" 表头）时用通用明细解析（含数量/单价/金额，驱动分类与多服务展示）
    if not inv.line_items:
        gli = generic.extract_line_items(doc.lines)
        if gli:
            items: List[LineItem] = []
            for it in gli:
                items.append(LineItem(
                    description=it["description"],
                    quantity=amt.parse_amount(it["quantity"])[0] if it["quantity"] else None,
                    unit_price=amt.parse_amount(it["unit_price"])[0] if it["unit_price"] else None,
                    amount=amt.parse_amount(it["amount"])[0] if it["amount"] else None,
                    amount_raw=it["amount"],
                    line_confidence=config.GENERIC_FIELD_CONFIDENCE,
                    source_file=inv.file_name))
            inv.line_items = items

    # 兜底剔除：任何路径若把 Subtotal/Tax/Total 等**合计行**误当明细，一律从明细里移除（防污染勾稽）
    if inv.line_items:
        inv.line_items = [li for li in inv.line_items if not generic.is_summary_desc(li.description)]
    # 非贷记单：明细金额不应为负——负号多为 OCR 把货币符号($/¥)误读成"-"，取正（贷记单/退款保留负数）
    _fl = (full or "").lower()
    _is_credit = any(k in _fl for k in ("credit note", "credit memo", "贷记", "红冲"))
    if inv.line_items and not _is_credit:
        for li in inv.line_items:
            if li.amount is not None and li.amount < 0:
                li.amount = -li.amount
    # 汇总字段（小计/税额/合计）同理，非贷记单不应为负：负号来自 OCR 把**印章椭圆弧线**/货币符号
    # 误读成"-"（红章压小计旁 → "-3,562.00"），或文本件里总额写成句中括号重述被当会计负数（($2,500.00)）。
    _correct_negative_summaries(inv, _is_credit)

    # --- 尾随"类别明细附表"：按类别归属到主明细行 + 勾稽（子行合计 == 该行金额）---
    _attach_detail_schedules(inv, generic.extract_detail_schedule(doc.lines))

    # 散文式金额兜底：subtotal/tax/total 写在句子里（"The total amount due is USD 2,806.89."）
    prose = generic.prose_amounts(full)
    for k in ("subtotal", "sales_tax", "total_due"):
        if _empty(inv, k) and prose.get(k):
            _set_amount_field(inv, k, prose[k], config.GENERIC_FIELD_CONFIDENCE, source + "_generic")

    # 中国增值税发票：多列表（金额列非最右），列感知解析覆盖 明细 + 小计/税额/价税合计
    vat = generic.extract_cn_vat(doc.lines)
    if vat and vat.get("line_items"):
        inv.line_items = [LineItem(description=it["description"],
                                   amount=amt.parse_amount(it["amount"])[0],
                                   amount_raw=it["amount"],
                                   line_confidence=conf, source_file=inv.file_name)
                          for it in vat["line_items"]]
        for k in ("subtotal", "sales_tax", "total_due"):
            if vat.get(k) is not None:
                _set_amount_field(inv, k, vat[k], conf, source + "_cnvat")

    # 税内含兜底：'Incl. tax (15%): GBP 11,700.00' → 税额 + 税率（无单独 Subtotal 行）
    if _empty(inv, "sales_tax"):
        rate, tax_raw = generic.incl_tax_fallback(full)
        if tax_raw:
            _set_amount_field(inv, "sales_tax", tax_raw, config.GENERIC_FIELD_CONFIDENCE, source + "_generic")
            if rate and _empty(inv, "tax_rate"):
                _set_text_field(inv, "tax_rate", rate, config.GENERIC_FIELD_CONFIDENCE, source + "_generic")

    # 水印/印章糊掉 Total/Tax 标签、但数值作为孤立行幸存时，按算术一致性把真实值归位
    _recover_obscured_totals(inv, full)

    # 币种兜底：列头 'AMOUNT (GBP)' / 脚注 'All amounts in USD'
    if _empty(inv, "currency_settlement"):
        gccy = generic.currency_fallback(full)
        if gccy:
            for k in ("currency_settlement", "invoice_ccy_raw"):
                if _empty(inv, k):
                    _set_text_field(inv, k, gccy, config.GENERIC_FIELD_CONFIDENCE, source + "_generic")

    # --- 付款信息（钱包地址 + 链）---
    payments, pay_issues = wl.extract_payments(full, inv.file_name, ccy_raw)
    inv.payments = payments
    for code, msg, sev in pay_issues:
        inv.add_issue(code, msg, field_="wallet_address", severity=sev)

    # --- 多发票/多页检测：不静默丢，明确提示（原文已完整归档）---
    # 多发票信号看"发票起始标记(发票号/INVOICE 标题)"的**个数**，而非合计标记数——
    # 一张发票常把合计重复写两次(Total Due + Amount Payable)或跨两页各有合计，那只是 1 个发票号，
    # 不该报"可能多张"（避免"说多张、其实一张两页"的矛盾）。真多张才有 ≥2 个发票起始。
    n_starts = len(_INVOICE_START_RE.findall(full))
    if n_starts > 1 and count_total_markers(full) > 1:
        inv.add_issue("MULTI_INVOICE",
                      f"文件中检测到 {n_starts} 处发票起始（发票号），可能含多张发票；"
                      f"建议拆分为单独文件。当前按首张解析，完整原文已归档于 Raw Text Archive。",
                      None, "warning")


def parse_plain_text(inv: Invoice, text: str, source: str = "ocr") -> None:
    """对线性文本（如 OCR 输出）跑同一套标签正则（兜底路径）。"""
    conf = inv.ocr_quality if source == "ocr" else config.PDF_TEXT_CONFIDENCE
    header = _match(_HEADER_PATTERNS, text)
    _set_text_field(inv, "invoice_no", header.get("invoice_no"), conf, source)
    _set_date_field(inv, "invoice_date", header.get("invoice_date"), conf, source)
    _set_date_field(inv, "payment_due_date", header.get("payment_due_date"), conf, source)
    _set_date_field(inv, "fund_valuation_date", header.get("fund_valuation_date"), conf, source)
    _set_text_field(inv, "customer_name", header.get("customer_name"), conf, source)
    _set_text_field(inv, "contact_email", header.get("contact_email"), conf, source)

    ccy_raw = header.get("invoice_ccy_raw")
    _set_text_field(inv, "invoice_ccy_raw", ccy_raw, conf, source)
    _set_text_field(inv, "currency_display_symbol", "$" if "$" in text else None, conf, source)
    _set_text_field(inv, "currency_settlement", ccy_raw, conf, source)

    start, end = dt.extract_period(text)
    _set_text_field(inv, "service_start", start, conf, source)
    _set_text_field(inv, "service_end", end, conf, source)

    totals = _match(_TOTAL_PATTERNS, text)
    _set_amount_field(inv, "subtotal", totals.get("subtotal"), conf, source)
    _set_amount_field(inv, "sales_tax", totals.get("sales_tax"), conf, source)
    _set_amount_field(inv, "total_due", totals.get("total_due"), conf, source)
    _set_text_field(inv, "tax_rate", totals.get("tax_rate"), conf, source)
    _set_text_field(inv, "issuer_name", None, conf, source)
    _set_amount_field(inv, "payment_due", None, conf, source)

    payments, pay_issues = wl.extract_payments(text, inv.file_name, ccy_raw)
    inv.payments = payments
    for code, msg, sev in pay_issues:
        inv.add_issue(code, msg, field_="wallet_address", severity=sev)


# ---- 通用兜底字段回填 ----------------------------------------------------
def _empty(inv: Invoice, key: str) -> bool:
    return inv.f(key).raw in (None, "")


def _fix_address(inv: Invoice, key: str, gval: Optional[str], conf: float, gsrc: str) -> None:
    """地址纠错：当前值为空、或抓串了（含标签/免责声明/区块头噪声）时，用 generic 干净值替换。"""
    cur = inv.f(key).value
    if not gval or generic.addr_noisy(gval):
        return
    if cur in (None, "") or generic.addr_noisy(str(cur)):
        _set_text_field(inv, key, gval, conf, gsrc)


def _fill_from_generic(inv: Invoice, g: Dict[str, str], conf: float, source: str) -> None:
    """把通用提取结果填进**仍为空**的字段（绝不覆盖模板已抽到的值）。"""
    gsrc = source + "_generic"
    conf = config.GENERIC_FIELD_CONFIDENCE   # 启发式抽取：置信度低于模板精确命中，触发复核
    if _empty(inv, "invoice_no") and g.get("invoice_no"):
        _set_text_field(inv, "invoice_no", g["invoice_no"], conf, gsrc)
    # 日期：模板为空、**或模板抓到的原文无法解析成日期(value=None)** 时，用 generic 的干净日期覆盖
    # （模板对"标签在上、日期在下一行"或长格式常抓错、留下无效 raw，会挡住兜底 → 这里放开）
    if inv.f("invoice_date").value is None and g.get("invoice_date"):
        _set_date_field(inv, "invoice_date", g["invoice_date"], conf, gsrc)
    if inv.f("payment_due_date").value is None and g.get("payment_due_date"):
        _set_date_field(inv, "payment_due_date", g["payment_due_date"], conf, gsrc)
    # 开票方公司名纠错：模板抓到的名若不像公司名（发票号/标题/标签等噪声），
    # 用 generic 识别到的公司名替换；模板为空也填。
    gname = g.get("issuer_name")
    cur_name = inv.f("issuer_name").value
    if gname and generic.looks_like_company(gname) and not (cur_name and generic.looks_like_company(cur_name)):
        _set_text_field(inv, "issuer_name", gname, conf, gsrc)
    # 地址：模板抓到的若为空或**抓串了**（混入 Date:/Terms:/NOT FOR PAYMENT/区块头），
    # 用 generic 的干净地址纠正；纠正后落到启发式置信度（0.90），不再虚假 100%。
    _fix_address(inv, "issuer_address", g.get("issuer_address"), conf, gsrc)
    _fix_address(inv, "customer_address", g.get("customer_address"), conf, gsrc)
    if _empty(inv, "issuer_email") and g.get("issuer_email"):
        _set_text_field(inv, "issuer_email", g["issuer_email"], conf, gsrc)
    if _empty(inv, "issuer_phone") and g.get("issuer_phone"):
        _set_text_field(inv, "issuer_phone", g["issuer_phone"], conf, gsrc)
    if _empty(inv, "contact_phone") and g.get("contact_phone"):
        _set_text_field(inv, "contact_phone", g["contact_phone"], conf, gsrc)
    if _empty(inv, "customer_name") and g.get("customer_name"):
        _set_text_field(inv, "customer_name", g["customer_name"], conf, gsrc)
    if _empty(inv, "contact_email") and g.get("contact_email"):
        _set_text_field(inv, "contact_email", g["contact_email"], conf, gsrc)
    if g.get("tax_rate") and _empty(inv, "tax_rate"):
        _set_text_field(inv, "tax_rate", g["tax_rate"], conf, gsrc)
    if g.get("fund_valuation_date") and inv.f("fund_valuation_date").value is None:
        _set_date_field(inv, "fund_valuation_date", g["fund_valuation_date"], conf, gsrc)
    # 银行明细（结构化）
    for k in ("bank_name", "bank_account_name", "bank_account_no", "bank_swift"):
        if _empty(inv, k) and g.get(k):
            _set_text_field(inv, k, g[k], conf, gsrc)

    # 币种：结算币种 / 原始币种 / 显示符号
    ccy = g.get("currency")
    if ccy:
        if _empty(inv, "currency_settlement"):
            _set_text_field(inv, "currency_settlement", ccy, conf, gsrc)
        if _empty(inv, "invoice_ccy_raw"):
            _set_text_field(inv, "invoice_ccy_raw", ccy, conf, gsrc)
        if ccy in ("$", "€", "£") and _empty(inv, "currency_display_symbol"):
            _set_text_field(inv, "currency_display_symbol", ccy, conf, gsrc)

    # 金额
    for k in ("subtotal", "sales_tax", "total_due"):
        if _empty(inv, k) and g.get(k):
            _set_amount_field(inv, k, g[k], conf, gsrc)

    # 服务期间区间（generic 已解析跨行 "起 … to 止"）；用 _set_date_field：value=ISO、raw=原始文本
    # （raw 保留原文，审核界面才能把它定位回原件）
    if g.get("period_start") and _empty(inv, "service_start"):
        _set_date_field(inv, "service_start", g["period_start"], conf, gsrc)
    if g.get("period_end") and _empty(inv, "service_end"):
        _set_date_field(inv, "service_end", g["period_end"], conf, gsrc)


# ---- 辅助 ----------------------------------------------------------------
def _first_left_name(doc: PdfDoc) -> Optional[str]:
    """左栏首个非空行作为开票方名称。"""
    for ln in doc.lines:
        lt = ln.left_text().strip()
        if lt and lt.upper() != "INVOICE":
            return lt
    return None


def _extract_party_blocks(inv: Invoice, doc: PdfDoc, source: str, conf: float) -> None:
    """提取开票方（左栏）与收票方（右栏 Bill to 之后）的完整多行地址块。

    名称 / 地址 / 邮箱分开成字段；地址逐行拼接，绝不丢行。
    遇到终止关键字或较大 y 间隔即认为地址块结束。
    """
    lines = doc.lines

    # ---- 开票方：左栏 ----
    name_idx = None
    for i, ln in enumerate(lines):
        lt = ln.left_text().strip()
        if lt and lt.upper() != "INVOICE" and not _PARTY_STOP.search(lt):
            name_idx = i
            break
    issuer_name = None
    issuer_addr: List[str] = []
    issuer_email = None
    if name_idx is not None:
        issuer_name = lines[name_idx].left_text().strip()
        last_y = lines[name_idx].y
        for j in range(name_idx + 1, len(lines)):
            lt = lines[j].left_text().strip()
            if not lt:
                continue
            if _PARTY_STOP.search(lt) or generic.is_line_item_row(lt) or lines[j].y - last_y > _PARTY_GAP:
                break                                    # 撞到明细表头/明细行(含金额)即止
            last_y = lines[j].y
            m = _EMAIL.search(lt)
            if m:
                issuer_email = issuer_email or m.group(0)
                lt = lt.replace(m.group(0), "")
            lt = lt.strip(" ,;|")
            if lt:
                issuer_addr.append(lt)
    _set_text_field(inv, "issuer_name", issuer_name, conf, source)
    _set_text_field(inv, "issuer_address", ", ".join(issuer_addr) or None, conf, source)
    _set_text_field(inv, "issuer_email", issuer_email, conf, source)

    # ---- 收票方：右栏 Bill to 之后 ----
    bill_idx = None
    for i, ln in enumerate(lines):
        if re.search(r"Bill\s*to", ln.right_text(), re.IGNORECASE):
            bill_idx = i
            break
    cust_addr: List[str] = []
    if bill_idx is not None:
        last_y = lines[bill_idx].y
        for j in range(bill_idx + 1, len(lines)):
            rt = lines[j].right_text().strip()
            if not rt:
                continue
            if re.search(r"Emails?\s*Contacts?", rt, re.IGNORECASE):
                break
            if _PARTY_STOP.search(rt) or generic.is_line_item_row(rt) or lines[j].y - last_y > _PARTY_GAP:
                break                                    # 撞到明细表头/明细行(含金额)即止
            last_y = lines[j].y
            rt = re.sub(r"^Address\s*[:：]\s*", "", rt, flags=re.IGNORECASE)
            m = _EMAIL.search(rt)
            if m:
                rt = rt.replace(m.group(0), "")
            rt = rt.strip(" ,;|")
            if rt:
                cust_addr.append(rt)
    _set_text_field(inv, "customer_address", ", ".join(cust_addr) or None, conf, source)


def _parse_service_period(inv: Invoice, doc: PdfDoc, source: str, conf: float) -> None:
    """服务期间：右栏 'Invoice for Service Period <起>' + 续行 <止>；
    否则从明细描述中提取。"""
    right_lines = [ln.right_text() for ln in doc.lines if ln.right_text()]
    start = end = None
    for i, line in enumerate(right_lines):
        m = re.search(r"Invoice\s*for\s*Service\s*Period\s+([0-9A-Za-z\-/\.]+)", line, re.IGNORECASE)
        if m:
            start, _ = dt.normalize_date(m.group(1))
            # 续行若是裸日期，作为结束日期
            if i + 1 < len(right_lines):
                nxt = right_lines[i + 1].strip()
                if re.fullmatch(r"[0-9A-Za-z\-/\.]+", nxt):
                    end, _ = dt.normalize_date(nxt)
            break
    if start is None and end is None:
        # 从明细描述提取（如 "for period 06/26/2025 - 07/31/2025"）
        desc = " ".join(li.description or "" for li in inv.line_items)
        if not desc:
            desc = doc.full_text
        start, end = dt.extract_period(desc)
    _set_text_field(inv, "service_start", start, conf, source)
    _set_text_field(inv, "service_end", end, conf, source)


def _parse_line_items(doc: PdfDoc, file_name: str) -> List[LineItem]:
    """从明细表区域抽取明细行。"""
    # 定位表头行与合计起始行
    header_y = None
    totals_y = None
    for ln in doc.lines:
        t = ln.text()
        if header_y is None and re.search(r"Item\s*#", t) and re.search(r"Description", t):
            header_y = ln.y
        if re.search(r"Subtotal|TOTAL\s*DUE", t, re.IGNORECASE):
            totals_y = ln.y if totals_y is None else min(totals_y, ln.y)
    if header_y is None:
        return []
    region = [ln for ln in doc.lines
              if ln.y > header_y + 1 and (totals_y is None or ln.y < totals_y - 1)]

    amounts: List[tuple] = []   # (y, raw)
    desc_words: List[tuple] = []  # (y, x, text)  —— 除金额/行号外的所有词，绝不丢弃
    item_nos: List[tuple] = []   # (y, text)
    for ln in region:
        for (x0, x1, txt) in ln.words:
            if x0 >= _AMOUNT_X and _MONEY.fullmatch(txt.replace(" ", "")):
                amounts.append((ln.y, txt))
            elif x0 < _ITEM_X and re.fullmatch(r"\d+", txt):
                item_nos.append((ln.y, txt))
            else:
                # 其余一律保留为描述（含金额列里的非金额备注、行号列的非数字等）
                desc_words.append((ln.y, x0, txt))

    conf = config.PDF_TEXT_CONFIDENCE
    items: List[LineItem] = []

    # 情况一：无金额，但有描述/行号 -> 仍保留明细行并标记，不丢信息
    if not amounts:
        desc = " ".join(t for (_, _, t) in sorted(desc_words, key=lambda d: (d[0], d[1])))
        if not desc and not item_nos:
            return []
        no = item_nos[0][1] if item_nos else "1"
        items.append(LineItem(item_no=no, description=desc.strip() or None,
                              service_period=_period_str(desc), amount=None, amount_raw=None,
                              line_confidence=conf, note="未识别到金额，待人工复核",
                              source_file=file_name))
        return items

    # 情况二：单一金额 -> 合并全部描述为一行
    if len(amounts) == 1:
        ay, araw = amounts[0]
        desc = " ".join(t for (_, _, t) in sorted(desc_words, key=lambda d: (d[0], d[1])))
        val, _susp, _ = amt.parse_amount(araw)
        no = item_nos[0][1] if item_nos else "1"
        items.append(LineItem(item_no=no, description=desc.strip() or None,
                              service_period=_period_str(desc), amount=val, amount_raw=araw,
                              line_confidence=conf,
                              note=None if val is not None else "金额解析失败，保留原文",
                              source_file=file_name))
        return items

    # 情况三：多金额 -> 按 y 行带匹配描述；并兜底未归属的描述词
    assigned_idx = set()
    sorted_amts = sorted(amounts)
    for idx, (ay, araw) in enumerate(sorted_amts):
        band = [(i, w) for i, w in enumerate(desc_words) if abs(w[0] - ay) <= 16]
        assigned_idx.update(i for i, _ in band)
        desc = " ".join(t for (_, (_, _, t)) in sorted(band, key=lambda d: (d[1][0], d[1][1])))
        no = next((itxt for (iy, itxt) in item_nos if abs(iy - ay) <= 16), str(idx + 1))
        val, _susp, _ = amt.parse_amount(araw)
        items.append(LineItem(item_no=no, description=desc.strip() or None,
                              service_period=_period_str(desc), amount=val, amount_raw=araw,
                              line_confidence=conf,
                              note=None if val is not None else "金额解析失败，保留原文",
                              source_file=file_name))
    # 未被任何金额行收纳的描述词 -> 单独保留为备注行，避免丢失
    leftover = [w for i, w in enumerate(desc_words) if i not in assigned_idx]
    if leftover:
        txt = " ".join(t for (_, _, t) in sorted(leftover, key=lambda d: (d[0], d[1])))
        if txt.strip():
            items.append(LineItem(item_no=None, description=txt.strip(), amount=None,
                                  amount_raw=None, line_confidence=conf,
                                  note="未归属到具体金额行的描述，保留待复核", source_file=file_name))
    return items


def _norm_cat(s: Optional[str]) -> str:
    """类别名规范化用于匹配：小写、去非字母数字、去尾复数 s（"Sundry Expense"≈"Sundry Expenses"）。"""
    t = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return t[:-1] if t.endswith("s") else t


def _attach_detail_schedules(inv: Invoice, schedules: list) -> None:
    """把尾随"类别明细附表"的子行按类别名匹配到主发票明细行，挂到该行 sub_items，并勾稽
    （Σ子行 == 该行金额，容差 0.01）。匹配不到的类别单独提示；子行不进主 Σ明细校验（避免重复计）。"""
    if not schedules or not inv.line_items:
        return
    for g in schedules:
        cat, rows = g.get("category"), g.get("rows") or []
        if not cat or not rows:
            continue
        nc = _norm_cat(cat)
        li = next((it for it in inv.line_items
                   if it.description and nc and
                   (nc in _norm_cat(it.description) or _norm_cat(it.description) in nc)), None)
        if li is None:
            inv.add_issue("DETAIL_UNMATCHED",
                          f"明细附表类别「{cat}」未匹配到发票行，请人工核对", None, "warning")
            continue
        li.sub_items = [{"date": r.get("date"), "description": r.get("description"),
                         "amount": r.get("amount")} for r in rows]
        ssum, ok = Decimal("0"), True
        for r in rows:
            v, _s, _n = amt.parse_amount(r.get("amount"))
            if v is None:
                ok = False
                break
            ssum += v
        tail = f"（{len(rows)} 笔明细）"
        if ok and li.amount is not None and abs(ssum - li.amount) <= Decimal("0.01"):
            li.note = (li.note + "；" if li.note else "") + f"已按明细勾稽 ✓{tail}"
        elif ok and li.amount is not None:
            li.note = (li.note + "；" if li.note else "") + f"明细合计 {ssum} ≠ 行金额 {li.amount}"
            inv.add_issue("DETAIL_MISMATCH",
                          f"类别「{cat}」明细合计 {ssum} 与发票行金额 {li.amount} 不一致",
                          None, "warning")


def _period_str(desc: str) -> Optional[str]:
    s, e = dt.extract_period(desc)
    if s or e:
        return f"{s or '?'} ~ {e or '?'}"
    return None
