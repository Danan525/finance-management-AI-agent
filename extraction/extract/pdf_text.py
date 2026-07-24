"""PDF 文本抽取：PyMuPDF 主抽取（带坐标，分左右栏）+ pdfplumber 交叉复核。

固定格式发票为双栏布局：右栏(x>=COL_SPLIT)是表头标签/Bill to/合计，
左栏是开票方与付款地址。按坐标分栏可避免线性抽取把两栏挤在一行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

import fitz  # PyMuPDF
import pdfplumber

# 常见"非断行空格家族" → 普通空格；"真连字符/图形连字符" → ASCII "-"。
# PDF/字体常把 ASCII 空格/连字符渲染成 U+00A0 / U+2011 等（如 Noto CJK），导致
# 分词与日期/编号解析失败（"JP-2026-5005" 被 U+2011 拆成多词、日期含 U+2011 无法解析）。
# 只归一无歧义的空格/连字符，**不动** en/em dash(U+2013/2014，语义不同、别处已按需处理)。
_TXT_NORMALIZE = {
    0xA0: " ", 0x2007: " ", 0x202F: " ", 0x2060: "", 0x200B: "",   # nbsp/figure/narrow/word-joiner/zero-width
    0x2010: "-", 0x2011: "-", 0x2012: "-",                          # hyphen / non-breaking hyphen / figure dash
}


def _norm_text(s: str) -> str:
    return s.translate(_TXT_NORMALIZE) if s else s

# 左右栏分界 x 坐标（样例右栏标签起于 ~304/306，值在 391/411）。
# 300 是对标准竖版(A4 595 / Letter 612)调校过、正好落在左右栏之间的值；
# 但它是**绝对坐标**，对横版/异常宽页会严重偏左 → 见 col_split_for()：宽页按页宽取中线。
COL_SPLIT = 300.0
_WIDE_PAGE = 700.0     # 超过此宽度视为横版/宽页（标准竖版 ≤ ~660）
# 同一视觉行的 y 容差
Y_TOL = 3.0


def col_split_for(width: float) -> float:
    """按页宽给出左右栏分界：标准竖版沿用调校过的 300（零回归），宽页/横版按页宽取中线。"""
    if width and width > _WIDE_PAGE:
        return width * 0.5
    return COL_SPLIT


@dataclass
class Line:
    y: float
    words: List[Tuple[float, float, str]]  # (x0, x1, text) 按 x 排序
    col_split: float = COL_SPLIT           # 本行所属页的左右栏分界（按页宽自适应）

    def text(self) -> str:
        return " ".join(w[2] for w in self.words)

    def left_text(self) -> str:
        return " ".join(w[2] for w in self.words if w[0] < self.col_split)

    def right_text(self) -> str:
        return " ".join(w[2] for w in self.words if w[0] >= self.col_split)


@dataclass
class PdfDoc:
    full_text: str = ""            # PyMuPDF 线性文本（原文归档用）
    plumber_text: str = ""         # pdfplumber 文本（交叉复核用）
    lines: List[Line] = field(default_factory=list)
    char_count: int = 0
    # 页内坐标词元 (page, x0, y0, x1, y1, text)，y 为页内坐标（不做跨页偏移），
    # 供审核界面把字段定位到原件页上叠框；page_sizes 为各页尺寸 [[w,h],...]（pt）。
    words_geom: List[Tuple[int, float, float, float, float, str]] = field(default_factory=list)
    page_sizes: List[list] = field(default_factory=list)

    def right_block(self) -> str:
        return "\n".join(ln.right_text() for ln in self.lines if ln.right_text())

    def left_block(self) -> str:
        return "\n".join(ln.left_text() for ln in self.lines if ln.left_text())


def _group_lines(words, col_split: float = COL_SPLIT) -> List[Line]:
    """把 (x0,y0,x1,y1,word,...) 词列表按 y 分组成视觉行。col_split 为该页左右栏分界。"""
    words = sorted(words, key=lambda w: (round(w[1], 1), w[0]))
    lines: List[Line] = []
    cur: List[Tuple[float, float, str]] = []
    cur_y = None
    for w in words:
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        if not txt.strip():
            continue
        if cur_y is None:
            cur_y = y0
        if abs(y0 - cur_y) > Y_TOL:
            cur.sort(key=lambda c: c[0])
            lines.append(Line(cur_y, cur, col_split))
            cur = []
            cur_y = y0
        cur.append((x0, x1, txt))
    if cur:
        cur.sort(key=lambda c: c[0])
        lines.append(Line(cur_y, cur, col_split))
    return lines


def pdfdoc_from_word_tuples(words, full_text: str = "", col_split: float = COL_SPLIT,
                            words_geom=None, page_sizes=None) -> PdfDoc:
    """从 (x0,y0,x1,y1,text) 词元构造 PdfDoc（供 OCR 坐标重建复用文本路径解析）。
    words_geom/page_sizes 传入时一并带上，使图片/扫描件也能把字段定位到原件叠框。"""
    lines = _group_lines(words, col_split)
    return PdfDoc(full_text=full_text, plumber_text="", lines=lines,
                  char_count=len(full_text.strip()),
                  words_geom=list(words_geom or []), page_sizes=list(page_sizes or []))


def extract_pdf(path: Path) -> PdfDoc:
    """抽取全部页的结构化文本。

    多页时按累计页高对 y 做偏移，既避免跨页行被错误合并，又保证所有页的
    文本/词元都进入归档与解析，绝不丢页（计划：完整 PDF 文本是回溯安全网）。
    """
    doc = fitz.open(path)
    texts: List[str] = []
    all_words = []
    words_geom: List[Tuple[int, float, float, float, float, str]] = []
    page_sizes: List[list] = []
    y_offset = 0.0
    for pno, page in enumerate(doc):
        texts.append(_norm_text(page.get_text("text")))
        page_sizes.append([page.rect.width, page.rect.height])
        for w in page.get_text("words"):
            x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], _norm_text(w[4])
            all_words.append((x0, y0 + y_offset, x1, y1 + y_offset, txt))
            if txt.strip():
                words_geom.append((pno, x0, y0, x1, y1, txt))   # 页内坐标，定位用
        y_offset += page.rect.height + 20.0   # 页间留间隔，防止跨页合并
    doc.close()
    full_text = "\n".join(texts)
    # 左右栏分界按首页宽自适应（横版/宽页不再钉死 300）
    split = col_split_for(page_sizes[0][0]) if page_sizes else COL_SPLIT
    lines = _group_lines(all_words, split)

    plumber_text = ""
    try:
        with pdfplumber.open(path) as pdf:
            parts = [(p.extract_text() or "") for p in pdf.pages]
            plumber_text = "\n".join(parts)
    except Exception:
        plumber_text = ""

    return PdfDoc(
        full_text=full_text,
        plumber_text=plumber_text,
        lines=lines,
        char_count=len(full_text.strip()),
        words_geom=words_geom,
        page_sizes=page_sizes,
    )
