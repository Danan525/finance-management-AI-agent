"""对账服务：提取校验闸门 → 构建候选池 → 跑匹配入库 → 列表/确认/拒绝。

流程（对齐用户要求）：
  自动提取+校验 → 通过者进匹配池 → 自动匹配 → 高可信唯一进「待批量确认」（一键全过）
  中等/一对多/多对一/差额 → 「人工确认对应关系」；多候选 → 「重点审核」；无匹配 → 「待定」。
  未通过提取校验的记录进「提取纠错队列」（沿用原按类型审核页），暂不参与匹配。
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from core import config, db
from core.models import Invoice
from . import matcher


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- 提取校验闸门 ----------------------------------------------------------
# 阻断进匹配池的**硬**问题（必须先纠错）：重复、总额/明细勾稽不平、金额/小数点严重存疑。
# 注意：KEY_FIELD_LOW 等"启发式抽取"软警告**不阻断**——完整抽出的发票可直接进匹配，
# 由对账界面「一次确认提取+对应关系」，符合"自动匹配为主、集中人工确认"的流程。
_HARD_ISSUE_CODES = {"DUPLICATE", "TOTAL_MISMATCH", "LINE_SUM_MISMATCH",
                     "AMOUNT_CONF_LOW", "DECIMAL_CONF_LOW", "OCR_QUALITY_LOW", "PARSE_FAILED"}

# 硬问题 → 大白话说明（供审核页"必须先修正才能匹配"提示，比 CODE 直观）
_HARD_ISSUE_LABELS = {
    "DUPLICATE": "疑似重复上传（同一张发票出现多次）",
    "TOTAL_MISMATCH": "总额与明细合计对不上（勾稽不平）",
    "LINE_SUM_MISMATCH": "明细行合计对不上（勾稽不平）",
    "AMOUNT_CONF_LOW": "金额识别置信过低（可能认错数字）",
    "DECIMAL_CONF_LOW": "小数点位置存疑（金额可能差百倍）",
    "OCR_QUALITY_LOW": "扫描/OCR 质量过低（辨认不清）",
    "PARSE_FAILED": "自动解析失败",
}
# 发票必填字段 → 大白话名（与前端 FILLABLE 一致）
_REQUIRED_LABELS = {"invoice_no": "发票号", "invoice_date": "发票日期", "total_due": "总金额"}


def match_block_reasons(inv: Invoice) -> List[dict]:
    """列出"必须先修正、否则**不能参与对账匹配**"的**具体**原因（与 `extraction_passed` 同一判据、
    单一真源）。返回 [{kind, field?, code?, label}]；**空列表 = 已可参与匹配**。
    kind: parse(提取失败) / missing(缺必填,附 field) / issue(硬问题,附 code) / empty(流水无交易) / critical(重点审核)。"""
    if inv.parse_status == "failed":                      # 彻底失败：其它判断无意义，单条提示手工录入
        return [{"kind": "parse", "label": "自动提取失败：请对照原件手工录入关键字段后再通过"}]
    if inv.doc_type == "statement":
        if not inv.transactions:
            return [{"kind": "empty", "label": "未识别到任何交易明细，无法参与对账"}]
        return []
    out = []
    for f in config.REQUIRED_FIELDS:                      # 发票必填缺失（逐条列出，便于点击定位）
        if inv.f(f).value in (None, ""):
            out.append({"kind": "missing", "field": f,
                        "label": "缺少必填字段：" + _REQUIRED_LABELS.get(f, f)})
    seen = set()
    for i in inv.issues:                                  # 硬问题（重复/勾稽不平/金额存疑/OCR 差…）
        if i.code in _HARD_ISSUE_CODES and i.code not in seen:
            seen.add(i.code)
            label = _HARD_ISSUE_LABELS.get(i.code, i.code)
            if i.message:
                label += "（" + i.message + "）"
            out.append({"kind": "issue", "code": i.code, "label": label})
    if inv.critical_review and not out:                   # 重点审核但无具体硬问题 → 兜底提示
        out.append({"kind": "critical", "label": "被系统标为重点审核，请核对确认后再通过"})
    if not out and inv.parse_status != "parsed":          # 其它"未完成"状态兜底（保证与闸门等价）
        out.append({"kind": "parse", "label": "提取尚未完成，请补齐关键字段后再通过"})
    return out


def extraction_passed(inv: Invoice) -> bool:
    """是否通过自动提取校验（可直接进匹配池）。未通过者进纠错队列，不参与匹配。
    判据 = **没有任何** `match_block_reasons` 阻断项（解析成功 + 无硬问题 + 非重点审核 + 完整性）。"""
    return not match_block_reasons(inv)


def summary_needs_fix(s: dict) -> bool:
    """基于紧凑 summary（不重建对象）判断该记录是否"未通过提取校验"——与 `extraction_passed()`
    **同一判据**，供审核队列排序/标记复用：解析失败或未完成、重点审核、命中硬问题、
    发票必填缺失、或流水无交易。summary 里已存 parse_status/critical_review/issues/txn_count 等字段。"""
    if (s.get("parse_status") or "") != "parsed":
        return True
    if s.get("critical_review"):
        return True
    codes = {(i or {}).get("code") for i in (s.get("issues") or [])}
    if codes & _HARD_ISSUE_CODES:
        return True
    if (s.get("doc_type") or "invoice") == "statement":
        return not (s.get("txn_count") or 0)
    return any(s.get(f) in (None, "") for f in config.REQUIRED_FIELDS)


def _inv_rec(inv: Invoice) -> dict:
    return {"hash": inv.file_hash, "invoice_no": inv.f("invoice_no").value,
            "vendor": inv.f("issuer_name").value, "customer": inv.f("customer_name").value,
            "currency": inv.f("currency_settlement").value,
            "amount": inv.f("total_due").value, "date": inv.f("invoice_date").value,
            "due_date": inv.f("payment_due_date").value}


def _txn_recs(inv: Invoice) -> List[dict]:
    from . import classify
    ccy = inv.f("currency_settlement").value
    out = []
    for i, t in enumerate(inv.transactions):
        amt = t.expense if t.expense is not None else t.income
        if amt is None:
            continue
        direction = "out" if t.expense is not None else "in"
        cls = classify.classify_txn(t.description, direction)
        # 防误判漏票：只有「无需票类型 + 高置信 + 不含单据号引用 + 非大额」才允许**自动**判为无需匹配；
        # 否则（弱置信/含发票号/大额）即便疑似无需票，也不自动跳过 → 交人工确认（hold_reason 说明为何）。
        big = False
        try:
            from decimal import Decimal as _D
            big = _D(str(amt)) >= _D(str(config.RECONCILE_NO_MATCH_MAX_AMOUNT))
        except Exception:
            pass
        has_ref = classify.looks_referenced(t.description)
        auto_no_match = cls["no_match_ok"] and cls["confidence"] == "high" and not has_ref and not big
        hold = []
        if cls["no_match_ok"] and not auto_no_match:
            if big:
                hold.append("大额")
            if has_ref:
                hold.append("含单据号/发票号")
            if cls["confidence"] != "high":
                hold.append("类型判断不确定")
        out.append({"stmt_hash": inv.file_hash, "index": i, "date": t.date,
                    "description": t.description, "currency": t.currency or ccy,
                    "amount": amt, "direction": direction, "counterparty": t.description,
                    "txn_type": cls["type"], "txn_label": cls["label"],
                    "no_match_ok": cls["no_match_ok"], "auto_no_match": auto_no_match,
                    "hold_why": "、".join(hold)})
    return out


def build_pool() -> tuple:
    """返回 (发票记录, 交易记录, 纠错队列计数)。已在**已确认**匹配里的成员会被排除。"""
    used_inv, used_txn = db.confirmed_member_refs()
    invs, txns = [], []
    need_fix = {"invoice": 0, "statement": 0}
    for inv in db.load_all_invoices().values():
        if (inv.approve_status or "") == "Rejected":
            continue                                     # 已人工拒绝：不参与对账（防把废单/可疑单对上账）
        if not extraction_passed(inv):
            need_fix[inv.doc_type or "invoice"] = need_fix.get(inv.doc_type or "invoice", 0) + 1
            continue
        if inv.doc_type == "statement":
            for r in _txn_recs(inv):
                if (r["stmt_hash"], r["index"]) not in used_txn:
                    txns.append(r)
        else:
            if inv.file_hash not in used_inv:
                invs.append(_inv_rec(inv))
    return invs, txns, need_fix


def run_matching() -> dict:
    """重建所有未确认匹配：清 proposed → 建池 → 匹配 → 入库。返回统计。"""
    db.clear_proposed_matches()
    invs, txns, need_fix = build_pool()
    proposals = matcher.match(invs, txns, blocked=db.rejected_pairs())   # 已判"不成立"的对不再配在一起
    now = _now()
    saved = 0
    for p in proposals:
        p = dict(p); p["status"] = "proposed"; p["created_at"] = now
        if db.save_match(p):
            saved += 1
    counts = db.match_counts()
    return {"pool_invoices": len(invs), "pool_transactions": len(txns),
            "need_fix": need_fix, "saved": saved, "counts": counts}


# ---- 展示视图 --------------------------------------------------------------
def _inv_brief(h: str) -> dict:
    inv = db.get_invoice(h)
    if inv is None:
        return {"file_hash": h, "missing": True}
    return {"file_hash": h, "file_name": inv.file_name,
            "invoice_no": _s(inv.f("invoice_no").value), "vendor": _s(inv.f("issuer_name").value),
            "currency": _s(inv.f("currency_settlement").value), "amount": _s(inv.f("total_due").value),
            "date": _s(inv.f("invoice_date").value), "due_date": _s(inv.f("payment_due_date").value)}


def _txn_brief(h: str, idx: int) -> dict:
    inv = db.get_invoice(h)
    if inv is None or idx is None or idx >= len(inv.transactions):
        return {"stmt_hash": h, "index": idx, "missing": True}
    t = inv.transactions[idx]
    return {"stmt_hash": h, "index": idx, "stmt_name": inv.file_name, "date": _s(t.date),
            "description": t.description, "income": _s(t.income), "expense": _s(t.expense),
            "currency": _s(inv.f("currency_settlement").value)}


def _view(m: dict) -> dict:
    m = dict(m)
    m["invoices"] = [_inv_brief(h) for h in m["invoices"]]
    m["txns"] = [_txn_brief(h, i) for h, i in m["txns"]]
    return m


def matches_view(category: Optional[str] = None, status: str = "proposed") -> List[dict]:
    return [_view(m) for m in db.list_matches(category=category, status=status)]


def summary() -> dict:
    invs, txns, need_fix = build_pool()
    return {"counts": db.match_counts(), "need_fix": need_fix,
            "pool_invoices": len(invs), "pool_transactions": len(txns)}


# ---- 确认 / 拒绝 -----------------------------------------------------------
def _member_keys(m: dict) -> list:
    return ["inv:" + h for h in m["invoices"]] + ["txn:%s#%s" % (h, i) for (h, i) in m["txns"]]


def _unapproved_invoices(m: dict) -> list:
    """匹配里**尚未审核通过（应计确认）**的发票 hash 列表。对账确认前须先审核这些发票。"""
    out = []
    for h in m["invoices"]:
        inv = db.get_invoice(h)
        if inv is not None and (inv.approve_status or "") != "Approved":
            out.append(h)
    return out


def _mark_confirmed(m: dict, by: str):
    """确认一条匹配=**资金结算**：原子预留成员 + 置 confirmed。**不再改发票的 approve_status**——
    "应计确认"由发票审核负责、此处应已通过；这里只标 review_status=Reconciled（已对账/已结算）。
    返回 None=成功；返回冲突的 member_key=竞态下成员已被占用（未确认）。"""
    now = _now()
    conflict = db.confirm_match_tx(m["id"], _member_keys(m), by, now)   # 数据库唯一约束 → 原子防重复
    if conflict is not None:
        return conflict
    for h in m["invoices"]:
        inv = db.get_invoice(h)
        if inv is None:
            continue
        inv.review_status = "Reconciled"          # 仅标"已对账/结算"，不动 approve_status（应计已在审核确认）
        db.resave_and_log(inv, "_reconcile", None,      # 原子：状态回写 + 留痕同事务
                          "对账结算（匹配#%s，%s）" % (m["id"], m.get("match_type")),
                          by, "对账匹配人工确认（资金结算）", now)
    return None


def _dup_conflict(m: dict, used_inv: set, used_txn: set):
    """该匹配的成员是否已在**别的已确认匹配**里（防重复对账/重复入账）。返回冲突描述或 None。"""
    for h in m["invoices"]:
        if h in used_inv:
            inv = db.get_invoice(h)
            return {"kind": "invoice", "hash": h,
                    "label": (inv.f("invoice_no").value if inv else None) or (inv.file_name if inv else h)}
    for (h, i) in m["txns"]:
        if (h, i) in used_txn:
            inv = db.get_invoice(h)
            desc = inv.transactions[i].description if (inv and i < len(inv.transactions)) else None
            return {"kind": "txn", "hash": h, "index": i, "label": desc or ("%s#%s" % (h[:8], i))}
    return None


def confirm_match(match_id: int, by: str = "reviewer") -> dict:
    m = db.get_match(match_id)
    if m is None:
        raise KeyError("未找到匹配 %s" % match_id)
    if m["status"] == "confirmed":
        return {"ok": True, "already": True}
    if m["status"] != "proposed":       # 已拒绝等非待确认状态：不凭旧 id 直接复活确认
        return {"ok": False, "message": "该匹配当前状态为 %s，请重新匹配后再确认" % m["status"]}
    if not m["invoices"] or not m["txns"]:   # 未匹配项（单边、无对应关系）不可确认——否则会把孤立记录误标已对账
        return {"ok": False, "message": "该记录没有可确认的对应关系（未匹配项），不能确认。"}
    # 应计先于结算：发票未审核通过（未做应计确认）→ 不在此"顺手盖章"，跳去该发票审核，通过后再回来确认对账
    unapproved = _unapproved_invoices(m)
    if unapproved:
        inv0 = db.get_invoice(unapproved[0])
        return {"ok": False, "needs_invoice_review": True, "match_id": match_id,
                "invoice_hash": unapproved[0],
                "invoice_no": (inv0.f("invoice_no").value if inv0 else None),
                "message": "该发票尚未审核确认（应计），请先在发票审核里通过，再回来确认对账。"}
    used_inv, used_txn = db.confirmed_member_refs()
    conflict = _dup_conflict(m, used_inv, used_txn)
    if conflict:
        # 防重复：成员已入账到另一笔匹配，拒绝再次确认
        noun = "发票" if conflict["kind"] == "invoice" else "流水交易"
        return {"ok": False, "blocked": "duplicate_member", "conflict": conflict,
                "message": "该%s「%s」已在另一笔已确认对账里，不能重复对应/入账。" % (noun, conflict["label"])}
    if _mark_confirmed(m, by) is not None:      # 并发竞态：应用层检查后、提交前被别的请求抢先占用
        return {"ok": False, "blocked": "race_conflict",
                "message": "该匹配的成员刚被另一操作确认占用，请刷新后重试。"}
    return {"ok": True, "match_id": match_id}


def confirm_batch(category: str = "auto", by: str = "reviewer") -> dict:
    """一键批量确认某类别（默认 auto=高可信唯一）的全部未确认匹配。
    防重复：跳过成员已入账到别处的匹配，返回 skipped 计数。"""
    used_inv, used_txn = db.confirmed_member_refs()
    done, skipped, skipped_need_review = 0, 0, 0
    for m in db.list_matches(category=category, status="proposed"):
        if not m["invoices"] or not m["txns"]:    # 跳过未匹配单边项
            continue
        if _unapproved_invoices(m):               # 发票未审核（应计）→ 批量不代审，跳过（请逐条去审核后再确认）
            skipped_need_review += 1
            continue
        if _dup_conflict(m, used_inv, used_txn) or _mark_confirmed(m, by) is not None:
            skipped += 1                               # 冲突或竞态占用 → 跳过
            continue
        used_inv.update(m["invoices"])                 # 本批内也去重：同一成员不会被两条同批匹配重复确认
        used_txn.update(tuple(x) for x in m["txns"])
        done += 1
    return {"ok": True, "confirmed": done, "skipped": skipped,
            "skipped_need_review": skipped_need_review, "category": category}


# ---- 手工匹配：把未自动配上的流水人工关联到一张发票 ------------------------
def _cand_brief(h: str, tag: str, used_inv: set) -> Optional[dict]:
    inv = db.get_invoice(h)
    if inv is None:
        return None
    td = inv.f("total_due").value
    return {"file_hash": h,
            "invoice_no": inv.f("invoice_no").value or "",
            "issuer": inv.f("issuer_name").value or "",
            "date": inv.f("invoice_date").value or "",
            "total_due": ("" if td in (None, "") else str(td)),
            "currency": inv.f("currency_settlement").value or "",
            "approved": (inv.approve_status or "") == "Approved",
            "reconciled_elsewhere": h in used_inv,
            "group": tag}


def manual_match_candidates(stmt_hash: str, index: int, q: str = "", limit: int = 40) -> dict:
    """未匹配流水手工选发票的候选：**未匹配发票（在待定队列、单边只有发票）优先**；
    给了关键词再**搜全部已提取发票**（发票号/供应商/金额）。每条带是否已审核、是否已在别处对账，供防呆。"""
    used_inv, _u = db.confirmed_member_refs()
    out, seen = [], set()
    for m in db.list_matches(category="unmatched", status="proposed"):
        if m["invoices"] and not m["txns"]:
            for h in m["invoices"]:
                if h in seen:
                    continue
                b = _cand_brief(h, "unmatched", used_inv)
                if b:
                    out.append(b); seen.add(h)
    ql = (q or "").strip().lower()
    if ql:
        for h, inv in db.load_all_invoices().items():
            if h in seen or (getattr(inv, "doc_type", "invoice") or "invoice") != "invoice":
                continue
            hay = " ".join([str(inv.f("invoice_no").value or ""), str(inv.f("issuer_name").value or ""),
                            str(inv.f("total_due").value or "")]).lower()
            if ql in hay:
                b = _cand_brief(h, "search", used_inv)
                if b:
                    out.append(b); seen.add(h)
    return {"candidates": out[:limit], "truncated": len(out) > limit, "total": len(out)}


def manual_match(stmt_hash: str, index: int, invoice_hash: str, by: str = "reviewer") -> dict:
    """人工把一笔未匹配流水关联到一张发票：建 1:1 proposed 匹配 → 复用 `confirm_match`（全套护栏：
    发票未审核→跳去审核、防重复成员、竞态）。成功建后清掉这两成员遗留的单边未匹配 proposed 记录。"""
    stmt = db.get_invoice(stmt_hash)
    if stmt is None or (getattr(stmt, "doc_type", "") or "") != "statement":
        return {"ok": False, "message": "未找到该银行流水。"}
    if index < 0 or index >= len(stmt.transactions or []):
        return {"ok": False, "message": "流水交易下标越界。"}
    inv = db.get_invoice(invoice_hash)
    if inv is None or (getattr(inv, "doc_type", "invoice") or "invoice") != "invoice":
        return {"ok": False, "message": "未找到该发票。"}
    used_inv, used_txn = db.confirmed_member_refs()
    if invoice_hash in used_inv:
        return {"ok": False, "message": "该发票已在另一笔已确认对账里，不能重复对应。"}
    if (stmt_hash, index) in used_txn:
        return {"ok": False, "message": "这笔流水已在另一笔已确认对账里，不能重复对应。"}
    tx = stmt.transactions[index]
    amt = tx.expense if tx.expense not in (None, "") else tx.income
    td = inv.f("total_due").value
    proposal = {"invoices": [invoice_hash], "txns": [(stmt_hash, index)],
                "category": "confirm", "match_type": "1:1（手工）", "match_score": 100,
                "currency": (inv.f("currency_settlement").value or None),
                "invoice_total": (None if td in (None, "") else str(td)),
                "matched_total": (None if amt in (None, "") else str(amt)),
                "amount_delta": None, "basis": ["人工指定匹配（未自动配上）"],
                "status": "proposed", "created_at": _now()}
    mid = db.save_match(proposal)
    if not mid:
        return {"ok": False, "message": "该发票与这笔流水的匹配已存在，请在列表中确认。"}
    for m in db.list_matches(status="proposed"):     # 清残留单边未匹配（新配对已代表它们）
        if m["id"] != mid and not (m["invoices"] and m["txns"]) \
                and (invoice_hash in m["invoices"] or (stmt_hash, index) in m["txns"]):
            db.delete_match(m["id"])
    res = confirm_match(mid, by)          # 复用护栏：未审核发票→needs_invoice_review；否则直接结算
    res["match_id"] = mid
    return res


def reject_match(match_id: int, by: str = "reviewer") -> dict:
    """拒绝一条匹配（对应关系不成立）。成员回到候选池，下次 run_matching 重新参与。"""
    m = db.get_match(match_id)
    if m is None:
        raise KeyError("未找到匹配 %s" % match_id)
    if not m["invoices"] or not m["txns"]:   # 单边项（未匹配/无需匹配）没有对应关系，"不成立"无从谈起，且会致成员消失
        return {"ok": False, "message": "该项没有对应关系（未匹配/无需匹配项），无需也不能标记不成立。"}
    db.set_match_status(match_id, "rejected", by=by, confirmed_at=_now())
    db.release_members(match_id)     # 释放预留成员，使其可重新参与对账（撤销已确认亦适用）
    return {"ok": True, "match_id": match_id}


def ack_no_match(match_id: int, by: str = "reviewer") -> dict:
    """确认「无需发票」：对一条单边的『无需匹配』项人工确认它确实不需要发票。
    机制=把这条(仅含该笔交易)确认入库并占用该交易→计入"已处理"(该交易在审核里锁定、流水待办减少、全处理完自动移出)。可撤销。"""
    m = db.get_match(match_id)
    if m is None:
        raise KeyError("未找到匹配 %s" % match_id)
    if m["category"] != "no_match_needed" or m["invoices"] or not m["txns"]:
        return {"ok": False, "message": "只有『无需匹配』（单边交易）项可确认无需发票。"}
    if m["status"] == "confirmed":
        return {"ok": True, "already": True}
    if m["status"] != "proposed":
        return {"ok": False, "message": "该项当前状态为 %s，无法确认。" % m["status"]}
    _cinv, ctxn = db.confirmed_member_refs()
    for (h, i) in m["txns"]:
        if (h, i) in ctxn:
            return {"ok": False, "message": "该交易已在别处确认/对账，请刷新。"}
    if db.confirm_match_tx(m["id"], _member_keys(m), by, _now()) is not None:
        return {"ok": False, "message": "该交易刚被占用，请刷新后重试。"}
    return {"ok": True, "match_id": match_id}


def unack_no_match(match_id: int, by: str = "reviewer") -> dict:
    """撤销「确认无需发票」：退回待核（释放该交易占用）。"""
    m = db.get_match(match_id)
    if m is None:
        raise KeyError("未找到匹配 %s" % match_id)
    if m["status"] != "confirmed" or m["invoices"] or m["category"] != "no_match_needed":
        return {"ok": False, "message": "只有『已确认无需发票』项可撤销。"}
    db.set_match_status(match_id, "proposed", by=by, confirmed_at=None)
    db.release_members(match_id)
    return {"ok": True, "match_id": match_id}


def unconfirm_match(match_id: int, by: str = "reviewer") -> dict:
    """撤销「已确认对账」(反做/资金结算撤销)：把已确认的真对账（发票↔流水）退回"待确认"，
    释放发票/流水占用 → 对应记录**解锁**（可再改/删/重新对账），发票 review_status 由
    Reconciled 复原为 Approved（应计仍在）。全程留痕。"""
    m = db.get_match(match_id)
    if m is None:
        raise KeyError("未找到匹配 %s" % match_id)
    if m["status"] != "confirmed" or not m["invoices"] or not m["txns"]:
        return {"ok": False, "message": "只有『已对账（发票↔流水已确认）』的匹配可撤销对账；"
                                         "单边『无需发票』请用其自己的撤销。"}
    now = _now()
    db.set_match_status(match_id, "proposed", by=by, confirmed_at=None)
    db.release_members(match_id)     # 释放占用 → 发票/流水解锁，可重新改删或参与对账
    for h in m["invoices"]:
        inv = db.get_invoice(h)
        if inv is None:
            continue
        if (inv.review_status or "") == "Reconciled":
            inv.review_status = "Approved"    # 复原为"应计已确认"（对账要求发票此前已 Approved）
            db.resave_and_log(inv, "_unreconcile", None, "撤销对账结算（匹配#%s）" % match_id,
                              by, "对账人工撤销（反做资金结算）", now)   # 原子：状态回写+留痕同事务
        else:
            db.log_change(h, "_unreconcile", None, "撤销对账结算（匹配#%s）" % match_id,
                          by, "对账人工撤销（反做资金结算）", now)
    return {"ok": True, "match_id": match_id}


def unreject_match(match_id: int, by: str = "reviewer") -> dict:
    """撤销「不成立」：删掉该 rejected 记录（移出黑名单）并**重跑匹配**——这一对即可再次被自动配上。"""
    m = db.get_match(match_id)
    if m is None:
        raise KeyError("未找到匹配 %s" % match_id)
    if m["status"] != "rejected":
        return {"ok": False, "message": "该匹配不是「已拒绝」状态，无需撤销。"}
    db.delete_match(match_id)         # 删除=移出黑名单
    res = run_matching()             # 重评全池：这一对不再被封，若仍最优会重新提案
    return {"ok": True, "match_id": match_id, "counts": res["counts"]}


def statement_marked_png(match_id: int, stmt_hash: str):
    """某匹配里该流水的对照图：**①原件源文件节选**（真实字节，可独立核对）+ **②系统提取证据卡**（完整显示
    日期/金额/整段附言，日期绿·金额橙·发票号蓝框），竖直拼成一张。前端直接显示。返回 png_bytes 或 None。"""
    from extraction.extract import textrender
    m = db.get_match(match_id)
    inv = db.get_invoice(stmt_hash)
    if m is None or inv is None or not inv.transactions:
        return None
    ccy = inv.f("currency_settlement").value
    idxs = sorted({i for (h, i) in m["txns"] if h == stmt_hash and 0 <= i < len(inv.transactions)})
    inv_nos = [x for x in (db.get_invoice(hh).f("invoice_no").value for hh in m["invoices"]
                           if db.get_invoice(hh)) if x]
    vendors = [str(x).strip() for x in (db.get_invoice(hh).f("issuer_name").value for hh in m["invoices"]
                                        if db.get_invoice(hh)) if x and str(x).strip()]
    vendors = list(dict.fromkeys(v for v in vendors if len(v) >= 3))
    rows = []
    for i in idxs:
        t = inv.transactions[i]
        # 显示正数金额 + 方向标注（OUT=支出/付款，IN=收入/收款），使金额大小与发票正数直接对得上、方向清楚，
        # 不再用易误解的负号（OFX/QFX 原文用带符号金额，负=流出，那是原件本身，在①原件节选里如实呈现）。
        if t.expense is not None:
            amt = "OUT " + str(t.expense)
        elif t.income is not None:
            amt = "IN " + str(t.income)
        else:
            amt = "—"
        rows.append({"date": t.date, "description": t.description, "amount": amt,
                     "currency": t.currency or ccy})
    if not rows:
        return None
    card = textrender.render_txn_evidence(rows, inv_nos, vendors=vendors)
    # 原件里也把 日期(绿)/金额(橙)/发票号(蓝) 都框出——用「解析出的原始值 + 常见变体」适配各格式写法
    date_needles, amt_needles = [], []
    for i in idxs:
        t = inv.transactions[i]
        date_needles += _date_variants(t.date, t.date_raw)
        for v in (t.expense, t.income):
            if v is not None:
                amt_needles += _amt_variants(str(v))
    groups = [{"needles": list(dict.fromkeys(date_needles)), "color": (15, 143, 95)},
              {"needles": list(dict.fromkeys(amt_needles)), "color": (232, 130, 12)},
              {"needles": inv_nos, "color": (47, 111, 237)},
              {"needles": vendors, "color": (123, 63, 191)}]     # 公司名(紫)
    raw = textrender.render_raw_excerpt(inv.file_path, groups, anchor_needles=inv_nos)  # 发票号锚定命中行
    parts = []
    if raw:
        parts.append(("① 原件·源文件节选（真实字节，可独立核对）：%s" % (inv.file_name or ""), raw))
    parts.append(("② 系统提取（供比对；如与原件不符即为解析问题）", card))
    return textrender.stack_labeled(parts)


def _date_variants(iso, raw) -> list:
    """日期在原件里的常见写法，供在原文里定位：优先原始 date_raw，再加 ISO/紧凑/斜杠/两位年 等变体。"""
    out = []
    if raw:
        out.append(str(raw))
    s = str(iso or "")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        y, m, dd = s[:4], s[5:7], s[8:10]
        out += ["%s-%s-%s" % (y, m, dd), "%s%s%s" % (y, m, dd), "%s/%s/%s" % (y, m, dd),
                "%s/%s/%s" % (dd, m, y), "%s/%s/%s" % (m, dd, y), "%s%s%s" % (y[2:], m, dd)]
    return [v for v in out if len(v) >= 6]


def _amt_variants(amt_str: str) -> list:
    """金额在原件里的常见写法：去正负号；含点写法 + 欧洲逗号小数写法（MT940 等）。"""
    s = str(amt_str).lstrip("+-").strip()
    out = [s]
    if "." in s:
        out.append(s.replace(".", ","))
    return [v for v in out if len(v) >= 3]


def _s(v):
    return None if v is None else str(v)


def matched_cash_for_invoice(invoice_hash: str) -> Optional[dict]:
    """该发票若有**已确认**对账匹配,返回其对应银行交易的现金合计 + 日期(供总账"据对账结算")。

    连接 reconcile → ledger 结算(计划 §3.3 第二段"从流水检索候选→对照"的落点):
    已确认 match → 其 txn 成员 → 流水 file_hash + txn_index → 交易金额(支出优先取 expense,否则 income)。
    无已确认匹配 / 取不到金额 → None。
    """
    from decimal import Decimal
    Z = Decimal("0")
    with db.connect() as c:
        row = c.execute(
            "SELECT m.id AS mid FROM matches m JOIN match_members mm ON mm.match_id = m.id "
            "WHERE m.status='confirmed' AND mm.kind='invoice' AND mm.invoice_hash=? LIMIT 1",
            (invoice_hash,)).fetchone()
        if not row:
            return None
        txns = c.execute(
            "SELECT invoice_hash AS sh, txn_index AS ti FROM match_members "
            "WHERE match_id=? AND kind='txn'", (row["mid"],)).fetchall()
    from ledger import store as _lstore          # 局部导入避免层次循环
    total, dates, n = Z, [], 0
    already_posted = False
    for t in txns:
        stmt = db.get_invoice(t["sh"])
        i = t["ti"]
        if stmt and i is not None and 0 <= i < len(stmt.transactions or []):
            tx = stmt.transactions[i]
            amt = tx.expense if (tx.expense is not None and tx.expense != Z) else tx.income
            if amt is not None:
                total += amt
                n += 1
                if tx.date:
                    dates.append(tx.date)
            # H2 护栏：该流水若已作【非发票流水入账】，据对账结算会让同一笔现金二次入账
            if _lstore.existing_posted("statement", "%s#%s" % (t["sh"], i)):
                already_posted = True
    if total <= Z or n == 0:
        return None
    return {"cash": str(total), "date": (max(dates) if dates else ""),
            "txn_count": n, "already_posted": already_posted}
