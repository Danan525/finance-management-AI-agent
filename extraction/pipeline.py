"""处理流水线编排。

落盘 → 判型 → PDF文本抽取(主+交叉) / OCR兜底 → 字段解析 →
置信度评估 → 校验 → 风险评分 → 规则分类 → 入库 → 可生成 Excel。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core import config, db, storage
from .classify import engine as classifier
from .excel import writer as excel_writer
from .extract import ocr as ocr_mod
from .extract import excel, office, pdf_text, pdf_type, word
from . import locate
from core.models import Invoice
from .parse import template_rules
from .validate import checks, confidence, risk

# 小数位用 \.\d+ 而非写死两位：与 amount.py 的"保留真实精度"一致，
# 否则高精度加密金额（>2 位小数）会匹配不到、导致双引擎/二次 OCR 复核被静默跳过。
_TOTAL_RE = re.compile(r"TOTAL\s*DUE\s*\$?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_INVNO_RE = re.compile(r"Invoice\s*#\s*[:：]\s*([A-Za-z0-9\-]+)", re.IGNORECASE)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_txt(s: str) -> str:
    """规范化文本用于"值是否出现在原件上"的核对：小写、去空白/标点，使多行地址等能连成一片比对。"""
    return re.sub(r"\W+", "", (s or "").lower())


def _docx_needs_libreoffice(path) -> bool:
    """该 .docx 是否需走 LibreOffice 转 PDF 补救：fitz 抽不到文本层，或**含中文**（fitz 对 CJK
    docx 的文本层/版式不稳，常被判可疑转 OCR 又读花）——LibreOffice 转出的 PDF 有干净文本层更可靠。
    前提：python-docx 确认该 docx 有文本内容（无文本的交给图片/其它路径）。"""
    try:
        wt = word._text(path)
    except Exception:
        wt = ""
    if len(wt.strip()) < 10:
        return False
    try:
        import fitz
        with fitz.open(str(path)) as d:
            ft = "".join(pg.get_text() for pg in d)
    except Exception:
        ft = ""
    if len(ft.strip()) < 10:
        return True                                  # fitz 抽空
    return bool(re.search(r"[一-鿿]", wt))    # 含中文 → 用 LibreOffice


def _apply_learned_defaults(inv: Invoice) -> None:
    """按对手方(开票方)填入人工确认过的字段默认值——**核对后再填、不盲填**（防过拟合）：
    仅填**当前为空**的字段，且**该默认值确实出现在当前发票原件文本里**（验证它就是这张发票上的值）
    才填；没核对到就留空交人工。学到的值标 source='learned'、置信度<1（随确认次数升）、仍走人工审核。
    """
    key = db.norm_key(inv.f("issuer_name").value)
    if not key:
        return
    from core.models import FieldValue
    ntext = _norm_txt(inv.raw_pdf_text or inv.raw_ocr_text or "")
    if not ntext:
        return
    for field, (value, cnt) in db.lookup_field_defaults(key).items():
        if inv.f(field).raw not in (None, "") or value in (None, ""):
            continue
        if _norm_txt(str(value)) not in ntext:      # 先核对：原件上确实出现该值 → 才填（不盲填）
            continue
        conf = min(0.97, 0.88 + 0.02 * (cnt or 1))
        inv.set(field, FieldValue(raw=str(value), value=value, confidence=conf,
                                  source="learned", note=f"对手方默认值·已核对原件(确认 x{cnt})"))


def _apply_learned_locators(inv: Invoice) -> None:
    """用**已启用的"字段定位线索"**在当前发票原文里现场按标签取值，补齐**空/通用兜底低置信**字段。

    软先验、非死模板：找不到/不合法即忽略；**绝不覆盖**模板精确命中或人工值；置信度随确认次数升、
    标 source='learned' 进人工复核。作用域按 对手方 或 类型指纹 命中。学习表为空时整体无操作。"""
    from core.models import FieldValue
    from . import learn
    from .parse import amount as amt, dates as dt
    text = inv.raw_pdf_text or inv.raw_ocr_text or ""
    if not text:
        return
    locs = db.active_field_locators(db.norm_key(inv.f("issuer_name").value), learn.fingerprint(text))
    for loc in locs:
        field, label, cnt = loc["field"], loc["label"], loc["confirm_count"]
        fv = inv.f(field)
        occupied = fv.raw not in (None, "")
        src = fv.source or ""
        # 已可信（模板精确命中/人工值/已学过且高置信）→ 不动；仅补 空 或 通用兜底(_generic)低置信
        if occupied and not src.endswith("_generic") and (fv.confidence or 0) >= 0.95:
            continue
        raw = learn.value_by_label(text, label, field)
        if not raw:
            continue
        conf = min(0.95, 0.80 + 0.03 * (cnt or 1))
        if field in learn._AMOUNT_FIELDS:
            val, susp, note = amt.parse_amount(raw)
            if val is None:
                continue
            inv.set(field, FieldValue(raw=raw, value=val, confidence=conf, source="learned",
                                      suspicious=susp, note="按学到的标签定位"))
        elif field in learn._DATE_FIELDS:
            iso, _nr = dt.normalize_date(raw)
            if iso is None:
                continue
            inv.set(field, FieldValue(raw=raw, value=iso, confidence=conf, source="learned",
                                      note="按学到的标签定位"))
        else:
            # 按字段类型校验注入值（如 invoice_no 是 id、必须像编号/含数字）——
            # 拒绝坏规则抓来的散文垃圾（如把"invoice number by email to"错学成发票号）。
            from .parse import generic as _g
            typ = {f: t for f, _p, t, _w in _g._LABELS}.get(field)
            val = _g._accept(typ, raw) if typ else raw
            if not val:
                continue
            inv.set(field, FieldValue(raw=raw, value=val, confidence=conf, source="learned",
                                      note="按学到的标签定位"))


def _classify_with_learned(inv: Invoice):
    """分类：优先用"该对手方"人工确认过的科目（学习），否则回退规则引擎。"""
    from core.models import Classification
    key = db.norm_key(inv.f("issuer_name").value)
    learned = db.lookup_classification(key) if key else None
    if learned and (learned["category"] or learned["account"]):
        cnt = learned["confirm_count"] or 1
        return Classification(category=learned["category"], account=learned["account"],
                              confidence=min(0.97, 0.85 + 0.03 * cnt),
                              hit_rules=[f"learned:issuer(确认 x{cnt})"], needs_review=True)
    return classifier.classify(inv)


def _finalize(inv: Invoice, ocr_pdf_mismatch: bool = False,
              dual_ocr_mismatch: bool = False, detect_reupload: bool = True) -> Invoice:
    """单张发票的收尾流程：置信度 → 查重 → 校验 → 风险 → 分类 → 状态 → 入库。

    detect_reupload=False 时不按"相同文件"判重（供系统内部重处理/再识别同一文件，避免自判重复）。
    """
    _apply_learned_defaults(inv)   # 规则即数据：按对手方填人工确认过的默认值（仅填空、不覆盖）
    confidence.assess(inv)
    dup = db.find_duplicate(inv.file_hash, inv.f("invoice_no").value, same_file=detect_reupload)
    checks.run_checks(inv, duplicate_of=dup)
    risk.compute(inv, ocr_pdf_mismatch=ocr_pdf_mismatch, dual_ocr_mismatch=dual_ocr_mismatch)
    inv.classification = _classify_with_learned(inv)
    # 提取完整性闸门：必填身份字段缺失 → 标记 incomplete + 强制人工，绝不评 Excellent
    missing_req = [f for f in config.REQUIRED_FIELDS if inv.f(f).raw in (None, "")]
    if missing_req:
        inv.parse_status = "incomplete"
        inv.critical_review = inv.critical_review or ("total_due" in missing_req or "invoice_no" in missing_req)
    # 所有单据都走普通 approve 确认，但"重点人工审核"只在真有风险时置位
    inv.needs_manual_review = (
        inv.critical_review
        or bool(missing_req)
        or inv.risk_score > config.RISK_THRESHOLD
        or inv.has_multiple_payment_methods   # 多个付款方式：须重点核对付给哪一方
        or any(i.severity in ("error", "critical") for i in inv.issues)
    )
    inv.review_status = "Pending Review"
    inv.approve_status = "Pending"
    inv.processed_at = _now()
    db.save_invoice(inv)   # 含完整 JSON 快照，作为导出/恢复的唯一数据源
    return inv


def process_path(path: Path, original_name: Optional[str] = None,
                 file_hash: Optional[str] = None, reprocess: bool = False,
                 split: str = "auto", doc_type: str = "invoice") -> List[Invoice]:
    """处理一个已落盘的文件，返回 Invoice 列表。

    文本型 PDF 若含多张发票（多个 Invoice # / TOTAL DUE），按边界切分逐张解析，
    每张成为独立记录（file_hash 加 #序号 以保持唯一）；其余情况返回单元素列表。

    reprocess=True：系统内部对**已入库的同一文件**重新识别（不按"相同文件"判重，避免自判重复）。
    用户上传走默认 False —— 重复上传同一文件会得到重复提醒。

    split：多发票切分模式——"auto"（默认，自动检测）/"single"（强制当作单张、不拆，
    供人工纠正"其实只有一张却被拆成多张"时用）。
    """
    detect = not reprocess
    path = Path(path)
    name = original_name or path.name
    if file_hash is None:
        file_hash = storage.sha256_of_file(path)

    suffix = path.suffix.lower()
    is_image = suffix in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")

    # 银行流水：走独立解析（结构化格式 CSV/MT940/OFX/CAMT/JSON 直解；PDF/Excel/扫描走版面+OCR）。
    # 放在 office 转换之前——否则 .csv/.xls 等会被当办公文档送去 LibreOffice。
    if doc_type == "statement":
        return [_process_statement(path, name, file_hash, is_image, detect_reupload=detect)]

    # 旧版/其它办公格式（.doc/.xls/.ppt/.rtf/.odt…）：用 LibreOffice 转成**带文本层的 PDF**，
    # 再走成熟的 PDF 路径（提取+预览+高亮+多发票拆分）。LibreOffice 不可用则抛→兜底 failed 记录。
    if office.is_convertible(suffix):
        if not office.available():
            raise ValueError(f"该格式需 LibreOffice 转换，但其不可用：{suffix}（请人工录入）")
        # 电子表格（.xls/.ods…）转 PDF 会按可见列宽裁剪单元格文字 → 改转 .xlsx 走 openpyxl，
        # 读全量单元格（不裁剪），再复用成熟的 Excel 路径。
        if office.is_spreadsheet(suffix):
            xlsx_bytes = office.to_xlsx(path)
            sub_hash = storage.sha256_of_bytes(f"{file_hash}:office-xlsx".encode())
            dest = config.UPLOAD_DIR / f"{sub_hash[:12]}_{Path(name).stem}.xlsx"
            dest.write_bytes(xlsx_bytes)
            return process_path(dest, original_name=name, file_hash=sub_hash,
                                reprocess=reprocess, split=split, doc_type=doc_type)
        # 文书类转 .docx（表格用 fitz 原生逐格读，避免 PDF 渲染把单元格粘连成一行）。
        if office.is_worddoc(suffix):
            docx_bytes = office.to_docx(path)
            sub_hash = storage.sha256_of_bytes(f"{file_hash}:office-docx".encode())
            dest = config.UPLOAD_DIR / f"{sub_hash[:12]}_{Path(name).stem}.docx"
            dest.write_bytes(docx_bytes)
            return process_path(dest, original_name=name, file_hash=sub_hash,
                                reprocess=reprocess, split=split, doc_type=doc_type)
        pdf_bytes = office.to_pdf(path)
        sub_hash = storage.sha256_of_bytes(f"{file_hash}:office-pdf".encode())   # 确定性、去重
        dest = config.UPLOAD_DIR / f"{sub_hash[:12]}_{Path(name).stem}.pdf"
        dest.write_bytes(pdf_bytes)
        return process_path(dest, original_name=name, file_hash=sub_hash,
                            reprocess=reprocess, split=split, doc_type=doc_type)

    # 其它无法自动提取的格式：抛出 → process_upload 兜底为 failed 记录入库，进队列置顶、
    # 供人工对照原件（可下载）录入（绝不静默丢弃）。
    _KNOWN = (".pdf", ".docx", ".docm", ".xlsx", ".xlsm",
              ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")
    if suffix not in _KNOWN:
        # 若是**银行流水**专属格式（CSV/OFX/MT940/QIF…）却按"发票"上传 → 明确提示改选"银行流水"，
        # 而非笼统"不支持"（.csv 其实支持、只是要选流水；"随手上传"最易在这一步选错类型）
        from .parse import statement_structured as _sst
        if suffix in getattr(_sst, "STRUCTURED_EXTS", ()) or suffix in (".mt940", ".sta", ".ofx", ".qfx"):
            raise ValueError(f"{suffix} 是银行流水格式——请在上传时选择「银行流水」而非「发票」")
        raise ValueError(f"暂不支持自动提取的格式：{suffix or '无扩展名'}（请人工录入）")

    # --- Excel(.xlsx) ---
    # 用 openpyxl 读结构化单元格（日期/金额按 number_format 正确取值，避免 fitz 把日期
    # 渲染成序列号），合成文本布局后复用通用解析；原件显示仍由 fitz 渲染 xlsx 图片（不叠框）。
    if excel.is_excel(path):
        # 图片形式发票（单元格几乎无文本、内嵌发票图）→ 把嵌入原图另存，逐张走图片(OCR)路径
        if excel.is_image_form(path):
            return _process_embedded_images(path, name, excel.extract_images(path), reprocess)
        # 多发票 xlsx（多表/单表内多段）→ 物理拆成一文件一发票，逐张走单张路径（同 PDF 逻辑）
        units = _cap_split(excel.invoice_units(path), "xlsx")
        if units and split != "single":
            out: List[Invoice] = []
            for dest, sub_name, sub_hash in excel.split_xlsx(path, name, units):
                out += process_path(dest, original_name=sub_name,
                                    file_hash=sub_hash, reprocess=reprocess)
            return out
        doc = excel.excel_to_pdfdoc(path)
        inv = Invoice(file_name=name, file_hash=file_hash, file_path=str(path),
                      uploaded_at=_now(), parse_method="excel",
                      raw_pdf_text=doc.full_text, page_sizes=doc.page_sizes)
        template_rules.parse_pdfdoc(inv, doc, source="excel")
        _apply_learned_locators(inv)                        # 审核期学到的标签线索补齐空/弱字段（软先验）
        locate.resolve_field_bboxes(inv, doc.words_geom)   # 字段→合成坐标，与自渲染 PNG 对齐可高亮
        locate.resolve_line_item_bboxes(inv, doc.words_geom)   # 明细→合成坐标，可高亮
        _store_word_geom(inv, doc)                          # 存词几何 → 框选取字走快路径（用单元格文本，免实时 OCR）
        return [_finalize(inv, detect_reupload=detect)]

    # --- Word(.docx) 多发票 ---
    # 与 PDF/Excel 同思路：python-docx 按发票锚点把 docx 物理拆成一文件一发票的 docx，
    # 逐张走下面的单张 Word 路径（fitz 渲染 + 提取 + bbox），切不清楚则不拆、走单张。
    if word.is_word(path):
        # 图片形式发票（几乎无可提取文本、内嵌发票图）→ 把嵌入原图另存，逐张走图片(OCR)路径
        if word.is_image_form(path):
            return _process_embedded_images(path, name, word.extract_images(path), reprocess)
        # CJK/复杂 docx：fitz 抽不到文本层（中文 docx 常见）但 python-docx 确有文本
        # → 用 LibreOffice 转成带正确文本层的 PDF，再走成熟 PDF 路径（含多发票拆分/字段/高亮）。
        if office.available() and _docx_needs_libreoffice(path):
            pdf_bytes = office.to_pdf(path)
            sub_hash = storage.sha256_of_bytes(f"{file_hash}:office-pdf".encode())
            dest = config.UPLOAD_DIR / f"{sub_hash[:12]}_{Path(name).stem}.pdf"
            dest.write_bytes(pdf_bytes)
            return process_path(dest, original_name=name, file_hash=sub_hash,
                                reprocess=reprocess, split=split, doc_type=doc_type)
        units = _cap_split(word.invoice_units(path), "docx")
        if units and split != "single":
            out: List[Invoice] = []
            for dest, sub_name, sub_hash in word.split_docx(path, name, units):
                out += process_path(dest, original_name=sub_name,
                                    file_hash=sub_hash, reprocess=reprocess)
            return out

    # --- 文本型 PDF / Word(.docx) ---
    # PyMuPDF(fitz) 原生可打开 .docx，故 Word 发票走与文本型 PDF 相同的路径
    # （含字段定位 bbox、原件渲染、明细解析；表格 docx 由 generic 的竖排表格回退处理）。
    if not is_image:
        _guard_pdf_pages(path)               # 页数上限（防千页 PDF 渲染/OCR 放大版 DoS）
        is_text, _total_chars, _pages = pdf_type.classify_pdf(path)
        doc = pdf_text.extract_pdf(path) if is_text else None
        # 文本层疑似乱码（CID 字体无 ToUnicode，抽出私用区字符）→ 改走 OCR，避免静默解析出错误数据
        text_suspect = bool(doc) and pdf_type.text_layer_suspect(doc.full_text) and ocr_mod.ocr_available()
        if text_suspect:
            is_text = False
        if is_text:
            n_inv = template_rules.count_total_markers(doc.full_text)
            # 『单张/多张』软先验（按版面指纹命中已启用规则）：仅 nudge，**仍需文档实际有边界**——
            #   prior='single' → 抑制切分（该公司多页发票其实是一张）；
            #   prior='multi'  → 即使 TOTAL DUE 只数到 1，也尝试按发票锚点分段（找不到锚点仍单张）。
            prior = None
            if split == "auto":
                try:
                    from extraction import learn
                    prior = db.multi_invoice_prior(learn.fingerprint(doc.full_text))
                except Exception:
                    prior = None
            eff_single = (split == "single") or (prior == "single")
            # 发票拆分引擎（多发票文件 → 先无损物理拆成"一文件一发票"）：
            # 三层——①数 TOTAL DUE 估发票数 ②内容锚点(INVOICE 标题/Invoice 号)定起始页范围
            # ③每段含恰好一个 TOTAL DUE 的完整性校验。通过才物理拆（lossless：只切页、不改格式），
            # 每张再走下面成熟的单张路径（bbox / 原件按本张渲染 / 交叉复核）。
            ranges = _invoice_page_ranges(path, n_inv) if len(doc.page_sizes) >= 2 else None
            ranges = _cap_split(ranges, "PDF") if ranges else ranges
            if ranges and not eff_single:
                out: List[Invoice] = []
                for dest, sub_name, sub_hash in _split_pdf_by_ranges(path, name, ranges):
                    out += process_path(dest, original_name=sub_name,
                                        file_hash=sub_hash, reprocess=reprocess)
                return out
            # 内容分段兜底：无法按页物理拆（如 Word/docx 被 fitz 折叠到一页、或同页多张）时，
            # 高置信地按发票锚点（INVOICE 标题 / 发票号）把文本切成多段、逐张解析。各段不claim
            # 渲染原件（page_sizes 空 → 审核页显示本段归档文本），避免"多段共用同一张图"的误导。
            if (n_inv >= 2 or prior == "multi") and not eff_single:
                segs = _cap_split(template_rules.split_invoice_segments(doc), "文本段")
                if len(segs) >= 2:
                    out2: List[Invoice] = []
                    stem, ext = Path(name).stem, Path(name).suffix
                    for i, seg in enumerate(segs, start=1):
                        sh = storage.sha256_of_bytes(f"{file_hash}:seg{i}/{len(segs)}".encode())
                        sinv = Invoice(file_name=f"{stem}_发票{i}of{len(segs)}{ext}", file_hash=sh,
                                       file_path=str(path), uploaded_at=_now(),
                                       parse_method="pdf_text", raw_pdf_text=seg.full_text)
                        template_rules.parse_pdfdoc(sinv, seg, source="pdf_text")
                        out2.append(_finalize(sinv, detect_reupload=detect))
                    return out2
            # 单张：常规路径（保留 pdfplumber 交叉复核）
            inv = Invoice(file_name=name, file_hash=file_hash, file_path=str(path),
                          uploaded_at=_now(), parse_method="pdf_text",
                          raw_pdf_text=doc.full_text, cross_engine_text=doc.plumber_text,
                          page_sizes=doc.page_sizes)
            template_rules.parse_pdfdoc(inv, doc, source="pdf_text")
            _apply_learned_locators(inv)                        # 审核期学到的标签线索补齐空/弱字段（软先验）
            locate.resolve_field_bboxes(inv, doc.words_geom)   # 字段→原件坐标，供审核界面双向联动
            locate.resolve_line_item_bboxes(inv, doc.words_geom)   # 明细→原件坐标，供高亮
            return [_finalize(inv, ocr_pdf_mismatch=_cross_validate_plumber(inv, doc), detect_reupload=detect)]
        # 扫描型 PDF → OCR（多发票切分对 OCR 不可靠，保持单张 + MULTI_INVOICE 提示）
        inv = Invoice(file_name=name, file_hash=file_hash, file_path=str(path),
                      uploaded_at=_now())
        _process_pdf_ocr(inv, path)
        if text_suspect:
            inv.add_issue("TEXT_LAYER_SUSPECT",
                          "原 PDF 文本层疑似乱码（字体无 ToUnicode 映射），已改用 OCR 识别，请仔细核对",
                          None, "warning")
        dual = _maybe_recheck_ocr(inv, path, is_pdf=True)
        return [_finalize(inv, dual_ocr_mismatch=dual, detect_reupload=detect)]

    # --- 图片 → OCR（单张）---
    inv = Invoice(file_name=name, file_hash=file_hash, file_path=str(path),
                  uploaded_at=_now())
    _process_image(inv, path)
    dual = _maybe_recheck_ocr(inv, path, is_pdf=False)
    return [_finalize(inv, dual_ocr_mismatch=dual, detect_reupload=detect)]


# 发票起始页的内容锚点（续页没有）：独立成行的 "INVOICE" 标题，或发票号标签
_PAGE_TITLE_RE = re.compile(r"^\s*(tax\s+)?invoice\s*$", re.IGNORECASE | re.MULTILINE)
_PAGE_NO_RE = re.compile(r"invoice\s*#|invoice\s*(no\.?|number)\b|\bbill\s*no\.?\b", re.IGNORECASE)


def _invoice_page_ranges(path: Path, n_inv: int):
    """发票边界检测 + 完整性校验 → 各发票的页范围 [(起页, 止页), ...]，否则 None。

    三层（对应"版面分析 / 边界检测 / 完整性验证"）：
    1) 用 TOTAL DUE 数 n_inv 估发票张数（调用方传入）；
    2) 内容锚点定起始页：优先 INVOICE 标题，回退发票号标签；起始页数须 == n_inv 且首张在第 0 页；
    3) **完整性校验**：每段（起始页到下一起始页前，含续页）须恰好含 1 个 TOTAL DUE，否则判边界不可信。
    仅在校验全部通过时才返回页范围（据此无损物理拆分）；任何不确定都返回 None（绝不误拆）。
    """
    if n_inv < 2:
        return None
    import fitz
    src = fitz.open(path)
    try:
        page_text = [src[k].get_text("text") for k in range(src.page_count)]
    finally:
        src.close()
    n_pages = len(page_text)
    if n_pages < 2:
        return None

    def _ranges_for(starts):
        if len(starts) != n_inv or not starts or starts[0] != 0:
            return None
        bounds = starts + [n_pages]
        ranges = [(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]
        # 完整性校验：每段恰含一个 TOTAL DUE（多/少都说明边界切错）
        for a, b in ranges:
            seg_text = "\n".join(page_text[p] for p in range(a, b + 1))
            if template_rules.count_total_markers(seg_text) != 1:
                return None
        return ranges

    title_starts = [k for k, t in enumerate(page_text) if _PAGE_TITLE_RE.search(t)]
    no_starts = [k for k, t in enumerate(page_text) if _PAGE_NO_RE.search(t)]
    return _ranges_for(title_starts) or _ranges_for(no_starts)


def _split_pdf_by_ranges(path: Path, base_name: str, ranges: List[tuple]) -> List[tuple]:
    """把 PDF 按页范围物理拆成多个 PDF 文件（一文件一发票），落盘到 uploads。

    返回 [(落盘路径, 文件名, 文件哈希), ...]。文件哈希由**原文件哈希 + 页范围确定性派生**
    （而非拆分后的 PDF 字节——后者含 fitz 随机 ID/时间戳，每次不同会致重复记录），
    使同一文件重处理/重传走 UPSERT 去重。后续按单张路径处理即自带 bbox / 原件按本张渲染。
    """
    import fitz
    stem = Path(base_name).stem
    suffix = ".pdf"   # 输出恒为 PDF 字节（含 .docx 多发票拆分），故部件统一 .pdf 命名，避免错标
    n = len(ranges)
    src_hash = storage.sha256_of_file(path)
    src = fitz.open(path)
    out: List[tuple] = []
    try:
        for idx, (a, b) in enumerate(ranges, start=1):
            one = fitz.open()
            one.insert_pdf(src, from_page=a, to_page=b)
            data = one.tobytes()
            one.close()
            sub_hash = storage.sha256_of_bytes(f"{src_hash}:p{a}-{b}".encode())  # 确定性
            sub_name = f"{stem}_发票{idx}of{n}{suffix}"
            dest = config.UPLOAD_DIR / f"{sub_hash[:12]}_{sub_name}"
            with open(dest, "wb") as f:
                f.write(data)
            out.append((dest, sub_name, sub_hash))
    finally:
        src.close()
    return out


def _cut_pos(page: int, y0: float, page_sizes) -> float:
    """把 (页, 页内 y) 归一成全局位置 = 页号 + 该页内的纵向比例(0~1)，用于按人工画线归段。"""
    h = page_sizes[page][1] if (0 <= page < len(page_sizes) and page_sizes[page][1]) else 1.0
    return page + max(0.0, min(1.0, (y0 or 0.0) / h))


def resplit_by_cuts(path: Path, name: str, file_hash: str, cuts: list) -> List[Invoice]:
    """按人工画线边界把一个文件切成多张发票（自动找不到边界时的兜底）。

    cuts：新发票**起始**边界列表 [{"page":int,"pos":0~1}, ...]（不含隐含的开头 0.0）。
    文本型 PDF：按词元几何(页,y)归段、重建每段文本逐张解析；扫描/无文本层：回退按页物理拆。
    """
    path = Path(path)
    bnds = sorted({(int(c["page"]), max(0.0, min(1.0, float(c["pos"]))))
                   for c in (cuts or [])})
    # 图片件（png/jpg…）：按画线的 Y 位置**裁剪图片**成多张，各自走图片(OCR)路径。
    # （此前对图片也调 extract_pdf→兜底按页拆 PDF，会 RuntimeError: not a PDF。）
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"):
        return _cut_image(path, name, file_hash, bnds)
    doc = pdf_text.extract_pdf(path)
    if doc.words_geom and doc.page_sizes:
        return _cut_text_pdf(path, name, file_hash, doc, bnds)
    # 兜底：无文本层（扫描件）→ 按"画线所在页"作为新发票起始页，物理拆页
    starts = sorted({0} | {p for (p, _pos) in bnds})
    try:
        import fitz
        with fitz.open(path) as d:
            n_pages = d.page_count
    except Exception:
        n_pages = len(doc.page_sizes) or 1
    b = starts + [n_pages]
    ranges = [(b[i], b[i + 1] - 1) for i in range(len(b) - 1) if b[i] <= b[i + 1] - 1]
    out: List[Invoice] = []
    for dest, sub_name, sub_hash in _split_pdf_by_ranges(path, name, ranges):
        out += process_path(dest, original_name=sub_name, file_hash=sub_hash,
                            reprocess=True, split="single")
    return out


def _cut_image(path: Path, name: str, file_hash: str, bnds) -> List[Invoice]:
    """图片件人工画线切分：按 Y 位置横向裁剪成多张图，各自另存后走图片(OCR)路径。"""
    from PIL import Image
    img = Image.open(path)
    W, H = img.size
    poss = sorted({0.0} | {pos for (_p, pos) in bnds}) + [1.0]
    stem = Path(name).stem
    ext = path.suffix.lower()
    segs = [(int(poss[i] * H), int(poss[i + 1] * H)) for i in range(len(poss) - 1)]
    segs = [(a, b) for (a, b) in segs if b - a >= 10]           # 跳过过薄的条
    out: List[Invoice] = []
    n = len(segs)
    for i, (y0, y1) in enumerate(segs, start=1):
        strip = img.crop((0, y0, W, y1))
        sub_hash = storage.sha256_of_bytes(f"{file_hash}:imgcut{i}/{n}".encode())
        sub_name = f"{stem}_发票{i}of{n}{ext}" if n > 1 else f"{stem}{ext}"
        dest = config.UPLOAD_DIR / f"{sub_hash[:12]}_{sub_name}"
        strip.save(dest)
        out += process_path(dest, original_name=sub_name, file_hash=sub_hash, reprocess=True)
    return out


def _cut_text_pdf(path: Path, name: str, file_hash: str, doc, bnds) -> List[Invoice]:
    """文本型 PDF：把词元按 (页,y) 全局位置分到各段，逐段重建 PdfDoc 解析成独立发票。"""
    starts = sorted({0.0} | {p + pos for (p, pos) in bnds})
    buckets = [[] for _ in starts]
    for w in doc.words_geom:
        page, x0, y0, x1, y1, txt = w
        gp = _cut_pos(page, y0, doc.page_sizes)
        idx = 0
        for i, s in enumerate(starts):
            if gp >= s - 1e-9:
                idx = i
            else:
                break
        # y 按页偏移，避免跨页相近 y 的词被并成同一行
        buckets[idx].append((x0, page * 100000.0 + y0, x1, page * 100000.0 + y1, txt))
    kept = [b for b in buckets if b]
    total = len(kept)
    stem, ext = Path(name).stem, Path(name).suffix
    out: List[Invoice] = []
    for i, sw in enumerate(kept, start=1):
        sdoc = pdf_text.pdfdoc_from_word_tuples(sw)
        sdoc.full_text = "\n".join(ln.text() for ln in sdoc.lines)
        sh = storage.sha256_of_bytes(f"{file_hash}:cut{i}/{total}".encode())
        sinv = Invoice(file_name=f"{stem}_发票{i}of{total}{ext}", file_hash=sh,
                       file_path=str(path), uploaded_at=_now(),
                       parse_method="pdf_text", raw_pdf_text=sdoc.full_text)
        template_rules.parse_pdfdoc(sinv, sdoc, source="pdf_text")
        _apply_learned_locators(sinv)
        out.append(_finalize(sinv, detect_reupload=False))
    return out


def _cap_split(seq, label: str):
    """拆分数量上限：单文件拆出的发票数超过 `MAX_INVOICES_PER_FILE` 时只取前 N 并记日志
    （绝不静默全量），防"小文件放大成海量记录"的资源放大 DoS。同 MAX_EMBEDDED_IMAGES 思路。"""
    if seq and len(seq) > config.MAX_INVOICES_PER_FILE:
        print(f"[pipeline] {label} 拆出 {len(seq)} 张超上限 {config.MAX_INVOICES_PER_FILE}，"
              f"只处理前 {config.MAX_INVOICES_PER_FILE} 张（其余请拆分后单独上传）")
        return seq[:config.MAX_INVOICES_PER_FILE]
    return seq


def _guard_pdf_pages(path: Path) -> None:
    """页数上限守卫：超过 MAX_PDF_PAGES 直接抛错（→ process_upload 兜底 failed 记录），
    避免千页 PDF 的渲染/OCR/多发票扫描拖垮共享机（放大版 DoS）。"""
    try:
        import fitz
        with fitz.open(path) as d:
            pc = d.page_count
    except Exception:
        return                                # 打不开交给后续流程报错
    if pc > config.MAX_PDF_PAGES:
        raise ValueError(f"页数过多（{pc} 页），上限 {config.MAX_PDF_PAGES} 页，请拆分后上传")


def _process_embedded_images(src_path: Path, name: str, images: List[tuple],
                             reprocess: bool) -> List[Invoice]:
    """把从 Word/Excel 提取出的内嵌发票图各自另存为图片文件，逐张走图片(OCR)路径。

    一张图=一张发票（与 PDF/Word/Excel 多发票物理拆分同思路）；哈希由 原文件哈希+序号
    确定性派生（重处理/重传 UPSERT 去重）。无可用图片则回退空列表（调用方继续常规路径）。
    """
    if not images:
        return []
    if len(images) > config.MAX_EMBEDDED_IMAGES:   # 内嵌图数上限：防几百张图→几百次 OCR/记录（放大版 DoS）
        print(f"[pipeline] 内嵌图 {len(images)} 张超上限 {config.MAX_EMBEDDED_IMAGES}，"
              f"只处理前 {config.MAX_EMBEDDED_IMAGES} 张（其余请拆分后单独上传）")
        images = images[:config.MAX_EMBEDDED_IMAGES]
    src_hash = storage.sha256_of_file(src_path)
    stem = Path(name).stem
    n = len(images)
    out: List[Invoice] = []
    for idx, (blob, ext) in enumerate(images, start=1):
        sub_hash = storage.sha256_of_bytes(f"{src_hash}:img{idx}".encode())
        sub_name = f"{stem}_发票{idx}of{n}{ext}" if n > 1 else f"{stem}_图{ext}"
        dest = config.UPLOAD_DIR / f"{sub_hash[:12]}_{sub_name}"
        dest.write_bytes(blob)
        out += process_path(dest, original_name=sub_name, file_hash=sub_hash, reprocess=reprocess)
    return out


def _stamp_source(invs: List[Invoice], src_hash: str, src_name: str, src_path: str) -> List[Invoice]:
    """给一个源文件切出的所有发票打"合集关联"标记并重存。

    segment_total = 实际切出的张数；>1 即为多发票合集，审核页按 source_file_hash 归为一组。
    """
    n = len(invs)
    for i, inv in enumerate(invs, start=1):
        inv.source_file_hash = src_hash
        inv.source_file_name = src_name
        inv.source_file_path = src_path
        inv.segment_index = i
        inv.segment_total = n
        db.save_invoice(inv)   # _finalize 已存过一次；补上合集标记后 UPSERT 覆盖（廉价、仅上传时）
    return invs


def process_upload(data: bytes, original_name: str, doc_type: str = "invoice") -> List[Invoice]:
    dest, file_hash = storage.save_upload(data, original_name)
    try:
        out = process_path(dest, original_name=original_name, file_hash=file_hash, doc_type=doc_type)
        return _stamp_source(out, file_hash, original_name, str(dest))
    except Exception as e:
        # 提取失败也**入库为一条记录**（原文件已落盘）：进审核队列、右侧字段全空，
        # 供人工对照左侧原件手工录入。绝不静默丢弃（计划 §3.7 完整性控制）。
        inv = Invoice(file_name=original_name, file_hash=file_hash, file_path=str(dest),
                      uploaded_at=_now(), doc_type=doc_type, parse_status="failed", review_status="Pending",
                      source_file_hash=file_hash, source_file_name=original_name,
                      source_file_path=str(dest), segment_index=1, segment_total=1)
        inv.add_issue("PARSE_FAILED", f"自动提取失败，请人工对照原件录入：{type(e).__name__}: {e}",
                      None, "critical")
        db.save_invoice(inv)
        return [inv]


def process_local(src: Path, reprocess: bool = False) -> List[Invoice]:
    dest, file_hash = storage.import_local_file(src)
    out = process_path(dest, original_name=Path(src).name, file_hash=file_hash, reprocess=reprocess)
    return _stamp_source(out, file_hash, Path(src).name, str(dest))


def export_excel(invoices: List[Invoice], filename: Optional[str] = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = filename or f"invoices_{stamp}.xlsx"
    out = config.EXPORT_DIR / fname
    return excel_writer.build_workbook(invoices, out)


# ---- OCR 路径 ------------------------------------------------------------
def _process_statement(path: Path, name: str, file_hash: str, is_image: bool,
                       detect_reupload: bool = True) -> Invoice:
    """银行流水处理：取文本+几何(文本型PDF/Excel/扫描OCR) → parse_statement → 收尾。"""
    from .parse import statement as stmt
    from .parse import statement_structured as sstruct
    from .extract import excel, pdf_text
    inv = Invoice(file_name=name, file_hash=file_hash, file_path=str(path),
                  uploaded_at=_now(), doc_type="statement")
    # 结构化格式（CSV/TSV/JSON/NDJSON/MT940/OFX/QFX/CAMT053）——确定性直解，最可靠
    if sstruct.is_structured(path):
        res = sstruct.parse_structured(path)
        if res is not None:
            hdr, txns = res
            from core.models import FieldValue
            for k, v in hdr.items():
                if k.startswith("_"):                 # sentinel（如 _multi_account）不是字段，跳过
                    continue
                if v is not None:
                    inv.set(k, FieldValue(raw=str(v), value=v, confidence=0.98, source="structured"))
            if hdr.get("_multi_account"):             # 单文件含多个账户 → 警告（余额/期末可能跨账户不连续）
                inv.add_issue("STMT_MULTI_ACCOUNT",
                              f"检测到 {hdr['_multi_account']} 个账户混在一个文件里，"
                              f"建议按账户拆分后分别上传；当前期末余额/余额连续性可能不适用，请人工核对",
                              None, "warning")
            if hdr.get("_pending_skipped"):           # 跳过未入账/挂起（预授权）行 → 提示（不计入收支）
                inv.add_issue("STMT_PENDING_SKIPPED",
                              f"已跳过 {hdr['_pending_skipped']} 笔未入账/挂起（预授权）交易——"
                              f"它们非已结算、不计入收支（避免与结算行重复）；如需请人工核对原件",
                              None, "info")
            inv.transactions = txns
            inv.parse_method = "structured"
        return _finalize_statement(inv, detect_reupload=detect_reupload)
    # 旧版办公格式（.xls 等）→ LibreOffice 转 PDF 再按版面解析
    from .extract import office
    if office.is_convertible(path.suffix.lower()) and office.available():
        try:
            pdf_bytes = office.to_pdf(path)
            dest = config.UPLOAD_DIR / f"{storage.sha256_of_bytes((file_hash+':stmt-pdf').encode())[:12]}_{path.stem}.pdf"
            dest.write_bytes(pdf_bytes)
            path = dest
        except Exception:
            pass
    doc = None
    if is_image:
        from PIL import Image
        inv.parse_method = "ocr"; inv.ocr_used = True
        inv.ocr_engine = ocr_mod.ocr_engine_name() or "OCR"
        r = ocr_mod.run_ocr_best(Image.open(path))
        if r is not None:
            inv.raw_ocr_text = r.text; inv.ocr_quality = r.overall
            try:
                doc = r.to_pdfdoc()
            except Exception:
                doc = None
        src = "ocr"
    elif excel.is_excel(path):
        inv.parse_method = "excel"
        doc = excel.excel_to_pdfdoc(path)
        inv.raw_pdf_text = doc.full_text; inv.page_sizes = doc.page_sizes
        src = "excel"
    else:
        _guard_pdf_pages(path)
        is_text, _tc, _pg = pdf_type.classify_pdf(path)
        if is_text and not pdf_type.text_layer_suspect(pdf_text.extract_pdf(path).full_text):
            doc = pdf_text.extract_pdf(path)
            inv.raw_pdf_text = doc.full_text; inv.page_sizes = doc.page_sizes
            src = "pdf_text"
        else:                                   # 扫描型流水 → OCR
            inv.parse_method = "ocr"; inv.ocr_used = True
            inv.ocr_engine = ocr_mod.ocr_engine_name() or "OCR"
            r = ocr_mod.ocr_pdf(path)
            if r is not None:
                inv.raw_ocr_text = r.text; inv.ocr_quality = r.overall
                try:
                    doc = r.to_pdfdoc()
                except Exception:
                    doc = None
            src = "ocr"
    if doc is not None:
        try:
            stmt.parse_statement(inv, doc, source=src)
        except Exception:
            pass
    # 版面解析没抓到逐笔时，回退到「竖排/转置表」文本还原（每字段各占一行的导出）
    if not inv.transactions:
        text = inv.raw_pdf_text or inv.raw_ocr_text or ""
        if text:
            try:
                hdr, txns = sstruct.parse_pdf_transposed(text)
                if txns:
                    from core.models import FieldValue
                    for k, v in hdr.items():
                        if v is not None and inv.f(k) is None:
                            inv.set(k, FieldValue(raw=str(v), value=v, confidence=0.9, source="transposed"))
                    inv.transactions = txns
            except Exception:
                pass
    return _finalize_statement(inv, detect_reupload=detect_reupload)


def _finalize_statement(inv: Invoice, detect_reupload: bool = True) -> Invoice:
    """流水收尾：查重 + 基本完整性 + 置状态 + 入库（不做发票的分类/必填闸门）。"""
    from .validate import confidence
    inv.doc_type = "statement"
    try:
        confidence.assess_statement(inv)   # 流水专用评估，不套发票必填字段
    except Exception:
        pass
    dup = db.find_duplicate(inv.file_hash, None, same_file=detect_reupload)
    if dup:
        inv.add_issue("DUPLICATE", f"疑似重复上传：{dup}", None, "error")
    if not inv.transactions and not inv.f("bank_name").value:
        inv.parse_status = "incomplete"
        inv.add_issue("STMT_EMPTY", "未识别到交易明细或账户信息，请对照原件人工补录", None, "warning")
    inv.needs_manual_review = True
    inv.critical_review = inv.parse_status in ("failed", "incomplete")
    inv.review_status = "Pending Review"
    inv.approve_status = inv.approve_status or "Pending"
    inv.processed_at = _now()
    db.save_invoice(inv)
    return inv


def _process_pdf_ocr(inv: Invoice, path: Path) -> None:
    inv.parse_method = "ocr"
    inv.ocr_used = True
    inv.ocr_engine = ocr_mod.ocr_engine_name() or "OCR"
    result = ocr_mod.ocr_pdf(path)
    if result is None:
        inv.parse_status = "failed"
        inv.add_issue("OCR_UNAVAILABLE", "扫描型 PDF 需 OCR，但 PaddleOCR 不可用", None, "critical")
        inv.ocr_quality = 0.0
        return
    inv.raw_ocr_text = result.text
    inv.ocr_quality = result.overall
    # 用 OCR 坐标重建结构，复用文本路径的分栏解析；失败则退化为线性解析
    try:
        doc = result.to_pdfdoc()
        template_rules.parse_pdfdoc(inv, doc, source="ocr")
        _apply_learned_locators(inv)            # 已学标签线索补齐空/弱字段（OCR 件同享，此前漏调）
        _locate_from_ocr_doc(inv, doc)          # 字段/明细→原件坐标，图片/扫描件也能双向高亮
        _fill_all_page_sizes(inv, path)         # 多页扫描件：补全每页尺寸，审核左栏可翻看全部页（字段框仍在首页）
    except Exception:
        template_rules.parse_plain_text(inv, result.text, source="ocr")


def _fill_all_page_sizes(inv: Invoice, path: Path) -> None:
    """扫描型 PDF：把原 PDF 全部页尺寸(pt)填进 page_sizes，使审核左栏能翻看每一页。
    首页尺寸沿用 OCR 重建的(与字段 bbox 同尺度、对齐不变)；其余页用 fitz 真实尺寸。"""
    try:
        import fitz
        with fitz.open(path) as d:
            if d.page_count <= 1:
                return
            sizes = [[pg.rect.width, pg.rect.height] for pg in d]
    except Exception:
        return
    if inv.page_sizes:                 # 首页保留 OCR 尺度，避免首页字段框错位
        sizes[0] = inv.page_sizes[0]
    inv.page_sizes = sizes


def _store_word_geom(inv: Invoice, doc) -> None:
    """把整页词几何按页归一化(0~1)存入 inv.ocr_words，供框选取字直接按坐标取词（免实时 OCR）。
    尺度无关，与前端框选归一化坐标一致；OCR 件用 OCR 词，Excel 件用合成单元格词（更准）。"""
    geom = getattr(doc, "words_geom", None)
    sizes = getattr(doc, "page_sizes", None)
    if not geom or not sizes:
        return
    words = []
    for w in geom:
        pno, x0, y0, x1, y1, txt = w
        if not (txt or "").strip() or pno >= len(sizes):
            continue
        pw, ph = sizes[pno]
        if not pw or not ph:
            continue
        words.append([pno, round(x0 / pw, 5), round(y0 / ph, 5),
                      round(x1 / pw, 5), round(y1 / ph, 5), txt])
    inv.ocr_words = words


def _locate_from_ocr_doc(inv: Invoice, doc) -> None:
    """OCR 件补齐 page_sizes 与字段/明细 bbox（与文本型路径一致），使审核界面可字段↔原件双向定位；
    并存整页词几何供框选取字走快路径。"""
    sizes = getattr(doc, "page_sizes", None)
    if sizes:
        inv.page_sizes = sizes
    geom = getattr(doc, "words_geom", None)
    if geom:
        locate.resolve_field_bboxes(inv, geom)
        locate.resolve_line_item_bboxes(inv, geom)
        _store_word_geom(inv, doc)


def _process_image(inv: Invoice, path: Path) -> None:
    from PIL import Image
    inv.parse_method = "ocr"
    inv.ocr_used = True
    inv.ocr_engine = ocr_mod.ocr_engine_name() or "OCR"
    result = ocr_mod.run_ocr_best(Image.open(path))
    if result is None:
        inv.parse_status = "failed"
        inv.add_issue("OCR_UNAVAILABLE", "图片需 OCR，但 PaddleOCR 不可用", None, "critical")
        inv.ocr_quality = 0.0
        return
    inv.raw_ocr_text = result.text
    inv.ocr_quality = result.overall
    try:
        doc = result.to_pdfdoc()
        template_rules.parse_pdfdoc(inv, doc, source="ocr")
        _apply_learned_locators(inv)            # 已学标签线索补齐空/弱字段（OCR 件同享，此前漏调）
        _locate_from_ocr_doc(inv, doc)          # 字段/明细→原件坐标，图片件也能双向高亮
    except Exception:
        template_rules.parse_plain_text(inv, result.text, source="ocr")


def _maybe_recheck_ocr(inv: Invoice, path: Path, is_pdf: bool) -> bool:
    """关键字段强制二次识别（计划第六节 14）：再跑一次 OCR 比对 TOTAL DUE。

    返回 双 OCR 是否不一致。
    """
    if not inv.ocr_used or inv.ocr_quality <= 0:
        return False
    # 仅当**金额可疑**或**质量确实低(<Warning 0.90)**时才二次识别——常见的 0.9x 但正常的件不再白跑
    # 第三遍整页 OCR（PDF 还按 400dpi 重渲染），显著加快重新提取。
    if inv.ocr_quality >= config.OCR_QUALITY_WARNING and not inv.f("total_due").suspicious:
        return False
    try:
        from PIL import Image
        if is_pdf:
            img = ocr_mod.render_pdf_page(path, 0, dpi=400)   # 只渲首页（金额复核用），不渲全部页
        else:
            img = Image.open(path)
        if img is None:
            return False
        result2 = ocr_mod.run_ocr(img)
        inv.recheck_count += 1
        if result2 is None:
            return False
        first = inv.f("total_due").value
        m = _TOTAL_RE.search(result2.text)
        if m and first is not None:
            from .parse.amount import parse_amount
            second, _, _ = parse_amount(m.group(1))
            if second is not None and second != first:
                inv.add_issue("DUAL_OCR_MISMATCH",
                              f"两次 OCR 的 TOTAL DUE 不一致: {first} vs {second}",
                              "total_due", "critical")
                return True
    except Exception as e:  # 二次识别失败不阻断主流程
        inv.add_issue("RECHECK_ERROR", f"二次识别异常: {e}", None, "warning")
    return False


# ---- 交叉复核（PyMuPDF vs pdfplumber）-----------------------------------
def _cross_validate_plumber(inv: Invoice, doc: pdf_text.PdfDoc) -> bool:
    """用 pdfplumber 复核关键金额与发票号。返回是否存在不一致。"""
    plumber = doc.plumber_text or ""
    if not plumber:
        return False
    mismatch = False

    # TOTAL DUE
    total = inv.f("total_due").value
    m = _TOTAL_RE.search(plumber)
    if m and total is not None:
        from .parse.amount import parse_amount
        p_total, _, _ = parse_amount(m.group(1))
        if p_total is not None and p_total != total:
            inv.add_issue("DUAL_TEXT_MISMATCH",
                          f"PyMuPDF 与 pdfplumber 的 TOTAL DUE 不一致: {total} vs {p_total}",
                          "total_due", "error")
            mismatch = True

    # Invoice No
    ino = inv.f("invoice_no").value
    m = _INVNO_RE.search(plumber)
    if m and ino and m.group(1) != ino:
        inv.add_issue("DUAL_TEXT_MISMATCH",
                      f"两引擎发票号不一致: {ino} vs {m.group(1)}", "invoice_no", "error")
        mismatch = True

    return mismatch
