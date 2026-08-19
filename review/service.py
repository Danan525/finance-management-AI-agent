"""第五模块：人工审核领域逻辑。

在 extraction（提取模块）结果之上提供审核动作：列待审队列、取详情、
人工改字段（留痕）、确认 / 拒绝 / 待定（状态机 + 审计轨迹）。

原则（对应 `计划/人工审核模块计划_V1.md`）：
- 强制人工、不自动入账；人决策、不用 LLM。
- approved 前关键字段 + 内部勾稽**硬校验**（§3.3）。
- 所有修改 / 动作**只追加留痕、不可删**（审计不可篡改）。
- `approved` 是后续总账记账的输入信号（总账模块就绪后对接）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional


def _to_amount(v):
    """把人工输入/框选到的金额文本宽松解析成 Decimal（**容忍货币符号** US$/HK$/€/¥ 及千分位）；
    取不到数字返回 None。人工填金额不该因为带个符号就报错。"""
    if v in (None, ""):
        return None
    from extraction.parse import amount as amt
    val, _susp, _note = amt.parse_amount(str(v))     # 先走标准解析（处理 $/USD/括号负数等）
    if val is not None:
        return val
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v))    # 兜底：从文本里取数字部分（忽略 US$/HK$/€ 等前缀）
    if m:
        try:
            return Decimal(m.group(0).replace(",", ""))
        except InvalidOperation:
            return None
    return None


def _amount_or_raise(v, field: str) -> Decimal:
    val = _to_amount(v)
    if val is None:
        raise ValueError(f"{field} 不是合法数字：{v}")
    return val

from core import config, db
from core.models import Invoice, FieldValue

# 审核状态（计划 §3.2：待审 / 已确认 / 已拒绝 / 待定）
PENDING = "Pending"
APPROVED = "Approved"
REJECTED = "Rejected"
HOLD = "Hold"
ACTIONS = {APPROVED, REJECTED, HOLD}

# approved 前必须非空的关键字段（硬校验，计划 §3.3）
REQUIRED_FIELDS = ("invoice_no", "invoice_date", "total_due")
# 金额类字段（人工修改时按 Decimal 解析、容忍货币符号）
AMOUNT_FIELDS = ("subtotal", "sales_tax", "total_due", "payment_due")
# 日期类字段（人工修改时归一化到 ISO、无法解析则标待复核）
DATE_FIELDS = ("invoice_date", "payment_due_date", "service_start", "service_end",
               "fund_valuation_date")
# 币种类字段（人工修改时符号→代码 / 统一大写）
CCY_FIELDS = ("currency_settlement", "invoice_ccy_raw")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(inv: Invoice) -> str:
    return inv.approve_status or PENDING


# ---- 队列与详情 -------------------------------------------------------

def review_queue(status: Optional[str] = None,
                 limit: Optional[int] = None, offset: int = 0,
                 doc_type: Optional[str] = None, fix_first: bool = False) -> list[dict]:
    """待审队列；status 可筛选（None=全部），doc_type 选发票/流水，支持分页（limit/offset）。

    直接读 DB 的紧凑摘要（不重建完整对象、不加载 OCR/PDF 大文本），排序由 SQL 完成
    （失败置顶 + 上传时间倒序）。每条附 `needs_fix`（是否未通过提取校验，与对账闸门同判据）。
    `fix_first=True` 时把"需纠错"的记录排到最前（读全部 summary 再稳定排序、Python 分页；
    summary 紧凑，本地规模可接受），供从对账页「提取纠错」横幅跳转后自动落到有问题的记录。"""
    from reconcile.service import summary_needs_fix
    # 已对账映射：发票（cinv，整张已对账）+ 流水逐笔（ctxn）——用于进度显示 + 前端预判"删除受保护"
    cinv, ctxn = db.confirmed_member_refs()
    recmap = {}
    for (h, i) in ctxn:
        recmap.setdefault(h, set()).add(i)
    # fix_first：需读取全部再排序分页；否则沿用 SQL 分页
    load_limit = None if fix_first else limit
    load_offset = 0 if fix_first else offset
    out = []
    for s in db.load_summaries(limit=load_limit, offset=load_offset, status=status, doc_type=doc_type):
        rc = len(recmap.get(s["file_hash"], set()))
        tc = s.get("txn_count") or 0
        if (s.get("doc_type") == "statement") and tc > 0 and rc >= tc:
            continue                              # 该流水全部交易已对账 → 自动移出待审队列
        out.append({
            "needs_fix": summary_needs_fix(s),    # 未通过提取校验（暂不参与对账匹配）→ 队列可标记/置顶
            "file_hash": s["file_hash"],
            "file_name": s["file_name"],
            "doc_type": s.get("doc_type") or "invoice",
            "invoice_no": s["invoice_no"],
            "total_due": s["total_due"],
            # 流水列表展示项
            "bank_name": s.get("bank_name"),
            "bank_account_no": s.get("bank_account_no"),
            "statement_period_start": s.get("statement_period_start"),
            "statement_period_end": s.get("statement_period_end"),
            "txn_count": s.get("txn_count"),
            "reconciled_count": rc,               # 已对账笔数 → 队列显示「已对账 X/N」
            # 是否已对账入账（删除受保护）：发票=整张在已确认匹配里；流水=有任一交易已对账
            "reconciled": (s["file_hash"] in cinv) if (s.get("doc_type") != "statement") else (rc > 0),
            "closing_balance": s.get("closing_balance"),
            "approve_status": s.get("approve_status") or PENDING,
            "parse_status": s["parse_status"],
            "parse_failed": s["parse_status"] == "failed",
            "risk_score": s["risk_score"],
            "needs_manual_review": s["needs_manual_review"],
            "critical_review": s["critical_review"],
            "uploaded_at": s["uploaded_at"],        # 同名文件靠上传时间 + 短哈希区分
            "is_duplicate": s["is_duplicate"],
            # 多发票合集：>1 时前端按 source_file_hash 折叠成一组
            "source_file_hash": s.get("source_file_hash") or "",
            "source_file_name": s.get("source_file_name") or "",
            "segment_index": s.get("segment_index") or 1,
            "segment_total": s.get("segment_total") or 1,
        })
    if fix_first:
        # 稳定排序：needs_fix 在前，组内保持 SQL 原顺序（失败置顶 + 上传倒序）；再做 Python 分页
        out.sort(key=lambda x: 0 if x["needs_fix"] else 1)
        if limit is not None:
            out = out[offset:offset + limit]
    return out


def collection_detail(source_file_hash: str) -> Optional[dict]:
    """多发票合集详情：源文件名 + 原件页数 + 组内各单张发票（供折叠展开）。

    兜底：无源链接（旧记录 source_file_hash 为空）时，把**该记录自身**当作单条合集，
    使画线切割等对旧记录也可用（用它自己的原件文件当源）。
    """
    sibs = db.siblings_by_source(source_file_hash)
    inv_self = None
    if not sibs:
        inv_self = db.get_invoice(source_file_hash)   # 传进来的其实是记录自身 hash（无源链接）
        if inv_self is None:
            return None
        sibs = [{"file_hash": inv_self.file_hash, "file_name": inv_self.file_name,
                 "invoice_no": inv_self.f("invoice_no").value,
                 "total_due": _s(inv_self.f("total_due").value),
                 "approve_status": _status(inv_self), "parse_status": inv_self.parse_status,
                 "risk_score": inv_self.risk_score, "critical_review": inv_self.critical_review,
                 "segment_index": inv_self.segment_index or 1,
                 "source_file_name": inv_self.source_file_name or inv_self.file_name}]
    members = []
    for s in sibs:
        members.append({
            "file_hash": s["file_hash"],
            "file_name": s["file_name"],
            "invoice_no": s.get("invoice_no"),
            "total_due": s.get("total_due"),
            "approve_status": s.get("approve_status") or PENDING,
            "parse_status": s.get("parse_status"),
            "parse_failed": s.get("parse_status") == "failed",
            "risk_score": s.get("risk_score"),
            "critical_review": s.get("critical_review"),
            "segment_index": s.get("segment_index") or 1,
            "is_duplicate": s.get("is_duplicate"),
        })
    # 原件页数：取 source_file_path（无则回退记录自身 file_path，兼容旧记录），用 fitz 数页
    page_count = 0
    inv0 = inv_self or db.get_invoice(sibs[0]["file_hash"])
    src_path = (inv0.source_file_path or inv0.file_path) if inv0 else None
    if src_path:
        try:
            import fitz
            with fitz.open(src_path) as doc:
                page_count = doc.page_count
        except Exception:
            page_count = 0
    return {
        "source_file_hash": source_file_hash,
        "source_file_name": sibs[0].get("source_file_name") or (inv0.file_name if inv0 else ""),
        "count": len(members),
        "page_count": page_count,
        "members": members,
    }


def queue_count(status: Optional[str] = None, doc_type: Optional[str] = None) -> int:
    """队列总数（供分页；不受 limit 影响）。"""
    return db.count_invoices(status, doc_type=doc_type)


def queue_summary(doc_type: Optional[str] = None) -> dict:
    """各状态计数（进度可视，计划 §3.7）。SQL 聚合，不重建对象。"""
    summary = {PENDING: 0, APPROVED: 0, REJECTED: 0, HOLD: 0}
    summary.update(db.status_counts(doc_type=doc_type))
    return summary


def recent_statement_transactions(limit: int = 10) -> list:
    """最近成功识别的流水交易，供上传页「识别进度」卡片。

    按**流水上传时间倒序**跨最新几张流水取；每张流水内按交易顺序**倒序**（末笔在前，更"最近"）。
    只扫最新若干张即可凑够 limit（每张通常多笔），不重建全库。每笔带其所属流水 file_hash，
    供前端 `/review?hash=` 深链进详情核对。"""
    out: list = []
    if limit <= 0:
        return out
    for s in db.load_summaries(limit=max(limit, 20), doc_type="statement"):
        if s.get("parse_status") == "failed" or not s.get("txn_count"):
            continue
        inv = db.get_invoice(s.get("file_hash"))
        if inv is None or not inv.transactions:
            continue
        ccy_hdr = inv.f("currency_settlement").value if inv.f("currency_settlement") else None
        bank = (inv.f("bank_name").value if inv.f("bank_name") else None) \
            or (inv.f("bank_account_no").value if inv.f("bank_account_no") else None)
        n = len(inv.transactions)
        for j, t in enumerate(reversed(inv.transactions)):
            out.append({
                "file_hash": inv.file_hash,
                "file_name": inv.file_name,
                "txn_index": n - 1 - j,          # 该笔在本流水交易表中的原始下标（供深链定位到具体行）
                "date": t.date or t.date_raw,
                "description": t.description,
                "income": str(t.income) if t.income is not None else None,
                "expense": str(t.expense) if t.expense is not None else None,
                "balance": str(t.balance) if t.balance is not None else None,
                "currency": t.currency or ccy_hdr,
                "source": bank or inv.file_name,
                "uploaded_at": s.get("uploaded_at"),
            })
            if len(out) >= limit:
                return out
    return out


_CONTENT_STOP = {"the", "and", "for", "with", "from", "this", "that", "per", "inc",
                 "ltd", "llc", "fee", "fees", "service", "services", "invoice"}


def _sig_tokens(text: str) -> set:
    import re
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _CONTENT_STOP}


def classification_suggestions(inv: Invoice) -> list:
    """基于"已启用的内容规则"，对相似内容给**分类参考建议**（不自动套用，仅供人工参考采纳）。"""
    from extraction.classify.engine import _dominant_desc
    toks = _sig_tokens(_dominant_desc(inv.line_items))
    if not toks:
        return []
    out, seen = [], set()
    for r in db.active_content_rules():
        rtoks = _sig_tokens(r.get("value"))
        if not rtoks:
            continue
        overlap = len(toks & rtoks) / len(rtoks)
        key = (r.get("category"), r.get("account"))
        if overlap >= 0.5 and key not in seen:
            seen.add(key)
            out.append({"category": r.get("category"), "account": r.get("account"),
                        "score": round(overlap, 2), "from": (r.get("value") or "")[:48]})
    return sorted(out, key=lambda x: -x["score"])[:3]


def classification_options() -> list:
    """分类下拉候选：规则内置 (类别,科目) 种子 + 已学规则里的对，去重。供审核界面 datalist。"""
    from extraction.classify import engine
    seen, out = set(), []
    for cat, acct in list(engine.suggestion_pairs()) + db.learned_class_pairs():
        key = (cat or "", acct or "")
        if key == ("", "") or key in seen:
            continue
        seen.add(key)
        out.append({"category": cat, "account": acct})
    return out


def review_detail(file_hash: str) -> Optional[dict]:
    """单张详情：字段 + 置信度 + 原文 + 校验问题 + 人工修改轨迹 + 分类参考建议。"""
    inv = db.get_invoice(file_hash)
    if inv is None:
        return None
    # 按规范字段顺序展示（如 customer_address 紧跟 customer_name）；未列入的排其后。
    # 提取失败的记录：展示**全部规范字段（空）**，供人工对照原件逐项录入。
    from core.models import CANONICAL_FIELDS, STATEMENT_FIELDS
    from reconcile.service import match_block_reasons as _match_block_reasons
    is_stmt = (inv.doc_type == "statement")
    base_fields = STATEMENT_FIELDS if is_stmt else CANONICAL_FIELDS
    order = {k: i for i, k in enumerate(base_fields)}
    failed = inv.parse_status == "failed"
    okeys = list(base_fields) if (failed or is_stmt) else sorted(inv.fields.keys(), key=lambda k: order.get(k, 999))
    fields = {k: {"value": _s(inv.f(k).value), "raw": inv.f(k).raw,
                  "confidence": inv.f(k).confidence, "source": inv.f(k).source,
                  "suspicious": inv.f(k).suspicious, "bbox": inv.f(k).bbox,
                  "note": inv.f(k).note}
              for k in okeys}
    # 逐笔交易；流水有交易时，用「规范交易表」的行级 bbox + 画布尺寸，供审核页双向高亮
    kinds = db.confirmed_txn_kinds() if is_stmt else {}   # 该笔的处理类型：对账 / 无需发票 / None
    txns = [{"date": _s(t.date), "description": t.description,
             "income": _s(t.income), "expense": _s(t.expense),
             "balance": _s(t.balance), "note": t.note, "bbox": t.bbox,
             "settled": kinds.get((inv.file_hash, i)),           # 'reconciled'(对账) | 'no_invoice'(无需发票) | None
             "reconciled": (inv.file_hash, i) in kinds}          # 是否已处理(两类都算)→ 前端锁定
            for i, t in enumerate(inv.transactions)]
    page_sizes = inv.page_sizes
    if is_stmt and inv.transactions:
        from extraction.extract import textrender
        L = textrender.statement_layout(inv)
        cx = L["col_x"]   # [pad, 日期|, 摘要|, 收入|, 支出|, 余额|]
        for i, tx in enumerate(txns):
            row = L["boxes"][i]; y0, y1 = row[2], row[4]
            tx["bbox"] = row
            # 分栏框：日期列(0)、摘要列(1)、金额列（支出=3/收入=2），供对账页按类型分别高亮、与发票同色对应
            tx["date_bbox"] = [0, cx[0], y0, cx[1], y1]
            tx["desc_bbox"] = [0, cx[1], y0, cx[2], y1]   # 附言/摘要列（兜底）
            amt_col = 3 if inv.transactions[i].expense is not None else 2
            tx["amount_bbox"] = [0, cx[amt_col], y0, cx[amt_col + 1], y1]
            # 精确框住附言里的发票号子串：渲染文本 + 起始 x + 等宽字符宽（前端按字符位置算 x 范围）
            tx["desc_render"] = L["desc_texts"][i]
            tx["ref_x0"] = cx[1] + L["text_pad"]
            tx["char_w"] = L["char_w"]
        page_sizes = [[L["width"], L["height"]]]
    return {
        "file_hash": inv.file_hash,
        "file_name": inv.file_name,
        "doc_type": inv.doc_type or "invoice",
        # 银行流水逐笔交易（doc_type='statement' 时非空）
        "transactions": txns,
        "parse_method": inv.parse_method,
        "parse_status": inv.parse_status,
        "approve_status": _status(inv),
        "rev": inv.rev,                 # 乐观锁版本：前端提交修改时回传，后端据此判并发冲突
        # 多发票合集：>1 时详情页显示"是否多张"纠正控件
        "source_file_hash": inv.source_file_hash or inv.file_hash,
        "source_file_name": inv.source_file_name or inv.file_name,
        "segment_index": inv.segment_index or 1,
        "segment_total": inv.segment_total or 1,
        "fields": fields,
        "page_sizes": page_sizes,    # [[w,h],...] pt，供前端按比例叠加字段框（流水=规范交易表画布尺寸）
        "raw_pdf_text": inv.raw_pdf_text,
        "raw_ocr_text": inv.raw_ocr_text,
        "issues": [{"code": i.code, "message": i.message, "severity": i.severity}
                   for i in inv.issues],
        # 必须先修正、否则不能参与对账匹配的具体原因（与对账闸门 extraction_passed 同判据）
        "match_blocks": _match_block_reasons(inv),
        "classification": {"category": inv.classification.category,
                           "account": inv.classification.account,
                           "confidence": inv.classification.confidence,
                           "needs_review": inv.classification.needs_review},
        "classification_suggestions": classification_suggestions(inv),
        "category_options": classification_options(),
        "line_split_suggestions": line_split_suggestions(inv),
        "line_items": [{"description": li.description,
                        "quantity": _s(li.quantity),
                        "unit_price": _s(li.unit_price),
                        "amount": _s(li.amount),
                        "note": li.note,
                        "bbox": li.bbox,
                        "sub_items": li.sub_items or [],
                        "reconcile": line_reconcile(li)}
                       for li in inv.line_items],
        "missing_required": _missing_required(inv),
        "is_duplicate": any(i.code == "DUPLICATE" for i in inv.issues),
        "changes": db.list_changes(file_hash),
    }


def _refresh_duplicate_flag(file_hash: str) -> None:
    """重新评估某记录的"重复"标记：若已无其它同号/同文件的重复记录，则**清除过期 DUPLICATE 标记**。
    （删除/去重后调用，避免"重复的都删了，幸存那张还被当重复、点击还跳比对页"。）"""
    inv = db.get_invoice(file_hash)
    if inv is None or not any(i.code == "DUPLICATE" for i in inv.issues):
        return
    others = [c for c in db.find_duplicate_candidates(file_hash, inv.f("invoice_no").value, same_file=True)
              if c["file_hash"] != file_hash]
    if not others:                       # 已无重复对象 → 清标记、重存（摘要 is_duplicate 随之变 False）
        inv.issues = [i for i in inv.issues if i.code != "DUPLICATE"]
        db.resave_invoice(inv)


def _refresh_dup_flags_for(invoice_no: Optional[str]) -> None:
    """刷新与某发票号相关的所有记录的重复标记（删掉一条后，同号其余记录可能不再重复）。"""
    if not invoice_no:
        return
    for c in db.find_duplicate_candidates("", invoice_no, same_file=False):
        _refresh_duplicate_flag(c["file_hash"])


def _refresh_collection(source_file_hash: str) -> None:
    """删掉合集成员后，重算幸存成员的"共几张/第几张"（否则徽标显示过期张数、剩 1 张还被当合集）。"""
    if not source_file_hash:
        return
    sibs = db.siblings_by_source(source_file_hash)     # 已按 segment_index 排序
    n = len(sibs)
    for idx, s in enumerate(sibs, start=1):
        inv = db.get_invoice(s["file_hash"])
        if inv is None:
            continue
        if inv.segment_total != n or inv.segment_index != idx:
            inv.segment_total = n
            inv.segment_index = idx
            db.save_invoice(inv)                       # UPSERT 更新摘要（含 segment_total）


def duplicate_candidates(file_hash: str) -> Optional[dict]:
    """取某发票的疑似重复候选（供对比确认界面）。返回 {file_name, candidates:[...]} 或 None。

    **排除记录自身**（同 file_hash 的那条就是它本人，不是重复）——否则"真重复都删了、只剩这一张"
    时仍会把自己列成一条候选、显示"还有重复"。若无真候选且记录仍带过期 DUPLICATE 标记 → 自愈清除。
    """
    inv = db.get_invoice(file_hash)
    if inv is None:
        return None
    cands = [c for c in db.find_duplicate_candidates(file_hash, inv.f("invoice_no").value, same_file=True)
             if c["file_hash"] != file_hash]      # 剔除自身
    if not cands:
        _refresh_duplicate_flag(file_hash)         # 已无真重复 → 顺手清掉过期"重复"标记（自愈）
    return {"file_hash": file_hash, "file_name": inv.file_name,
            "invoice_no": inv.f("invoice_no").value,
            "approve_status": _status(inv), "candidates": cands}


def drop_unapproved(group: list, by: str = "reviewer", reason: str = "") -> dict:
    """删除一组里**所有未入账(非 Approved)的重复，保留全部已入账的**。
    用于"和已入账的重复了 → 把没入账的重复清掉"。返回 {deleted:[...], kept:[...]}。"""
    deleted, kept = [], []
    for h in dict.fromkeys(group or []):
        inv = db.get_invoice(h)
        if inv is None:
            continue
        if _status(inv) == APPROVED:
            kept.append(h)
            continue
        db.log_change(h, "_deleted", inv.file_name,
                      "重复去重：删除未入账重复（保留已入账）", by, reason or "duplicate dedupe", _now())
        no = inv.f("invoice_no").value
        src = inv.source_file_hash
        db.delete_invoice(h)
        deleted.append(h)
        _refresh_dup_flags_for(no)         # 删完刷新同号其余记录（含保留的已入账）的重复标记
        _refresh_collection(src)           # 若属合集：重算其余成员张数/序号
    return {"deleted": deleted, "kept": kept}


def dedupe(keep: str, group: list, by: str = "reviewer", reason: str = "") -> dict:
    """一组重复里**只保留 keep、删除其余**（每条仍走 Approved 守卫：已入账的跳过、不误删）。

    返回 {kept, deleted:[...], skipped:[...]}；skipped 为因已 Approved 未删的。
    """
    keep = (keep or "").strip()
    drop = [h for h in (group or []) if h and h != keep]
    deleted, skipped = [], []
    for h in dict.fromkeys(drop):          # 去重、保序
        inv = db.get_invoice(h)
        if inv is None:
            continue
        if _status(inv) == APPROVED:       # 已确认入账 → 不直接删（走红冲/拒绝），跳过并汇报
            skipped.append(h)
            continue
        db.log_change(h, "_deleted", inv.file_name,
                      f"重复去重：只保留 {keep}", by, reason or "duplicate dedupe", _now())
        src = inv.source_file_hash
        db.delete_invoice(h)
        deleted.append(h)
        _refresh_collection(src)           # 若属合集：重算其余成员张数/序号
    _refresh_duplicate_flag(keep)          # 其余重复已删 → 清掉保留那张的过期"重复"标记
    return {"kept": keep, "deleted": deleted, "skipped": skipped}


def resolve_duplicate(file_hash: str, against: str, is_duplicate: bool,
                      by: str = "reviewer", reason: str = "") -> dict:
    """人工对一个疑似重复候选下结论（留痕）。确认重复 → 把本件拒绝(标记为重复)。"""
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    decision = "confirmed_duplicate" if is_duplicate else "not_duplicate"
    at = _now()
    db.log_change(file_hash, "_duplicate_check", against,
                  decision + (f": {reason}" if reason else ""), by, reason, at)
    if is_duplicate:
        inv.approve_status = REJECTED
        inv.review_status = "Rejected (duplicate)"
        with db.connect() as conn:                      # 原子：状态回写 + 审核轨迹同事务
            db.resave_invoice(inv, conn)
            db.record_review(file_hash, REJECTED, "Rejected (duplicate)", by, at, conn=conn)
    else:
        # 确认非重复：清除 DUPLICATE 标记（决策已入 change_log 留痕），队列不再显示"重复?"，
        # 保持为正常待审项。
        from core.models import ValidationIssue
        inv.issues = [i for i in inv.issues if i.code != "DUPLICATE"]
        inv.issues.append(ValidationIssue("DUPLICATE_DISMISSED",
                                          f"人工确认非重复（已对比 {against}）", None, "info"))
        db.resave_invoice(inv)
    return {"file_hash": file_hash, "against": against, "resolution": decision,
            "approve_status": inv.approve_status}


def delete_invoice(file_hash: str, by: str = "reviewer", reason: str = "") -> dict:
    """删除一条发票记录（审核前的提取记录清理：重复/测试/脏数据）。

    删除前在 change_log 留痕（审计可追溯），再硬删 invoices 行——队列里即彻底消失。
    已确认入账（Approved）的记录不允许直接删除（账目应红冲，不可悄悄抹除）。
    """
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    if _status(inv) == APPROVED:
        raise ValueError("该发票已确认入账，不能直接删除；如需作废请走红冲/拒绝流程")
    if _is_reconciled(file_hash):     # 流水确认对账后其自身不标 Approved，故单独挡：删除会留下悬空匹配
        raise ValueError("该记录已对账入账，不能直接删除；请先在对账页撤销相关匹配")
    no = inv.f("invoice_no").value
    src = inv.source_file_hash
    db.log_change(file_hash, "_deleted", inv.file_name,
                  f"deleted: {reason}" if reason else "deleted", by, reason, _now())
    db.delete_invoice(file_hash)
    _refresh_dup_flags_for(no)     # 删掉这条后，同号其余记录若已不再重复则清掉其"重复"标记
    _refresh_collection(src)       # 若属多发票合集：重算其余成员"共几张/第几张"
    return {"file_hash": file_hash, "deleted": True}


def _collection_of(file_hash: str):
    """给定组内任一记录 hash，解析出 (源哈希, 源文件名, 源路径, 组内所有记录 hash)。"""
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    src_hash = inv.source_file_hash or inv.file_hash
    src_name = inv.source_file_name or inv.file_name
    src_path = inv.source_file_path or inv.file_path
    sibs = db.siblings_by_source(src_hash)
    old_hashes = [s["file_hash"] for s in sibs] or [file_hash]
    return src_hash, src_name, src_path, old_hashes


def resplit(file_hash: str, mode: str = "single", by: str = "reviewer",
            reason: str = "", cuts=None) -> dict:
    """对一个多发票文件（或被误判的单张）**重新切分并替换整组记录**。

    mode：
      - "single"：强制当作单张重新提取（合并回一张）——"识别成多张但其实只有一张"；
      - "auto"  ：重新自动检测切分——"识别成单张但其实是多张"（找不到边界则不动、提示走人工画线）；
      - "manual"：按人工画线边界 cuts 切分（见 pipeline.resplit_by_cuts）。
    守卫：整组中若有已 Approved 的记录 → 拒绝（不可悄悄丢弃已入账的）。
    重新提取会重置该组的人工修改（等同"整组重新提取"语义）。
    """
    if mode not in ("single", "auto", "manual"):
        raise ValueError(f"未知切分模式：{mode}")
    src_hash, src_name, src_path, old_hashes = _collection_of(file_hash)
    from ledger import service as _ledger
    for h in old_hashes:                                   # 已入账不可重切
        o = db.get_invoice(h)
        if o is not None and _status(o) == APPROVED:
            raise ValueError("该文件已有发票确认入账，不能重新切分；如需更正请先撤销/红冲")
        if _ledger.posted_statement_indices(h):            # 含已流水入账的交易 → 重切会重排下标、致重复入账
            raise ValueError("该流水已有交易做了流水入账，不能重新切分；如需更正请先在「已过账分录」红冲相关流水入账")
    if not src_path or not Path(src_path).exists():
        raise ValueError("源文件已不可用，无法重新切分")

    from extraction import pipeline
    if mode == "manual":
        if not cuts:
            raise ValueError("人工切分需要提供切割边界")
        out = pipeline.resplit_by_cuts(Path(src_path), src_name, src_hash, cuts)
    else:
        sp = "single" if mode == "single" else "auto"
        out = pipeline.process_path(Path(src_path), original_name=src_name,
                                    file_hash=src_hash, reprocess=True, split=sp)
    if mode == "auto" and len(out) < 2:                    # 自动仍找不到多发票边界
        # 不动原记录（process_path 可能已 UPSERT 覆盖了源 hash 记录，但组结构未变），提示走人工画线
        return {"resplit": False, "reason": "auto_no_boundary",
                "message": "自动仍未找到多发票边界，请用「人工画线切割」手动指定。"}

    new_hashes = {i.file_hash for i in out}
    for h in old_hashes:                                   # 删旧（复用同 hash 的不删，已被新数据覆盖）
        if h in new_hashes:
            continue
        db.log_change(h, "_resplit", "记录", f"重新切分({mode})替换为新记录", by, reason, _now())
        db.delete_invoice(h)
    pipeline._stamp_source(out, src_hash, src_name, str(src_path))   # 打合集标记并重存
    pre = _pending_ids()
    _learn_multi(src_path, out, len(out), by)              # 学习"单张/多张"软先验（待启用）
    return {"resplit": True, "mode": mode, "count": len(out),
            "source_file_hash": src_hash,
            "records": [{"file_hash": i.file_hash,
                         "invoice_no": i.f("invoice_no").value} for i in out],
            "learned": _learned_since(pre)}


def _learn_multi(src_path: str, out: list, count: int, by: str) -> None:
    """学『该版面(指纹)倾向 单张/多张』软先验（人工重切分即信号；pending 待启用，不写死）。

    指纹取**源文件全文**（而非某个切片），使日后同版面整份上传能命中。best-effort。
    """
    try:
        from extraction import learn
        from extraction.extract import pdf_text
        # 与 pipeline 应用先验时**同一提取器**取全文，保证指纹一致（否则学到的指纹命不中）
        fp = learn.fingerprint(pdf_text.extract_pdf(Path(src_path)).full_text)
        issuer = ""
        for i in out:
            if i.f("issuer_name").value:
                issuer = i.f("issuer_name").value
                break
        db.learn_multi_invoice(fp, "single" if count < 2 else "multi", issuer or "", by)
    except Exception:
        pass


def set_classification(file_hash: str, category: str, account: str,
                       changed_by: str = "reviewer", reason: str = "") -> dict:
    """人工确认/修正分类（建议科目）：更新 → 写 change_log 留痕 → 重存。"""
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    old = f"{inv.classification.category} / {inv.classification.account}"
    inv.classification.category = category or None
    inv.classification.account = account or None
    inv.classification.confidence = 1.0          # 人工确认 → 满置信
    inv.classification.needs_review = False
    inv.classification.hit_rules = list(inv.classification.hit_rules) + ["manual_review"]
    db.resave_and_log(inv, "_classification", old, f"{category} / {account}",
                      changed_by, reason, _now())
    # 学习（规则即数据，均先 pending 待启用）：
    # ① 对手方 → 科目（同供应商下次带出）；② 内容 → 科目（相似内容给参考建议）
    pre = _pending_ids()
    issuer = inv.f("issuer_name").value
    if issuer:
        db.learn_classification(db.norm_key(issuer), category or None, account or None, changed_by)
    from extraction.classify.engine import _dominant_desc
    desc = _dominant_desc(inv.line_items)
    if desc:
        db.learn_content_class(desc, category or None, account or None, changed_by)
    return {"category": category, "account": account, "learned": _learned_since(pre)}


# 可按对手方学默认值的字段 = **对手方稳定属性**（每张发票不变的）；填时一律"核对原件后才填"（见
# pipeline._apply_learned_defaults）。**不学**发票号/日期/各金额/服务期/客户方联系人等**单据级/易变**字段。
LEARNABLE_DEFAULTS = ("currency_settlement", "invoice_ccy_raw", "tax_rate",
                      "issuer_address", "issuer_email", "issuer_phone",
                      "bank_name", "bank_account_name", "bank_account_no", "bank_swift")


# ---- 审核动作 ---------------------------------------------------------

def _region_to_bbox(inv: Invoice, region) -> Optional[list]:
    """把归一化框选区 {page,x0,y0,x1,y1}(0~1) 按页 pt 尺寸转成 bbox=[page,x0,y0,x1,y1](pt)。"""
    if not region or not inv.page_sizes:
        return None
    try:
        pg = int(region.get("page", 0))
        if not (0 <= pg < len(inv.page_sizes)):
            return None
        w, h = inv.page_sizes[pg]
        x0, x1 = sorted((float(region["x0"]) * w, float(region["x1"]) * w))
        y0, y1 = sorted((float(region["y0"]) * h, float(region["y1"]) * h))
        return [pg, x0, y0, x1, y1]
    except (KeyError, TypeError, ValueError):
        return None


def _pending_ids() -> set:
    """当前所有"待启用"规则的 id 集合（用于对比出本次动作新学到的规则）。"""
    return {r["id"] for r in db.list_learned() if (r.get("status") or "") != "active"}


def _learned_since(pre_ids: set) -> list:
    """本次动作**新学到的待启用规则**（供审核页当场弹窗启用；已启用/此前就有的不算）。

    返回 [{id, rule_type, match_key, target, value, category, account, can_global}]。
    """
    out = []
    for r in db.list_learned():
        if r["id"] in pre_ids or (r.get("status") or "") == "active":
            continue
        out.append({"id": r["id"], "rule_type": r["rule_type"], "match_key": r.get("match_key"),
                    "target": r.get("target"), "value": r.get("value"),
                    "category": r.get("category"), "account": r.get("account"),
                    "note": r.get("note"),
                    "can_global": r["rule_type"] == "field_locator"})
    return out


_RECONCILE_LOCK_MSG = "该记录已对账入账，不能修改/删除；如需变更请先在对账页撤销相关匹配"


def _is_reconciled(file_hash: str) -> bool:
    """该记录（发票 hash，或其某笔交易所属流水 hash）是否在**已确认**对账匹配里。
    是则应锁定：禁止改字段/增删交易/删除，避免破坏已确认对应（如重排交易下标）。"""
    cinv, ctxn = db.confirmed_member_refs()
    return file_hash in cinv or any(h == file_hash for (h, _i) in ctxn)


def _reconciled_txn_indices(stmt_hash: str) -> set:
    """某流水中**已对账**的交易下标集合（用于按笔锁定/标记，而非整条锁死）。"""
    _cinv, ctxn = db.confirmed_member_refs()
    return {i for (h, i) in ctxn if h == stmt_hash}


def change_field(file_hash: str, field: str, new_value: str,
                 changed_by: str = "reviewer", reason: str = "", region=None) -> dict:
    """人工修改一个字段：更新值 → 写 change_log（留原值）→ 重存快照。

    region 给定（框选填入）时，把该字段的 bbox 设为框选区域，使它和自动识别的字段一样
    能在原件上高亮对应；普通编辑（无 region）则**保留原 bbox**（值改了但位置没变）。
    """
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    # 发票：已对账则锁定不可改。流水：账户头字段（银行/账号/期间…）编辑不影响交易对应，放行；
    # 交易本身的锁在 save_transaction 里按笔处理。
    if inv.doc_type != "statement" and _is_reconciled(file_hash):
        raise ValueError(_RECONCILE_LOCK_MSG)
    old = inv.f(field).value
    parsed = new_value
    note = "人工修改"
    # 手工编辑也走**和提取一样的按字段类型归一化/校验**（不再把非金额一律当纯文本）：
    if field in AMOUNT_FIELDS and new_value not in (None, ""):
        parsed = _amount_or_raise(new_value, f"金额字段 {field}")     # 金额：容忍 US$/HK$/€ 货币符号
    elif field in DATE_FIELDS and new_value not in (None, ""):
        from extraction.parse import dates as _dt
        iso, _need = _dt.normalize_date(str(new_value))              # 日期：归一化到 ISO
        parsed = iso or str(new_value).strip()                       # 解析不了则保留原文并标待复核
        if not iso:
            note = "人工修改（日期格式待复核）"
    elif field in CCY_FIELDS and new_value not in (None, ""):
        from extraction.parse import generic as _g
        parsed = _g.currency_fallback(str(new_value)) or str(new_value).strip().upper()  # 币种：符号→代码/大写
    elif field == "tax_rate" and new_value not in (None, ""):
        s = str(new_value).strip()
        parsed = (s + "%") if re.fullmatch(r"\d+(?:\.\d+)?", s) else s   # 税率：裸数字补 %
    elif new_value is not None:
        parsed = str(new_value).strip() or None                      # 其余文本：去首尾空白
    bbox = _region_to_bbox(inv, region) or inv.f(field).bbox   # 框选→新框；否则保留原框
    inv.set(field, FieldValue(raw=str(new_value), value=parsed, confidence=1.0,
                              source="manual_review", note=note, bbox=bbox))
    _revalidate(inv)   # 改完据最新值重算校验/查重/风险/闸门（改发票号也会重新查重）
    db.resave_and_log(inv, field, old, parsed, changed_by, reason, _now())   # 原子：快照+留痕同事务
    # 学习：对手方稳定属性（币种/税率/开票方地址电话邮箱）→ 记为该供应商默认值
    pre = _pending_ids()
    issuer = inv.f("issuer_name").value
    if issuer and field in LEARNABLE_DEFAULTS and new_value not in (None, ""):
        db.learn_field_default(db.norm_key(issuer), field, str(new_value), changed_by)
    # 学习「字段定位线索」（软先验、非死模板）：取该值在原文里旁边的**标签关键词**，
    # 按 对手方 + 类型指纹 存 pending；日后同类发票用它**现场按标签找值**（找不到即忽略）。
    try:
        from extraction import learn
        text = inv.raw_pdf_text or inv.raw_ocr_text or ""
        lab = learn.derive_label(text, new_value, field)
        if lab:
            db.learn_field_locator(db.norm_key(issuer), field, lab, learn.fingerprint(text), changed_by)
    except Exception:
        pass
    return {"field": field, "old": _s(old), "new": _s(parsed), "learned": _learned_since(pre)}


def clear_field_locate(file_hash: str, field: str,
                       by: str = "reviewer", reason: str = "") -> dict:
    """清除某字段的原件定位框(bbox)：识别错位置时，改完值后把赖在错误处的高亮框去掉。
    仅动 bbox、不改字段值；留痕 + 重存（走乐观锁 rev）。"""
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    fv = inv.fields.get(field)
    if fv is None or fv.bbox is None:
        return {"field": field, "cleared": False}      # 本就没有定位框
    fv.bbox = None
    db.resave_and_log(inv, field, "(定位框)", "已清除定位高亮",
                      by, reason or "定位错误，清除高亮", _now())
    return {"field": field, "cleared": True}


def save_transaction(file_hash: str, index: int, field: str, value,
                     by: str = "reviewer") -> dict:
    """银行流水交易行的人工修改/新增/删除（留痕）。
    field: date/description/income/expense/balance 改某笔；'__add__' 追加空行；'__del__' 删第 index 行。"""
    from core.models import Transaction
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    # 按笔锁（而非整条锁死）：未对账/未入账的交易仍可编辑；**已对账或已流水入账**的笔不可改、
    # 且不可增删——增删会重排下标，而流水入账的幂等键基于下标，漂移后会导致同一笔现金重复入账
    # （自检发现，2026-08-11 修：入账下标须与对账下标一并锁定）。
    from ledger import service as _ledger
    locked_idx = _reconciled_txn_indices(file_hash) | _ledger.posted_statement_indices(file_hash)
    if field in ("__add__", "__del__"):
        if locked_idx:
            raise ValueError("该流水已有交易对账或流水入账，不能增删交易行（会打乱行号、破坏已确认对应或重复入账）；"
                             "请先在对账页撤销匹配 / 在「已过账分录」红冲相关流水入账")
    elif index in locked_idx:
        raise ValueError("该笔交易已对账或已流水入账，不能修改；请先撤销匹配 / 红冲流水入账")
    txns = list(inv.transactions)
    if field == "__add__":
        txns.append(Transaction())
    elif field == "__del__":
        if 0 <= index < len(txns):
            txns.pop(index)
    elif 0 <= index < len(txns):
        t = txns[index]
        v = ("" if value is None else str(value)).strip()
        if field == "date":
            iso, _n = _dt.normalize_date(v)
            t.date = iso or (v or None); t.date_raw = v or None
        elif field == "description":
            t.description = v or None
        elif field in ("income", "expense", "balance"):
            setattr(t, field, _to_amount(v) if v else None)
        else:
            raise ValueError(f"未知交易字段：{field}")
    else:
        raise ValueError("交易行号越界")
    inv.transactions = txns
    db.resave_and_log(inv, f"txn[{index}].{field}", None, _s(value), by, "流水交易人工修改", _now())
    return {"ok": True, "count": len(txns)}


_LINE_FIELDS = ("description", "quantity", "unit_price", "amount")


def change_line_item(file_hash: str, index: int, field: str, value,
                     by: str = "reviewer", reason: str = "") -> dict:
    """人工修改一条服务明细（描述/数量/单价/金额，留痕）。"""
    if field not in _LINE_FIELDS:
        raise ValueError(f"不可改的明细字段：{field}")
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    if _is_reconciled(file_hash):
        raise ValueError(_RECONCILE_LOCK_MSG)
    if not (0 <= index < len(inv.line_items)):
        raise ValueError(f"明细行号越界：{index}")
    li = inv.line_items[index]
    old = getattr(li, field, None)
    if field in ("quantity", "unit_price", "amount"):
        parsed = _amount_or_raise(value, f"明细 {field}") if value not in (None, "") else None
        if field == "amount":
            li.amount_raw = str(value) if value not in (None, "") else None
    else:
        parsed = (str(value).strip() or None) if value is not None else None
    setattr(li, field, parsed)
    li.line_confidence = 1.0
    li.note = (li.note or "") and li.note  # 保留原备注
    _revalidate(inv)   # 明细金额变了 → 重算明细合计校验(LINE_SUM_MISMATCH)/风险
    db.resave_and_log(inv, f"line_item[{index}].{field}", _s(old), _s(parsed), by, reason, _now())
    return {"index": index, "field": field, "old": _s(old), "new": _s(parsed),
            "reconcile": line_reconcile(inv.line_items[index])}   # 改金额后据此提醒勾稽是否对上


# 明细断句：候选分隔方式（名 → 正则）。推断时按此顺序，谁能复现拆分结果就学谁。
_SPLIT_DELIMS = (
    ("newline",   r"[\r\n]+"),
    ("numbered",  r"(?:^|\s)\d+[.)]\s+"),
    ("bullet",    r"\s*[•·▪‣*]\s+"),
    ("semicolon", r"\s*;\s*"),
    ("pipe",      r"\s*\|\s*"),
    ("dash",      r"\s+[-–—]\s+"),
)
_SPLIT_RX = dict(_SPLIT_DELIMS)


def _apply_split(prose: str, pattern: str) -> list:
    """按断句方式把一段文字拆成多段（去空白、去空段）。"""
    import re
    rx = _SPLIT_RX.get(pattern)
    if not rx or not prose:
        return []
    return [p.strip() for p in re.split(rx, prose) if p and p.strip()]


def infer_split_pattern(prose: str, pieces) -> Optional[str]:
    """从「原文 + 人工拆出的多段」反推用了哪种分隔方式；都对不上则 None（不学）。"""
    target = [str(p).strip() for p in (pieces or []) if str(p or "").strip()]
    if len(target) < 2 or not prose:
        return None
    for name, _ in _SPLIT_DELIMS:
        if _apply_split(prose, name) == target:
            return name
    return None


def line_split_suggestions(inv: Invoice) -> list:
    """基于该对手方「已启用」的断句规则，对仍是大段文字的明细给**拆分建议**。

    仅预览（含拆分后各段），不自动拆；人工采纳才调用 split_line_item。
    """
    issuer = inv.f("issuer_name").value
    rules = db.active_line_split_rules(db.norm_key(issuer)) if issuer else []
    if not rules:
        return []
    out = []
    for idx, li in enumerate(inv.line_items):
        desc = (li.description or "").strip()
        if len(desc) < 24:                      # 太短不像需要拆的大段
            continue
        for r in rules:
            pieces = _apply_split(desc, r["pattern"])
            if len(pieces) >= 2:
                out.append({"index": idx, "pattern": r["pattern"], "pieces": pieces})
                break                            # 一行给一个建议即可
    return out


def split_line_item(file_hash: str, index: int, pieces, by: str = "reviewer",
                    reason: str = "") -> dict:
    """把一条大段描述的明细按人工给的分段拆成多条（留痕），并**学习断句方式**。

    - 用 pieces 覆盖第 index 行 + 追加其余行；金额留给人工再填（原行金额保留在首段，避免丢总额）。
    - 从「原描述 + pieces」反推分隔方式 → learn_line_split（先 pending，人工启用后才作建议）。
    """
    from core.models import LineItem
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    if not (0 <= index < len(inv.line_items)):
        raise ValueError(f"明细行号越界：{index}")
    segs = [str(p).strip() for p in (pieces or []) if str(p or "").strip()]
    if len(segs) < 2:
        raise ValueError("拆分至少需要 2 段")
    orig = inv.line_items[index]
    orig_desc = orig.description or ""
    # 首段沿用原行（保留其金额/数量/单价，避免丢失），其余段为新行（金额待填）
    new_rows = [LineItem(description=segs[0], quantity=orig.quantity,
                         unit_price=orig.unit_price, amount=orig.amount,
                         amount_raw=orig.amount_raw, source_file=orig.source_file,
                         line_confidence=1.0, note="人工拆分")]
    new_rows += [LineItem(description=s, source_file=orig.source_file,
                          line_confidence=1.0, note="人工拆分") for s in segs[1:]]
    inv.line_items[index:index + 1] = new_rows
    _revalidate(inv)   # 明细行数/金额变化 → 重算校验/风险
    db.resave_and_log(inv, f"line_item[{index}].split",
                      orig_desc[:80], f"{len(segs)} segments", by, reason, _now())
    # 学习断句方式（仅当能复现，且有对手方可归属）
    learned = None
    pre = _pending_ids()
    pattern = infer_split_pattern(orig_desc, segs)
    issuer = inv.f("issuer_name").value
    if pattern and issuer:
        db.learn_line_split(db.norm_key(issuer), pattern, orig_desc, by)
        learned = pattern
    return {"index": index, "segments": len(segs), "count": len(inv.line_items),
            "learned_pattern": learned, "learned": _learned_since(pre)}


def add_line_item(file_hash: str, by: str = "reviewer", reason: str = "",
                  description=None, amount=None, region=None) -> dict:
    """新增一条服务明细（漏识别时手工补，留痕）。返回新行号。

    可选 description/amount 预填（如从原件框选内容加明细）；region 给定时把该框设为明细 bbox，
    使新明细也能在原件上高亮定位（与框选填字段同理）。"""
    from core.models import LineItem
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    amt_val = _to_amount(amount)                 # 容忍货币符号；解析不了就留空，描述仍保留
    desc = (str(description).strip() or None) if description not in (None, "") else None
    bbox = _region_to_bbox(inv, region) if region else None
    inv.line_items.append(LineItem(description=desc, amount=amt_val,
                                   amount_raw=str(amount) if amt_val is not None else None,
                                   source_file=inv.file_name, line_confidence=1.0,
                                   note="人工新增", bbox=bbox))
    idx = len(inv.line_items) - 1
    _revalidate(inv)   # 新增明细 → 重算校验/风险
    db.resave_and_log(inv, f"line_item[{idx}]", None,
                      f"added(manual): {desc or ''}", by, reason, _now())
    return {"index": idx, "count": len(inv.line_items)}


# ---- 明细勾稽子行（尾随类别明细，可人工改/加/删；改金额时提示勾稽是否对上）---------
_SUB_FIELDS = ("date", "description", "amount")


def _sub_sum(li):
    """子明细金额合计 + 是否全部可解析。"""
    total, ok = Decimal("0"), True
    for s in (li.sub_items or []):
        a = s.get("amount")
        if a in (None, ""):
            continue
        val = _to_amount(a)                      # 容忍货币符号
        if val is None:
            ok = False
        else:
            total += val
    return total, ok


def line_reconcile(li) -> dict:
    """该行勾稽状态：有子明细时返回 {has_detail, matched, sub_sum, line_amount, parse_ok}。"""
    if not (li.sub_items or []):
        return {"has_detail": False}
    from extraction.parse import amount as amt
    ssum, ok = _sub_sum(li)
    # L1:容差按金额小数位收紧(3 位小数币种→0.001),不再一律 0.01(隐含 2 位、对海湾币种偏松)
    _dp = max(2, amt.decimal_places(str(li.amount)) or 2) if li.amount is not None else 2
    _tol = min(Decimal("0.01"), Decimal(10) ** (-_dp))
    matched = bool(ok and li.amount is not None and abs(ssum - li.amount) <= _tol)
    return {"has_detail": True, "matched": matched, "parse_ok": ok,
            "sub_sum": str(ssum), "line_amount": _s(li.amount)}


def _get_line(file_hash: str, li_index: int):
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    if not (0 <= li_index < len(inv.line_items)):
        raise ValueError(f"明细行号越界：{li_index}")
    return inv, inv.line_items[li_index]


def change_sub_item(file_hash: str, li_index: int, sub_index: int, field: str, value,
                    by: str = "reviewer", reason: str = "") -> dict:
    """改一条勾稽子明细的 日期/描述/金额（留痕）；返回该行最新勾稽状态。"""
    if field not in _SUB_FIELDS:
        raise ValueError(f"不可改的子明细字段：{field}")
    if _is_reconciled(file_hash):
        raise ValueError(_RECONCILE_LOCK_MSG)
    inv, li = _get_line(file_hash, li_index)
    subs = li.sub_items or []
    if not (0 <= sub_index < len(subs)):
        raise ValueError(f"子明细行号越界：{sub_index}")
    if field == "amount" and value not in (None, ""):
        _amount_or_raise(value, "子明细金额")     # 容忍货币符号；确实无数字才报错
    old = subs[sub_index].get(field)
    if field == "date" and value not in (None, ""):
        from extraction.parse import dates as _dt
        iso, _need = _dt.normalize_date(str(value))   # 子明细日期同样归一化到 ISO（解析不了保留原文）
        new_val = iso or str(value).strip()
    else:
        new_val = (str(value).strip() or None) if value is not None else None
    subs[sub_index][field] = new_val
    li.sub_items = subs
    db.resave_and_log(inv, f"line_item[{li_index}].sub[{sub_index}].{field}",
                      _s(old), _s(subs[sub_index][field]), by, reason, _now())
    return {"index": li_index, "sub_index": sub_index, "field": field,
            "reconcile": line_reconcile(li)}


def add_sub_item(file_hash: str, li_index: int, by: str = "reviewer", reason: str = "") -> dict:
    """给某明细行加一条空白勾稽子明细（漏识别时手工补）。"""
    inv, li = _get_line(file_hash, li_index)
    li.sub_items = (li.sub_items or []) + [{"date": None, "description": None, "amount": None}]
    db.resave_and_log(inv, f"line_item[{li_index}].sub[{len(li.sub_items) - 1}]",
                      None, "added(manual)", by, reason, _now())
    return {"index": li_index, "sub_index": len(li.sub_items) - 1, "reconcile": line_reconcile(li)}


def delete_sub_item(file_hash: str, li_index: int, sub_index: int,
                    by: str = "reviewer", reason: str = "") -> dict:
    """删一条勾稽子明细（识别错的）。"""
    inv, li = _get_line(file_hash, li_index)
    subs = li.sub_items or []
    if not (0 <= sub_index < len(subs)):
        raise ValueError(f"子明细行号越界：{sub_index}")
    removed = subs.pop(sub_index)
    li.sub_items = subs
    db.resave_and_log(inv, f"line_item[{li_index}].sub[{sub_index}]",
                      f"{removed.get('description')} / {removed.get('amount')}", "deleted", by, reason, _now())
    return {"index": li_index, "reconcile": line_reconcile(li)}


def delete_line_item(file_hash: str, index: int, by: str = "reviewer", reason: str = "") -> dict:
    """删除一条多识别/错误的服务明细（留痕）。"""
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    if not (0 <= index < len(inv.line_items)):
        raise ValueError(f"明细行号越界：{index}")
    removed = inv.line_items.pop(index)
    _revalidate(inv)   # 删明细 → 重算校验/风险
    db.resave_and_log(inv, f"line_item[{index}]",
                      f"{removed.description} / {_s(removed.amount)}", "deleted", by, reason, _now())
    return {"index": index, "deleted": True, "remaining": len(inv.line_items)}


def reapply_learned(file_hash: str, by: str = "reviewer") -> dict:
    """对**未 Approve** 的记录**从原件重新提取**（吃到最新代码 + 已启用规则），
    但**保留人工修改/确认的部分**：
    - 人工改过的字段（source='manual_review'，含框选填入）→ 保留；
    - 人工确认过的分类（needs_review=False）→ 保留；
    - 若明细被人工编辑过（改/加/删/拆/子明细）→ 整块明细保留；
    - 其余**全部重新识别**。已 Approve 的直接跳过；原件不可用则不改。"""
    from pathlib import Path
    from core.models import CANONICAL_FIELDS
    from extraction import pipeline
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    if _status(inv) == APPROVED:
        return {"file_hash": file_hash, "applied": False, "reason": "已通过(Approved)，不改动"}
    # 已对账入账保护：该记录（发票或其某笔流水交易）若在已确认匹配里，重新提取会重排字段/交易下标、
    # 破坏已确认对应关系（对账引用 (stmt_hash, txn_index)）。拒绝，提示先撤销对账。
    cinv, ctxn = db.confirmed_member_refs()
    if file_hash in cinv or any(h == file_hash for (h, _i) in ctxn):
        return {"file_hash": file_hash, "applied": False,
                "reason": "该记录已对账入账，重新提取会破坏已确认的对应关系；如需修改请先在对账页撤销相关匹配"}
    p = Path(inv.file_path) if inv.file_path else None
    if not (p and p.exists()):
        return {"file_hash": file_hash, "applied": False, "reason": "原件不可用，无法重新提取"}

    # 记住人工痕迹（重新提取后覆盖回去）
    manual_fields = {k: fv for k, fv in inv.fields.items() if (fv.source or "") == "manual_review"}
    keep_cls = inv.classification if inv.classification.needs_review is False else None
    _changes = db.list_changes(file_hash)
    manual_lines = any((c.get("field") or "").startswith("line_item") for c in _changes)
    keep_lines = list(inv.line_items) if manual_lines else None
    # 流水：若逐笔交易被人工改过（save_transaction 留痕 txn[...]），重提后原样保留，避免覆盖人工修正
    manual_txns = any((c.get("field") or "").startswith("txn[") for c in _changes)
    keep_txns = list(inv.transactions) if manual_txns else None
    before = {k: _s(inv.f(k).value) for k in CANONICAL_FIELDS}

    try:
        fresh_list = pipeline.process_path(p, original_name=inv.file_name,
                                           file_hash=file_hash, reprocess=True,
                                           doc_type=inv.doc_type or "invoice")
    except Exception as e:
        return {"file_hash": file_hash, "applied": False, "reason": f"重新提取失败: {type(e).__name__}"}
    fresh = next((x for x in fresh_list if x.file_hash == file_hash), None) or db.get_invoice(file_hash)
    if fresh is None:
        return {"file_hash": file_hash, "applied": False, "reason": "重新提取无结果"}

    for k, fv in manual_fields.items():        # 人工改过的字段：原样保留
        fresh.set(k, fv)
    if keep_cls is not None:                   # 人工确认过的分类：保留
        fresh.classification = keep_cls
    if keep_lines is not None:                 # 人工编辑过明细：整块保留
        fresh.line_items = keep_lines
    if keep_txns is not None:                   # 人工编辑过流水交易：整块保留
        fresh.transactions = keep_txns
    fresh.uploaded_at = inv.uploaded_at        # 不改上传时间（队列顺序不乱）
    fresh.approve_status = inv.approve_status  # 保持原审核状态（Pending/Hold/Rejected）
    fresh.review_status = inv.review_status
    _revalidate(fresh)   # 覆盖回人工字段/明细后再重算：校验/风险须反映"最终值"而非重提原始值
    changed = [k for k in CANONICAL_FIELDS if before[k] != _s(fresh.f(k).value)]
    db.resave_and_log(fresh, "_reapply_rules", None,
                      "重新提取(保留人工修改/确认)" + (("；变更 " + ", ".join(changed)) if changed else "；无变更"),
                      by, "按最新规则重新提取", _now())
    return {"file_hash": file_hash, "applied": True, "changed": changed}


def reapply_learned_all(by: str = "reviewer", doc_type: Optional[str] = None) -> dict:
    """对**未 Approve** 记录批量补齐。doc_type 限定范围：
    None=全部（发票+流水）、'invoice'=只发票、'statement'=只流水。
    返回 {scanned, updated, records:[{file_hash, changed}]}。"""
    dt = doc_type if doc_type in ("invoice", "statement") else None
    out = {"scanned": 0, "updated": 0, "records": []}
    targets = [i.file_hash for i in db.load_all_invoices().values()
               if _status(i) != APPROVED and (dt is None or (i.doc_type or "invoice") == dt)]
    for fh in targets:
        out["scanned"] += 1
        try:
            r = reapply_learned(fh, by)
        except KeyError:
            continue
        if r.get("changed"):
            out["updated"] += 1
            out["records"].append({"file_hash": fh, "changed": r["changed"]})
    return out


def act(file_hash: str, action: str, by: str = "reviewer", reason: str = "",
        force: bool = False) -> dict:
    """审核动作：Approved / Rejected / Hold。

    - Approved 前做关键字段硬校验（缺失则拒绝通过）。
    - **疑似重复 + 已有入账副本**：不直接通过，返回 blocked=duplicate 让前端强制比对确认；
      force=True（人工核对后确认非重复、仍要入账）才放行，并清除重复标记留痕。
    - Rejected 必须填原因。
    - 状态写回 + file_audit 审计轨迹 + change_log 记动作。
    """
    if action not in ACTIONS:
        raise ValueError(f"未知审核动作：{action}")
    inv = db.get_invoice(file_hash)
    if inv is None:
        raise KeyError(f"未找到记录 {file_hash}")
    if action == APPROVED:
        missing = _missing_required(inv)
        if missing:
            raise ValueError(f"关键字段缺失，不能通过：{', '.join(missing)}")
        _revalidate(inv)   # 通过前据最新值重算校验/勾稽（可能已被人工修正，也可能新引入不一致）
        bad = [i for i in inv.issues if i.code in _RECONCILE_BLOCK]
        stale_date = [f for f in ("invoice_date", "payment_due_date")
                      if "待复核" in (inv.f(f).note or "")]
        if (bad or stale_date) and not reason:
            parts = [i.message for i in bad] + [f"{f} 日期格式待复核" for f in stale_date]
            raise ValueError("账目未对平/待复核，不能直接通过：" + "；".join(parts) +
                             "。请先修正，或在原因中说明后再通过。")
        # 疑似重复 且 已有一张"已入账"的重复 → 强制比对确认，未确认(force)不放行
        still_dup = any(i.code == "DUPLICATE" for i in inv.issues)
        if still_dup:
            approved_dups = [c for c in db.find_duplicate_candidates(file_hash, inv.f("invoice_no").value, same_file=True)
                             if c["file_hash"] != file_hash and (c.get("approve_status") == APPROVED)]
            if approved_dups and not force:
                return {"file_hash": file_hash, "blocked": "duplicate",
                        "approved_dups": [{"file_hash": c["file_hash"], "file_name": c["file_name"]}
                                          for c in approved_dups],
                        "message": "这张与已入账的发票疑似重复，请先比对确认后再决定是否入账。"}
        if force and still_dup:   # 人工核对后仍入账 → 清重复标记、留痕
            from core.models import ValidationIssue
            inv.issues = [i for i in inv.issues if i.code != "DUPLICATE"]
            inv.issues.append(ValidationIssue("DUPLICATE_OVERRIDDEN",
                                              "人工比对确认非重复后仍入账", None, "info"))
    if action == REJECTED and not reason:
        raise ValueError("拒绝必须填写原因")

    inv.approve_status = action
    inv.review_status = action
    at = _now()
    # 原子：状态回写 + 审核轨迹 +（可选）理由留痕同一事务，避免"已 Approved 却无审计"
    with db.connect() as conn:
        db.resave_invoice(inv, conn)
        db.record_review(file_hash, action, action, by, at, conn=conn)
        if reason:
            db.log_change(file_hash, "_review_action", None, f"{action}: {reason}",
                          by, reason, at, conn=conn)
    return {"file_hash": file_hash, "approve_status": action, "by": by, "at": at}


# ---- 辅助 -------------------------------------------------------------

# run_checks 产出的校验 issue code。重算时先剔除这批再重跑，
# 从而**保留** parse 阶段的问题（MULTI_INVOICE / PARSE_FAILED / DETAIL_* / OCR_* 等）。
_CHECK_ISSUE_CODES = {
    "AMOUNT_FORMAT", "AMOUNT_SUSPICIOUS", "DECIMAL_MISSING", "DECIMAL_NONSTANDARD",
    "TOTAL_MISMATCH", "TAX_RATE_CONFLICT", "PAYMENT_DUE_MISMATCH",
    "LINE_NO_AMOUNT", "LINE_SUM_MISMATCH", "CURRENCY_SPLIT", "CURRENCY_AMBIGUOUS",
    "DUPLICATE", "MISSING_INVOICE_NO", "MISSING_DATE", "MISSING_PARTY",
    "MISSING_TOTAL", "NO_PAYMENT_INFO", "MULTI_PAYMENT_METHOD",
}
# Approve 前若仍存在这些 error 级"账对不平"→ 挡住（除非人工填原因显式放行）。
# 仅取硬错误：小计+税≠总额、明细合计≠小计；PAYMENT_DUE 差异只是 warning（预付/尾款常见）不硬挡。
_RECONCILE_BLOCK = ("TOTAL_MISMATCH", "LINE_SUM_MISMATCH")


def _revalidate(inv: Invoice) -> None:
    """人工改动后**就地重算**校验/查重/风险/闸门（不重新提取原件）。

    只重跑 checks 那组 issue（先剔除旧的、再重跑），parse 阶段的问题原样保留；
    据最新值重算 风险分 / 重点审核标记 / 完整性状态，避免"改完仍显示旧结论"。
    重算失败不阻断保存（维持既有状态）。
    """
    from extraction.validate import checks, confidence, risk
    inv.issues = [i for i in inv.issues if i.code not in _CHECK_ISSUE_CODES]
    inv.critical_review = False        # 交给下面据最新值重新置位（可因修正而清除）
    # 流水：用流水专用评估，不套发票必填字段/发票查重/发票风险规则
    if inv.doc_type == "statement":
        try:
            confidence.assess_statement(inv)
            dup = db.find_duplicate(inv.file_hash, None, same_file=False)
            if dup:
                inv.add_issue("DUPLICATE", f"疑似重复上传：{dup}", None, "error")
        except Exception:
            return
        inv.needs_manual_review = True
        return
    try:
        confidence.assess(inv)
        dup = db.find_duplicate(inv.file_hash, inv.f("invoice_no").value, same_file=False)
        checks.run_checks(inv, duplicate_of=dup)
        risk.compute(inv)
    except Exception:
        return
    missing_req = [f for f in config.REQUIRED_FIELDS if inv.f(f).raw in (None, "")]
    inv.needs_manual_review = (
        inv.critical_review
        or bool(missing_req)
        or inv.risk_score > config.RISK_THRESHOLD
        or inv.has_multiple_payment_methods
        or any(i.severity in ("error", "critical") for i in inv.issues)
    )
    if missing_req and inv.parse_status != "failed":
        inv.parse_status = "incomplete"
    elif not missing_req and inv.parse_status == "incomplete":
        inv.parse_status = "parsed"


def _missing_required(inv: Invoice) -> list:
    # 银行流水没有发票必填字段（发票号/日期/总额）——改要求「至少一笔交易」，否则整套发票闸门会误挡通过
    if inv.doc_type == "statement":
        return [] if inv.transactions else ["交易明细"]
    return [f for f in REQUIRED_FIELDS if inv.f(f).value in (None, "")]


def _s(v):
    return None if v is None else str(v)
