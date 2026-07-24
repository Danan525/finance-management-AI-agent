"""发票 ↔ 银行流水交易 的自动匹配引擎（纯函数，不碰 DB，便于测试）。

设计对齐 Dynamics 365 / Oracle Account Reconciliation 的「规则优先级 + 自动匹配为主、人工处理例外」：
匹配依据按优先级打分（invoice_no/附言完全一致 > 金额+币种+方向 > 供应商/账号 > 日期区间 > 模糊名），
产出 match_score（0–100）与分类：
  auto      高可信且唯一 → 待批量确认
  confirm   中等可信 / 一对多 / 多对一 / 金额差额(手续费·外币) → 人工确认对应关系
  multi     多个可信候选 → 重点人工审核
  unmatched 无匹配 → 待定队列
支持基数：1:1 / 1:N（一票多次付）/ N:1（多票合并付）/ N:N（检出并路由人工）。
"""
from __future__ import annotations

import re
from decimal import Decimal
from itertools import combinations
from typing import Dict, List, Optional, Tuple

# 分数阈值与权重
AUTO = 80          # ≥ 此分且唯一 → 自动（待批量）
MED = 50           # ≥ 此分 → 至少进人工确认
AMBIG_GAP = 15     # 次优候选与最优差距 < 此值 → 视为多候选（ambiguous）

W_REF = 55         # 发票号/附言完全命中
W_AMT_EXACT = 30   # 金额+币种完全一致
W_AMT_FEE = 16     # 金额在手续费/舍入容差内（币种一致）
W_VENDOR = 14      # 供应商名一致
W_DATE_IN = 8      # 付款日期落在发票日~到期日合理区间
W_DATE_NEAR = 3    # 日期虽超区间但仍在大致范围


def _norm(s) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def dec(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def ref_hit(invoice_no, description) -> bool:
    """发票号（规范化后）是否作为子串出现在流水附言里。要求 ≥5 字避免短号误命中。"""
    a = _norm(invoice_no)
    if len(a) < 5:
        return False
    return a in _norm(description)


def vendor_hit(vendor, text) -> bool:
    v = _norm(vendor)
    if len(v) < 4:
        return False
    t = _norm(text)
    if v in t:
        return True
    # 供应商名首两个词的规范化子串（应对 "Greyvane Partners" ↔ "Greyvane Partners Ltd"）
    toks = [w for w in re.split(r"\s+", str(vendor or "").strip()) if w]
    if len(toks) >= 2:
        head = _norm(toks[0] + toks[1])
        return len(head) >= 6 and head in t
    return False


def _amount_tol(amount: Decimal) -> Decimal:
    """手续费/舍入容差：max(1.0, 0.5% 票额)。"""
    return max(Decimal("1"), (amount.copy_abs() * Decimal("0.005")))


def _date_in_window(inv_date, due_date, txn_date) -> int:
    """0=无信息/太远, W_DATE_NEAR=大致范围, W_DATE_IN=合理区间。"""
    from datetime import date
    def d(s):
        try:
            y, m, dd = str(s)[:10].split("-")
            return date(int(y), int(m), int(dd))
        except Exception:
            return None
    ti = d(txn_date)
    if ti is None:
        return 0
    lo = d(inv_date)
    hi = d(due_date) or lo
    if lo and hi:
        lo2 = lo.toordinal() - 3
        hi2 = hi.toordinal() + 45
        if lo2 <= ti.toordinal() <= hi2:
            return W_DATE_IN
        if lo.toordinal() - 30 <= ti.toordinal() <= hi.toordinal() + 120:
            return W_DATE_NEAR
        return 0
    return W_DATE_NEAR


def score_pair(inv: dict, txn: dict) -> Tuple[int, List[str], Optional[Decimal]]:
    """给一对 (发票, 交易) 打分。返回 (score, 依据列表, 金额差额或None)。"""
    basis: List[str] = []
    score = 0
    ia = dec(inv.get("amount"))
    ta = dec(txn.get("amount"))
    inv_ccy, txn_ccy = inv.get("currency"), txn.get("currency")
    # 只有**两边都有币种且不同**才算真外币冲突、不可直接比；有一边未标注币种（裸 CSV 流水常见）
    # 按"不冲突"处理、照常比金额（否则金额+供应商+日期全对的干净匹配会被漏配）。
    ccy_conflict = bool(inv_ccy) and bool(txn_ccy) and inv_ccy != txn_ccy
    both_ccy = bool(inv_ccy) and bool(txn_ccy)
    delta = None

    if ref_hit(inv.get("invoice_no"), txn.get("description")):
        score += W_REF
        basis.append("发票号/附言完全一致：%s" % inv.get("invoice_no"))

    if ia is not None and ta is not None and not ccy_conflict:
        delta = (ia - ta)
        ccy = inv_ccy or txn_ccy or ""
        if delta.copy_abs() <= Decimal("0.01"):
            score += W_AMT_EXACT
            basis.append("金额+币种完全一致" if both_ccy else "金额一致（一方未标注币种，按可比处理，请核对币种）")
            delta = Decimal("0")
        elif delta.copy_abs() <= _amount_tol(ia):
            score += W_AMT_FEE
            basis.append("金额在手续费/舍入容差内（差 %s %s）" % (delta, ccy))
        else:
            basis.append("金额不一致（差 %s %s）" % (delta, ccy))
    elif ia is not None and ta is not None and ccy_conflict:
        basis.append("币种不同（%s vs %s），疑外币换算，金额不可直接比对"
                     % (inv_ccy, txn_ccy))

    text = (txn.get("description") or "") + " " + (txn.get("counterparty") or "")
    vhit = vendor_hit(inv.get("vendor"), text)
    chit = vendor_hit(inv.get("customer"), text)
    if vhit:
        score += W_VENDOR
        basis.append("供应商名一致：%s" % inv.get("vendor"))
    # 收付方向一致性（软信号）：应付(供应商开票)应对应付款/支出，应收(开给客户)应对应收款/收入。
    # 仅在方向与对手方角色**明显矛盾**时标记存疑（并在 match() 里禁止自动通过、转人工确认）；含糊则中性不罚。
    direction = txn.get("direction")
    if direction == "in" and vhit and not chit:
        basis.append("⚠ 收付方向存疑：供应商发票却对应到「收款」（可能退款/冲销或对错方向）")
    elif direction == "out" and chit and not vhit:
        basis.append("⚠ 收付方向存疑：客户发票却对应到「付款」（可能退款/冲销或对错方向）")

    dsc = _date_in_window(inv.get("date"), inv.get("due_date"), txn.get("date"))
    if dsc == W_DATE_IN:
        score += dsc; basis.append("付款日期落在发票日~到期日合理区间")
    elif dsc == W_DATE_NEAR:
        score += dsc

    return min(score, 100), basis, delta


def _category(score: int, ambiguous: bool, exact_amount: bool) -> str:
    if ambiguous:
        return "multi"
    if score >= AUTO and exact_amount:
        return "auto"
    if score >= AUTO:
        return "confirm"      # 高分但金额非完全一致（差额/缺金额）→ 仍需人工确认
    if score >= MED:
        return "confirm"
    return "unmatched"


def match(invoices: List[dict], txns: List[dict], blocked=None) -> List[dict]:
    """主匹配。返回提案列表，每条：
    {invoices:[hash], txns:[(stmt_hash,idx)], match_score, category, match_type,
     currency, invoice_total, matched_total, amount_delta, basis:[...]}。
    blocked: {(invoice_hash, stmt_hash, txn_index)} 已被判「不成立」的配对黑名单——不再配在一起。"""
    proposals: List[dict] = []
    blocked = blocked or set()

    def _blk(inv, txn):
        return (inv["hash"], txn["stmt_hash"], txn["index"]) in blocked

    # 1) 全部候选对（分数 ≥ MED，且至少命中 ref 或金额，不靠供应商/日期单独成立）
    pairs = []
    for inv in invoices:
        for txn in txns:
            if _blk(inv, txn):          # 已判不成立的对：不参与配对（各自落回未匹配）
                continue
            sc, basis, delta = score_pair(inv, txn)
            if sc < MED:
                continue
            amt_exact = any(b.startswith("金额+币种") or b.startswith("金额一致") for b in basis)
            amt_fee = any(b.startswith("金额在手续费") for b in basis)
            strong_ref = any(b.startswith("发票号") for b in basis)
            if not (strong_ref or amt_exact or amt_fee):
                continue
            # 金额是否一致：完全一致/手续费容差内/币种不同或缺金额(无法直接比对)→ 视为“不冲突”
            amt_ok = (delta is None) or amt_exact or amt_fee
            pairs.append({"inv": inv, "txn": txn, "score": sc, "basis": basis, "delta": delta,
                          "amt_ok": amt_ok, "amt_exact": amt_exact})
    pairs.sort(key=lambda p: p["score"], reverse=True)

    cand_by_inv: Dict[str, list] = {}
    cand_by_txn: Dict[tuple, list] = {}
    for p in pairs:
        cand_by_inv.setdefault(p["inv"]["hash"], []).append(p)
        cand_by_txn.setdefault((p["txn"]["stmt_hash"], p["txn"]["index"]), []).append(p)

    used_inv, used_txn = set(), set()

    def _ambiguous(p, ih, tk):
        # comp = 与 p **共享一侧**（同发票或同流水）的其它候选：任一存在即代表"这张发票/这笔流水
        # 还有别的可信对法" → 歧义。comp 里每个 q 恰好共享一侧（另一侧必不同，否则就是 p 本身、已排除），
        # 故只需比分数；旧实现多加的 `q.txn≠tk AND q.inv≠ih` 对 comp 恒为假 → _ambiguous 永远 False、
        # "多候选/重点审核"桶成死代码、歧义匹配被贪心静默自动确认（2026-07-22 修）。
        comp = [q for q in cand_by_inv.get(ih, []) if q is not p] + \
               [q for q in cand_by_txn.get(tk, []) if q is not p]
        return any(q["score"] >= p["score"] - AMBIG_GAP for q in comp)

    # 2) 贪心 1:1（高分优先）——**仅当金额一致**才作为干净 1:1 消费；
    #    金额明显不一致的（可能是分次付款/合并付款）留给 3) 分组，别抢先吃掉。
    for p in pairs:
        ih = p["inv"]["hash"]; tk = (p["txn"]["stmt_hash"], p["txn"]["index"])
        if ih in used_inv or tk in used_txn or not p["amt_ok"]:
            continue
        # 收付方向存疑 → 不进 auto，降级为人工确认（视作"非完全一致"）
        dir_bad = any("收付方向存疑" in b for b in p["basis"])
        clean = (p["delta"] == Decimal("0")) and not dir_bad
        cat = _category(p["score"], _ambiguous(p, ih, tk), clean)
        used_inv.add(ih); used_txn.add(tk)
        proposals.append(_pair_proposal(p, cat))

    # 3) 一票多付 1:N / 多票合并付 N:1（在剩余项里按 ref/供应商+币种分组做子集求和）
    proposals += _group_matches(invoices, txns, used_inv, used_txn, blocked)

    # 4) 剩余「发票号/供应商命中但金额对不上」的对：既非干净 1:1、也没凑成组 → 交人工确认（差额/部分付款）
    for p in pairs:
        ih = p["inv"]["hash"]; tk = (p["txn"]["stmt_hash"], p["txn"]["index"])
        if ih in used_inv or tk in used_txn or p["amt_ok"]:
            continue
        used_inv.add(ih); used_txn.add(tk)
        pr = _pair_proposal(p, "multi" if _ambiguous(p, ih, tk) else "confirm")
        pr["basis"] = list(pr["basis"]) + ["⚠ 金额对不上，请人工确认（可能是部分付款/多次付款/差额）"]
        proposals.append(pr)

    # 5) 未匹配：剩余发票、剩余交易各自成条，进待定
    for inv in invoices:
        if inv["hash"] not in used_inv:
            proposals.append({"invoices": [inv["hash"]], "txns": [], "match_score": 0,
                              "category": "unmatched", "match_type": "none",
                              "currency": inv.get("currency"), "invoice_total": _s(inv.get("amount")),
                              "matched_total": None, "amount_delta": None,
                              "basis": ["未找到对应流水"]})
    for txn in txns:
        tk = (txn["stmt_hash"], txn["index"])
        if tk not in used_txn:
            # 「未匹配 ≠ 异常」：按交易类型分流。**防误判漏票**：只有 auto_no_match（无需票类型+高置信+无单据号+非大额）
            # 才自动归「无需匹配」；无需票类型但大额/含发票号/判断不确定 → 归「未匹配·待确认」而非静默跳过；
            # 需发票却缺 → 「未匹配」带结构化原因。
            label = txn.get("txn_label") or "未分类"
            if txn.get("auto_no_match"):
                cat, reason = "no_match_needed", "%s · 无需发票匹配" % label
            elif txn.get("no_match_ok"):
                cat = "unmatched"
                reason = "疑似无需发票（%s）但需人工确认：%s" % (label, txn.get("hold_why") or "请核对是否需要发票")
            else:
                cat, reason = "unmatched", _unmatched_reason(txn)
            proposals.append({"invoices": [], "txns": [tk], "match_score": 0,
                              "category": cat, "match_type": "none",
                              "currency": txn.get("currency"), "invoice_total": None,
                              "matched_total": _s(txn.get("amount")), "amount_delta": None,
                              "txn_type": txn.get("txn_type"), "reason": reason,
                              "basis": [reason]})
    return proposals


def _unmatched_reason(txn: dict) -> str:
    """需发票却未匹配的结构化原因（供未匹配队列分「待认领/待补证/真正异常」）。"""
    t = txn.get("txn_type")
    if t == "reimbursement":
        return "员工报销 · 待补报销单/发票"
    if t == "refund":
        return "退款/冲正 · 待核对原交易或红字发票"
    if t == "customer_receipt":
        return "客户收款 · 待销售发票/应收单"
    if t == "vendor_payment":
        return "供应商付款 · 待采购发票"
    return "未分类 · 待人工判断"


def _pair_proposal(p: dict, cat: str) -> dict:
    inv, txn = p["inv"], p["txn"]
    return {"invoices": [inv["hash"]], "txns": [(txn["stmt_hash"], txn["index"])],
            "match_score": p["score"], "category": cat, "match_type": "1:1",
            "currency": inv.get("currency"), "invoice_total": _s(inv.get("amount")),
            "matched_total": _s(txn.get("amount")),
            "amount_delta": _s(p["delta"]) if p["delta"] is not None else None,
            "basis": p["basis"]}


def _group_matches(invoices, txns, used_inv, used_txn, blocked=None) -> List[dict]:
    """剩余项里的一对多 / 多对一：按「共享发票号 token 或 供应商+币种」聚组，做小规模子集求和。
    blocked 里的 (发票,交易) 对不进同组。"""
    out = []
    blocked = blocked or set()
    free_inv = [i for i in invoices if i["hash"] not in used_inv]
    free_txn = [t for t in txns if (t["stmt_hash"], t["index"]) not in used_txn]

    # 1:N —— 一张发票 = 若干笔交易之和
    for inv in free_inv:
        if inv["hash"] in used_inv:
            continue
        ia = dec(inv.get("amount"))
        if ia is None:
            continue
        pool = [t for t in free_txn
                if (t["stmt_hash"], t["index"]) not in used_txn
                and (inv["hash"], t["stmt_hash"], t["index"]) not in blocked
                and t.get("currency") == inv.get("currency")
                and (ref_hit(inv.get("invoice_no"), t.get("description"))
                     or vendor_hit(inv.get("vendor"), (t.get("description") or "") + " " + (t.get("counterparty") or "")))]
        combo = _subset_sum([dec(t.get("amount")) for t in pool], ia)
        if combo and len(combo) >= 2:
            chosen = [pool[k] for k in combo]
            tot = sum(dec(t.get("amount")) for t in chosen)
            used_inv.add(inv["hash"])
            for t in chosen:
                used_txn.add((t["stmt_hash"], t["index"]))
            out.append({"invoices": [inv["hash"]], "txns": [(t["stmt_hash"], t["index"]) for t in chosen],
                        "match_score": 72, "category": "confirm", "match_type": "1:N",
                        "currency": inv.get("currency"), "invoice_total": _s(ia),
                        "matched_total": _s(tot), "amount_delta": _s(ia - tot),
                        "basis": ["一票多付：%d 笔交易合计 = 票额" % len(chosen),
                                  "供应商/附言一致"]})

    # N:1 —— 一笔交易 = 若干发票之和
    free_txn = [t for t in txns if (t["stmt_hash"], t["index"]) not in used_txn]
    for txn in free_txn:
        tk = (txn["stmt_hash"], txn["index"])
        if tk in used_txn:
            continue
        ta = dec(txn.get("amount"))
        if ta is None:
            continue
        pool = [i for i in invoices if i["hash"] not in used_inv
                and (i["hash"], txn["stmt_hash"], txn["index"]) not in blocked
                and i.get("currency") == txn.get("currency")
                and (ref_hit(i.get("invoice_no"), txn.get("description"))
                     or vendor_hit(i.get("vendor"), (txn.get("description") or "") + " " + (txn.get("counterparty") or "")))]
        combo = _subset_sum([dec(i.get("amount")) for i in pool], ta)
        if combo and len(combo) >= 2:
            chosen = [pool[k] for k in combo]
            tot = sum(dec(i.get("amount")) for i in chosen)
            used_txn.add(tk)
            for i in chosen:
                used_inv.add(i["hash"])
            out.append({"invoices": [i["hash"] for i in chosen], "txns": [tk],
                        "match_score": 72, "category": "confirm", "match_type": "N:1",
                        "currency": txn.get("currency"), "invoice_total": _s(tot),
                        "matched_total": _s(ta), "amount_delta": _s(tot - ta),
                        "basis": ["多票合并付：%d 张发票合计 = 交易额" % len(chosen),
                                  "供应商/附言一致"]})
    return out


def _subset_sum(amounts: List[Optional[Decimal]], target: Decimal, max_k: int = 4):
    """在 amounts 里找和 ≈ target 的子集（size 2..max_k），返回下标列表；容差 = target 的手续费容差。"""
    idxs = [k for k, a in enumerate(amounts) if a is not None]
    if len(idxs) < 2:
        return None
    tol = _amount_tol(target)
    for k in range(2, min(max_k, len(idxs)) + 1):
        for combo in combinations(idxs, k):
            s = sum(amounts[c] for c in combo)
            if (s - target).copy_abs() <= tol:
                return list(combo)
    return None


def _s(v):
    return None if v is None else str(v)
