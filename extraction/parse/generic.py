"""版式无关的通用字段提取（标签锚点 + 几何就近取值）。

固定模板正则（template_rules）只认唯一一张样例发票的字面标签与双栏坐标，
对"多供应商、多版式"的真实发票几乎全部落空。本模块作为**增量兜底**：对模板
没抽到的字段，用"标签同义词 + 几何关系"补齐，覆盖三类常见取值位置——

  1) 内联：`标签: 值` 同一单元格（Date: 12 January 2025 / Currency: GBP）。
  2) 同行右侧单元格（Subtotal  AUD  92,000.00 / DUE DATE  16 March 2025）。
  3) 下一行按 x 列对齐单元格（网格版式：标签一行、值在正下方一行）。

身份/日期类标签按"前缀"匹配（常带内联值）；金额类标签按"整格"匹配
（Subtotal/Tax/Total 必须是整个单元格，避免把 "Tax Advisory" 这类服务名误判为税额）。
只读结构、不臆改；抽不到就留空（交由完整性闸门/风险评分暴露），绝不假装成功。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import dates as dt

_GAP = 26.0       # 行内分列的 x 间隔阈值
_COL_TOL = 28.0   # 值单元格与标签列对齐的 x 容差

Cell = Tuple[float, float, str]   # (x0, x1, text)

# (field, pattern, type, whole)
#   whole=True  → 整个单元格须等于该标签（金额类，防服务名误命中）
#   whole=False → 前缀匹配，标签后的剩余文本即内联值（身份/日期类）
_LABELS: List[Tuple[str, str, str, bool]] = [
    ("invoice_no",       r"(?:tax\s+)?invoice\s*(no\.?|number|num|#|ref)|inv\.?\s*no\.?|bill\s*(no|number)|receipt\s*(no\.?|number|#)|proforma\s*(no\.?|number|#)|credit\s*note\s*(no\.?|number|#)|(?:our|your)\s*ref|document\s*(no\.?|number|#)|no\.?\b|reference|ref\.?\s*(no|number)|发票号码?|发票编号|单据编号|收据编号|票据号码?|請求書?番号|rechnungs\s*nr\.?|rechnungsnummer|facture\s*n[o°]?|n[o°]\s*(?:de\s*)?facture", "id", False),
    ("invoice_date",     r"invoice\s*date|issue\s*date|date\s*of\s*issue|dated|date|开票日期|开具日期|出票日期|日期|請求日|rechnungsdatum|datum|date\s*de\s*facture", "date", False),
    ("payment_due_date", r"due\s*date|payment\s*due|pay(ment)?\s*by|due|到期日|付款期限", "date", False),
    ("fund_valuation_date", r"fund\s*valuation\s*date|valuation\s*date|nav\s*date|fund\s*val\b", "date", False),
    ("currency",         r"currency|invoice\s*ccy|ccy|币种|货币", "ccy", False),
    ("customer_name",    r"bill(?:ed)?\s*to\b|sold\s*to\b|invoice\s*to\b|customer|client|to\s*[:：]|购买方|购货方|客户名称?|买方", "name", False),
    ("issuer_name",      r"from|销售方|开票方|销货方|卖方", "name", True),   # 整格恰为 "From"/中文开票方标签
    ("period",           r"service\s*period|period|服务期间?|费用期间?", "period", False),
    ("subtotal",         r"sub[\s-]*total|net\s*(?:total|amount|fees?|charges?)|goods\s*total|total\s*(?:excl|before|ex)\.?\s*\w*|amount\s*before\s*tax|小计|不含税金额|税前金额|金额合计|小計|zwischensumme|sous[\s-]*total|total\s*ht", "amount", True),
    ("sales_tax",        r"(sales\s*tax|(?:[A-Za-z]+\s+)?tax|vat|gst|cgst|sgst|igst|utgst|pst|hst|qst|service\s*charge|税额|税金|增值税额?|销项税额?|服务费|消費税|mwst\.?|ust\.?|mehrwertsteuer|tva)", "amount", True),
    ("total_due",        r"(total\s*due|amount\s*due|balance\s*(?:due|payable|outstanding|owing)|grand\s*total|gross\s*total|invoice\s*total|total\s*(amount|payable|credit|credited)|credit\s*(?:note\s*)?total|amount\s*(payable|owing|to\s*pay)|net\s*payable|please\s*pay|sum\s*(due|payable)|final\s*total|total\s*\(?\s*incl\w*\.?\s*(?:of\s*)?(?:\d+(?:\.\d+)?\s*%\s*)?(?:gst|vat|tax)?\s*\)?|total\s*charges?|total\s*(?:for\s+)?this\s*period|current\s*charges?|charges?\s*this\s*period|amount\s*due\s*this\s*period|current\s*amount\s*due|total|价税合计|价税总计|合计金额|应付金额|本期应付|本期费用合计|应付款项?|总计金额?|总金额|实付金额|合計|総計|お支払金額|gesamtbetrag|gesamtsumme|rechnungsbetrag|total\s*ttc|montant\s*total)", "amount", True),
]
# 金额标签整格匹配时允许的**尾部噪声**：多词(Total Amount Due)、及 (USD)/(15%) 后缀——
# 放宽 fullmatch，避免 "Total Amount Due (USD)" / "Balance Payable" 这类整格标签匹配不上。
_AMT_TAIL = r"(?:\s+(?:amount|due|payable|net|now))*(?:\s*[（(]?\s*(?:\d+(?:\.\d+)?\s*%|[A-Za-z]{1,4}\$?)\s*[）)]?)?"

# 币种符号字符类（与 amount 一致；不再只认 $€£，补 ¥ ₹ ₩ ฿ ₱ ₦ ₫ ₨ ₪）
_SYMC = r"[$€£¥₹₩฿₱₦₫₨₪]"
# 货币符号（含**字母前缀**写法：HK$ / US$ / S$ / NT$ / R$ …）——否则 "HK$11,610" 只剥掉 "$11,610"、
# 残留 "HK" 粘进描述，令合计行 "Net fees HK" 逃过过滤、混入明细。前缀 1–3 个字母，紧贴符号。
_SYM = r"(?:[A-Za-z]{1,3})?" + _SYMC
# 金额检测：① 符号锚定→符号后任意数（含整数/任意小数，覆盖 ¥10000/$500/HK$1,200）；② 千分位；③ 2~3 位小数。
# 仍**不认无符号无标点的裸整数**（避免把数量/年份/参考号当钱）——整数金额由"总额标签/币种码"锚定的路径认。
_GRP_US = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"                    # 美式：1,234.56（逗号千分位、点小数）
_GRP_EU = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?(?!\d)"              # 欧式：1.234,56（点千分位、逗号小数）；(?!\d) 防把 "2.500000" 前缀吃成 "2.500"（应落 _DEC 作 6 位小数）
_GRP_SP = r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+[.,]\d{1,3}"    # 空格千分位：1 234,56（含普通/不断行空格；须带小数消歧，避免吞参考号）
_GRP_AP = r"\d{1,3}(?:['\u2019]\d{3})+(?:[.,]\d+)?"    # 瑞士撇号千分位：1'234.56 / 2'500'000.00（撇号只作千分位，无歧义）
_DEC = r"\d+[.,]\d{2,8}"                                      # 无千分位、2~8 位小数：12.50 / 加密 0.041500 / BTC 8 位（有界，防吞超长串）
_PLAIN = r"\d+(?:[.,]\d+)?"                                   # 裸数/单小数（仅在币种符号锚定时才认，如 HK$690.5 / ¥5）
# 金额检测：① 符号必需→符号后任意数（含整数/任意小数，覆盖 ¥5 / HK$690.5 / € 1.234,56）；
# ②③ 符号可选但**须有千分位或小数**（不认无符号裸整数，避免把数量/年份/参考号当钱）。
# _NUM 内分支顺序：美式→欧式→空格→纯小数——欧式/空格须排在纯小数之前，否则 `\d+\.\d{2,3}` 会把
# "1.234,56" 先吃成 "1.234"（3 位小数）致欧式误读；交替不含裸空格串，避免同行两金额粘连。
_NUM_SYM = r"(?:" + _GRP_US + r"|" + _GRP_EU + r"|" + _GRP_SP + r"|" + _GRP_AP + r"|" + _DEC + r"|" + _PLAIN + r")"
_NUM_OPT = r"(?:" + _GRP_US + r"|" + _GRP_EU + r"|" + _GRP_SP + r"|" + _GRP_AP + r"|" + _DEC + r")"
_MONEY = re.compile(
    r"-?\(?\s*" + _SYM + r"\s*" + _NUM_SYM + r"\)?"          # 符号必需→可带字母前缀(HK$/US$/€)
    r"|-?\(?\s*" + _SYMC + r"?\s*" + _NUM_OPT + r"\)?")      # 符号可选→**不**加字母前缀(否则 "USD 950" 的 USD 被吃进金额)

# 值槽里"无千分位无小数的整数金额"（日元/韩元等无小数币种，或没写千分位的整数）：整格 =
# 可选币种码/符号 + (千分位整数 或 ≥3 位裸整数)。仅在 _resolve 的**金额值槽**里作兜底认——
# 那里右侧就是金额、无 qty 列，故不会误吃数量；限 ≥3 位裸整数避免把 "3"/"10" 当金额。
_INT_AMT = re.compile(r"^\s*(?:[A-Z]{3}\s+|" + _SYMC + r"\s*)?(\d{1,3}(?:,\d{3})+|\d{3,})\s*$")

# 行内"无标签"日期子串（用于发票日期与发票号同址、无 ISSUE DATE 标签的版式）
_DATE_SUB = re.compile(
    r"\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}[/-][A-Za-z0-9]{1,9}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/_]{2,}")
_CCY = re.compile(r"\b([A-Z]{3})\b|(" + _SYMC + r")")
_RATE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# 区块标题/列头等——绝不能当作字段值
_STOP = re.compile(
    r"^(invoice(\s*details)?|contact|description|service|bank|payment|terms|amount|qty|"
    r"quantity|unit\s*price|details|notes?|reference|currency|period|subtotal|tax|total|"
    r"bill(ed)?\s*to|sold\s*to|due\s*date|issue\s*date|charge(\s*narrative)?|"
    r"line\s*items?|narrative|scenario|po\s*(number|#)|purchase\s*order|billing)\b",
    re.IGNORECASE)
# 地址里若出现这些 → 是抓串了（标签行/免责声明/区块头混入），应判噪声并用更干净的来源
_ADDR_NOISE = re.compile(
    r"\b(date|due|terms?|invoice|po\s*number|purchase\s*order|not\s+for\s+payment|"
    r"simulated|fixture|charge\s*narrative|line\s*items?|scenario|subtotal|total\s*due)\b",
    re.IGNORECASE)


def addr_noisy(s: str) -> bool:
    """地址是否抓串了（混入标签/免责声明/区块头/**明细金额**）。"""
    return bool(_ADDR_NOISE.search(s or "")) or _price_in(s or "")


def _cells(words: List[Cell]) -> List[Cell]:
    """把一行的词按 x 间隔聚成单元格（列）。words 已按 x 排序。"""
    cells: List[Cell] = []
    cur: List[Cell] = []
    for w in words:
        if cur and w[0] - cur[-1][1] > _GAP:
            cells.append((cur[0][0], cur[-1][1], " ".join(c[2] for c in cur)))
            cur = []
        cur.append(w)
    if cur:
        cells.append((cur[0][0], cur[-1][1], " ".join(c[2] for c in cur)))
    return cells


def _is_label(text: str) -> bool:
    t = text.strip()
    return any(re.match(pat, t, re.IGNORECASE) for _, pat, _, _ in _LABELS)


def _label_match(text: str) -> Optional[Tuple[str, str, str]]:
    """单元格是否命中某字段标签；返回 (field, type, 内联剩余值)。"""
    t = text.strip()
    for field, pat, typ, whole in _LABELS:
        if whole:
            # pat 用非捕获组包裹：pat 内的 `|` 分支不再让 _AMT_TAIL / 内联冒号值 只绑定到最后一个分支
            # （曾致 "Subtotal: 2,741" 的内联值取不到 → 回退取到下方 Total Due 的额，税前额被误成总额）。
            base = r"(?:" + pat + r")" + _AMT_TAIL
            if re.fullmatch(base + r"\s*[.．:：]*", t, re.IGNORECASE):
                return field, typ, ""            # 整格即标签（容尾部冒号/句点，如 "Subtotal:"、OCR 常把 "Total" 读成 "Total."）→ 值在相邻格/下一行
            # 内联冒号值："Total Due: USD 2,953.43" / "Tax: USD 212.43"（防 "Tax Advisory" 误命中）
            m = re.match(base + r"\s*[:：]\s*(?P<v>\S.*)$", t, re.IGNORECASE)
            if m:
                return field, typ, m.group("v")
        else:
            m = re.match(pat, t, re.IGNORECASE)
            if m:
                return field, typ, t[m.end():].strip(" :：-–\t")
    return None


def _money(text: str) -> Optional[str]:
    m = _MONEY.search(text or "")
    if not m:
        return None
    tok = m.group(0).strip()
    # 保留负号：负号可能被币种码隔开（"-GBP 1,453.68" → _MONEY 只匹配到 "1,453.68"），
    # 或整体被括号包住（会计式负数 "(1,453.68)"）——贷记单/退款常见。
    if not tok.startswith("-"):
        pre = (text or "")[:m.start()]
        if re.search(r"[-−]\s*(?:[A-Za-z]{1,4}\s*)?[$€£¥₹₩฿₱₦₫₨₪]?\s*$", pre) or \
           (pre.rstrip().endswith("(") and ")" in (text or "")[m.end():]):
            tok = "-" + tok.lstrip("(").rstrip(")")
    return tok


def _accept(typ: str, text: str) -> Optional[str]:
    """按类型校验候选值文本；通过返回规整后的 raw，否则 None。"""
    s = (text or "").strip(" :：-–\t")
    if not s:
        return None
    if typ == "amount":
        return _money(s)
    if typ == "date":
        if not re.search(r"\d", s):
            return None
        iso, _ = dt.normalize_date(s)
        return s if iso else None
    if typ == "id":
        if not re.search(r"\d", s) or _STOP.match(s):
            return None
        m = _ID.search(s)
        return m.group(0) if m else None
    if typ == "ccy":
        m = _CCY.search(s)
        return (m.group(1) or m.group(2)) if m else None
    # name / period：拒绝区块标题/列头与纯标签
    if _STOP.match(s) or _is_label(s):
        return None
    return s


# 账单"累计/年初至今"列表头（本期/累计双金额列版式）——取金额时排除该列，只取本期
_YTD_HEAD = re.compile(r"year\s*to\s*date|y[\s.\-]*t[\s.\-]*d\b|\bytd\b|cumulative|"
                       r"prior\s*period|累计|本年累计|年初至今|至今累计", re.IGNORECASE)


def _ytd_col_x(rows) -> Optional[float]:
    """检测"YTD/累计"金额列的 x 中心（本期/累计双列账单）；无则 None。"""
    for cells in rows:
        for (x0, x1, t) in cells:
            if _YTD_HEAD.search(t or ""):
                return (x0 + x1) / 2
    return None


def _resolve(typ: str, ci: int, row_cells, lx0: float, following_rows, exclude_x=None) -> Optional[str]:
    """按"内联 → 同行右侧 → 下方同列"顺序取值。金额取该位置最靠右的钱数。
    following_rows：标签行**之后的若干行**（不止紧邻一行）——多栏发票里标签与其正下方的值
    常被其它栏的行插隔（如标题行），故向下扫描、跳过该列为空的行，直到该列首个被占的行为止。
    exclude_x：若给定（本期/累计双列发票里的"YTD/累计"列 x），取金额时**跳过该列**，避免抓到累计额。"""
    # 同行右侧
    right = []
    for (cx0, _cx1, ctext) in row_cells[ci + 1:]:
        if _is_label(ctext):
            break
        if exclude_x is not None and abs((cx0 + _cx1) / 2 - exclude_x) <= _COL_TOL:
            continue                          # 跳过 YTD/累计列的金额（只取本期列）
        right.append(ctext)
    if typ == "amount":
        # 空格千分位（1 234,56）会被 fitz 在空格处切成多个 word 单元格（'88'/'400,00'/'€'）——
        # 合计标签行右侧只有金额、无 qty 列，可安全地把碎片拼回再认（拼后含空格才用，避免影响常规单值）。
        joined = _money(" ".join(right))
        if joined and re.search(r"\d[\s\u00a0\u202f]\d", joined):
            return joined
        monies = [_money(c) for c in right if _money(c)]
        if monies:
            return monies[-1]            # 金额列在最右
        # 兜底：无小数/无千分位的整数金额（日元/韩元或没写千分位）——_MONEY 不认，值槽里整格纯整数即金额
        ints = [c for c in right if _INT_AMT.match(c or "")]
        if ints:
            return _INT_AMT.match(ints[-1]).group(1).replace(",", "")
    else:
        for c in right:
            v = _accept(typ, c)
            if v is not None:
                return v
    # 下方同列：逐行向下找 x 对齐的单元格；该列为空的行跳过，遇首个被占的行即定（是则取值，
    # 非本类型/是标签则停，不再越过它继续找，避免抓到更下方无关行的值）。
    for nxt in (following_rows or []):
        aligned = sorted((abs(cx0 - lx0), ctext)
                         for (cx0, _c1, ctext) in (nxt or []) if abs(cx0 - lx0) <= _COL_TOL)
        if not aligned:
            continue                     # 这一行在该列没有单元格（被其它栏插隔）→ 继续往下
        for _d, ctext in aligned:
            v = _accept(typ, ctext)
            if v is not None:
                return v
        break                            # 该列首个被占的行不是本类型/是标签 → 停，不越过
    return None


def _amount_key(raw: str) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", raw.replace(",", "")) or 0)
    except ValueError:
        return 0.0


# "强"应付总额标签（明确表示"应付/最终合计"）——用于在多个 total 候选中优先取主发票的应付额，
# 而不被尾随明细页的孤立 "Total" 覆盖。裸 "Total" 属弱标签。
# 全系统**唯一**的"应付总额"标记表（字段提取 + 多发票张数估计共用，避免多套不一致）。
# 均为"最终应付/合计"强标签；裸 "Total" 属弱标签、不入表（会误数明细小计）。
_TOTAL_STRONG = re.compile(
    r"total\s*due|amount\s*due|balance\s*due|grand\s*total|gross\s*total|"
    r"invoice\s*total|total\s*payable|amount\s*payable|net\s*total", re.IGNORECASE)


_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
# 电话：国际格式 "+国码 号码"，或带 Tel/Phone/Fax 标签的号码（紧规则，避免误命中发票号/账号）
_PHONE_INTL = re.compile(r"\+\d[\d\s().\-]{6,}\d")
_PHONE_LABELED = re.compile(
    r"(?:tel|phone|fax|mobile|cell|ph|telephone)\b\s*[:.]?\s*(\+?\(?\d[\d\s().\-]{6,}\d)", re.IGNORECASE)
# 本地/无标签电话：括号区号 或 **3+ 组** 2~4 位数字(空格/短横/点分隔)。金额只有 1 个小数点、
# 日期用斜杠或能被 normalize_date 解析——都靠下方过滤排除，避免误吞金额/账号/日期。
_PHONE_LOCAL = re.compile(r"\(?\d{2,4}\)?[\s.\-]\d{2,4}[\s.\-]\d{2,4}(?:[\s.\-]\d{2,4})?")


def find_phone(text: str) -> Optional[str]:
    """从文本里取电话号码：Tel/Phone 标签号 → 带 + 国际号 → 本地分组号（排除金额/日期/过短过长）。"""
    m = _PHONE_LABELED.search(text or "")
    if m:
        return m.group(1).strip()
    m = _PHONE_INTL.search(text or "")
    if m:
        cand = m.group(0).strip()
        if 7 <= len(re.sub(r"\D", "", cand)) <= 15:   # 电话位数区间；防病态 "+1 1 1…" 存成数 KB
            return cand
    for m in _PHONE_LOCAL.finditer(text or ""):
        cand = m.group(0).strip()
        digits = re.sub(r"\D", "", cand)
        if not (7 <= len(digits) <= 15):
            continue                              # 电话位数区间外（太短=数量/年份，太长=账号）
        if dt.normalize_date(cand)[0]:
            continue                              # 能解析成日期 → 是日期不是电话
        if re.search(r"[.,]\d{2}\b", cand):
            continue                              # 形如 …,dd / ….dd → 像金额尾，跳过
        return cand
    return None
# 银行明细子标签（在 BANK/PAYMENT 区块内匹配）。account_no 用否定式避免"Account Name/Holder"误入。
_BANK_LABELS = [
    ("bank_name",         r"beneficiary\s*bank|^our\s*bank\b|^bank\s*name|^bank\b"),
    ("bank_account_name", r"account\s*name|account\s*holder|^beneficiary\b|payee|^name$"),
    ("bank_account_no",   r"account\s*(?:no\.?|number|#|:|\b\d)|acct\.?\s*no|a/?c\s*no|\biban\b|\bbsb\b"),
    ("bank_swift",        r"swift(?:\s*/?\s*bic)?(?:\s*code)?|^bic(?:\s*code)?$|routing|sort\s*code|\baba\b"),
]
# 银行/付款区块表头（放宽：banking/account/payment·wire instructions/our bank/beneficiary…都算）
_BANK_HEADER = re.compile(
    r"bank(?:ing)?\s*(?:details|information|info|account)|bank\s*transfer|account\s*details|"
    r"payment\s*(?:details|information|info|instruction|method)|wire\s*(?:details|transfer|instruction)|"
    r"payment\s*by\s*wire|remit(?:tance)?|swift\s*code|our\s*bank|beneficiary(?:\s*details|\s*bank)?|"
    r"pay(?:able)?\s*to|how\s*to\s*pay|for\s*(?:payment|wire)", re.IGNORECASE)
_BILLTO = re.compile(
    r"^(billed?\s*to|bill\s*to|sold\s*to|invoice\s*to|ship\s*to|"
    r"buyer|recipient|attention|attn)\b", re.IGNORECASE)


def _value_after(cells, ci, lx0, next_cells) -> Optional[str]:
    """标签后取值：同行右侧首个非空非标签单元格，否则下一行按列对齐。"""
    for (_x0, _x1, ct) in cells[ci + 1:]:
        s = ct.strip()
        if s and not _is_label(s):
            return s
    if next_cells:
        for (cx0, _x1, ct) in next_cells:
            if abs(cx0 - lx0) <= _COL_TOL and ct.strip() and not _is_label(ct):
                return ct.strip()
    return None


_PAGE_CENTER = 300.0   # 左右半区分界（与抽取层 COL_SPLIT 一致；标准竖版调校值）
_WIDE_CONTENT = 700.0  # 内容右缘超过此值视为宽页/横版 → 用内容中线而非固定 300


def _content_center(rows) -> float:
    """左右半区分界：标准竖版沿用 300（零回归）；内容明显偏宽(横版)则取内容 x 中线自适应。"""
    xs = [x for cells in rows for (x0, x1, t) in cells if t.strip() for x in (x0, x1)]
    if not xs:
        return _PAGE_CENTER
    lo, hi = min(xs), max(xs)
    return (lo + hi) / 2 if hi > _WIDE_CONTENT else _PAGE_CENTER


def _region_cell(cells, lx0, center: float = _PAGE_CENTER) -> Optional[str]:
    """取该行与标签**同半区**（左/右）内最靠左的单元格文本。

    比固定列带稳：BILL TO 块的值常与标签右对齐却左缘错位（名字长→起点更靠左），
    用"同半区"收集可兼容，又能排除交错的另一栏。center 为左右半区分界（按内容宽自适应）。
    """
    if lx0 >= center:
        cand = sorted((x0, t) for (x0, _x1, t) in cells if x0 >= center and t.strip())
    else:
        cand = sorted((x0, t) for (x0, _x1, t) in cells if x0 < center and t.strip())
    return cand[0][1] if cand else None


def extract_billto(rows) -> Tuple[Optional[str], List[str], Optional[str], Optional[str]]:
    """BILL TO 多行块：返回 (客户名, 地址行列表, 邮箱, 电话)。按"标签列带向下收集"，容交错行。"""
    idx = lx = None
    for i, cells in enumerate(rows):
        for (x0, _x1, t) in cells:
            if _BILLTO.match(t.strip()):
                idx, lx = i, x0
                break
        if idx is not None:
            break
    if idx is None:
        return None, [], None, None
    center = _content_center(rows)        # 左右半区分界按内容宽自适应（横版不再钉死 300）
    name = None
    addr: List[str] = []
    email = None
    phone = None
    collected = 0                         # 按"实际收集到的同区行"计数（交错的另一栏不计），
    for j in range(idx + 1, len(rows)):   # 否则交错行会吃满窗口、读不到后面的邮箱
        if collected >= 8:
            break
        cell = _region_cell(rows[j], lx, center)
        if cell is None:
            continue                      # 该行此列带内无内容（交错的另一栏），跳过、不计数
        s = cell.strip()
        if not s:
            continue
        if _STOP.match(s) or _is_label(s) or is_line_item_row(s):
            break                         # 撞到区块标题/明细表头/明细行(含金额)即止
        collected += 1
        m = _EMAIL.search(s)
        if m:
            email = email or m.group(0)
            s = s.replace(m.group(0), "")
        ph = find_phone(s)
        if ph:
            phone = phone or ph
            s = s.replace(ph, "")
        s = s.strip(" ,;|\t")             # 去每行首尾分隔符，避免拼接出 ",," 重复逗号
        if not s:
            continue
        if name is None:
            name = s
        else:
            addr.append(s)
    return name, addr, email, phone


def extract_bank(rows) -> Dict[str, str]:
    """银行明细：定位 BANK/PAYMENT 区块后，逐子标签取值（同行右侧/下一行对齐）。"""
    start = None
    for i, cells in enumerate(rows):
        if _BANK_HEADER.search(" ".join(c[2] for c in cells)):
            start = i
            break
    if start is None:
        return {}
    out: Dict[str, str] = {}
    for i in range(start + 1, len(rows)):
        cells = rows[i]
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        for ci, (x0, _x1, t) in enumerate(cells):
            for field, pat in _BANK_LABELS:
                if field in out:
                    continue
                m = re.match(pat, t.strip(), re.IGNORECASE)
                if not m:
                    continue
                rem = t[m.end():].strip(" :：-–.\t#")    # 去尾标点：'Account No.' 的 '.' 不算值
                val = rem or _value_after(cells, ci, x0, nxt)
                if val and not _is_label(val) and not _STOP.match(val):
                    out[field] = val
    return out


# ---- 无需表头的银行正则回填（健壮，哪儿都能扫；补齐行版路径漏掉的四个 bank_ 字段）----
_RX_SWIFT = re.compile(r"\b(?:swift|bic)\b(?:\s*code)?\s*[:#]?\s*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", re.I)
_RX_IBAN = re.compile(r"\biban\b\s*(?:no\.?|number)?\s*[:#]?\s*([A-Z]{2}\d{2}[A-Z0-9]{8,30})\b", re.I)
_RX_ACCT = re.compile(r"\b(?:account|acct|a/c)\b\s*(?:no\.?|number|#)?\s*[:#]?\s*([0-9][0-9\- ]{5,}[0-9])", re.I)
_RX_SORT = re.compile(r"\b(?:sort\s*code|routing(?:\s*(?:no\.?|number))?|aba|bsb)\b\s*[:#]?\s*([0-9][0-9\- ]{3,})", re.I)
# 户名/行名的分隔符：冒号/井号 **或** 列间隙(2+空格)/制表符——覆盖"Account Name␣␣ACME"这类无冒号列对齐。
# 值须以字母/数字起头（防把散文如 "beneficiary should…" 当户名；单空格散文因需 2+空格而天然不命中）。
_NSEP = r"(?:\s*[:#]\s*|\s{2,}|\t+)"
_RX_BENE = re.compile(r"\b(?:beneficiary(?:\s*name)?|account\s*(?:name|holder)|payee)\b" + _NSEP + r"([A-Za-z0-9][^\n]*)", re.I)
_RX_BANKNM = re.compile(r"\b(?:beneficiary\s*bank|bank\s*name|our\s*bank)\b" + _NSEP + r"([A-Za-z0-9][^\n]*)", re.I)
_RX_BANK_COLON = re.compile(r"\bbank\b\s*[:#]\s*([A-Za-z0-9][^\n]*)", re.I)   # 裸 "Bank" 仅认冒号(避免 "Bank charges" 误入)
# 标签独占一行（值在下一行）的匹配
_RX_BENE_LBL = re.compile(r"^\s*(?:beneficiary(?:\s*name)?|account\s*(?:name|holder)|payee)\s*[:#]?\s*$", re.I)
_RX_BANKNM_LBL = re.compile(r"^\s*(?:beneficiary\s*bank|bank\s*name|our\s*bank|bank)\s*[:#]?\s*$", re.I)
# 下一行是否"又是个标签"（是则不能当值）
_BANK_LABEL_WORDS = re.compile(
    r"^\s*(?:swift|bic|iban|account|acct|a/c|sort\s*code|routing|aba|bsb|bank|beneficiary|"
    r"payee|address|tel|phone|email|attn|attention)\b", re.I)


def _next_line_name(lines, lbl_rx) -> Optional[str]:
    """标签独占一行时，取下一非空行作为值（值须像名字、且本身不是另一个标签）。"""
    for i, ln in enumerate(lines):
        if lbl_rx.match(ln) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and re.match(r"[A-Za-z0-9]", nxt) and not _BANK_LABEL_WORDS.match(nxt):
                return _clean_bank_val(nxt)
    return None


def _clean_bank_val(v: str) -> Optional[str]:
    """银行值取到列间隙/行尾前，去噪、限长。"""
    v = re.split(r"\s{2,}", (v or "").strip())[0].strip(" :：-–.,;\t")
    return v[:80] or None


def bank_from_text(text: str) -> Dict[str, str]:
    """从全文用正则直接抓银行四字段——**不需要区块表头、版式无关**（行版路径的健壮兜底）。"""
    out: Dict[str, str] = {}
    text = text or ""
    m = _RX_SWIFT.search(text)
    if m:
        out["bank_swift"] = m.group(1).strip()
    m = _RX_IBAN.search(text) or _RX_ACCT.search(text)
    if m:
        out["bank_account_no"] = m.group(1).strip()
    lines = text.split("\n")
    # 户名：内联(冒号/列间隙) → 标签独占行时取下一行
    m = _RX_BENE.search(text)
    name = _clean_bank_val(m.group(1)) if m else None
    name = name or _next_line_name(lines, _RX_BENE_LBL)
    if name:
        out["bank_account_name"] = name
    # 行名：显式"Bank Name/Beneficiary Bank/Our Bank"(灵活分隔) → 裸"Bank:"(仅冒号) → 标签独占行下一行
    m = _RX_BANKNM.search(text) or _RX_BANK_COLON.search(text)
    bn = _clean_bank_val(m.group(1)) if m else None
    bn = bn or _next_line_name(lines, _RX_BANKNM_LBL)
    if bn:
        out["bank_name"] = bn
    if "bank_swift" not in out:                 # 无 SWIFT 时用 sort code/routing 兜到 swift 字段
        ms = _RX_SORT.search(text)
        if ms:
            out["bank_swift"] = ms.group(1).strip()
    return out


def extract_period_range(rows) -> Tuple[Optional[str], Optional[str]]:
    """服务期间：标签后取起始日期，并在其值列的下一行找 'to/– 止日期'（跨行区间）。"""
    for i, cells in enumerate(rows):
        for ci, (x0, _x1, t) in enumerate(cells):
            if not re.match(r"(service\s*period|^period$|period\b)", t.strip(), re.IGNORECASE):
                continue
            rem = re.sub(r"^(service\s*period|period)\s*[:：]?\s*", "", t.strip(), flags=re.IGNORECASE)
            # 收集本标签相关的文本：内联剩余 + 同行右侧（遇下个标签即止）+ 后续数行同列带
            segs = [rem] if rem else []
            vx = None
            for (cx0, _c1, ct) in cells[ci + 1:]:
                if _is_label(ct):
                    break                 # 同行的下个标签（如 CURRENCY），不是期间值
                segs.append(ct)
                vx = vx if vx is not None else cx0
            anchor = vx if vx is not None else x0   # 无同行值则锚定标签列（值在正下方）
            for j in range(i + 1, min(i + 4, len(rows))):
                for (cx0, _c1, ct) in rows[j]:
                    if abs(cx0 - anchor) <= _COL_TOL * 2:
                        segs.append(ct)
            blob = " ".join(s for s in segs if s).strip()
            # 抽出所有可解析日期，**保留原始文本**（取首=起、末=止）；
            # 返回原始文本而非 ISO，使审核界面能把它定位回原件（raw 与原文一致）。
            raws = [dm.group(0).strip() for dm in _DATE_SUB.finditer(blob)
                    if dt.normalize_date(dm.group(0))[0]]
            if not raws:
                s, e = dt.extract_period(blob)
                if s or e:
                    return s, e
            elif len(raws) == 1:
                return raws[0], None
            else:
                return raws[0], raws[-1]
    return None, None


_LI_HEAD1 = re.compile(r"description|service|particular|item|fee|charge|narrative|details?|项目|名称|品名|商品|服务|摘要|货物|品目", re.IGNORECASE)
_LI_HEAD2 = re.compile(r"qty|quantity|amount|total|rate|unit|price|fee|charge|value|cost|sum|period|year\s*to\s*date|ytd|金额|数量|单价|税额|价税|税率|本期|累计", re.IGNORECASE)


def _looks_li_header(txt: str) -> bool:
    """是否像明细表头：需**描述列名**与**金额/数量列名**命中在**不重叠**的位置（即两个不同的词/列），
    而非同一个词同时充当两列——否则 "STATEMENT OF FEES"/"Net fees" 里单个 "fee" 同时命中描述列名
    与金额列名正则，会被误判成表头（致其下方抬头被当明细、真表头真明细全错过）。"""
    h1 = [(m.start(), m.end()) for m in _LI_HEAD1.finditer(txt or "")]
    h2 = [(m.start(), m.end()) for m in _LI_HEAD2.finditer(txt or "")]
    for a in h1:
        for b in h2:
            if a[1] <= b[0] or b[1] <= a[0]:      # 两个列名命中区间不重叠 = 确是两列
                return True
    return False


# 数量：整数(任意位)/千分位/小数（工时如 2.5、数量 10000/1,500）。价格样(带小数/货币符号)另行排除。
_QTY_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _price_in(s: str) -> bool:
    """行内是否出现"价格样"金额（**带小数点或货币符号**）——地址不会有、明细行才有。
    仅"逗号千分位无小数无符号"（如街号 1,234）不算，避免误伤真实地址。"""
    m = _MONEY.search(s or "")
    if not m:
        return False
    tok = m.group(0)
    return ("." in tok) or bool(re.search(_SYMC, tok))


def is_line_item_row(s: str) -> bool:
    """一行是否像**明细行/明细表头**（地址块收到这里就该停）：含价格样金额，
    或**同时**含"描述列词 + 金额/数量列词"（明细表头）。版式无关的通用信号，不写死表头文字。"""
    if _price_in(s):
        return True
    return bool(_LI_HEAD1.search(s) and _LI_HEAD2.search(s))


def _is_price_token(tok: str) -> bool:
    """金额 token 是否"价格样"（带小数点或货币符号）——用于把门牌号/账号里的**裸逗号数**
    （如 "1,234 Main Street" 的 1,234）排除在"行金额"之外（明细侧对称守卫）。"""
    return ("." in (tok or "")) or bool(re.search(_SYMC, tok or ""))


# 明细收集时"已离开表、进入地址/收款方块"的强信号（该行无金额时用于止收，防左下角地址被当明细）
_ADDR_STOP = re.compile(
    r"\b(suite|floor|avenue|boulevard|blvd|street|road|lane|p\.?\s*o\.?\s*box|postal|"
    r"zip\s*code|tower|level|block|building|bldg|district|province|beneficiary|"
    r"account\s*(name|no\.?|number)|remit\s*to|pay(able)?\s*to)\b", re.IGNORECASE)
# 明细区终止标记：在 各类"合计/小计"处自然结束（含 Net/Gross Total，避免合计行被当明细）；
# 不含 ^tax/vat/gst（否则会把名为 "Tax Advisory" 的服务行误判为税额行而提前截断）。
_LI_STOP = re.compile(
    r"sub[\s-]*total|net\s*(total|amount|fees?|charges?)|gross\s*total|total\s*due|grand\s*total|"
    r"invoice\s*total|total\s*(fees?|charges?)|amount\s*(due|payable)|"
    r"total\s*payable|^total\b|incl\.?\s*tax|^bank\b|payment|swift|beneficiary|notes?\b|thank|please\s+|"
    r"^discount\b|^less\b|^rounding\b|round\s*off|"
    r"goods\s*total|total\s*(excl|before|ex|incl|charges?)|amount\s*before\s*tax|"
    r"balance\s*(outstanding|owing)|amount\s*(owing|to\s*pay)|net\s*payable|sum\s*(due|payable)|final\s*total|service\s*charge|"
    r"zwischensumme|sous[\s-]*total|total\s*ht|total\s*ttc|montant\s*total|gesamtbetrag|mwst|"
    r"小计|价税合计|价税总计|^合计|税额|税金|增值税|应付(金额|款)|总计金额?|折扣|优惠|服务费|小計|^合計|消費税",
    re.IGNORECASE)


# 汇总/合计行的描述：整条描述**就是**一个合计标签（可带尾随 % / 币种 / 冒号），如
# "Subtotal" / "Sales Tax" / "Total Due" / "Net Amount" / "VAT (10%)"。用于把误入明细的
# 合计/税/小计**汇总行**剔除，避免污染明细勾稽；"Tax Advisory Services" 这类含额外词的**服务名不误删**。
_SUMMARY_DESC = re.compile(
    r"^(?:"
    r"sub[\s-]*total|grand\s*total|gross\s*total|"
    r"net\s*(?:total|amount|fees?|charges?)|"        # Net Total / Net Amount / Net Fees / Net Charges
    r"invoice\s*total|total\s*(?:fees?|charges?)|"   # Invoice Total / Total Fees / Total Charges
    r"total(?:\s+(?:amount|due|payable|now))*|"      # Total / Total Due / Total Amount Due …
    r"amount\s*(?:due|payable|owing|to\s*pay)|balance\s*(?:due|payable|outstanding|owing)|"
    r"sales\s*tax|(?:[A-Za-z]+\s+)?tax|vat|gst|pst|hst|qst|mwst\.?|ust\.?|mehrwertsteuer|tva|"
    r"goods\s*total|total\s*(?:excl|before|ex|incl|charges?|payable|amount|due)\w*|amount\s*before\s*tax|"
    r"net\s*payable|sum\s*(?:due|payable)|final\s*total|please\s*pay|service\s*charge|"
    r"discount|less(?:\s+discount)?|freight|shipping|delivery|handling|rounding|round\s*off|"
    r"zwischensumme|sous[\s-]*total|total\s*ht|total\s*ttc|montant\s*total|gesamtbetrag|gesamtsumme|rechnungsbetrag|"
    r"小计|价税合计|价税总计|合计|税额|税金|增值税额?|销项税额?|应付金额|总计金额?|总金额|折扣|优惠|服务费|小計|合計|総計|消費税"
    r")\s*[:：]?\s*(?:\(?\s*(?:\d+(?:\.\d+)?\s*%|[A-Za-z]{1,4}\$?)\s*\)?)?$", re.IGNORECASE)


def is_summary_desc(desc: Optional[str]) -> bool:
    """描述是否本身就是一条"合计/税/小计"汇总行（应从明细里剔除，防止污染勾稽）。"""
    return bool(_SUMMARY_DESC.match((desc or "").strip()))


# 水印/印章词（PAID/COPY/作废/发票专用章…）——整行只由这些词组成时是水印/印章、非真实明细，剔除
_WM_WORDS = (r"copy|paid|draft|void|original|duplicate|specimen|cancell?ed|unpaid|overdue|"
             r"not\s*for\s*payment|作废|副本|已付款?|付讫|收讫|样本|正本|复印件|"
             r"(?:[一-鿿]+\s*)?(?:发票专用章|财务专用章|公章|专用章|official\s*seal|seal)")
_WATERMARK = re.compile(r"^(?:\s*(?:" + _WM_WORDS + r")\s*[\W_]*)+$", re.IGNORECASE)


_WM_ONE = re.compile(r"(?:" + _WM_WORDS + r")", re.IGNORECASE)


def is_watermark(text: Optional[str]) -> bool:
    """整行是否只由水印/印章词组成（可重复/带标点/被页宽裁断，如 "COPY COPY COPY"、
    "ORIGINAL ORIGINAL ORIG"、"★ 发票专用章"）。真实服务名（如 "Copy editing service"）因含
    实质非水印词而不命中，安全。"""
    t = (text or "").strip()
    if not t:
        return False
    if _WATERMARK.fullmatch(t):
        return True
    # 去掉所有完整水印词后，若剩余只是**短拉丁碎片**（≤4 字母，多为被页宽裁断的英文水印词尾，如 ORIG）
    # 且原行确有水印词 → 仍算水印。**中文/较长剩余不算**（避免把"作废 技术咨询服务"里的真实中文明细误删）。
    if _WM_ONE.search(t):
        rest = re.sub(r"[\W_]+", " ", _WM_ONE.sub(" ", t)).strip()
        if not rest or all(re.fullmatch(r"[A-Za-z]{1,4}", w) for w in rest.split()):
            return True
    return False


def _is_amount_cell(t: str) -> bool:
    """单元格是否整体就是金额（去掉所有钱数/币种后为空）——含两金额列被并成一格的情况。"""
    rest = _MONEY.sub("", t or "")
    rest = re.sub(r"[\$€£,\s]|[A-Z]{3}\b", "", rest)
    return not rest and bool(_MONEY.search(t or ""))


# 描述文本里"内嵌的金额"：带币种代码/符号、千分位、或两位小数的钱数
# （比 _MONEY 多认 `USD 5,000` / `$ 690` 这类带币种但无小数的；不认裸小整数=数量）
_DESC_MONEY = re.compile(
    r"(?:USD|EUR|GBP|CNY|RMB|HKD|SGD|JPY|AUD|CAD|CHF|NZD|THB|INR|KRW|AED)\s*" + _SYMC + r"?\s*\d[\d,]*(?:\.\d+)?"
    r"|" + _SYM + r"\s*\d[\d,]*(?:\.\d+)?"                  # 含字母前缀符号 HK$/US$/S$，整体剥离不留 "HK"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d+\.\d{2,3}\b", re.IGNORECASE)
# 描述内的服务分隔符：分号 / 竖线 / 换行（不切逗号——地址/金额里常见）
_DESC_DELIM = re.compile(r"\s*[;|]\s*|\s*[\r\n]+\s*")


# 金额前后的悬挂残留：连接词（"… at"/"… for"/"@"）、或被拆散的孤立币种码（"… at USD"）
_TRAIL_CONNECT = re.compile(
    r"[\s,:–—-]*\b(?:at|for|of|priced(?:\s+at)?|costing|amounting\s+to|@|=|totalling|totaling|"
    r"usd|eur|gbp|cny|rmb|hkd|sgd|jpy|aud|cad|chf)\s*$",
    re.IGNORECASE)
_LEAD_CCY = re.compile(r"^\s*(?:usd|eur|gbp|cny|rmb|hkd|sgd|jpy|aud|cad|chf)\b\s*", re.IGNORECASE)


_MAX_DESC_LEN = 500        # 单条描述合理上限：真实明细描述远短于此；超长必为畸形/攻击输入


def _strip_money(text: str) -> str:
    """规则①：描述里不出现金额——剥离内嵌钱数，清掉残留的连接标点/悬挂连接词/孤立币种码与多余空白。

    **两道防护**避免超长畸形单元格触发算法复杂度 DoS（旧 `while s!=prev` 每轮对整串重跑 sub、
    按尾部逐个剥 → O(n²)，实测 4000 字符 22s）：① 先按 `_MAX_DESC_LEN` 截断（描述不该超长）；
    ② 剥离循环设**迭代上限**（正常 1–2 轮即稳定；恶意 "…at USD"×n 也最多跑 8 轮即停）。
    """
    s = (text or "")[:_MAX_DESC_LEN]
    s = _DESC_MONEY.sub(" ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,;:|·•–—-\t")
    for _ in range(8):                     # 反复剥尾部悬挂连接词/币种码（"… at USD"→""）与首部孤立币种码
        prev = s
        s = _TRAIL_CONNECT.sub("", s)
        s = _LEAD_CCY.sub("", s)
        s = s.strip(" ,;:|·•–—-\t")
        if s == prev:
            break
    return s


def explode_description(desc: Optional[str], base_amount, base_qty=None, base_unit=None) -> List[dict]:
    """规则②：把一条描述按 分隔符 / 内嵌金额 自动划分为多条服务（每条剥离金额）。

    - 先按 `;`/`|`/换行 切；某段内若有 ≥1 个内嵌金额，则金额作为该服务的结尾（一金额一服务）。
    - 描述一律剥离金额（规则①）。未发生拆分时沿用原 数量/单价/金额（金额优先用表格右侧已解析值）。
    - 拆成多条时：首条沿用原 数量/单价；各服务金额取其内嵌金额，无内嵌金额则首条留原金额防丢总额。
    返回 [{description, quantity, unit_price, amount}]。
    """
    desc = desc or ""
    chunks = [c.strip() for c in _DESC_DELIM.split(desc) if c.strip()] or [desc]
    services: List[Tuple[Optional[str], Optional[str]]] = []   # (clean_desc, amount)
    for chunk in chunks:
        monies = list(_DESC_MONEY.finditer(chunk))
        if monies:
            pos = 0
            for m in monies:
                services.append((_strip_money(chunk[pos:m.end()]), m.group(0).strip()))
                pos = m.end()
            tail = _strip_money(chunk[pos:])
            if tail:
                services.append((tail, None))
        else:
            services.append((_strip_money(chunk), None))
    services = [(d, a) for (d, a) in services if d or a]
    if len(services) <= 1:
        d, a = services[0] if services else (_strip_money(desc), None)
        return [{"description": d or None, "quantity": base_qty,
                 "unit_price": base_unit, "amount": base_amount or a}]
    out = []
    for idx, (d, a) in enumerate(services):
        out.append({"description": d or None,
                    "quantity": base_qty if idx == 0 else None,
                    "unit_price": base_unit if idx == 0 else None,
                    "amount": (a or base_amount) if idx == 0 else a})
    return out


# 竖排表格列名（Word/docx 表格被 fitz 提取时每个单元格各占一行）
_COL_DESC = re.compile(r"^(?:(?:description|service|particulars?|item|details?)\b|项目|名称|品名|商品|服务|摘要|货物)", re.IGNORECASE)
_COL_QTY = re.compile(r"^(?:(?:qty|quantity|units?|hours?)\b|数量)", re.IGNORECASE)
_COL_PRICE = re.compile(r"^(?:(?:unit\s*price|unit\s*cost|rate|price|unit)\b|单价)", re.IGNORECASE)
_COL_AMT = re.compile(r"^(?:(?:amount|line\s*total|total|fee|charge|sum|value|cost|price)\b|金额|价税|合计)", re.IGNORECASE)


def _col_of(text: str) -> Optional[str]:
    t = (text or "").strip()
    if _COL_DESC.match(t):
        return "description"
    if _COL_AMT.match(t):
        return "amount"
    if _COL_PRICE.match(t):
        return "unit_price"
    if _COL_QTY.match(t):
        return "quantity"
    return None


def _vertical_line_items(texts: List[str]) -> List[dict]:
    """竖排表格（每个单元格独占一行）→ 明细行。

    先找连续若干"整行=一个列名"的竖排表头（须含 description 与 amount、≥2 列），
    定下列序与列数 n；其后按 n 个数据行一组重组成明细，遇 Subtotal/Total 等止。
    """
    # 找竖排表头段
    hdr_i, cols = None, None
    i = 0
    while i < len(texts):
        if len(texts[i].strip()) <= 14 and _col_of(texts[i]):
            j, run = i, []
            while j < len(texts) and len(texts[j].strip()) <= 14 and _col_of(texts[j]):
                run.append(_col_of(texts[j]))
                j += 1
            if "description" in run and "amount" in run and len(run) >= 2:
                hdr_i, cols = i, run
                break
            i = j
        else:
            i += 1
    if hdr_i is None:
        return []
    n = len(cols)
    items: List[dict] = []
    buf: List[str] = []
    for t in texts[hdr_i + n:]:
        t = t.strip()
        if not t:
            continue
        if _LI_STOP.search(t):
            break
        buf.append(t)
        if len(buf) == n:
            rec = {"description": None, "quantity": None, "unit_price": None, "amount": None}
            for col, val in zip(cols, buf):
                rec[col] = val
            # 金额列只留钱数；数量列只留整数
            for k in ("amount", "unit_price"):
                if rec[k]:
                    m = _MONEY.search(rec[k])
                    rec[k] = m.group(0).strip() if m else rec[k]
            items.append(rec)
            buf = []
    return items


# 散文"合并服务费"叙述：取"...sentence form:/following work…:"到"the charge is presented/
# consolidated service fee/the subtotal/applicable tax"之间的整段（拼接全文，**不受换行影响**）
_NARRATIVE_RX = re.compile(
    r"(?:sentence\s+form:|following\s+work[^:]*:)(.*?)"
    r"(?:the\s+charge\s+is\s+presented|consolidated\s+service\s+fee|the\s+subtotal\b|applicable\s+tax\b)",
    re.IGNORECASE | re.DOTALL)


_INLINE_DESC = re.compile(r"(?:description|service|particulars?|item|details?)\s*[:：]\s*(.+)", re.IGNORECASE)
_INLINE_AMT = re.compile(r"(?:amount\s*(?:due|payable)?|fees?|charges?|total)\s*[:：]\s*(.+)", re.IGNORECASE)


def _inline_single_item(rows) -> List[dict]:
    """兜底：无表格/竖排/散文时，识别 "Description: 服务名" + "Amount Due: 金额" 的内联单明细
    （如零售式简单发票）。只在其它路径都空手时用，避免误伤。"""
    desc = amt = None
    for cells in rows:
        txt = " ".join(c[2] for c in cells).strip()
        m = _INLINE_DESC.match(txt)
        if m and desc is None and not is_summary_desc(txt):
            cand = _strip_money(m.group(1)).strip()
            if cand and 3 <= len(cand) <= 80:      # 明细描述不会是整段页脚——限长防误吞免责声明
                desc = cand
        m2 = _INLINE_AMT.match(txt)
        if m2 and amt is None:
            mv = _MONEY.search(m2.group(1))
            if mv:
                amt = mv.group(0).strip()
    # 必须**同时**有内联描述与内联金额才作单明细（保守，避免把无金额页脚/抬头误当明细）
    if desc and amt is not None:
        return [{"description": desc, "quantity": None, "unit_price": None, "amount": amt}]
    return []


_VAT_TOTAL_RX = re.compile(r"价税合计[^0-9]*([0-9,]+\.\d{2})")
_NUMCELL = re.compile(r"^[\d,.\s%¥￥]+$")


def extract_cn_vat(lines) -> Optional[dict]:
    """中国增值税发票（专用/普通/电子）：多列表 名称|规格|单位|数量|单价|**金额**|税率|**税额**，
    金额列**不在最右**（税额在最右）——须按表头"金额"/"税额"列的 x 定位取值。
    返回 {line_items:[{description,amount}], subtotal, sales_tax, total_due} 或 None（非此版式）。"""
    from decimal import Decimal
    rows = [_cells(list(ln.words)) for ln in lines]
    full = "".join(c[2] for cs in rows for c in cs)
    if "增值税" not in full and "价税合计" not in full:
        return None
    amt_x = tax_x = None; hdr_i = None
    for i, cs in enumerate(rows):
        ts = [c[2] for c in cs]
        if any("金额" in t for t in ts) and any("税额" in t for t in ts):
            for (x0, _x1, t) in cs:
                if "金额" in t and amt_x is None:
                    amt_x = x0
                if "税额" in t:
                    tax_x = x0
            hdr_i = i; break
    if hdr_i is None or amt_x is None:
        return None

    items, sub, taxsum = [], Decimal(0), Decimal(0)
    for cs in rows[hdr_i + 1:]:
        txt = " ".join(c[2] for c in sorted(cs)).strip()   # 按 x 顺序
        if not txt:
            continue
        if txt.startswith("合计") or "价税合计" in txt:
            break
        # 该行钱按顺序依次是 单价→金额→税额（税率是 %、数量是裸整数，均不匹配）；
        # 故 金额=倒数第二个钱、税额=最后一个钱（列被合并成一格时 x 定位失效，靠顺序最稳）。
        monies = re.findall(r"\d[\d,]*\.\d{2}", txt)
        if not monies:
            continue
        amt = monies[-2] if len(monies) >= 2 else monies[-1]
        tax = monies[-1] if len(monies) >= 2 else None
        desc = next((t for (_x0, _x1, t) in sorted(cs)
                     if t.strip() and not _NUMCELL.match(t) and "%" not in t), None)
        items.append({"description": desc, "amount": amt})
        try:
            sub += Decimal(amt.replace(",", ""))
        except Exception:
            pass
        if tax:
            try:
                taxsum += Decimal(tax.replace(",", ""))
            except Exception:
                pass
    if not items:
        return None
    m = _VAT_TOTAL_RX.search(full)
    total = m.group(1) if m else str(sub + taxsum)
    return {"line_items": items, "subtotal": str(sub),
            "sales_tax": (str(taxsum) if taxsum else None), "total_due": total}


def extract_narrative_line_items(full_text: str) -> List[dict]:
    """散文叙述型明细：把整段叙述（拼接后）按分号切成各服务，再 explode 清洗。

    关键：**先拼接成整段再拆**，因此 Word/PDF 不同的换行宽度都得到一致结果
    （解决"同一发票 Word 比 PDF 多碎几条"——那是按物理行解析受换行影响所致）。
    """
    m = _NARRATIVE_RX.search(full_text or "")
    if not m:
        return []
    seg = re.sub(r"\s+", " ", m.group(1)).strip()
    out: List[dict] = []
    for piece in seg.split(";"):
        for it in explode_description(piece, None):
            d, a = it["description"], it["amount"]
            if a or re.search(r"[A-Za-z0-9]", d or ""):   # 丢掉纯标点碎片（如末尾 "."）
                out.append({"description": d, "quantity": None, "unit_price": None, "amount": a})
    return out


def extract_line_items(lines) -> List[dict]:
    """通用明细行：定位表头(DESCRIPTION/SERVICE + QTY/AMOUNT/…) → 取每行 描述/数量/单价/金额。

    行金额取**整行最右一个钱数**（= 行合计；单价为其左一个钱数；数量为纯小整数列）。
    无金额的续行并入上一条描述；遇 Subtotal/Total 等即止。
    优先级：① 散文"合并服务费"叙述（拼接整段拆分，**不受换行影响**，Word/PDF 一致）；
    ② 横排表头；③ 竖排表格（Word/docx 单元格逐行）。
    返回 [{description, quantity, unit_price, amount}]（缺项为 None，原始文本）。
    """
    # ① 散文叙述优先（拼接全文再拆，消除 Word/PDF 换行差异）
    joined = " ".join(w.text if hasattr(w, "text") else w[-1]
                      for ln in lines for w in ln.words)
    narr = extract_narrative_line_items(joined)
    if narr:
        return narr
    rows = [_cells(list(ln.words)) for ln in lines]
    ytd_x = _ytd_col_x(rows)             # 本期/累计双列账单：明细金额也排除"累计/YTD"列、只取本期
    hdr = None
    for i, cells in enumerate(rows):
        txt = " ".join(c[2] for c in cells)
        # 表头行必是**短列名行**（≤60 字）——排除含 service/fee/charge 等词的长页脚句子被误判成表头；
        # 且合计/税/小计行不可能是表头；表头需描述列名 + 金额列名命中在**不重叠**位置（两列），
        # 否则 "STATEMENT OF FEES"/"Net fees" 里单个 "fee" 同时命中两列名会被误判成表头。
        if len(txt) <= 60 and not is_summary_desc(txt) and _looks_li_header(txt):
            hdr = i
            break
    if hdr is None:
        # 横排表头未命中 → 尝试竖排表格（Word/docx）
        texts = [" ".join(c[2] for c in cells).strip() for cells in rows]
        vitems = _vertical_line_items(texts)
        if vitems:
            exploded: List[dict] = []
            for it in vitems:
                exploded.extend(explode_description(it["description"], it["amount"],
                                                    it["quantity"], it["unit_price"]))
            return [it for it in exploded
                    if (it["description"] or it["amount"]) and not is_summary_desc(it["description"]) and not is_watermark(it["description"])]
        return _inline_single_item(rows)      # 兜底：内联 "Description: …" + "Amount Due: …" 单明细
    items: List[dict] = []
    for i in range(hdr + 1, len(rows)):
        cells = rows[i]
        txt = " ".join(c[2] for c in cells).strip()
        if not txt:
            continue
        if is_watermark(txt):        # 水印/印章行（COPY/PAID/发票专用章…）——跳过，不当明细
            continue
        if _LI_STOP.search(txt):
            break
        # 整行钱数（按 x 从左到右，带 x）；只认"价格样(带小数/货币符号)或整格即金额"，
        # 排除门牌号/账号里的裸逗号数（如 "1,234 Main Street" 的 1,234 不算行金额）。
        money_cells = []
        for (x0, _x1, t) in sorted(cells):
            if ytd_x is not None and abs((x0 + _x1) / 2 - ytd_x) <= _COL_TOL:
                continue                 # 跳过 YTD/累计列的金额（明细只取本期列）
            amount_cell = _is_amount_cell(t)
            for m in _MONEY.finditer(t):
                tok = m.group(0).strip()
                if amount_cell or _is_price_token(tok):
                    money_cells.append((x0, tok))
        # 无有效金额且整行像地址/收款方块 → 判定已离开明细表，止收（防左下角地址被当明细）
        if not money_cells and _ADDR_STOP.search(txt):
            break
        money_x = {x0 for (x0, _t) in money_cells}
        monies = [tok for (_x, tok) in money_cells]
        # 数量：金额格以外的"纯数"单元格（放宽原 \d{1,3}：认 10000 / 2.5 / 任意位整数等）。
        # 用"是否金额格"区分金额 vs 数量，不再按小数点排除（否则工时 2.5 会被误当价格漏掉）。
        qty = None
        for (x0, _x1, t) in sorted(cells):
            s = t.strip()
            if s and x0 not in money_x and _QTY_RE.fullmatch(s):
                qty = s
                break
        desc = " ".join(t for (x0, _x1, t) in sorted(cells)
                        if not _is_amount_cell(t) and not _QTY_RE.fullmatch(t.strip())).strip()
        if monies:
            items.append({"description": desc or None, "quantity": qty,
                          "unit_price": monies[-2] if len(monies) >= 2 else None,
                          "amount": monies[-1]})
        elif desc and items:
            items[-1]["description"] = ((items[-1]["description"] or "") + " " + desc).strip()  # 续行并入
        elif desc:
            items.append({"description": desc, "quantity": None, "unit_price": None, "amount": None})
    # 规则①②：每条描述剥离金额 + 按分号/金额自动划分为不同服务
    exploded: List[dict] = []
    for it in items:
        exploded.extend(explode_description(it["description"], it["amount"],
                                            it["quantity"], it["unit_price"]))
    # 剔除误入的合计/税/小计汇总行（防污染明细勾稽），保留含额外词的服务名（如 "Tax Advisory"）
    return [it for it in exploded
            if (it["description"] or it["amount"]) and not is_summary_desc(it["description"]) and not is_watermark(it["description"])]


# ---- 尾随"类别明细附表"（发票主体之后，按类别分组列出子明细 + 每类小计）----------
# 起点信号：显式明细标题，或"主发票合计之后再次出现的明细表头"
_DETAIL_HEAD = re.compile(
    r"\b(detail|details|breakdown|schedule|itemi[sz]ed|analysis\s+of|supporting|appendix|"
    r"particulars\s+of|expense\s+detail)\b", re.IGNORECASE)
_MAIN_TOTAL = re.compile(
    r"gross\s*total|net\s*total|grand\s*total|total\s*due|amount\s*(due|payable)|balance\s*due",
    re.IGNORECASE)
# 真正的"表头行"列名（用于区分明细表头 vs 含 charge/total 字样的普通句子/页脚）
_HDR_COL = re.compile(
    r"^(date|description|service|particulars?|item|details?|narrative|qty|quantity|units?|hours?|"
    r"unit\s*price|unit\s*cost|rate|price|amount|line\s*total|total|fee|charge|cost|sum|value|"
    r"no\.?|ref\.?)$", re.IGNORECASE)
_HDR_DESC = re.compile(r"^(description|service|particulars?|item|details?|narrative)$", re.IGNORECASE)
_HDR_AMT = re.compile(r"^(amount|value|total|fee|charge|cost|price|sum|line\s*total)$", re.IGNORECASE)


def _is_table_header(cells) -> bool:
    """该行是否像"明细表头"：2–6 个都是短列名的单元格，且含 描述列 + 金额列。
    用来排除 'Zoe Leung … settle bank charges.' 这类含 charge/total 字样的普通句子被误当表头。"""
    txts = [c[2].strip() for c in cells if c[2].strip()]
    if not (2 <= len(txts) <= 6):
        return False
    if not all(len(x) <= 16 and _HDR_COL.match(x) for x in txts):
        return False
    return any(_HDR_DESC.match(x) for x in txts) and any(_HDR_AMT.match(x) for x in txts)


def extract_detail_schedule(lines) -> List[dict]:
    """解析尾随的"类别明细附表"：返回 [{category, rows:[{date,description,amount}], subtotal}]。

    通用识别（不写死版式）：
    - 起点 = 出现 Detail/Breakdown/Schedule/… 标题；或"主发票合计(Gross/Net/Grand Total…)之后
      再次出现的明细表头(Description + Value/Amount…)"。
    - 其后逐行：**纯文本行**=新类别头；**纯金额行**=该类别小计；**描述+金额行**=子明细
      （行首日期拆入 date）。遇文档级 "Total …" 或再次 Gross/Grand Total 即止。
    无此结构（普通发票）→ 返回 []（不影响原有解析）。
    """
    rows = [_cells(list(ln.words)) for ln in lines]
    row_txt = [" ".join(c[2] for c in cs).strip() for cs in rows]
    start = None
    seen_total = False
    for i, cells in enumerate(rows):
        t = row_txt[i]
        if _DETAIL_HEAD.search(t) and not _MONEY.search(t):
            start = i + 1
            break
        if _MAIN_TOTAL.search(t):
            seen_total = True
            continue
        if seen_total and _is_table_header(cells):   # 主合计之后再次出现"真正的明细表头"
            start = i + 1
            break
    if start is None:
        return []
    groups: List[dict] = []
    cur = None
    for i in range(start, len(rows)):
        cells = rows[i]
        t = row_txt[i]
        if not t:
            continue
        # 金额只认"整格即金额"的单元格（避免把参考号 517222.00001 这类长串误读为钱）
        amt_cells = [txt for (x0, _x1, txt) in sorted(cells) if _is_amount_cell(txt)]
        money = amt_cells[-1] if amt_cells else None
        desc = " ".join(txt for (x0, _x1, txt) in sorted(cells) if not _is_amount_cell(txt)).strip()
        if _MAIN_TOTAL.search(t) or (re.match(r"^total\b", t, re.IGNORECASE) and money):
            break                             # 文档级总计 → 明细区结束
        if desc and not money:
            if _is_label(desc) or _STOP.match(desc):
                continue                      # 字段标签/区块头/发票号前言 → 不作为类别
            cur = {"category": desc, "rows": [], "subtotal": None}
            groups.append(cur)
        elif money and not desc:
            if cur is not None and cur["subtotal"] is None:
                cur["subtotal"] = money        # 纯金额行 = 该类别小计
        elif desc and money:
            if cur is None:
                cur = {"category": None, "rows": [], "subtotal": None}
                groups.append(cur)
            dm = _DATE_SUB.search(desc)
            date = dm.group(0) if dm else None
            body = desc.replace(date, "").strip(" ,;|-\t") if date else desc
            cur["rows"].append({"date": date, "description": _strip_money(body) or body,
                                "amount": money})
    return [g for g in groups if g["rows"]]


_CCY_HEADER = re.compile(r"(?:amount|total|unit|price|rate|subtotal)\s*\(\s*([A-Z]{3})\s*\)", re.IGNORECASE)
_CCY_FOOTER = re.compile(r"(?:all\s+)?amounts?\s+(?:are\s+)?in\s+([A-Z]{3})\b|\bin\s+([A-Z]{3})\b", re.IGNORECASE)


_CCY_LABEL = re.compile(r"\b(?:currency|ccy)\b\s*[:：]?\s*([A-Z]{3})\b", re.IGNORECASE)
_CCY_AMOUNT = re.compile(   # "USD 690.00" / "JPY 10,000" 金额前的币种码（要求千分位或小数，避免误吞 "REF 1234"）
    r"\b([A-Z]{3})\s*(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{2,3})")
# 货币符号（含区域化 $）→ 币种码。US$/HK$/S$/… 明确无歧义；裸 "$" 有歧义(不映射，仅作显示符号)
_CCY_SYMBOLS = [
    (re.compile(r"US\$|\bUSD\b"), "USD"), (re.compile(r"HK\$"), "HKD"),
    (re.compile(r"S\$|SG\$"), "SGD"), (re.compile(r"A\$|AU\$"), "AUD"),
    (re.compile(r"C\$|CA\$"), "CAD"), (re.compile(r"NZ\$"), "NZD"),
    (re.compile(r"£|\bGBP\b"), "GBP"), (re.compile(r"€|\bEUR\b"), "EUR"),
    # 无歧义的区域符号（¥ 在 JPY/CNY 间有歧义，故不映射、仅作检测符号）
    (re.compile(r"₹"), "INR"), (re.compile(r"₩"), "KRW"), (re.compile(r"฿"), "THB"),
    (re.compile(r"₱"), "PHP"), (re.compile(r"₦"), "NGN"), (re.compile(r"₫"), "VND"),
    (re.compile(r"₪"), "ILS"),
]


def currency_fallback(full_text: str) -> Optional[str]:
    """币种兜底：'Currency: GBP' 标签 / 'AMOUNT (GBP)' 列头 / 'in USD' 脚注 / 'USD 690.00' 金额前缀 /
    货币符号(US$→USD、£→GBP、€→EUR…)。"""
    for rx, gi in ((_CCY_LABEL, (1,)), (_CCY_HEADER, (1,)), (_CCY_FOOTER, (1, 2)), (_CCY_AMOUNT, (1,))):
        m = rx.search(full_text)
        if m:
            return next(m.group(i) for i in gi if m.group(i)).upper()
    for rx, code in _CCY_SYMBOLS:      # 符号兜底（US$ 等区域化符号无歧义）
        if rx.search(full_text or ""):
            return code
    return None


# 散文式金额："The subtotal is USD 2,605.00. Applicable tax is USD 201.89. The total amount due is USD 2,806.89."
_AMT = r"[A-Z]{0,3}\s*" + _SYMC + r"?\s*([\d,]+(?:\.\d+)?)(?![\d,]|\.\d)(?!\s*%)"   # 吃到最大再排除百分比；不用原子组以兼容 Python 3.9
# 标签与金额之间**只允许良性连接**：is/of/等于号/冒号/空白（不允许跨越单词）——
# 否则 "Tax Advisory … 2"（服务行"税务咨询"）会被税额正则跨过"Advisory"误抓成税额=2。
_PROSE_SEP = r"(?:\s+(?:is|of|totalling|totaling|amounts?\s+to))?\s*[:：=]?\s*"
_PROSE = {
    "subtotal": re.compile(r"\bsub\s*total\b" + _PROSE_SEP + _AMT, re.IGNORECASE),
    # 否定后顾：不把 "Total excl. GST"/"incl. VAT"/"excluding tax" 里的税词当成税额（那是小计/总额标签的一部分）
    "sales_tax": re.compile(r"(?<!excl )(?<!excl\. )(?<!incl )(?<!incl\. )(?<!before )(?<!uding )"
                            r"(?:applicable\s+)?\b(?:sales\s+tax|tax|vat|gst)\b" + _PROSE_SEP + _AMT, re.IGNORECASE),
    "total_due": re.compile(r"\btotal\s+(?:amount\s+)?(?:due|payable|amount)?\b" + _PROSE_SEP + _AMT, re.IGNORECASE),
}


def prose_amounts(full_text: str) -> dict:
    """从散文句子里提取 subtotal / sales_tax / total_due（金额标签非表格/冒号时）。"""
    out = {}
    for key, rx in _PROSE.items():
        m = rx.search(full_text or "")
        if m:
            out[key] = m.group(1)
    return out


_INCL_TAX = re.compile(
    r"incl\.?\s*(?:of\s*)?tax\s*\(?\s*(\d+(?:\.\d+)?)\s*%\s*\)?\s*[:：]?\s*[A-Z]{0,3}\s*" + _SYMC + r"?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE)


def incl_tax_fallback(full_text: str):
    """税内含格式 'Incl. tax (15%): GBP 11,700.00' → 返回 (税率, 税额raw) 或 (None, None)。"""
    m = _INCL_TAX.search(full_text)
    if m:
        return m.group(1) + "%", m.group(2)
    return None, None


_PHONE = re.compile(r"^\+?\d[\d\s().-]{6,}$")
# 文档类标题/免责声明等噪声——绝不能当公司名
_DOC_NOISE = re.compile(
    r"^(simulated|tax|pro\s*forma|proforma|commercial|draft|credit|debit|recurring)?\s*invoice\b"
    r"|^your\s+invoice\b|^your\s+(bill|statement|receipt)\b"
    r"|not\s+for\s+payment|parser\s+test|test\s+fixture|^statement\b|^receipt\b|^remittance"
    r"|^(bill|billed|sold|ship|remit)\s*to\b"
    # 独立的文档类噪声词（"SIMULATED INVOICE" 被切成两格时单独的 "SIMULATED" 也拦下）
    r"|^(simulated|pro\s*forma|proforma|draft|copy|original|duplicate|specimen|sample|void)\b",
    re.IGNORECASE)
# 地址行特征（公司名一般不以数字开头、不含街道/邮编词）
_ADDR_LIKE = re.compile(
    r"^\d|\b(suite|ste\.?|floor|fl\.?|road|rd\.?|street|st\.?|ave\.?|avenue|drive|dr\.?|"
    r"lane|ln\.?|blvd|plaza|tower|unit|p\.?\s*o\.?\s*box|circle|center|centre|place)\b"
    r"|\b[A-Z]{2}\s*\d{5}\b|\b\d{4,}\b", re.IGNORECASE)
# 公司名后缀（强正向信号）
_COMPANY_SUFFIX = re.compile(
    r"\b(llc|l\.l\.c|ltd|limited|inc|incorporated|corp(oration)?|co|company|gmbh|group|"
    r"partners?|llp|plc|pte|ag|s\.?a|n\.?v|b\.?v|holdings?|capital|solutions?|services?|"
    r"systems?|consulting|advisory|advisors?|labs?|technologies|technology|ventures?|"
    r"associates|management|fiduciary|bank)\b\.?", re.IGNORECASE)


def looks_like_company(s: str) -> bool:
    """是否像公司名（排除文档标题/免责声明/字段标签/地址/电话/邮箱）。"""
    s = (s or "").strip()
    if not (2 <= len(s) <= 80):
        return False
    if _DOC_NOISE.search(s) or _is_label(s) or _STOP.match(s):
        return False
    if _EMAIL.search(s) or _PHONE.match(s) or _ADDR_LIKE.search(s):
        return False
    return True


def company_score(s: str) -> int:
    return 1 if _COMPANY_SUFFIX.search(s or "") else 0


def extract_issuer(rows, customer: Optional[str] = None) -> Tuple[Optional[str], List[str], Optional[str], Optional[str]]:
    """开票方(卖方)块：页眉里挑公司名(优先带 LLC/Ltd/Partners 等后缀) + 地址 + 邮箱 + 电话。

    跳过 'SIMULATED INVOICE'/'NOT FOR PAYMENT'/'Invoice No:'/'Your invoice' 等噪声与字段标签，
    并**排除已识别的客户名**（避免把 Bill to 的客户当成开票方）；在前若干行里取最像公司名的一行
    （后缀优先、否则最靠上），再收同区地址，并捕获电话。
    """
    cust = (customer or "").strip().lower()
    # 候选：前 8 行每行第一格里像公司名的文本
    cands = []   # (score, row, x, text)
    for i, cells in enumerate(rows[:8]):
        row_txt = " ".join(c[2] for c in cells).strip()
        if _DOC_NOISE.search(row_txt):     # 整行是文档标题/免责声明（如 "SIMULATED INVOICE"）→ 跳过
            continue
        for (x0, _x1, t) in sorted(cells):
            s = t.strip()
            if cust and s.lower() == cust:   # 是客户名 → 不是开票方，跳过
                continue
            if is_line_item_row(s):          # 明细行（含金额，如 "Consulting fee 100.00"）→ 不是开票方名
                continue
            if looks_like_company(s):
                cands.append((company_score(s), i, x0, s))
                break
    # 电话：页眉区任意行（公司名找不到也可能有电话）
    phone = next((find_phone(" ".join(c[2] for c in cs)) for cs in rows[:8]
                  if find_phone(" ".join(c[2] for c in cs))), None)
    if not cands:
        return None, [], None, phone
    # 有后缀的优先；同分取最靠上
    cands.sort(key=lambda c: (-c[0], c[1]))
    _sc, name_row, name_x, name = cands[0]
    center = _content_center(rows)        # 左右半区分界按内容宽自适应
    addr: List[str] = []
    email = None
    collected = 0
    for j in range(name_row + 1, len(rows)):
        if collected >= 6:
            break
        cell = _region_cell(rows[j], name_x, center)
        if cell is None:
            continue
        s = cell.strip()
        if not s or s.upper() == "INVOICE":
            continue
        if _is_label(s) or _STOP.match(s) or is_line_item_row(s):
            break
        collected += 1
        m = _EMAIL.search(s)
        if m:
            email = email or m.group(0)
            s = s.replace(m.group(0), "").strip(" ,;|")
        if _PHONE.match(s):
            phone = phone or find_phone(s)
            break                         # 电话之后通常是收票方/表头，止
        s = s.strip(" ,;|\t")
        if s:
            addr.append(s)
    return name, addr, email, phone


def _column_lines_below(rows, label_row: int, col_x: float, max_lines: int = 5) -> List[str]:
    """从标签所在行的下方、**同一列(col_x)** 逐行收集文本行（跳过该列为空的插隔行——
    多栏发票里 From/To 下的多行地址常被其它栏的行插隔）。遇标签/区块头/明细行即止。
    返回 [名字行, 地址行1, 地址行2, ...]（含名字，调用方按需丢首行）。"""
    out_lines: List[str] = []
    for j in range(label_row + 1, len(rows)):
        if len(out_lines) >= max_lines:
            break
        aligned = [t for (cx0, _c1, t) in rows[j] if abs(cx0 - col_x) <= _COL_TOL and (t or "").strip()]
        if not aligned:
            continue                          # 该列此行为空（被其它栏插隔）→ 跳过继续往下
        s = " ".join(aligned).strip()
        if _is_label(s) or _STOP.match(s) or is_line_item_row(s):
            break
        out_lines.append(s)
    return out_lines


def extract_generic(lines) -> Dict[str, str]:
    """对 PdfDoc.lines 跑标签锚点提取，返回 {field: raw}（仅含成功抽到的）。"""
    rows = [_cells(list(ln.words)) for ln in lines]
    row_text = [" ".join(c[2] for c in cells) for cells in rows]
    ytd_x = _ytd_col_x(rows)             # 本期/累计双列账单：取金额时排除"累计/YTD"列，只取本期
    out: Dict[str, str] = {}
    total_cands: List[Tuple[bool, str]] = []   # 总额候选 (是否强应付标签, 值)
    tax_cands: List[str] = []            # 税额候选（多条相加：印度 CGST+SGST 等拆分税）
    sub_cands: List[str] = []            # 小计候选（分组小计发票有多个，收齐后择"总小计"）
    no_row = None                        # 发票号所在行，供"同址无标签发票日期"回退
    from_pos = None                      # "From" 标签的 (行, 列x)，供开票方多行地址按列收集
    for r, cells in enumerate(rows):
        nxt = rows[r + 1:r + 5]          # 标签行之后最多 4 行，供"下方同列"跨插隔行取值
        for ci, (lx0, _lx1, ltext) in enumerate(cells):
            lm = _label_match(ltext)
            if not lm:
                continue
            field, typ, inline = lm
            if field == "sales_tax":
                mr = _RATE.search(ltext)
                if mr:
                    out.setdefault("tax_rate", mr.group(1) + "%")
            val = _accept(typ, inline) if inline else None
            if val is None:
                val = _resolve(typ, ci, cells, lx0, nxt, exclude_x=ytd_x)
            if val is None:
                continue
            if field == "total_due":
                total_cands.append((bool(_TOTAL_STRONG.search(ltext)), val))
            elif field == "sales_tax":
                tax_cands.append(val)
            elif field == "subtotal":
                sub_cands.append(val)                        # 分组小计发票有多个"subtotal"，收齐后择"总小计"
            elif field not in out:
                out[field] = val
                if field == "invoice_no" and no_row is None:
                    no_row = r
                if field == "issuer_name" and from_pos is None:   # "From" 标签位置，供地址按列收集
                    from_pos = (r, lx0)

    if tax_cands:
        if len(tax_cands) == 1:
            out["sales_tax"] = tax_cands[0]                  # 单条：原样保留（保留币种/格式）
        else:                                                # 多条（CGST+SGST 等）：相加
            from decimal import Decimal
            from extraction.parse import amount as _amt
            s = sum((_amt.parse_amount(v)[0] or Decimal(0)) for v in tax_cands)
            out["sales_tax"] = str(s)

    if total_cands:
        strong = [v for (s, v) in total_cands if s]
        weak = [v for (s, v) in total_cands if not s]
        # 应付总额优先取"强标签"(Total Due/Amount Due/Gross Total…)的**首个**（主发票在前，
        # 尾随明细页的孤立 "Total" 属弱标签、不会覆盖）；无强标签才回退取弱候选里的最大值。
        out["total_due"] = strong[0] if strong else max(weak, key=_amount_key)

    if sub_cands:
        # 分组小计发票有多个"subtotal"（各 section 小计 + 总小计）→ 取**总小计**：优先与 total 自洽
        # （sub + tax == total）的候选；否则取最大（总小计通常最大）；单个则原样。
        if len(sub_cands) == 1:
            out["subtotal"] = sub_cands[0]
        else:
            from decimal import Decimal
            from extraction.parse import amount as _amt
            tot = _amt.parse_amount(out.get("total_due"))[0]
            tax = _amt.parse_amount(out["sales_tax"])[0] if "sales_tax" in out else None
            pick = None
            if tot is not None:
                for c in sub_cands:
                    cv = _amt.parse_amount(c)[0]
                    if cv is not None and abs(cv + (tax or Decimal(0)) - tot) <= Decimal("0.01"):
                        pick = c; break
            out["subtotal"] = pick or max(sub_cands, key=_amount_key)
    # 发票日期无 ISSUE DATE 标签时：只从"带发票号的行"就近取日期（发票号行通常携签发日期，
    # 强位置信号）。取不到宁可留空交完整性闸门标记——绝不拿服务期间/页脚日期瞎凑（避免静默错值）。
    if "invoice_date" not in out and out.get("invoice_no"):
        nv = out["invoice_no"]
        due_raw = out.get("payment_due_date")
        # 候选行：每个含发票号的行，及其下一行（签发日期常在发票号同行或正下一行）
        idxs = set()
        if no_row is not None:
            idxs.update((no_row, no_row + 1))
        for i, rt in enumerate(row_text):
            if nv in rt:
                idxs.update((i, i + 1))
        for i in sorted(idxs):
            if not (0 <= i < len(row_text)):
                continue
            hit = next((m.group(0) for m in _DATE_SUB.finditer(row_text[i])
                        if dt.normalize_date(m.group(0))[0] and m.group(0) != due_raw), None)
            if hit:
                out["invoice_date"] = hit
                break

    # ---- 多行块/区块字段：客户(名+地址+邮箱+电话) / 银行明细 / 服务期间区间 ----
    name, addr, email, phone = extract_billto(rows)
    if name:
        out["customer_name"] = name              # 块提取的客户名更准，覆盖单值命中
    if addr:
        out["customer_address"] = ", ".join(addr)
    if email and "contact_email" not in out:
        out["contact_email"] = email
    if phone:
        out["contact_phone"] = phone
    out.update(extract_bank(rows))
    # 无需表头的正则兜底：补齐行版路径漏掉的银行字段（很多发票没有"Bank Details"表头/对齐不齐）
    if any(k not in out for k in ("bank_name", "bank_account_name", "bank_account_no", "bank_swift")):
        for k, v in bank_from_text("\n".join(row_text)).items():
            out.setdefault(k, v)
    iname, iaddr, iemail, iphone = extract_issuer(rows, customer=out.get("customer_name"))
    # 开票方=收款方：① 页眉里带公司后缀(LLC/Ltd/Partners…)的名最可信；② 否则用"收款户名/受益人"
    # （=收款方=开票方，如信笺只有域名、公司名在付款块的场景）；③ 再否则用页眉候选名。
    bank_acct = out.get("bank_account_name")
    if iname and company_score(iname):
        issuer = iname
    elif bank_acct and looks_like_company(bank_acct):
        issuer = bank_acct
    else:
        issuer = iname
    if issuer and "issuer_name" not in out:
        out["issuer_name"] = issuer
    if iaddr:
        out["issuer_address"] = ", ".join(iaddr)
    # 开票方来自 "From" 标签列时：按该列向下收集多行地址（跳过其它栏插隔行），与 issuer_name 对应，
    # 修多栏发票地址被拦腰截断（如 "3 Garden Road" 被中栏 "Due Date" 隔断）。
    if from_pos is not None:
        col_lines = _column_lines_below(rows, from_pos[0], from_pos[1])
        addr_lines = [s.rstrip(" ,;|") for s in col_lines[1:]        # 丢首行(=开票方名)，去行尾多余逗号
                      if s.strip().lower() != (out.get("issuer_name") or "").strip().lower()]
        addr_lines = [s for s in addr_lines if s]
        if addr_lines:
            out["issuer_address"] = ", ".join(addr_lines)
    if iemail:
        out["issuer_email"] = iemail
    if iphone:
        out["issuer_phone"] = iphone
    ps, pe = extract_period_range(rows)
    if ps:
        out["period_start"] = ps
    if pe:
        out["period_end"] = pe
    # 邮箱全文兜底：收/开票方块内都没抓到时，扫全文取一个邮箱兜到 contact_email（页脚/块外的邮箱）
    if "contact_email" not in out and "issuer_email" not in out:
        me = _EMAIL.search("\n".join(row_text))
        if me:
            out["contact_email"] = me.group(0)
    return out
