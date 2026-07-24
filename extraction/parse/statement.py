"""银行流水（对账单）解析：账户头 + 逐笔交易表。

思路（版式无关、软先验、可人工审核修正）：
1) 账户头：银行/账号/户名（复用 generic 的银行解析）、对账期间、期初/期末余额。
2) 交易表：先找**表头行**（含 日期/摘要/借/贷/余额 等列名），记下各列的 x 位置；
   再逐数据行——含日期的行即一笔交易，把行内每个金额按 x **就近归到 借/贷/余额/金额** 列。
   单金额列时按余额变化推正负（收入/支出）；识别不准的交给人工在审核界面逐行改。

绝不臆造：识别不到交易表就只出账户头，交完整性交人工补。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import List, Optional, Tuple

from core.models import Invoice, FieldValue, Transaction
from . import dates as dt
from . import amount as amt
from . import generic as g

# ---- 单据判定：是不是银行流水 ----
_STMT_MARKERS = re.compile(
    r"(account|bank)\s*statement|statement\s*of\s*account|对\s*账\s*单|账户流水|银行流水|"
    r"交易明细|流水明细|opening\s*balance|closing\s*balance|"
    r"balance\s*(?:b/?f|c/?f|brought\s*forward|carried\s*forward)", re.IGNORECASE)

# ---- 交易表列名 ----
_COL = {
    "date":    re.compile(r"^(date|date\s*time|txn\s*date|trans(?:action)?\s*date|posting\s*date|value\s*date|"
                          r"receipt\s*date|payment\s*date|"
                          r"日期|交易日期|交易时间|记账日期|记账时间|入账日期|入账时间|收款日期|到账日期|到账时间|付款日期|业务日期|交易日|时间)$", re.IGNORECASE),
    "desc":    re.compile(r"^(description|particulars?|narration|details?|memo|transactions?|reference|"
                          r"received\s*from|payer|paid\s*by|remitter|sender|beneficiary|"
                          r"摘要|摘要说明|交易摘要|用途|备注|交易描述|交易类型|付款方|付款人|汇款人|对方户名|对方账户|对方名称|收款方|收款人)$", re.IGNORECASE),
    "debit":   re.compile(r"^(debit|withdrawals?|dr\.?|paid\s*out|payments?|charges?|fees?|"
                          r"支出|支出金额|借方|借方金额|付款|付款金额|取出|转出|手续费)$", re.IGNORECASE),
    "credit":  re.compile(r"^(credit|deposits?|cr\.?|paid\s*in|receipts?|amount\s*received|received|"
                          r"收入|收入金额|贷方|贷方金额|收款|收款金额|到账金额|实收金额|存入|转入)$", re.IGNORECASE),
    "balance": re.compile(r"^(balance|bal\.?|running\s*balance|余额|结余|账户余额|卡余额|账面余额)$", re.IGNORECASE),
    "amount":  re.compile(r"^(amount|金额|发生额|交易金额)$", re.IGNORECASE),
}

_OPENING = re.compile(
    r"(?:opening\s*balance|balance\s*(?:b/?f|brought\s*forward)|期初余额|上期结转|上期余额)\s*[:：]?\s*"
    r"([A-Z]{0,3}\s*[$€£¥]?\s*[\d,]+(?:\.\d+)?)", re.IGNORECASE)
_CLOSING = re.compile(
    r"(?:closing\s*balance|balance\s*(?:c/?f|carried\s*forward)|期末余额|期末结存|本期结存)\s*[:：]?\s*"
    r"([A-Z]{0,3}\s*[$€£¥]?\s*[\d,]+(?:\.\d+)?)", re.IGNORECASE)
_PERIOD = re.compile(
    r"(?:statement\s*period|period|对账期间|账单周期|交易期间)\s*[:：]?\s*(.+?)\s*"
    r"(?:\bto\b|through|至|~|—|–|\s-\s)\s*(.+?)(?:\.|$|\n|\s{2,})", re.IGNORECASE)


def _leading_date(s: str):
    """取单元格开头的日期（日期常与摘要挤在一格）：试前 3/2/1 个词，返回 (iso, 匹配串, 剩余作摘要)。"""
    toks = (s or "").split()
    for n in (3, 2, 1):
        if len(toks) >= n:
            cand = " ".join(toks[:n])
            iso, _ = dt.normalize_date(cand)
            if iso:
                return iso, cand, " ".join(toks[n:]).strip()
    return None, None, s


def is_statement(text: str) -> bool:
    """是否银行流水：命中账单标记，或同时出现 日期列 + 余额列 + (借|贷|金额) 列。"""
    t = text or ""
    if _STMT_MARKERS.search(t):
        return True
    low = t.lower()
    has_date = bool(re.search(r"\bdate\b|日期", low))
    has_bal = bool(re.search(r"\bbalance\b|余额|结余", low))
    has_amt = bool(re.search(r"debit|credit|withdrawal|deposit|支出|收入|借方|贷方|金额", low))
    return has_date and has_bal and has_amt


def _cells(line) -> List[Tuple[float, float, str]]:
    return g._cells(list(line.words))


def _find_header(rows) -> Optional[dict]:
    """找交易表头行：返回 {列名: x中心}（至少含 date 且含 balance 或 amount/借/贷 之一）。"""
    for r, cells in enumerate(rows):
        cols = {}
        for (x0, x1, t) in cells:
            for name, rx in _COL.items():
                if name not in cols and rx.match(t.strip()):
                    cols[name] = (x0 + x1) / 2
        if "date" in cols and (("balance" in cols) or ("amount" in cols)
                               or ("debit" in cols) or ("credit" in cols)):
            cols["_row"] = r
            return cols
    return None


def _nearest(colx: dict, x: float) -> Optional[str]:
    best, bestd = None, 1e9
    for name, cx in colx.items():
        if name == "_row":
            continue
        d = abs(cx - x)
        if d < bestd:
            bestd, best = d, name
    return best


def parse_statement(inv: Invoice, doc, source: str = "pdf_text") -> None:
    """把银行流水解析进 inv：账户头字段 + inv.transactions。"""
    inv.doc_type = "statement"
    text = doc.full_text if hasattr(doc, "full_text") else (inv.raw_pdf_text or inv.raw_ocr_text or "")
    conf = 0.9 if source == "pdf_text" else 0.75

    # 账户头：银行/账号/户名（复用发票的银行解析）
    for k, v in g.bank_from_text(text).items():
        inv.set(k, FieldValue(raw=v, value=v, confidence=conf, source=source))
    rows = [_cells(ln) for ln in doc.lines] if hasattr(doc, "lines") else []
    if rows:
        for k, v in g.extract_bank(rows).items():
            if not inv.f(k).value:
                inv.set(k, FieldValue(raw=v, value=v, confidence=conf, source=source))
    # 币种
    ccy = g.currency_fallback(text) if hasattr(g, "currency_fallback") else None
    if ccy:
        inv.set("currency_settlement", FieldValue(raw=ccy, value=ccy, confidence=conf, source=source))
    # 期初/期末余额
    for key, rx in (("opening_balance", _OPENING), ("closing_balance", _CLOSING)):
        m = rx.search(text)
        if m:
            val, susp, note = amt.parse_amount(m.group(1))
            if val is not None:
                inv.set(key, FieldValue(raw=m.group(1).strip(), value=val,
                                        confidence=conf, source=source, suspicious=susp))
    # 对账期间
    mp = _PERIOD.search(text)
    if mp:
        s_iso, _ = dt.normalize_date(mp.group(1))
        e_iso, _ = dt.normalize_date(mp.group(2))
        if s_iso:
            inv.set("statement_period_start", FieldValue(raw=mp.group(1).strip(), value=s_iso,
                                                         confidence=conf, source=source))
        if e_iso:
            inv.set("statement_period_end", FieldValue(raw=mp.group(2).strip(), value=e_iso,
                                                       confidence=conf, source=source))

    # 交易表
    inv.transactions = _parse_transactions(rows, doc) if rows else []


_HL_ISMONEY = re.compile(
    r"[+\-−]?\s*[$€£¥]?\s*(?:\d{1,3}(?:[,  ]\d{3})+(?:\.\d{1,8})?|\d+\.\d{1,8})\)?$")


def _parse_headerless(rows, doc) -> List[Transaction]:
    """无表头流水兜底：每行以日期起、其后金额按位置取（末列=余额，其余最后一个=发生额，带符号）。

    仅在用户已选 doc_type=statement 且找不到表头时启用（`_parse_transactions` 里调用）；以"行首日期
    + 至少一个金额格"为锚，避免把非流水内容误判成交易。收/支判定：带 +/- 号照号；无号按余额变化推。
    """
    txns: List[Transaction] = []
    for cells in rows:
        if not cells:
            continue
        # 基于**整行文本**取金额（兼容"整行挤成一格"与"切成多格"两种版面）
        text = " ".join(t for _, _, t in cells).strip()
        iso, matched, rest = _leading_date(text)
        if not iso:
            continue
        toks = rest.split()
        vals = []                                       # [(原始串, 值)]，按出现顺序
        first_i = None
        for i, tok in enumerate(toks):
            if _HL_ISMONEY.match(tok):
                v = amt.parse_amount(tok)[0]
                if v is not None:
                    vals.append((tok, v))
                    if first_i is None:
                        first_i = i
        if not vals:
            continue
        balance = vals[-1][1] if len(vals) >= 2 else None
        amt_raw, amt_val = vals[-2] if len(vals) >= 2 else vals[-1]
        desc = " ".join(toks[:first_i]).strip(" -\t|") or None
        income = expense = None
        st = amt_raw.strip()
        if st.startswith(("-", "−", "(")) or amt_val < 0:
            expense = abs(amt_val)
        elif st.startswith("+"):
            income = abs(amt_val)
        else:                                           # 无符号：按余额变化推收/支
            prev = txns[-1].balance if txns else None
            if balance is not None and prev is not None:
                income, expense = (amt_val, None) if balance >= prev else (None, amt_val)
            else:
                income = amt_val                        # 无从判断 → 先记收入，交人工核对
        txns.append(Transaction(date=iso, date_raw=matched, description=desc,
                                income=income, expense=expense, balance=balance,
                                bbox=_row_bbox(cells, doc)))
    return txns


def _parse_transactions(rows, doc) -> List[Transaction]:
    hdr = _find_header(rows)
    if not hdr:
        return _parse_headerless(rows, doc)             # 无表头兜底（用户已选流水）
    colx = {k: v for k, v in hdr.items() if k != "_row"}
    page_h = doc.page_sizes[0][1] if getattr(doc, "page_sizes", None) else None
    txns: List[Transaction] = []
    for r in range(hdr["_row"] + 1, len(rows)):
        cells = rows[r]
        if not cells:
            continue
        # 找日期（日期常与摘要挤在同一格）：取单元格开头的日期，剩余作摘要
        date_iso = date_raw = None
        date_ci = -1
        desc_parts = []
        for ci, (x0, x1, t) in enumerate(cells):
            iso, matched, rest = _leading_date(t.strip())
            if iso:
                date_iso, date_raw, date_ci = iso, matched, ci
                if rest:
                    desc_parts.append(rest)
                break
        if not date_iso:
            continue                                   # 无日期 → 不是交易行（表头/小计/空行）
        # 各金额 cell 按 x 就近归列
        buckets = {"debit": None, "credit": None, "balance": None, "amount": None}
        for ci, (x0, x1, t) in enumerate(cells):
            if ci == date_ci:
                continue
            s = t.strip()
            money = g._money(s)
            if money:                                   # 是金额 → 归到最近的金额类列
                col = _nearest({k: v for k, v in colx.items() if k in buckets}, (x0 + x1) / 2)
                if col and buckets.get(col) is None:
                    buckets[col] = money
                    continue
            if not money:
                desc_parts.append(s)
        income = expense = balance = None
        bal_v = amt.parse_amount(buckets["balance"])[0] if buckets["balance"] else None
        cr_v = amt.parse_amount(buckets["credit"])[0] if buckets["credit"] else None
        dr_v = amt.parse_amount(buckets["debit"])[0] if buckets["debit"] else None
        am_v = amt.parse_amount(buckets["amount"])[0] if buckets["amount"] else None
        balance = bal_v
        note = None
        if cr_v is not None or dr_v is not None:
            income, expense = cr_v, dr_v
        elif am_v is not None:                          # 单金额列
            prev = txns[-1].balance if txns else None
            if am_v < 0:                                # **带符号金额**（负=支出）→ 取正数幅度记支出
                expense = -am_v
            elif balance is not None and prev is not None:  # 无符号 → 按余额变化推收/支
                if balance >= prev:
                    income = am_v
                else:
                    expense = am_v
            else:
                income = am_v                           # 无从判断 → 先记为收入，交人工核对
        elif balance is not None and txns and txns[-1].balance is not None:
            # 借贷/金额列都缺（如 OCR 把某行的发生额丢了、或水印压掉一格）但**余额幸存**：
            # 按与上一笔余额之差推发生额（增=收入、减=支出），留 note 供人工复核——从幸存数据恢复。
            d = balance - txns[-1].balance
            if d > 0:
                income = d
            elif d < 0:
                expense = -d
            if d != 0:
                note = "发生额未直接识别，按余额变化推导，请核对"
        bbox = _row_bbox(cells, doc)
        txns.append(Transaction(date=date_iso, date_raw=date_raw,
                                description=" ".join(desc_parts).strip() or None,
                                income=income, expense=expense, balance=balance, bbox=bbox, note=note))
    return txns


def _row_bbox(cells, doc):
    """该交易行的外接框（供审核界面高亮）。cells 只有 x；y 需从 doc 行取——此处退化不带 y 的近似。"""
    return None   # v1：交易行 bbox 后续接入（先保证字段/表提取；高亮可后补）
