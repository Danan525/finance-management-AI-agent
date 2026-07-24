"""判断 PDF 是文本型还是扫描型。"""
from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF

from core import config

# 未映射 CID 字体（子集化后缺 ToUnicode）抽出的"文本"多落在 Unicode 私用区(E000–F8FF)
# 或替换字符 U+FFFD——正常发票文本几乎不含这些，故以其占比作乱码信号，近乎零误伤。
_GARBLE_RE = re.compile("[-�]")


def text_layer_suspect(text: str) -> bool:
    """文本层疑似乱码（有文本却是不可读的 CID 私用区字符）→ 应改走 OCR，而非直接解析乱码。"""
    t = (text or "").strip()
    nonspace = [c for c in t if not c.isspace()]
    if len(nonspace) < 20:
        return False
    garble = len(_GARBLE_RE.findall(t))
    return garble / len(nonspace) > 0.08


def classify_pdf(path: Path) -> tuple[bool, int, int]:
    """返回 (是否文本型, 总字符数, 页数)。

    每页平均可抽取字符数低于阈值 -> 判为扫描型，需 OCR 兜底。
    """
    doc = fitz.open(path)
    total_chars = 0
    pages = doc.page_count
    for pg in doc:
        total_chars += len(pg.get_text("text").strip())
    doc.close()
    avg = total_chars / max(pages, 1)
    is_text = avg >= config.MIN_TEXT_CHARS_PER_PAGE
    return is_text, total_chars, pages
