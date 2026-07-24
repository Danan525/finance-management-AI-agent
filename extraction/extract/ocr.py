"""本地 OCR 兜底：PaddleOCR + 图像预处理 + 关键区域二次识别。

仅在 PDF 无法直接抽取文本（扫描型）或抽取置信度过低时启用。
完全本地运行，不调用任何云端服务。
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from core import config

# OCR 依赖（numpy/Pillow）为可选：未安装时本模块仍可导入，OCR 兜底优雅降级，
# 核心的「文本型 PDF」路径不受影响。
try:
    import numpy as np
    from PIL import Image, ImageOps, ImageFilter
    _DEPS_OK = True
    # 收紧 PIL 像素上限：超大图（解压炸弹）解码即抛 DecompressionBombError → 上层兜底为 failed 记录，
    # 不会撑爆内存拖垮共享机（PIL 在 2×上限处才抛错，故 //2 使有效硬上限≈MAX_IMAGE_PIXELS）。
    Image.MAX_IMAGE_PIXELS = max(1, int(config.MAX_IMAGE_PIXELS // 2))
except Exception:  # pragma: no cover
    np = None
    Image = ImageOps = ImageFilter = None
    _DEPS_OK = False

# ---- 多引擎 OCR：PaddleOCR 优先，RapidOCR(onnxruntime) 兜底 --------------
# RapidOCR 不依赖 paddlepaddle，在 Apple Silicon 等 Paddle 装不上的平台仍可用，
# 且模型随包分发、无需联网下载。两引擎输出统一归一化为 (text, score, box4点)。
_ENGINE = None
_KIND = None            # "paddle" | "rapid" | None
_READY = None           # 是否已尝试加载


def _try_paddle():
    from paddleocr import PaddleOCR
    # 放宽文字检测阈值：稀疏/浅色/低分辨率件（如浅灰扫描收据）默认阈值会漏检大片文字，
    # 调低 det 阈值 + 放大检测边长可显著提升召回（离线诊断实测：命中 8/10→9/10）。
    return PaddleOCR(use_angle_cls=True, lang="en", show_log=False,
                     det_db_thresh=0.2, det_db_box_thresh=0.3,
                     det_db_unclip_ratio=1.8, det_limit_side_len=2200)


def _try_rapid():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def _load_engine():
    """按偏好加载首个可用引擎；OCR_ENGINE 环境变量可强制 paddle/rapid。"""
    global _ENGINE, _KIND, _READY
    if _READY is not None:
        return _ENGINE
    _READY = True
    if not _DEPS_OK:
        print("[ocr] 未安装 OCR 依赖(numpy/Pillow)，OCR 兜底跳过；文本型 PDF 不受影响。")
        return None

    prefer = os.environ.get("OCR_ENGINE", "").strip().lower()
    order = [("paddle", _try_paddle), ("rapid", _try_rapid)]
    if prefer == "rapid":
        order.reverse()
    elif prefer == "paddle":
        pass  # 已是默认顺序

    errors = []
    for kind, loader in order:
        try:
            _ENGINE = loader()
            _KIND = kind
            print(f"[ocr] 使用 OCR 引擎: {kind}")
            return _ENGINE
        except Exception as e:
            errors.append(f"{kind}: {e}")
    _ENGINE, _KIND = None, None
    print("[ocr] 无可用 OCR 引擎（PaddleOCR/RapidOCR 均不可用），OCR 兜底跳过；"
          "文本型 PDF 不受影响。Apple Silicon 请安装 requirements-ocr-mac.txt。\n  " +
          "\n  ".join(errors))
    return None


def ocr_available() -> bool:
    return _load_engine() is not None


def ocr_engine_name() -> str:
    _load_engine()
    return _KIND or ""


# ---- 金额场景字符纠错（$ 常被 OCR 成 S；带底纹大字的小数点常被认成冒号）----
_MONEYISH = re.compile(r"^\$?\d[\d,]*[.:]\d{2}$|^\$?\d{1,3}(?:,\d{3})+$")


def _money_like(t: str) -> bool:
    return bool(_MONEYISH.fullmatch((t or "").strip()))


def _fix_amount_tokens(tokens: List[Tuple[str, float, list]]) -> List[Tuple[str, float, list]]:
    """把金额附近的常见 OCR 误识别纠正回来（仅在明确的金额语境，避免误纠普通字母）：
    ① "S7,000.00"/"S 7,000.00"（S 粘着金额）→ "$…"；
    ② 独立 "S"/"s" 且同行右侧紧邻一个金额 token → "$"（那个 S 其实是 $ 符号）；
    ③ 金额 token 内 数字:数字 的冒号 → 小数点（"$7,000:00" → "$7,000.00"）。"""
    fixed = [[t, s, b] for t, s, b in tokens]

    def cy(b):
        ys = [p[1] for p in b]; return (min(ys) + max(ys)) / 2

    def hgt(b):
        ys = [p[1] for p in b]; return max(ys) - min(ys)

    for it in fixed:
        t = (it[0] or "").strip()
        m = re.match(r"^[Ss]\s?(\d[\d,]*(?:[.:]\d+)?)$", t)      # ① S 粘金额
        if m and _money_like(m.group(1)):
            it[0] = "$" + m.group(1)

    page_has_money = any(_money_like(t) for t, _s, _b in fixed)  # 是含金额的单据（发票/收据）
    for i, (t, s, b) in enumerate(fixed):                        # ② 独立单字符 S → $
        if (t or "").strip() in ("S", "s"):
            x1 = max(p[0] for p in b)
            adjacent = False
            for j, (t2, s2, b2) in enumerate(fixed):
                if j == i or not _money_like(t2):
                    continue
                x0b = min(p[0] for p in b2)
                if abs(cy(b) - cy(b2)) < hgt(b) * 0.8 and 0 <= x0b - x1 < hgt(b) * 3:
                    adjacent = True
                    break
            # 右邻金额 → 必是 $；或即便数字没检出，只要整页有金额语境，孤立单字符 S 也基本是 $ 符号
            if adjacent or page_has_money:
                fixed[i][0] = "$"

    for it in fixed:                                             # ③ 金额内冒号→小数点
        if _money_like(it[0]) and ":" in it[0]:
            it[0] = re.sub(r"(?<=\d):(?=\d)", ".", it[0])

    return [(t, s, b) for t, s, b in fixed]


def _infer(arr) -> List[Tuple[str, float, list]]:
    """调用当前引擎并归一化输出为 [(文本, 置信度, 框4点), ...]。"""
    eng = _load_engine()
    if eng is None:
        return []
    out: List[Tuple[str, float, list]] = []
    if _KIND == "paddle":
        raw = eng.ocr(arr, cls=True)
        if raw and raw[0]:
            for box, (text, score) in raw[0]:
                out.append((text, float(score), box))
    else:  # rapid
        result, _elapse = eng(arr)
        for item in (result or []):
            box, text, score = item[0], item[1], item[2]
            out.append((text, float(score), box))
    return out


# ---- 中文兜底引擎（RapidOCR，中英通吃、onnx CPU 安全，不会像 paddle-ch 那样 SIGILL）----
# 主引擎为 paddle(lang=en) 时，遇到中文/非英文件会读成乱码；此时用 RapidOCR 再跑一遍择优，
# 精准补上中文识别，且完全不影响英文（英文件主引擎足够强、不触发兜底）。
_RAPID = None
_RAPID_READY = None
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")


def _load_rapid():
    global _RAPID, _RAPID_READY
    if _RAPID_READY is not None:
        return _RAPID
    _RAPID_READY = True
    if not _DEPS_OK:
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID = RapidOCR()
    except Exception:
        _RAPID = None
    return _RAPID


def _infer_rapid(arr) -> List[Tuple[str, float, list]]:
    eng = _load_rapid()
    if eng is None:
        return []
    result, _elapse = eng(arr)
    return [(item[1], float(item[2]), item[0]) for item in (result or [])]


def _cjk_count(s: str) -> int:
    return len(_CJK_RE.findall(s or ""))


def _useful_chars(s: str) -> int:
    """有效字符数：CJK 权重更高（本兜底就是为补中文）+ 字母数字。"""
    return _cjk_count(s) * 2 + sum(c.isalnum() for c in (s or ""))


# ---- 图像预处理 ----------------------------------------------------------
def preprocess(img: Image.Image) -> Image.Image:
    """灰度、增强对比度、去噪。**只做不改变几何(等比、不旋转)的处理**——

    否则 OCR 词坐标会落在被变换过的图坐标系里，与原件预览对不上（字段高亮错位、框选取词错位）。
    倾斜方向由 PaddleOCR 的 use_angle_cls 处理；不再做整图 deskew 旋转（PCA 测角在稀疏文本上会误判、
    把干净截图大幅错转，破坏坐标对齐——见 20260708 §五十三）。
    """
    gray = img.convert("L")                               # 转灰度（注：未特殊弱化红色印章通道，如遇红章遮挡需另加处理）
    gray = ImageOps.autocontrast(gray, cutoff=2)          # 增强对比度（不改几何）
    gray = gray.point(_GAMMA_LUT)                         # gamma>1 压暗中间调：带底纹/浅灰的字更黑、更易检测
    gray = gray.filter(ImageFilter.MedianFilter(size=3))  # 去噪（不改几何）
    return gray


# gamma≈1.6 的 256 级查找表：压暗中间调，让"带斜纹底纹/浅灰"的大字数字变黑、被检测到
# （对纯黑文字/白底几乎无影响；点变换、不改几何——不影响坐标对齐）。
_GAMMA_LUT = [min(255, int((i / 255.0) ** 1.6 * 255)) for i in range(256)]


def _deskew(img: Image.Image) -> Image.Image:
    """基于像素分布的粗略倾斜校正。"""
    arr = np.array(img)
    thr = arr < 128  # 文字像素
    coords = np.column_stack(np.where(thr))
    if coords.shape[0] < 50:
        return img
    # PCA 估计主方向角度
    coords = coords - coords.mean(axis=0)
    cov = np.cov(coords.T)
    try:
        evals, evecs = np.linalg.eigh(cov)
        vec = evecs[:, np.argmax(evals)]
        angle = np.degrees(np.arctan2(vec[0], vec[1]))
        if angle > 45:
            angle -= 90
        elif angle < -45:
            angle += 90
        if abs(angle) > 0.5:
            return img.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
    except np.linalg.LinAlgError:
        pass
    return img


def _upscale_small(img: Image.Image, min_side: int = 1800, target: int = 2000):
    """把过小的图（如截图/低分辨率照片）放大到合适尺寸再 OCR，显著提升稀疏细字检出。
    已足够大的图（如 PDF@300dpi 渲染页）原样返回。返回 (图, 放大倍数)。"""
    w, h = img.size
    m = min(w, h)
    if m >= min_side:
        return img, 1.0
    f = target / m
    return img.resize((round(w * f), round(h * f)), Image.LANCZOS), f


def _bound_large(img: Image.Image, max_side: Optional[int] = None):
    """OCR 前把超大图按最长边封顶缩小，限制内存/耗时（超出对识别无益）。返回 (图, 缩放倍数)。"""
    max_side = max_side or config.OCR_MAX_SIDE
    w, h = img.size
    m = max(w, h)
    if m <= max_side:
        return img, 1.0
    f = max_side / m
    return img.resize((max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS), f


def pdf_to_images(path: Path, dpi: int = 300) -> List[Image.Image]:
    """把 PDF 渲染为高分辨率图片（OCR 兜底用）。

    注意：**一次性渲染全部页进内存**，多页大 PDF 会占大量常驻内存。整档 OCR 请用 `ocr_pdf`
    （已改为逐页渲染），只取某页请用 `render_pdf_page`——都不必把全部页图同时留在内存。"""
    return [im for im in _iter_pdf_images(path, dpi)]


def _iter_pdf_images(path: Path, dpi: int = 300):
    """逐页生成器：一次只在内存里保留**当前一页**的渲染图（用完即可释放），避免多页 PDF 的内存尖峰。"""
    import fitz
    doc = fitz.open(path)
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    try:
        for pg in doc:
            pix = pg.get_pixmap(matrix=mat)
            yield Image.open(io.BytesIO(pix.tobytes("png")))
    finally:
        doc.close()


def render_pdf_page(path: Path, n: int = 0, dpi: int = 300) -> Optional[Image.Image]:
    """只渲染 PDF 的第 n 页（0 基）为图片——避免"为拿一页而渲染全部页"的浪费。"""
    import fitz
    doc = fitz.open(path)
    try:
        if n < 0 or n >= doc.page_count:
            return None
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = doc[n].get_pixmap(matrix=mat)
        return Image.open(io.BytesIO(pix.tobytes("png")))
    finally:
        doc.close()


# ---- OCR 识别 ------------------------------------------------------------
class OcrResult:
    def __init__(self, text: str, tokens: List[Tuple[str, float, list]], overall: float,
                 dpi: int = 200, img_size: Optional[Tuple[int, int]] = None):
        self.text = text
        self.tokens = tokens          # [(文本, 置信度, 框)]
        self.overall = overall        # 整体平均置信度
        self.dpi = dpi                # 渲染 DPI，用于把像素坐标换算回 PDF 点
        self.img_size = img_size      # OCR 所见图像像素尺寸 (w,h)，用于重建页尺寸供原件叠框

    def to_pdfdoc(self):
        """用 OCR 框坐标重建 PdfDoc，复用文本路径的分栏解析逻辑。

        像素坐标换算回 PDF 点（72/dpi），使 COL_SPLIT 等阈值与文本路径一致；
        同时产出 words_geom(第0页) 与 page_sizes，使**图片/扫描件也能字段↔原件双向定位高亮**。
        """
        from .pdf_text import pdfdoc_from_word_tuples
        scale = 72.0 / float(self.dpi)
        words = []
        geom = []
        for text, _score, box in self.tokens:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x0, y0, x1, y1 = min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale
            words.append((x0, y0, x1, y1, text))
            geom.append((0, x0, y0, x1, y1, text))     # 单页，页号 0
        page_sizes = [[self.img_size[0] * scale, self.img_size[1] * scale]] if self.img_size else []
        return pdfdoc_from_word_tuples(words, full_text=self.text,
                                       words_geom=geom, page_sizes=page_sizes)


def run_ocr(img: Image.Image, do_preprocess: bool = True,
            dpi: Optional[int] = None, use_rapid: bool = False) -> Optional[OcrResult]:
    """对单张图片做 OCR，返回文本、逐 token 置信度、整体置信度。

    dpi 用于把像素坐标换算回 PDF 点；若未知则按 A4 宽度(8.27in)估算。
    use_rapid=True 强制用 RapidOCR（中文兜底）而非主引擎。
    """
    if use_rapid:
        if _load_rapid() is None:
            return None
    elif _load_engine() is None:
        return None
    if dpi is None:
        dpi = max(72, int(round(img.width / 8.27)))
    # 过小图先放大（截图/低分辨率件召回的头号杠杆）；放大后按倍数同步提高 dpi，
    # 使像素坐标换算回 PDF 点保持一致（大图为 no-op，不影响 PDF@300dpi 等既有路径）。
    img, up = _upscale_small(img)
    if up != 1.0:
        dpi = int(round(dpi * up))
    # 巨图封顶缩小（防解压炸弹式 OOM）；缩小则按比例降 dpi，保持像素坐标换算回 PDF 点一致。
    img, dn = _bound_large(img)
    if dn != 1.0:
        dpi = max(72, int(round(dpi * dn)))
    if do_preprocess:
        img = preprocess(img)
    arr = np.array(img.convert("RGB"))
    tokens = _infer_rapid(arr) if use_rapid else _infer(arr)
    tokens = _fix_amount_tokens(tokens)      # 金额场景字符纠错：$↔S、数字间冒号→小数点
    text = _rows_to_text(tokens)
    overall = float(np.mean([t[1] for t in tokens])) if tokens else 0.0
    return OcrResult(text, tokens, overall, dpi=dpi, img_size=(img.width, img.height))


_JUNK_RE = re.compile(r"[&#@~^*_=<>{}\\|]")


def _garble_tokens(tokens) -> int:
    """疑似乱码 token 数：含 &#@~ 等符号，或短串(≤4)里有非常规标点——paddle(en) 读中文的典型产物。"""
    n = 0
    for tok in (tokens or []):
        t = tok[0] if isinstance(tok, (list, tuple)) else str(tok)
        if _JUNK_RE.search(t):
            n += 1
        elif len(t) <= 4 and sum(1 for c in t if not c.isalnum() and c not in " .,:%$()-/￥¥") >= 1:
            n += 1
    return n


def run_ocr_best(img: Image.Image, do_preprocess: bool = True,
                 dpi: Optional[int] = None) -> Optional[OcrResult]:
    """主引擎优先；结果偏弱（低置信/词少）时用 RapidOCR 中文兜底再跑，取"有效字符更多"者。

    英文件主引擎强、不触发兜底 → 行为与原来一致、零额外开销；
    中文件主引擎(paddle en)读成乱码 → 兜底 RapidOCR 读出中文并胜出。
    """
    primary = run_ocr(img, do_preprocess, dpi)
    if primary is None:
        return None
    if _KIND == "rapid":                 # 主引擎已是 RapidOCR（中英通吃），无需再兜
        return primary
    # 触发 RapidOCR 中文兜底的情形：① 拉丁字符极少（纯中文件）；② paddle(en) 读中文常得到
    # **乱码 token**（含 &#@~ 等符号、或短混杂串如 "iQi+"/"8&33"/"F#"）——混排件拉丁多但仍需中文兜底。
    latin = sum(1 for c in primary.text if c.isascii() and c.isalnum())
    suspect_non_latin = latin < max(10, len(primary.tokens))
    suspect = suspect_non_latin or _garble_tokens(primary.tokens) >= 2 or (primary.overall or 1) < 0.85
    if not suspect or _load_rapid() is None:
        return primary
    alt = run_ocr(img, do_preprocess, dpi, use_rapid=True)
    if alt is None:
        return primary
    return alt if _useful_chars(alt.text) > _useful_chars(primary.text) else primary


def _rows_to_text(tokens: List[Tuple[str, float, list]], y_tol: float = 12.0) -> str:
    """按坐标把 OCR token 重组成行，保证 'Invoice #: 555008' 这类 label:value 同行。"""
    if not tokens:
        return ""
    items = []
    for text, score, box in tokens:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append((min(ys), min(xs), text))
    items.sort(key=lambda t: (t[0], t[1]))
    lines: List[List[Tuple[float, str]]] = []
    cur: List[Tuple[float, str]] = []
    cur_y = None
    for y, x, text in items:
        if cur_y is None:
            cur_y = y
        if abs(y - cur_y) > y_tol:
            lines.append(cur)
            cur = []
            cur_y = y
        cur.append((x, text))
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        ln.sort(key=lambda t: t[0])
        out.append(" ".join(t[1] for t in ln))
    return "\n".join(out)


def ocr_pdf(path: Path, dpi: int = 300) -> Optional[OcrResult]:
    """对整份 PDF（渲染为图片后）做 OCR。MVP 固定格式发票为单页，取首页。"""
    # **逐页**渲染 + OCR（一次只驻留一页图，避免多页 PDF 的 1GB+ 内存尖峰 → OOM）。
    r = None
    for i, img in enumerate(_iter_pdf_images(path, dpi=dpi)):
        try:
            if i == 0:
                # 单页发票：对首页识别，保留坐标用于分栏重建（含中文兜底）
                r = run_ocr_best(img, dpi=dpi)
                if r is None:
                    return None
            else:
                # 多页时仅附加后续页**文本**；坐标(tokens)只保留首页——后续页像素坐标属各自
                # 页坐标系，并入首页会致原件高亮错位，故不并入。
                extra = run_ocr_best(img, dpi=dpi)
                if extra:
                    r.text += "\n" + extra.text
        finally:
            img.close()                       # 及时释放当前页，不累积
    return r if r is not None else OcrResult("", [], 0.0, dpi=dpi)


def ocr_region(img: Image.Image, bbox: Tuple[int, int, int, int]) -> Optional[OcrResult]:
    """对关键字段区域裁剪后二次识别（TOTAL DUE、Subtotal、Tax、Invoice No 等）。"""
    crop = img.crop(bbox)
    # 放大有助于小字识别
    crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    return run_ocr(crop)
