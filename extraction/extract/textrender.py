"""把**文本/结构化**文件渲染成 PNG，供审核页左栏在线预览。

针对银行流水的结构化格式（CSV/TSV/JSON/NDJSON/MT940/OFX/QFX/QIF/XML/定宽txt/HTML-xls）——
这些 fitz 打不开、原本只能下载。这里统一渲染成等宽文本（CSV/TSV/HTML 对齐成表格），
让「所有格式都能在网页完成预览」。纯本地渲染，不外发。
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import List, Optional

# 可在线预览的文本/结构化后缀（.xls 仅当其实是 HTML 表时）
TEXT_SUFFIXES = {".csv", ".tsv", ".json", ".ndjson", ".jsonl", ".mt940", ".sta",
                 ".ofx", ".qfx", ".qif", ".xml", ".txt", ".htm", ".html"}

_MAX_LINES = 400          # 预览最多渲染行数（超出截断并提示）
_MAX_COLS = 200           # 单行最多字符（超出换行）
_FONT_DIRS = [str(Path(__file__).resolve().parent / "fonts"),
              "/usr/share/fonts/truetype/dejavu"]
_FONT_CACHE: dict = {}


def can_preview(path) -> bool:
    p = Path(path)
    s = p.suffix.lower()
    if s in TEXT_SUFFIXES:
        return True
    if s in (".xls", ".xlsm"):        # 很多“.xls”其实是 Excel 兼容 HTML
        return _looks_html(p)
    return False


def _read(path) -> str:
    return Path(path).read_text(encoding="utf-8-sig", errors="ignore")


def _looks_html(path) -> bool:
    try:
        head = _read(path)[:200].lstrip().lower()
    except Exception:
        return False
    return head.startswith("<html") or head.startswith("<!doctype html") or "<table" in head


def _font(bold: bool, size_px: int):
    from PIL import ImageFont
    key = (bold, size_px)
    if key not in _FONT_CACHE:
        names = (["DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf"] if bold
                 else ["DejaVuSansMono.ttf"]) + ["DejaVuSans.ttf"]   # 等宽优先，非等宽兜底
        font = None
        for name in names:
            for d in _FONT_DIRS:
                try:
                    font = ImageFont.truetype(f"{d}/{name}", size_px)
                    break
                except Exception:
                    continue
            if font is not None:
                break
        _FONT_CACHE[key] = font or ImageFont.load_default()
    return _FONT_CACHE[key]


# ---- 各格式 → 展示行 --------------------------------------------------------
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
_TD = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _html_rows(txt: str) -> List[List[str]]:
    import html as _h
    out = []
    for tr in _TR.findall(txt):
        cells = [_h.unescape(_TAG.sub("", c)).strip() for c in _TD.findall(tr)]
        if any(cells):
            out.append(cells)
    return out


def _table_lines(rows: List[List[str]]) -> List[str]:
    """把表格行按列宽对齐成等宽文本行（含表头下划线）。"""
    if not rows:
        return []
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    widths = [min(28, max(len(str(r[i])) for r in rows)) for i in range(ncol)]

    def fmt(r):
        return " | ".join(str(r[i])[:widths[i]].ljust(widths[i]) for i in range(ncol))

    lines = [fmt(rows[0]), "-+-".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows[1:]]
    return lines


def _to_lines(path) -> List[str]:
    p = Path(path)
    s = p.suffix.lower()
    txt = _read(p)
    if s in (".htm", ".html") or (s in (".xls", ".xlsm") and _looks_html(p)):
        return _table_lines(_html_rows(txt))
    if s in (".csv", ".tsv"):
        delim = "\t" if s == ".tsv" else ","
        try:
            rows = list(csv.reader(io.StringIO(txt), delimiter=delim))
            return _table_lines([r for r in rows if any(c.strip() for c in r)])
        except Exception:
            pass
    return txt.splitlines()


def _wrap(lines: List[str], max_cols: int = _MAX_COLS) -> List[str]:
    out = []
    truncated = False
    for ln in lines:
        ln = ln.replace("\t", "    ")
        while len(ln) > max_cols:
            out.append(ln[:max_cols])
            ln = ln[max_cols:]
        out.append(ln)
        if len(out) >= _MAX_LINES:
            truncated = True
            break
    if truncated:
        out = out[:_MAX_LINES]
        out.append("… （内容较长，预览已截断，完整内容请下载原件）")
    return out or ["（空文件）"]


# ---- 银行流水「规范交易表」预览 -------------------------------------------
# 结构化/文本流水没有原件版面几何，无法像发票那样把交易定位回原件。
# 这里由**解析出的逐笔交易**渲染成一张固定版式的表格图片，并给每一笔一个**行级 bbox**
# （与图片像素坐标一致）——审核页即可复用发票的「字段↔原件框」双向高亮机制：点交易行→左侧高亮该行。
# 布局全部用固定像素常量，使 statement_layout()（纯计算 bbox）与 render_statement_png()（真正绘制）严格对齐。
STMT_COL_LABELS = ["Date", "Description", "Income", "Expense", "Balance"]
STMT_COL_W = [150, 470, 140, 140, 160]
_SPAD = 24          # 画布内边距
_STITLE = 46        # 标题行高
_SLH = 34           # 账户头每行高
_SRH = 44           # 交易行高


def _stmt_g(inv, k):
    fv = inv.f(k)
    return fv.value if fv else None


def _stmt_header_lines(inv) -> List[str]:
    lines = []
    acct = " / ".join(str(x) for x in (_stmt_g(inv, "bank_name"), _stmt_g(inv, "bank_account_no")) if x)
    if acct:
        lines.append("Account: " + acct)
    ps, pe = _stmt_g(inv, "statement_period_start"), _stmt_g(inv, "statement_period_end")
    if ps or pe:
        lines.append("Period: %s ~ %s" % (ps or "?", pe or "?"))
    seg = []
    cur, ob, cb = _stmt_g(inv, "currency_settlement"), _stmt_g(inv, "opening_balance"), _stmt_g(inv, "closing_balance")
    if cur:
        seg.append("Currency " + str(cur))
    if ob is not None:
        seg.append("Opening " + str(ob))
    if cb is not None:
        seg.append("Closing " + str(cb))
    if seg:
        lines.append(" · ".join(seg))
    return lines


_CELL_SIZE = 19      # 交易行单元格字号
_TEXT_PAD = 8        # 单元格内文字左内边距


def _cell_char_w() -> float:
    """交易行等宽字体单字符宽度（像素）。等宽 → 可按字符数精确定位子串（如发票号）位置。"""
    if not hasattr(_cell_char_w, "_w"):
        try:
            _cell_char_w._w = _font(False, _CELL_SIZE).getlength("0")
        except Exception:
            _cell_char_w._w = _CELL_SIZE * 0.6
    return _cell_char_w._w


def _fit(s, colw: int) -> str:
    """按单元格可容纳字符数截断（用真实字符宽算，render 与 bbox 定位共用，保证一致、不溢出）。"""
    s = str(s)
    maxc = max(4, int((colw - _TEXT_PAD * 2) / _cell_char_w()))
    return s if len(s) <= maxc else s[:maxc - 1] + "…"


def statement_layout(inv) -> dict:
    """纯计算（不绘制）：返回画布尺寸、账户头行、列头 y、首行 y 及每笔交易的行级 bbox。
    bbox 形如 [page=0, x0, y0, x1, y1]，坐标即图片像素；配合 page_sizes=[[W,H]] 供前端按百分比叠框。"""
    header_lines = _stmt_header_lines(inv)
    W = _SPAD * 2 + sum(STMT_COL_W)
    colhdr_y = _SPAD + _STITLE + len(header_lines) * _SLH + 8
    table_top = colhdr_y + _SRH
    xs = [_SPAD]
    for w in STMT_COL_W:
        xs.append(xs[-1] + w)
    cw = _cell_char_w()
    desc_maxc = max(6, int((STMT_COL_W[1] - 2 * _TEXT_PAD) / cw))   # 摘要列每行可容字符数
    boxes, desc_lines = [], []
    y = table_top
    for t in inv.transactions:
        dl = _wrap_words(_sv(t.description), desc_maxc)              # 摘要**按列宽换行、完整不截断**
        rh = max(_SRH, _SROWPAD * 2 + len(dl) * _SLINE)             # 行高随摘要行数增长
        boxes.append([0, float(_SPAD), float(y), float(W - _SPAD), float(y + rh)])
        desc_lines.append(dl)
        y += rh
    H = (y if inv.transactions else table_top + _SRH) + _SPAD
    return {"width": W, "height": H, "header_lines": header_lines,
            "colhdr_y": colhdr_y, "table_top": table_top, "boxes": boxes,
            # 列 x 边界（Date/Description/Income/Expense/Balance），供审核/对账按列画分栏高亮框
            "col_x": xs, "char_w": cw, "text_pad": _TEXT_PAD,
            "desc_lines": desc_lines, "line_h": _SLINE, "row_pad": _SROWPAD,
            "desc_texts": [_sv(t.description) for t in inv.transactions]}


def _sv(v) -> str:
    return "" if v in (None, "") else str(v)


_SLINE = 26          # 交易行内每行文字高度
_SROWPAD = 9         # 交易行上下内边距


def _wrap_words(s: str, maxc: int) -> list:
    """按单词折行（发票号等 token 不含空格→不被折断）；超长单词硬拆。至少返回一行。"""
    out, cur = [], ""
    for w in str(s).split(" "):
        cand = w if not cur else cur + " " + w
        if len(cand) <= maxc:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = w
        while len(cur) > maxc:
            out.append(cur[:maxc]); cur = cur[maxc:]
    if cur:
        out.append(cur)
    return out or [""]


def render_statement_png(inv, marks=None) -> bytes:
    """把解析出的逐笔交易渲染成规范交易表 PNG（与 statement_layout 的 bbox 对齐）。
    marks: [{"box":[x0,y0,x1,y1], "color":(r,g,b), "width":int}]——在**同一坐标系**里把高亮框直接画进图片，
    前端只需显示图片、无需再做百分比叠框（避免缩放/容器高度导致的叠框错位）。"""
    from PIL import Image, ImageDraw
    L = statement_layout(inv)
    W, H = L["width"], L["height"]
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    ftitle, fhdr, fcol, fcell = _font(True, 26), _font(False, 20), _font(True, 20), _font(False, 19)
    d.text((_SPAD, _SPAD + 4), "Bank Statement", font=ftitle, fill=(20, 40, 90))
    y = _SPAD + _STITLE
    for ln in L["header_lines"]:
        d.text((_SPAD, y), ln, font=fhdr, fill=(70, 70, 70)); y += _SLH
    xs = [_SPAD]
    for w in STMT_COL_W:
        xs.append(xs[-1] + w)
    cy = L["colhdr_y"]
    d.rectangle([_SPAD, cy, W - _SPAD, cy + _SRH], fill=(31, 78, 121))
    for j, lab in enumerate(STMT_COL_LABELS):
        d.text((xs[j] + 8, cy + 12), lab, font=fcol, fill=(255, 255, 255))
    for i, t in enumerate(inv.transactions):
        row = L["boxes"][i]; y0, y1 = int(row[2]), int(row[4])
        if i % 2:
            d.rectangle([_SPAD, y0, W - _SPAD, y1], fill=(246, 249, 252))
        ty = y0 + L["row_pad"]
        # 日期/收入/支出/余额：短、单行（顶对齐）
        for j, v in ((0, _sv(t.date)), (2, _sv(t.income)), (3, _sv(t.expense)), (4, _sv(t.balance))):
            d.text((xs[j] + _TEXT_PAD, ty), _fit(v, STMT_COL_W[j]), font=fcell, fill=(30, 30, 30))
        # 摘要：多行完整显示（不截断）
        for k, ln in enumerate(L["desc_lines"][i]):
            d.text((xs[1] + _TEXT_PAD, ty + k * L["line_h"]), ln, font=fcell, fill=(30, 30, 30))
        d.line([_SPAD, y1, W - _SPAD, y1], fill=(225, 230, 238))
    for x in xs:
        d.line([x, cy, x, H - _SPAD], fill=(225, 230, 238))
    for mk in (marks or []):
        b = mk["box"]
        d.rectangle([b[0], b[1], b[2], b[3]], outline=mk.get("color", (245, 166, 35)),
                    width=mk.get("width", 3))
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def render_raw_excerpt(path, groups, anchor_needles=None, width: int = 980, context: int = 1) -> Optional[bytes]:
    """渲染**原件源文件**的节选（真实字节，供独立核对）：用 anchor_needles（发票号，每笔唯一）**锚定命中行** ± 上下文，
    命中行淡黄底，并在命中行上把各组关键值按各自颜色框出（日期绿/金额橙/发票号蓝）。
    groups: [{"needles":[...], "color":(r,g,b)}]——needles 用「解析出的原始值 + 常见变体」以适配各格式写法。
    只用发票号锚定 + 只在命中行上框：避免同日期/同金额的其它交易被误标。"""
    from PIL import Image, ImageDraw
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="ignore")
    except Exception:
        return None
    lines = text.splitlines()
    groups = [{"needles": [str(x) for x in g["needles"] if x and len(str(x)) >= 3], "color": g["color"]}
              for g in (groups or [])]
    all_needles = [(n, g["color"]) for g in groups for n in g["needles"]]
    anchors = [str(x) for x in (anchor_needles or []) if x and len(str(x)) >= 3]
    hits = [i for i, l in enumerate(lines) if any(a.lower() in l.lower() for a in anchors)]
    if not hits:                                    # 没有发票号锚（如纯金额匹配）→ 退回用任意关键值锚定
        hits = [i for i, l in enumerate(lines) if any(n.lower() in l.lower() for n, _ in all_needles)]
    if hits:
        sel = sorted({j for i in hits for j in range(max(0, i - context), min(len(lines), i + context + 1))})
    else:
        sel = list(range(min(len(lines), 30)))     # 没定位到就给开头一段，仍是原件
    hitset = set(hits)
    SIZE, PAD, LH, GUT = 17, 14, 24, 54
    f = _font(False, SIZE)
    try:
        cw = f.getlength("0")
    except Exception:
        cw = SIZE * 0.6
    maxc = max(20, int((width - 2 * PAD - GUT) / cw))
    disp = []                                        # (行号或None, 文本片段, 是否命中行)
    for i in sel:
        raw = lines[i].replace("\t", "    ")
        chunks = [raw[k:k + maxc] for k in range(0, len(raw), maxc)] or [""]
        for ci, ch in enumerate(chunks):
            disp.append((i + 1 if ci == 0 else None, ch, i in hitset))
    H = max(60, PAD * 2 + LH * len(disp))
    img = Image.new("RGB", (width, H), (255, 255, 255)); d = ImageDraw.Draw(img)
    y = PAD
    for lno, ch, hit in disp:
        if hit:
            d.rectangle([PAD, y - 1, width - PAD, y + SIZE + 3], fill=(255, 249, 224))
        if lno is not None:
            d.text((PAD, y), str(lno).rjust(4), font=f, fill=(150, 160, 172))
        tx = PAD + GUT
        d.text((tx, y), ch, font=f, fill=(30, 30, 30))
        if hit:                                      # 只在命中行上框，避免上下文行/txn-id 里同名串的噪声
            up = ch.upper()
            for n, color in all_needles:             # 各组关键值按各自颜色框
                nu = n.upper(); s = 0
                while True:
                    p = up.find(nu, s)
                    if p < 0:
                        break
                    d.rectangle([tx + p * cw, y - 1, tx + (p + len(n)) * cw, y + SIZE + 3],
                                outline=color, width=2)
                    s = p + len(n)
        y += LH
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def stack_labeled(parts, width: int = 980) -> bytes:
    """把若干 (标题, png字节) 竖直拼成一张图，每段带深色标题条。"""
    from PIL import Image, ImageDraw
    imgs = [(lab, Image.open(io.BytesIO(p)).convert("RGB")) for lab, p in parts if p]
    LB = 28
    total = max(60, sum(LB + im.height + 8 for _, im in imgs))
    canvas = Image.new("RGB", (width, total), (255, 255, 255)); d = ImageDraw.Draw(canvas)
    f = _font(True, 15); y = 0
    for lab, im in imgs:
        d.rectangle([0, y, width, y + LB], fill=(31, 78, 121))
        d.text((10, y + 6), lab, font=f, fill=(255, 255, 255)); y += LB
        canvas.paste(im, (0, y)); y += im.height + 8
    buf = io.BytesIO(); canvas.save(buf, "PNG")
    return buf.getvalue()


def render_txn_evidence(rows: List[dict], invoice_numbers: List[str], width: int = 980,
                        vendors: Optional[List[str]] = None) -> bytes:
    """对账「证据卡」：逐笔**完整**显示 日期 / 金额 / 整段附言（等宽自动折行、**不截断**），
    并把 日期(绿)、金额(橙)、附言里出现的**发票号(蓝，可多处)** 精确框出——保证发票号看得全、框得到。
    rows: [{date, description, amount, currency}]。"""
    from PIL import Image, ImageDraw
    SIZE, PAD, LH, GAP = 20, 18, 30, 16
    f = _font(False, SIZE); flab = _font(True, 16)
    try:
        cw = f.getlength("0")
    except Exception:
        cw = SIZE * 0.6
    maxchars = max(10, int((width - 2 * PAD) / cw))
    nums = [str(n) for n in invoice_numbers if n and len(str(n)) >= 3]
    vends = [str(v).strip() for v in (vendors or []) if v and len(str(v).strip()) >= 3]

    def _wrap(desc):
        # 按单词折行（发票号不含空格 → 不会被折断，两处都能框）；超长单词再硬拆
        dlines, cur = [], ""
        for w in str(desc).split(" "):
            cand = w if not cur else cur + " " + w
            if len(cand) <= maxchars:
                cur = cand
            else:
                if cur:
                    dlines.append(cur)
                cur = w
            while len(cur) > maxchars:
                dlines.append(cur[:maxchars]); cur = cur[maxchars:]
        if cur:
            dlines.append(cur)
        return dlines or [""]

    # 预排版：算出每张卡的行 + 总高
    cards = [{"r": r, "dlines": _wrap(r.get("description") or "")} for r in rows]
    H = PAD
    for c in cards:
        H += LH * 2 + LH * len(c["dlines"]) + GAP   # 头行 + "附言:" + 折行 + 间隔
    H = max(H, 80)

    img = Image.new("RGB", (width, H), (255, 255, 255)); d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1, color):
        d.rectangle([x0, y0 - 2, x1, y0 + SIZE + 6], outline=color, width=3)

    y = PAD
    for c in cards:
        r = c["r"]
        date = str(r.get("date") or "—"); amt = str(r.get("amount") or "—"); ccy = str(r.get("currency") or "")
        # 头行： Date: <date>    Amount: <amt> <ccy>
        head = "Date: "; d.text((PAD, y), head, font=f, fill=(90, 90, 90))
        dx0 = PAD + len(head) * cw; d.text((dx0, y), date, font=f, fill=(20, 20, 20))
        box(dx0, y, dx0 + len(date) * cw, y, (15, 143, 95))          # 日期(绿)
        alab = "    Amount: "; ax = dx0 + len(date) * cw
        d.text((ax, y), alab, font=f, fill=(90, 90, 90))
        avx0 = ax + len(alab) * cw; aval = amt + (" " + ccy if ccy else "")
        d.text((avx0, y), aval, font=f, fill=(20, 20, 20))
        box(avx0, y, avx0 + len(aval) * cw, y, (232, 130, 12))       # 金额(橙)
        y += LH
        d.text((PAD, y), "Description:", font=flab, fill=(90, 90, 90)); y += LH
        for ln in c["dlines"]:
            d.text((PAD, y), ln, font=f, fill=(30, 30, 30))
            up = ln.upper()
            for no in nums:                                          # 发票号(蓝，可多处)
                nu = no.upper(); start = 0
                while True:
                    p = up.find(nu, start)
                    if p < 0:
                        break
                    box(PAD + p * cw, y, PAD + (p + len(no)) * cw, y, (47, 111, 237))
                    start = p + len(no)
            for vn in vends:                                         # 公司名(紫，可多处)
                vu = vn.upper(); start = 0
                while True:
                    p = up.find(vu, start)
                    if p < 0:
                        break
                    box(PAD + p * cw, y, PAD + (p + len(vn)) * cw, y, (123, 63, 191))
                    start = p + len(vn)
            y += LH
        y += GAP
        d.line([PAD, y - GAP // 2, width - PAD, y - GAP // 2], fill=(230, 234, 240))
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def render_text_png(path, scale: float = 2.0, max_cols: int = _MAX_COLS) -> Optional[bytes]:
    """渲染为 PNG 字节；无法渲染返回 None。max_cols 控制折行宽度（原件视图可传小值收窄画布，便于自适应/缩放）。"""
    from PIL import Image, ImageDraw
    try:
        lines = _wrap(_to_lines(path), max_cols=max_cols)
    except Exception:
        return None
    size = int(13 * scale)
    pad = int(16 * scale)
    line_h = int(size * 1.5)
    font = _font(False, size)
    # 估算画布宽度
    tmp = Image.new("RGB", (10, 10)); dtmp = ImageDraw.Draw(tmp)
    max_w = 0
    for ln in lines:
        try:
            w = dtmp.textlength(ln, font=font)
        except Exception:
            w = len(ln) * size * 0.6
        max_w = max(max_w, w)
    W = int(max_w) + pad * 2
    H = line_h * len(lines) + pad * 2
    W = max(320, min(W, int(2400 * scale)))
    H = max(120, H)
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    y = pad
    for i, ln in enumerate(lines):
        # 表头行（第 0 行）与分隔线加深，正文黑色
        color = (30, 64, 120) if i == 0 else (40, 40, 40)
        d.text((pad, y), ln, font=font, fill=color)
        y += line_h
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()
