"""银行流水的**结构化格式**解析（确定性、可靠，优于 PDF 版面猜测）：
CSV / TSV / JSON / NDJSON / MT940(SWIFT) / OFX·QFX(Quicken) / CAMT.053(ISO20022 XML)。

统一返回 (header: {字段: 值}, transactions: [Transaction])：
  header 可含 bank_account_no / bank_account_name / currency_settlement /
  opening_balance / closing_balance / statement_period_start / statement_period_end。
逐笔 Transaction：date / description / income(贷) / expense(借) / balance。
"""
from __future__ import annotations

import csv
import io
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core import config
from core.models import Transaction
from . import amount as _amt
from . import dates as _dt

STRUCTURED_EXTS = {".csv", ".tsv", ".json", ".ndjson", ".jsonl",
                   ".mt940", ".sta", ".ofx", ".qfx", ".xml", ".camt053",
                   ".qif", ".xlsx"}


def is_structured(path) -> bool:
    s = Path(path).suffix.lower()
    if s in STRUCTURED_EXTS:
        return True
    if Path(path).name.lower().endswith(".camt053.xml"):   # 双后缀
        return True
    if s in (".htm", ".html"):                             # HTML 表格对账单
        return True
    if s in (".xls", ".xlsm"):                             # 很多“.xls”其实是 HTML 表
        return _looks_html(path)
    if s == ".txt":                                        # 定宽文本对账单
        return _looks_fixed_width(path)
    return False


def _dec(v) -> Optional[Decimal]:
    """金额解析：**复用 `amount.parse_amount`**（美式/欧式 1.234,56/空格/瑞士撇号千分位、
    币种前缀、负号全支持），避免结构化流水自带的窄解析把欧洲银行金额算错或丢弃。"""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float, Decimal)):        # 已是数值（openpyxl/JSON 数字）→ 直接取，避免浮点转字符串误差
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None
    return _amt.parse_amount(str(v))[0]


def _dec_eu(s) -> Optional[Decimal]:
    """MT940 等用逗号作小数点、无千分位：92000,00 → 92000.00。"""
    if not s:
        return None
    try:
        return Decimal(str(s).strip().replace(".", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _decode(path) -> str:
    """字节→文本，编码兜底：utf-8-sig → gb18030(含 GBK/GB2312) → utf-16 → latin-1。
    国内微信/支付宝/银行导出常是 GBK/GB2312；只用 utf-8+errors=ignore 会把中文丢成乱码致列名不匹配。"""
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gb18030", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def _read(path) -> str:
    return _decode(path)


# ---- CSV / TSV --------------------------------------------------------------
_COLMAP = {   # 目标 → 候选列名（小写、去空格/下划线后比较；中文列名原样保留）
    "date": ["transactiondate", "date", "postingdate", "bookingdate", "txndate", "valuedate",
             "entrydate", "date(dd/mm/yyyy)", "dateposted", "receiptdate", "paymentdate", "datetime",
             "日期", "交易日期", "交易时间", "记账日期", "记账时间", "入账日期", "入账时间",
             "收款日期", "到账日期", "到账时间", "付款日期", "业务日期", "交易日", "时间"],
    "desc": ["counterpartyname", "description", "narrative", "details", "payee", "memo", "particulars",
             "narration", "transactiondetails", "remarks", "purpose", "receivedfrom", "payer", "paidby",
             "remitter", "sender", "beneficiary",
             "摘要", "摘要说明", "交易摘要", "用途", "备注", "交易描述", "交易类型",
             "付款方", "付款人", "汇款人", "对方户名", "对方账户", "对方名称", "收款方", "收款人", "对方"],
    "ref":  ["bankreference", "reference", "ref", "endtoendid", "chequeno", "transactionref", "凭证号", "流水号"],
    "debit": ["debitamount", "debit", "withdrawal", "withdrawals", "paidout", "dr", "payments",
              "payment", "moneyout", "outflow", "amountdr", "charges", "charge", "fee", "fees",
              "支出", "支出金额", "借方", "借方金额", "付款", "付款金额", "取出", "转出", "手续费"],
    "credit": ["creditamount", "credit", "deposit", "deposits", "paidin", "cr", "receipts",
               "receipt", "moneyin", "inflow", "amountcr", "amountreceived", "received",
               "收入", "收入金额", "贷方", "贷方金额", "收款", "收款金额", "到账金额", "实收金额", "存入", "转入"],
    "signed": ["signedamount", "amount", "transactionamount", "amountsigned", "netamount", "net",
               "金额", "金额元", "发生额", "交易金额", "入账金额"],
    # 方向列（"收/支"型账单：一列标方向、另一列放正数金额）——微信/支付宝/部分网银
    "direction": ["收支", "方向", "收支方向", "dc", "drcr", "收付", "借贷", "借贷标志", "借贷方向"],
    "balance": ["closingbalance", "balance", "runningbalance", "ledgerbalance", "bal",
                "balanceamount", "accountbalance", "availablebalance",
                "余额", "结余", "账户余额", "卡余额", "账面余额"],
    "opening": ["openingbalance", "broughtforward", "balancebf", "openingbal", "期初余额", "上期余额"],
    "account": ["statementaccountid", "accountid", "accountno", "accountnumber", "accountnum",
                "account", "iban", "acctno", "acct",
                "账号", "卡号", "账户", "账户号"],
    "account_name": ["statementaccountname", "accountname", "acctname", "户名", "账户名称", "账户名"],
    "currency": ["currency", "ccy", "curr", "currencycode", "币种", "货币"],
    "status": ["status", "state", "transactionstatus", "txnstatus", "状态", "交易状态"],
}

# 未入账/挂起状态：这些行是**临时/预授权**，非已结算交易，不计入收支（否则与后续结算行重复计）
_PENDING_STATUS = {"pending", "hold", "held", "authorized", "authorised", "auth", "processing",
                   "in process", "provisional", "未入账", "待入账", "冻结", "挂起", "处理中", "预授权"}


_INCOME_DIR = {"收入", "收", "贷", "贷方", "credit", "cr", "c", "+", "in", "收款", "存入", "转入", "进"}
_EXPENSE_DIR = {"支出", "支", "借", "借方", "debit", "dr", "d", "-", "out", "付款", "取出", "转出", "出"}


def _is_income_dir(v: str) -> bool:
    return v.strip().lower() in _INCOME_DIR or v.strip() in _INCOME_DIR


def _is_expense_dir(v: str) -> bool:
    return v.strip().lower() in _EXPENSE_DIR or v.strip() in _EXPENSE_DIR


def _strip_crdr(v):
    """剥出金额尾部的 Cr/Dr 方向标记：'1,234.56 Cr' → ('1,234.56','cr')；无则原样返回 + None。"""
    if not isinstance(v, str):
        return v, None
    m = re.search(r"\b(cr|dr)\b\.?\s*$", v.strip(), re.IGNORECASE)
    if m:
        return v.strip()[:m.start()].strip(), m.group(1).lower()
    return v, None


def _norm_key(s: str) -> str:
    # 去空白/下划线/点/斜杠/短横 + 括号（含全角）——让 "金额(元)"→"金额元"、"收/支"→"收支"、"amount(signed)"→"amountsigned"
    return re.sub(r"[\s_./\-()（）]+", "", (s or "").strip().lower())


def _colidx(headers: List[str]) -> Dict[str, str]:
    """把实际表头映射到目标键。返回 {目标: 实际列名}。"""
    norm = {_norm_key(h): h for h in headers}
    out = {}
    for tgt, cands in _COLMAP.items():
        for c in cands:
            if c in norm:
                out[tgt] = norm[c]
                break
    return out


def _rows_to_txns(records: List[dict]) -> Tuple[Dict, List[Transaction]]:
    if not records:
        return {}, []
    cm = _colidx(list(records[0].keys()))
    has_amt_cols = bool({"debit", "credit", "signed"} & set(cm))   # 有无任何"金额方向"列
    # 单文件多账户：余额跨账户不连续，**关闭"按余额差推收支"**（否则跨账户算差会得出错误发生额）
    multi_acct = "account" in cm and len({str(r.get(cm["account"])).strip()
                                          for r in records if r.get(cm["account"])}) > 1
    first = records[0]
    g0 = lambda k: first.get(cm[k]) if k in cm else None
    prev_bal = _dec(g0("opening")) if g0("opening") is not None else None   # 只余额列时按余额差推收支的锚
    dayfirst = _infer_dayfirst([r.get(cm["date"]) for r in records]) if "date" in cm else None
    txns: List[Transaction] = []
    dates = []
    pending_skipped = 0
    for r in records:
        g = lambda k: r.get(cm[k]) if k in cm else None
        # 未入账/挂起（预授权）行不计入收支——它们非已结算交易，且常与后续结算行重复
        if "status" in cm and str(g("status") or "").strip().lower() in _PENDING_STATUS:
            pending_skipped += 1
            continue
        raw = g("date")
        # **合法日期**判定用 normalize_date（失败返 None）；`_iso` 会回退原串、不能用于判定。
        if hasattr(raw, "strftime"):
            parsed = raw.strftime("%Y-%m-%d")
        else:
            raw_s = str(raw).strip() if raw is not None else ""
            parsed = _dt.normalize_date(raw_s, dayfirst=dayfirst)[0] if raw_s else None
            if parsed is None and raw_s.isdigit() and 20000 <= int(raw_s) <= 60000:
                # Excel 序列号日期（自 1899-12-30 起的天数；CSV 里日期列的 5 位整数几乎必是它）
                from datetime import date, timedelta
                try:
                    parsed = (date(1899, 12, 30) + timedelta(days=int(raw_s))).strftime("%Y-%m-%d")
                except Exception:
                    parsed = None
        # 有日期列时：跳过**日期列为空**（脚注/合计）或**有内容却非合法日期**（分节标题
        # `=== March 2026 ===`、文本合计行、说明行）的行——与"无(有效)日期不算交易"一致，
        # 避免这些行当成交易致笔数+1（旧实现只判空、漏了"非日期文本"）。
        if "date" in cm and parsed is None:
            continue
        iso = parsed
        income = _dec(g("credit"))
        expense = _dec(g("debit"))
        bal = _dec(g("balance"))
        # 金额尾缀 Cr/Dr（印度等：单金额列 + Cr/Dr 标方向）→ 剥出作方向、magnitude 走 _dec
        raw_signed, crdr = _strip_crdr(g("signed"))
        signed = _dec(raw_signed)
        if income is None and expense is None and signed is not None:  # 只有单个金额列
            dirv = str(g("direction") or "").strip() if "direction" in cm else (crdr or "")
            if dirv:                                    # 有"收/支"方向列或 Cr/Dr 尾缀 → 按方向路由（金额取正数幅度）
                mag = abs(signed)
                if _is_income_dir(dirv):
                    income = mag
                elif _is_expense_dir(dirv):
                    expense = mag
                # 其它（如"不计收支"/中性）→ 收支皆空
            elif signed < 0:                            # 无方向列 → 按签名正负
                expense = -signed
            else:                                       # 无符号、无方向列：用**余额变化**推方向
                prev = txns[-1].balance if txns else prev_bal   # 上一笔余额（首笔用期初）
                if bal is not None and prev is not None and not multi_acct:
                    expense = signed if bal < prev else None     # 余额降=支出、升=收入
                    income = None if bal < prev else signed
                else:
                    income = signed                     # 无余额锚（或多账户）→ 先记收入交人工
        # 只有余额列（无借贷/金额/签名列）：按与上一笔余额之差推收/支；首笔无锚（无期初）则留空交人工
        if not has_amt_cols and bal is not None and not multi_acct:
            if prev_bal is not None:
                delta = bal - prev_bal
                if delta > 0:
                    income = delta
                elif delta < 0:
                    expense = -delta
            prev_bal = bal
        if income == 0:            # 0 收 / 0 支视为空，避免每笔都显示 0.0
            income = None
        if expense == 0:
            expense = None
        desc = " ".join(str(x) for x in (g("desc"), g("ref")) if x).strip() or None
        if iso:
            dates.append(iso)
        cur = g("currency")
        txns.append(Transaction(date=iso, date_raw=(str(raw).strip() if raw else None), description=desc,
                                income=income, expense=expense, balance=bal,
                                currency=(str(cur).strip() if cur else None)))
    hdr = {}
    if multi_acct:                                        # 单文件多账户 → 传 sentinel 供 pipeline 加警告
        hdr["_multi_account"] = len({str(r.get(cm["account"])).strip()
                                     for r in records if r.get(cm["account"])})
    if pending_skipped:                                   # 跳过的未入账/挂起行 → 传 sentinel 供提示
        hdr["_pending_skipped"] = pending_skipped
    if g0("account"):
        hdr["bank_account_no"] = str(g0("account"))
    if g0("account_name"):
        hdr["bank_account_name"] = str(g0("account_name"))
    if g0("currency"):
        hdr["currency_settlement"] = str(g0("currency"))
    if g0("opening") is not None:
        hdr["opening_balance"] = _dec(g0("opening"))
    # 期末余额：末行的 balance（若有）
    last_bal = txns[-1].balance if txns else None
    if last_bal is not None:
        hdr["closing_balance"] = last_bal
    if dates:
        hdr["statement_period_start"] = _iso(min(dates))
        hdr["statement_period_end"] = _iso(max(dates))
    return hdr, txns


def _iso(s, dayfirst=None):
    if not s:
        return None
    if hasattr(s, "strftime"):        # datetime / date（如 openpyxl 读出的单元格）
        return s.strftime("%Y-%m-%d")
    from . import dates as dt
    iso, _ = dt.normalize_date(str(s), dayfirst=dayfirst)
    return iso or str(s)


def _infer_dayfirst(raws) -> Optional[bool]:
    """按整列日期推断 日/月 序：某行首段>12 → 日在前(True)；某行次段>12 → 月在前(False)；
    全歧义(都 ≤12) → None（交由默认）。让 UK/德式 DD/MM 整列一致解析、不逐个误判成 MM/DD。"""
    first_gt12 = second_gt12 = False
    for s in raws:
        m = re.match(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}\s*$", str(s or ""))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            first_gt12 = True
        if b > 12:
            second_gt12 = True
    if first_gt12 and not second_gt12:
        return True
    if second_gt12 and not first_gt12:
        return False
    return None


_TABLE_KEYS = {"debit", "credit", "signed", "direction", "balance"}


def _records_from_matrix(rows: List[list]) -> List[dict]:
    """从原始行矩阵里**找表头行**（跳过前导说明/空行），再把其后各行转成 dict 记录。
    表头 = 首个能映射到 date + (借/贷/金额/方向/余额之一) 的行——支持微信/支付宝等**带前导说明**的账单。"""
    # 行数上限：防 50 万行 CSV/xlsx 拖垮解析（×4 分隔符嗅探更甚）；超出只取前 N 并记日志
    cap = getattr(config, "MAX_STATEMENT_ROWS", 100000)
    if len(rows) > cap:
        print(f"[statement] 行数 {len(rows)} 超上限 {cap}，只解析前 {cap} 行（其余请拆分后上传）")
        rows = rows[:cap]

    def cells(r):
        return [str(c).strip() if c is not None else "" for c in r]
    hdr_i, header = None, None
    for i, r in enumerate(rows):
        cs = cells(r)
        if not any(cs):
            continue
        cm = _colidx(cs)
        if "date" in cm and (_TABLE_KEYS & set(cm)):
            hdr_i, header = i, cs
            break
    if header is None:                                  # 未找到规范表头 → 回退用首个非空行（旧行为）
        for i, r in enumerate(rows):
            cs = cells(r)
            if any(cs):
                hdr_i, header = i, cs
                break
    if header is None:
        return []
    recs = []
    for r in rows[hdr_i + 1:]:
        cs = cells(r)
        if not any(cs):
            continue
        recs.append({header[j]: (cs[j] if j < len(cs) else "") for j in range(len(header))})
    return recs


def _parse_csv(path, delim=None) -> Tuple[Dict, List[Transaction]]:
    """CSV/TSV：编码兜底(_decode) + **分隔符嗅探**（未指定时在 , ; \\t | 中选能映射出最多列的）。
    欧洲 CSV 常用 ; 分隔（逗号是小数点）；不嗅探会整行挤成一列、列名全不匹配。"""
    text = _decode(path)
    cands = [delim] if delim else [",", ";", "\t", "|"]
    best_recs, best_score = [], -1
    for d in cands:
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=d))
        except csv.Error:
            continue
        recs = _records_from_matrix(rows)
        # 打分：表头能映射到的目标列数（date + 借/贷/金额/方向/余额…越多越可信）
        score = len(_colidx(list(recs[0].keys()))) if recs else 0
        if score > best_score:
            best_recs, best_score = recs, score
    return _rows_to_txns(best_recs)


def _parse_json(path) -> Tuple[Dict, List[Transaction]]:
    txt = _read(path).strip()
    if "\n" in txt and not txt.lstrip().startswith("["):     # NDJSON / JSONL
        recs = [json.loads(ln) for ln in txt.splitlines() if ln.strip()]
    else:
        data = json.loads(txt)
        recs = data if isinstance(data, list) else (data.get("transactions") or data.get("entries") or [])
    return _rows_to_txns([r for r in recs if isinstance(r, dict)])


# ---- MT940 (SWIFT) ----------------------------------------------------------
_MT_61 = re.compile(r"^:61:(\d{6})(\d{4})?([A-Z]?)([DC])[A-Z]?([\d,]+)N", re.M)


def _parse_mt940(path) -> Tuple[Dict, List[Transaction]]:
    txt = _read(path)
    hdr = {}
    m = re.search(r":25:([^\r\n]+)", txt)
    if m:
        hdr["bank_account_no"] = m.group(1).strip()
    mo = re.search(r":60F:[CD](\d{6})([A-Z]{3})([\d,]+)", txt)
    if mo:
        hdr["currency_settlement"] = mo.group(2); hdr["opening_balance"] = _dec_eu(mo.group(3))
    mc = re.search(r":62F:[CD](\d{6})([A-Z]{3})([\d,]+)", txt)
    if mc:
        hdr["closing_balance"] = _dec_eu(mc.group(3))
    # 逐笔：:61: 行 + 紧随的 :86: 摘要
    lines = txt.splitlines()
    txns, dates = [], []
    i = 0
    while i < len(lines):
        m = _MT_61.match(lines[i].strip())
        if m:
            yymmdd, _entry, _fund, dc, amt = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            iso = _iso("20" + yymmdd[:2] + "-" + yymmdd[2:4] + "-" + yymmdd[4:6])
            val = _dec_eu(amt)
            desc = None
            if i + 1 < len(lines) and lines[i + 1].startswith(":86:"):
                desc = lines[i + 1][4:].replace("|", " ").strip()
                i += 1
            txns.append(Transaction(date=iso, date_raw=yymmdd, description=desc,
                                    income=val if dc == "C" else None,
                                    expense=val if dc == "D" else None))
            if iso:
                dates.append(iso)
        i += 1
    if dates:
        hdr["statement_period_start"] = min(dates); hdr["statement_period_end"] = max(dates)
    return hdr, txns


# ---- OFX / QFX --------------------------------------------------------------
def _ofx_tag(block, tag):
    m = re.search(r"<" + tag + r">([^<\r\n]*)", block, re.I)
    return m.group(1).strip() if m else None


def _parse_ofx(path) -> Tuple[Dict, List[Transaction]]:
    txt = _read(path)
    hdr = {}
    acct = _ofx_tag(txt, "ACCTID")
    if acct:
        hdr["bank_account_no"] = acct
    ccy = _ofx_tag(txt, "CURDEF")
    if ccy:
        hdr["currency_settlement"] = ccy
    txns, dates = [], []
    for blk in re.findall(r"<STMTTRN>.*?</STMTTRN>", txt, re.S | re.I):
        dt_raw = (_ofx_tag(blk, "DTPOSTED") or "")[:8]
        iso = _iso(dt_raw[:4] + "-" + dt_raw[4:6] + "-" + dt_raw[6:8]) if len(dt_raw) >= 8 else None
        amt = _dec(_ofx_tag(blk, "TRNAMT"))
        name = _ofx_tag(blk, "NAME"); memo = _ofx_tag(blk, "MEMO")
        desc = " ".join(x for x in (name, memo) if x) or None
        income = expense = None
        if amt is not None:
            if amt < 0:
                expense = -amt
            else:
                income = amt
        txns.append(Transaction(date=iso, date_raw=dt_raw, description=desc, income=income, expense=expense))
        if iso:
            dates.append(iso)
    if dates:
        hdr["statement_period_start"] = min(dates); hdr["statement_period_end"] = max(dates)
    return hdr, txns


# ---- CAMT.053 (ISO 20022 XML) ----------------------------------------------
def _parse_camt(path) -> Tuple[Dict, List[Transaction]]:
    import xml.etree.ElementTree as ET
    txt = _read(path)
    txt = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", txt, count=0)   # 去命名空间便于查找
    root = ET.fromstring(txt)
    hdr = {}
    acct = root.find(".//Acct/Id/IBAN")
    if acct is None:                       # 注意：无子元素的 Element 布尔为 False，须用 is None
        acct = root.find(".//Acct/Id/Othr/Id")
    if acct is not None and acct.text:
        hdr["bank_account_no"] = acct.text.strip()
    txns, dates = [], []
    for e in root.iter("Ntry"):
        amt_el = e.find("Amt")
        val = _dec(amt_el.text) if amt_el is not None else None
        if amt_el is not None and amt_el.get("Ccy"):
            hdr.setdefault("currency_settlement", amt_el.get("Ccy"))
        cd = e.findtext("CdtDbtInd") or ""
        d = e.findtext("BookgDt/Dt") or e.findtext("ValDt/Dt")
        iso = _iso(d) if d else None
        nm = e.findtext(".//RltdPties/Cdtr/Nm") or e.findtext(".//RltdPties/Dbtr/Nm")
        ust = e.findtext(".//RmtInf/Ustrd")
        desc = " ".join(x for x in (nm, ust) if x) or None
        txns.append(Transaction(date=iso, date_raw=d, description=desc,
                                income=val if cd == "CRDT" else None,
                                expense=val if cd == "DBIT" else None))
        if iso:
            dates.append(iso)
    if dates:
        hdr["statement_period_start"] = min(dates); hdr["statement_period_end"] = max(dates)
    return hdr, txns


# ---- QIF (Quicken) ----------------------------------------------------------
def _parse_qif(path) -> Tuple[Dict, List[Transaction]]:
    txns, dates = [], []
    cur = {}
    for ln in _read(path).splitlines():
        ln = ln.rstrip("\r")
        if not ln:
            continue
        code, val = ln[0], ln[1:].strip()
        if code == "^":               # 一笔结束
            if cur:
                amt = _dec(cur.get("T"))
                income = expense = None
                if amt is not None:
                    if amt < 0:
                        expense = -amt
                    else:
                        income = amt
                iso = _iso(cur.get("D"))
                desc = " ".join(x for x in (cur.get("P"), cur.get("M"), cur.get("N")) if x) or None
                txns.append(Transaction(date=iso, date_raw=cur.get("D"), description=desc,
                                        income=income, expense=expense))
                if iso:
                    dates.append(iso)
            cur = {}
        elif code in ("D", "T", "P", "M", "N", "L"):
            cur[code] = val
    hdr = {}
    if dates:
        hdr["statement_period_start"] = min(dates); hdr["statement_period_end"] = max(dates)
    return hdr, txns


# ---- Excel (.xlsx) —— 自动找“交易明细”所在 sheet（每 sheet 内再扫描找表头行）------
def _parse_xlsx(path) -> Tuple[Dict, List[Transaction]]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    best = None                       # (记录数, records)
    for ws in wb.worksheets:
        matrix = [list(r) for r in ws.iter_rows(values_only=True)]
        recs = _records_from_matrix(matrix)   # 扫描找表头行（跳过 summary/preamble 行）
        # 该 sheet 的表头得像交易表（有 date + 借/贷/金额/方向/余额之一），否则跳过
        if not recs or "date" not in _colidx(list(recs[0].keys())) \
                or not (_TABLE_KEYS & set(_colidx(list(recs[0].keys())))):
            continue
        if best is None or len(recs) > best[0]:
            best = (len(recs), recs)
    wb.close()
    return _rows_to_txns(best[1]) if best else ({}, [])


# ---- 定宽文本 (fixed-width) —— 常见列序：日期 值日 币种 借 贷 余额 对手 摘要 -----
_FW_LINE = re.compile(
    r"^\s*(\S+)\s+(\d{4}-\d\d-\d\d)\s+(\d{4}-\d\d-\d\d)\s+([A-Z]{3,5})\s+"
    r"([\d.,]+)\s+([\d.,]+)\s+(-?[\d.,]+?)([A-Za-z].*?)(?:\s{2,}(\S.*))?$")


def _parse_fixed_width(path) -> Tuple[Dict, List[Transaction]]:
    txns, dates = [], []
    ccy = None
    for ln in _read(path).splitlines():
        m = _FW_LINE.match(ln)
        if not m:
            continue
        _tid, tdate, _vdate, cur, debit, credit, bal, cparty, ref = m.groups()
        ccy = ccy or cur
        income = _dec(credit); expense = _dec(debit)
        if income == 0:
            income = None
        if expense == 0:
            expense = None
        iso = _iso(tdate)
        desc = " ".join(x for x in (cparty.strip(), (ref or "").strip()) if x) or None
        txns.append(Transaction(date=iso, date_raw=tdate, description=desc,
                                income=income, expense=expense, balance=_dec(bal), currency=cur))
        if iso:
            dates.append(iso)
    hdr = {}
    if ccy:
        hdr["currency_settlement"] = ccy
    if txns and txns[-1].balance is not None:
        hdr["closing_balance"] = txns[-1].balance
    if dates:
        hdr["statement_period_start"] = min(dates); hdr["statement_period_end"] = max(dates)
    return hdr, txns


def _looks_fixed_width(path) -> bool:
    head = _read(path)[:400].lower()
    return ("transaction" in head and "amount" in head) or "closing_balance" in head


# ---- HTML 表格（很多 “.xls” 其实是 Excel 兼容 HTML）----------------------------
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_TD = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _looks_html(path) -> bool:
    head = _read(path)[:200].lstrip().lower()
    return head.startswith("<html") or head.startswith("<!doctype html") or "<table" in head


def _html_cell(s: str) -> str:
    import html as _h
    return _h.unescape(_TAG.sub("", s)).strip()


def _parse_html_table(path) -> Tuple[Dict, List[Transaction]]:
    txt = _read(path)
    rows = [[_html_cell(c) for c in _TD.findall(tr)] for tr in _TR.findall(txt)]   # _TD 已含 <th>
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return {}, []
    # 复用 _records_from_matrix：扫描找表头行（跳过标题/说明行），比"首行即表头"更稳
    return _rows_to_txns(_records_from_matrix(rows))


# ---- 竖排/转置表 PDF —— 每个字段各占一行（标签块 + 逐笔的值块）------------------
_EXTRA_LABEL_NORMS = {"valuedate", "statementaccountid", "statementaccountname",
                      "simulatedmatchinvoiceid", "paymentchannel", "status", "transactionid"}


def _label_norms():
    s = set(_EXTRA_LABEL_NORMS)
    for cands in _COLMAP.values():
        s.update(cands)
    return s


_LABEL_NORMS = _label_norms()


def _is_label_line(line: str) -> bool:
    return len(line) <= 40 and _norm_key(line) in _LABEL_NORMS


def parse_pdf_transposed(text: str) -> Tuple[Dict, List[Transaction]]:
    """把「标签各占一行 + 每笔的值各占一行」的竖排导出还原成逐笔交易。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    labels: List[str] = []
    buf: List[str] = []
    records: List[dict] = []
    i, n = 0, len(lines)
    while i < n:
        if _is_label_line(lines[i]):        # 进入（新的）标签块 → 重置
            hdr = []
            while i < n and _is_label_line(lines[i]):
                hdr.append(lines[i]); i += 1
            if len(hdr) >= 4:               # 视为一整块列头
                labels = hdr; buf = []
            continue
        if labels:                          # 值行，按列数攒够一笔就落一条
            buf.append(lines[i])
            if len(buf) == len(labels):
                records.append({labels[k]: buf[k] for k in range(len(labels))})
                buf = []
        i += 1
    return _rows_to_txns(records)


def parse_structured(path) -> Optional[Tuple[Dict, List[Transaction]]]:
    """按扩展名/内容分派到对应结构化解析器；不认识返回 None。"""
    p = Path(path)
    name = p.name.lower()
    ext = p.suffix.lower()
    try:
        if ext == ".camt053" or name.endswith(".camt053.xml") or \
                (ext == ".xml" and "camt.053" in _read(p)[:2000].lower()):
            return _parse_camt(p)
        if ext == ".tsv":
            return _parse_csv(p, "\t")
        if ext == ".csv":
            return _parse_csv(p)          # delim=None → 嗅探 , ; \t |
        if ext in (".json", ".ndjson", ".jsonl"):
            return _parse_json(p)
        if ext in (".mt940", ".sta"):
            return _parse_mt940(p)
        if ext in (".ofx", ".qfx"):
            return _parse_ofx(p)
        if ext == ".qif":
            return _parse_qif(p)
        if ext == ".xlsx":
            return _parse_xlsx(p)
        if ext == ".txt" and _looks_fixed_width(p):
            return _parse_fixed_width(p)
        if ext in (".htm", ".html") or (ext in (".xls", ".xlsm") and _looks_html(p)):
            return _parse_html_table(p)
    except Exception:
        return None
    return None
