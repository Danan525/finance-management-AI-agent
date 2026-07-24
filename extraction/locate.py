"""字段→原件坐标回填（供审核界面双向联动）。

解析（template_rules）只产出字段的标准化值与原始文本 `raw`，不带坐标。
本模块在解析后用 `raw` 文本在页内词坐标里反查它落在原件的哪个框，
写回 `FieldValue.bbox = [page, x0, y0, x1, y1]`（pt，左上原点）。

设计取舍：
- 只对**文本型 PDF**（PyMuPDF 词坐标可靠）回填；OCR/图片经 deskew/预处理后
  坐标与原件不对齐，不回填（审核界面退化为只显示原件、字段仍可编辑）。
- 不改动 template_rules 的解析逻辑，纯后处理；定位不到就留空、不报错。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from core.models import Invoice

WordGeom = Tuple[int, float, float, float, float, str]  # (page, x0, y0, x1, y1, text)

_MAX_WINDOW = 24      # 一个字段值最多跨几个相邻词（放宽以容纳多行地址等长值）
_LINE_GAP_MAX = 28.0  # 相邻行 y 间隔上限（pt）：地址等可跨**正常行距**的多行，超过即断窗（防跨到无关块）
_MIN_LEN = 2          # 太短的值（如单字符）不定位，避免误命中
_BLOCK_MAX_H = 100.0  # 多行地址块的最大纵向高度(pt)：逐段定位并集时，只并同一簇，丢远处同名词离群命中


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _find_one(target: str, words: List[WordGeom]) -> Optional[List[float]]:
    """在词序列里找文本拼接 == target 的最小窗口，返回 bbox（可跨正常行距的多行，如地址）。

    精确等值优先返回；窗口可跨行但行距超过 _LINE_GAP_MAX 即断（避免把无关块也框进来）；
    长度明显超出即停。退而求其次用"包含"匹配里面积最小的。
    """
    nt = _norm(target)
    if len(nt) < _MIN_LEN:
        return None
    n = len(words)
    best_contains: Optional[List[float]] = None
    for i in range(n):
        page_i = words[i][0]
        last_y = words[i][2]
        concat = ""
        for j in range(i, min(i + _MAX_WINDOW, n)):
            pg, x0, y0, x1, y1, txt = words[j]
            if pg != page_i:
                break
            if j > i and y0 - last_y > _LINE_GAP_MAX:    # 跨过过大的行距 → 断窗（不同区块）
                break
            last_y = max(last_y, y0)
            concat += _norm(txt)
            box = [pg,
                   min(w[1] for w in words[i:j + 1]),
                   min(w[2] for w in words[i:j + 1]),
                   max(w[3] for w in words[i:j + 1]),
                   max(w[4] for w in words[i:j + 1])]
            if concat == nt:
                return box                          # 精确等值优先，立即返回
            if nt in concat:                        # 退而求其次：包含；取面积最小的窗口
                if best_contains is None or _area(box) < _area(best_contains):
                    best_contains = box
            if len(concat) > len(nt) + 4:
                break                                # 已明显超出，停窗
    return best_contains


def _area(b: List[float]) -> float:
    return (b[3] - b[1]) * (b[4] - b[2])


def _find_block(target: str, words: List[WordGeom]) -> Optional[List[float]]:
    """逗号分隔的多行值（如地址）整段匹配不到时：逐段定位再合并成一个框。

    适配"多行地址 + 与另一栏交错"导致连续词被打断的情况——每段（'12 Harbour Road'、
    'Suite 800'…）各自单行可定位，命中 ≥2 段就并成覆盖该块的一个框。
    """
    parts = [p.strip() for p in target.split(",") if len(_norm(p)) >= 3]
    if len(parts) < 2:
        return None
    boxes = [b for b in (_find_one(p, words) for p in parts) if b]
    if len(boxes) < 2:
        return None
    from collections import Counter
    pg = Counter(b[0] for b in boxes).most_common(1)[0][0]   # 取命中最多的页
    boxes = sorted((b for b in boxes if b[0] == pg), key=lambda b: b[2])
    # 选**纵向最紧凑**的一簇再并集：地址各段本应集中在相邻几行；像 "Central/Hong Kong/Road" 这类
    # 通用词常在别处(如银行 Branch 行)也命中，若无脑并集会把框撑到半页。以簇顶到框底 ≤ 阈值成簇，
    # 取包含段数最多(同数取纵向最窄)的簇，丢弃远处离群命中。
    best = None
    for i in range(len(boxes)):
        cluster = [boxes[i]]
        for b in boxes[i + 1:]:
            if b[4] - boxes[i][2] <= _BLOCK_MAX_H:
                cluster.append(b)
        span = cluster[-1][4] - cluster[0][2]
        if best is None or len(cluster) > len(best[0]) or (len(cluster) == len(best[0]) and span < best[1]):
            best = (cluster, span)
    cl = best[0]
    return [pg, min(b[1] for b in cl), min(b[2] for b in cl),
            max(b[3] for b in cl), max(b[4] for b in cl)]


def resolve_field_bboxes(inv: Invoice, words_geom: List[WordGeom]) -> None:
    """对每个有 raw 文本的字段回填 bbox（就地修改 inv）。"""
    if not words_geom:
        return
    words = sorted(words_geom, key=lambda w: (w[0], round(w[2], 1), w[1]))
    for fv in inv.fields.values():
        if fv.bbox is not None or not fv.raw:
            continue
        box = _find_one(fv.raw, words) or _find_block(fv.raw, words)
        if box is not None:
            fv.bbox = box


def _union(a: Optional[List[float]], b: Optional[List[float]]) -> Optional[List[float]]:
    """同页两个框取并集覆盖整行；异页/缺失则取其一。"""
    if a is None:
        return b
    if b is None or b[0] != a[0]:
        return a
    return [a[0], min(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3]), max(a[4], b[4])]


def _row_expand(box: List[float], words: List[WordGeom]) -> List[float]:
    """把一个词框扩成它所在整行：同页、y 与该框中心线重叠的所有词的并集。
    用于描述文本没匹配上、但金额匹配到时——据金额所在行覆盖整条明细（含描述）。"""
    pg, cy = box[0], (box[2] + box[4]) / 2
    row = [w for w in words if w[0] == pg and w[2] <= cy <= w[4]]
    if not row:
        return box
    return [pg, min(w[1] for w in row), min(w[2] for w in row),
            max(w[3] for w in row), max(w[4] for w in row)]


def resolve_line_item_bboxes(inv: Invoice, words_geom: List[WordGeom]) -> None:
    """给每条明细回填 bbox（就地修改 inv）：优先按描述文本定位并与金额框取并集覆盖整行；
    描述匹配不到（长/花描述）但金额匹配到时，用**金额所在整行**兜底覆盖该明细。

    与字段同法（文本匹配词坐标）；定位不到就留空、不报错。描述/金额在页内多处出现时取首个
    （已按 页→y→x 排序，主表通常在前）。"""
    if not words_geom:
        return
    words = sorted(words_geom, key=lambda w: (w[0], round(w[2], 1), w[1]))
    for li in inv.line_items:
        if li.bbox is not None:
            continue
        dbox = _find_one(li.description, words) if li.description else None
        abox = _find_one(li.amount_raw, words) if li.amount_raw else None
        if dbox and abox:
            box = _union(dbox, abox)
        elif dbox:
            box = dbox
        elif abox:
            box = _row_expand(abox, words)      # 描述没匹配上 → 金额行扩成整行，覆盖描述
        else:
            box = None
        if box is not None:
            li.bbox = box
        # 勾稽子明细各自定位（金额多唯一 → 优先按金额所在整行覆盖该子行；否则按描述）
        for s in (li.sub_items or []):
            if s.get("bbox"):
                continue
            sab = _find_one(s.get("amount"), words) if s.get("amount") else None
            sdb = _find_one(s.get("description"), words) if s.get("description") else None
            sbox = _row_expand(sab, words) if sab else sdb
            if sbox is not None:
                s["bbox"] = sbox
