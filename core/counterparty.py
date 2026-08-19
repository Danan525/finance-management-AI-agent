"""交易对手方主数据（建档 + 相似度查重 + 别名归并）。

为什么需要它（人工审核模块计划 §3.5，MVP 硬要求）：发票上的对手方名是**自由文本**——
同一家公司会写成 `ACME`、`ACME Inc.`、`Acme, Inc`、`ACME INC`。若不建主数据，往来明细、
供应商台账、按对手方汇总都会散成多个"假对手方"，且总账往来控制账户的明细无法归属。

设计要点：
- **人工建档**：新对手方首次出现只进"待建档队列"，由人确认建档或**并入已有对手方**（记为别名）。
  规则同全项目红线：**AI 只给候选建议，绝不自动建档/自动合并**。
- **归一化 + 相似度查重**：`normalize()` 去大小写/标点/公司后缀，`candidates()` 用
  归一化串的序列相似度 + 词集合 Jaccard 取大者，给出可能重复的已有对手方（防 ACME/ACME Inc 重复建档）。
- **别名即映射**：确认后把发票上的原始写法写进 `counterparty_aliases`，下次同写法自动识别为已建档。
- 纯本地、只用标准库（difflib）；不调外部服务。
"""
from __future__ import annotations

import datetime as _dt
import difflib
import re
import sqlite3 as _sqlite3
from typing import Dict, List, Optional

from . import db

# 公司形式后缀（归一化时剥离，使 "ACME" 与 "ACME Inc." 归一相同）
_SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "co", "corp", "corporation",
    "company", "gmbh", "ag", "sa", "sas", "bv", "nv", "plc", "pte", "pty", "kk", "oy",
    "ab", "aps", "srl", "spa", "kg", "ohg", "pc", "lp",
}
_CN_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团有限公司", "集团", "公司")
_PUNCT = re.compile(r"[.,;:!?'\"`´’“”()\[\]{}<>/\\|+*&#@~^$%_—–\-]+")

KIND_VENDOR, KIND_CUSTOMER, KIND_BOTH = "vendor", "customer", "both"
#: 本方主体（我方公司自己）——发票的 customer_name 常年是自家公司名，它会一直出现在待建档队列里；
#: 建档成 self 即从队列消失，且语义上不把自己算作"对手方"。
KIND_SELF = "self"
KINDS = (KIND_VENDOR, KIND_CUSTOMER, KIND_BOTH, KIND_SELF)

# ---- 多角色（kind 是角色集合，一个实体可同时是 我方self + 供应商vendor + 客户customer）----
# kind 存规范化角色串（排序逗号分隔，如 "self,vendor"）；'both' 是 vendor+customer 的输入别名。
_ROLES = ("self", "vendor", "customer")
_ROLE_ORDER = {"self": 0, "vendor": 1, "customer": 2}


def parse_roles(kind):
    """把 kind（单值 / 'both' / 逗号或+分隔的多角色）解析成基本角色集合。"""
    out = set()
    for tok in str(kind or "").replace("+", ",").replace("，", ",").split(","):
        t = tok.strip().lower()
        if t == KIND_BOTH:
            out |= {KIND_VENDOR, KIND_CUSTOMER}
        elif t in _ROLES:
            out.add(t)
    return out


def fmt_roles(roles):
    return ",".join(sorted(set(roles), key=lambda r: _ROLE_ORDER.get(r, 9)))


def _norm_kind(kind):
    roles = parse_roles(kind)
    if not roles:
        raise ValueError("未知对手方类型：%s（应为 self/vendor/customer/both 或其组合）" % (kind,))
    return fmt_roles(roles)


def has_role(party_or_kind, role):
    kind = party_or_kind.get("kind") if isinstance(party_or_kind, dict) else party_or_kind
    return role in parse_roles(kind)

#: 视为"疑似同一家"的相似度阈值（仅用于给人排序/提示，不自动合并）
DUP_THRESHOLD = 0.86
#: 列入候选提示的下限
SUGGEST_THRESHOLD = 0.62


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 归一化与相似度 ----------

def normalize(name: str) -> str:
    """对手方名归一化键：小写、去标点、剥公司形式后缀、压缩空白。"""
    s = (name or "").strip().lower()
    if not s:
        return ""
    for suf in _CN_SUFFIXES:                 # 中文后缀直接剥（无空格分词）
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    s = _PUNCT.sub(" ", s)
    toks = [t for t in s.split() if t and t not in _SUFFIXES]
    if not toks:                             # 名字整体就是个后缀词（罕见）→ 退回去标点结果
        toks = s.split()
    return " ".join(toks)


def similarity(a: str, b: str) -> float:
    """两个对手方名的相似度 [0,1]：归一化串的序列相似度与词集合 Jaccard 取大者。

    Jaccard 补序列相似度的短板："Acme Global Services" vs "Global Services Acme"（词序不同）。
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(seq, jac)


# ---------- 主数据读写 ----------

def _row_to_party(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "norm": r["norm"], "kind": r["kind"],
        "tax_id": r["tax_id"] or "", "default_account": r["default_account"] or "",
        "note": r["note"] or "", "status": r["status"] or "active",
        "created_by": r["created_by"] or "", "created_at": r["created_at"] or "",
    }


def list_parties(include_archived: bool = False, conn=None) -> List[dict]:
    """已建档对手方（含别名列表与关联发票数）。"""
    db._ensure_init()
    with db._conn_or(conn) as c:
        sql = "SELECT * FROM counterparties"
        if not include_archived:
            sql += " WHERE COALESCE(status,'active')='active'"
        sql += " ORDER BY name COLLATE NOCASE"
        out = []
        for r in c.execute(sql).fetchall():
            p = _row_to_party(r)
            p["aliases"] = [x["raw"] for x in c.execute(
                "SELECT raw FROM counterparty_aliases WHERE cp_id=? ORDER BY raw", (r["id"],)).fetchall()]
            out.append(p)
        return out


def get_party(cp_id: int, conn=None) -> Optional[dict]:
    db._ensure_init()
    with db._conn_or(conn) as c:
        r = c.execute("SELECT * FROM counterparties WHERE id=?", (int(cp_id),)).fetchone()
        return _row_to_party(r) if r else None


def resolve(name: str, conn=None) -> Optional[dict]:
    """按名字/别名（归一化后）找已建档对手方；找不到返回 None（→ 进待建档队列）。"""
    norm = normalize(name)
    if not norm:
        return None
    db._ensure_init()
    with db._conn_or(conn) as c:
        # ORDER BY id：历史数据里若曾出现同 norm 的两条 active（归档后重建再复活），
        # 至少保证解析结果**确定**（取最早那条）；新数据由 register/update_party 的唯一性检查阻止。
        r = c.execute("SELECT * FROM counterparties WHERE norm=? AND COALESCE(status,'active')='active' "
                      "ORDER BY id LIMIT 1", (norm,)).fetchone()
        if r:
            return _row_to_party(r)
        r = c.execute(
            "SELECT cp.* FROM counterparty_aliases a JOIN counterparties cp ON cp.id=a.cp_id "
            "WHERE a.norm=? AND COALESCE(cp.status,'active')='active' LIMIT 1", (norm,)).fetchone()
        return _row_to_party(r) if r else None


def candidates(name: str, limit: int = 5, conn=None) -> List[dict]:
    """相似的已建档对手方（查重候选，按相似度降序）。**只建议，不自动合并。**"""
    out = []
    for p in list_parties(conn=conn):
        best = similarity(name, p["name"])
        for al in p.get("aliases", []):
            best = max(best, similarity(name, al))
        if best >= SUGGEST_THRESHOLD:
            out.append({"id": p["id"], "name": p["name"], "kind": p["kind"],
                        "score": round(best, 3), "likely_same": best >= DUP_THRESHOLD})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def register(name: str, kind: str = KIND_VENDOR, tax_id: str = "", note: str = "",
             default_account: str = "", by: str = "reviewer",
             aliases: Optional[List[str]] = None, force: bool = False) -> dict:
    """人工建档一个新对手方（返回主数据 dict）。

    force=False 时，若已存在**疑似同名**（相似度 ≥ DUP_THRESHOLD）则拒绝并把候选带回，
    要求人先判断"并入已有"还是"确实是另一家"（force=True 表示人已确认是另一家）。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("对手方名称不能为空")
    kind = _norm_kind(kind)                 # 规范化多角色
    if resolve(name):
        raise ValueError(f"该对手方（或其别名）已建档：{name}")
    if not force:
        dups = [c for c in candidates(name) if c["likely_same"]]
        if dups:
            raise ValueError(
                "疑似与已建档对手方重复：%s。请选择并入已有，或确认确为另一家后再建档"
                % "、".join("%s(%.0f%%)" % (d["name"], d["score"] * 100) for d in dups))
    # 别名先校验：已归属他家的写法不能在这里"顺带"登记——否则 INSERT OR IGNORE 会**静默丢弃**，
    # 用户以为归并成功了（2026-08-03 自检发现）。
    seen = set()
    for raw in (aliases or []):
        an = normalize(raw)
        if not an or an in seen:
            continue
        seen.add(an)
        owner = resolve(raw)
        if owner:
            raise ValueError(f"别名「{raw}」已归属对手方：{owner['name']}，请先处理该归属再建档")
    now = _now()
    db._ensure_init()
    try:
        with db.connect() as c:
            cur = c.execute(
                "INSERT INTO counterparties(name, norm, kind, tax_id, default_account, note, "
                "status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (name, normalize(name), kind, tax_id or None, default_account or None,
                 note or None, "active", by, now))
            cp_id = cur.lastrowid
            for raw in (aliases or []):
                _insert_alias(c, cp_id, raw, by, now)
    except _sqlite3.IntegrityError as e:      # name UNIQUE 撞车（并发两人同时建同名）→ 当业务错误报
        raise ValueError(f"建档失败，该名称已存在（可能刚被他人建档）：{name}") from e
    return get_party(cp_id)


def _insert_alias(c, cp_id: int, raw: str, by: str, now: str) -> None:
    raw = (raw or "").strip()
    norm = normalize(raw)
    if not norm:
        return
    c.execute("INSERT OR IGNORE INTO counterparty_aliases(norm, raw, cp_id, created_by, created_at) "
              "VALUES (?,?,?,?,?)", (norm, raw, int(cp_id), by, now))


def add_alias(cp_id: int, raw: str, by: str = "reviewer") -> dict:
    """把发票上的一种写法并入已建档对手方（人工确认的查重结果）。"""
    p = get_party(cp_id)
    if p is None:
        raise ValueError(f"对手方不存在：{cp_id}")
    norm = normalize(raw)
    if not norm:
        raise ValueError("别名不能为空")
    other = resolve(raw)
    if other and other["id"] != p["id"]:
        raise ValueError(f"该写法已归属另一对手方：{other['name']}")
    db._ensure_init()
    with db.connect() as c:
        _insert_alias(c, p["id"], raw, by, _now())
    return get_party(p["id"])


def update_party(cp_id: int, kind: Optional[str] = None, tax_id: Optional[str] = None,
                 note: Optional[str] = None, default_account: Optional[str] = None,
                 status: Optional[str] = None) -> dict:
    """维护主数据字段（类型/税号/默认科目/备注/停用）。名称不改——改名请建新档并归并别名。"""
    if get_party(cp_id) is None:
        raise ValueError(f"对手方不存在：{cp_id}")
    if kind is not None:
        kind = _norm_kind(kind)
    if status is not None and status not in ("active", "archived"):
        raise ValueError(f"未知状态：{status}")
    if status == "active":
        # 复活（archived → active）前查归一化键冲突：归档期间可能已按同一名字建了新档，
        # 直接复活会出现**两条 active 同 norm**，`resolve` 取哪条就变成不确定行为（2026-08-03 自检发现）。
        me = get_party(cp_id)
        other = resolve(me["name"])
        if other and other["id"] != me["id"]:
            raise ValueError(
                f"无法启用：已有同名（归一化后）对手方「{other['name']}」在用。"
                f"请先归档它，或把本条的写法作为别名并入它")
    sets, params = [], []
    for col, val in (("kind", kind), ("tax_id", tax_id), ("note", note),
                     ("default_account", default_account), ("status", status)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val or None)
    if sets:
        db._ensure_init()
        with db.connect() as c:
            c.execute("UPDATE counterparties SET %s WHERE id=?" % ", ".join(sets),
                      params + [int(cp_id)])
    return get_party(cp_id)


# ---------- 待建档队列（从发票里出现的对手方名归集）----------

def invoice_parties() -> List[dict]:
    """扫描全部发票，按"对手方原始写法"归集：{raw, kind, count, samples[]}。

    对手方取自发票方向：我方收票（AP）→ issuer_name（供应商）；我方开票（AR）→ customer_name（客户）。
    当前提取阶段方向缺省为 AP（见 ledger.posting.infer_direction），故 issuer 记 vendor、
    customer 记 customer；同名两侧都出现则为 both。流水（doc_type='statement'）不在此列。
    """
    agg: Dict[str, dict] = {}
    for inv in db.load_all_invoices().values():
        if (getattr(inv, "doc_type", "invoice") or "invoice") != "invoice":
            continue
        for field, kind in (("issuer_name", KIND_VENDOR), ("customer_name", KIND_CUSTOMER)):
            raw = (inv.f(field).value or "").strip()
            if not raw:
                continue
            key = normalize(raw)
            if not key:
                continue
            a = agg.setdefault(key, {"raw": raw, "kind": kind, "count": 0, "samples": []})
            a["count"] += 1
            if a["kind"] != kind:
                a["kind"] = KIND_BOTH
            if len(a["samples"]) < 5:
                a["samples"].append({"file_hash": inv.file_hash,
                                     "invoice_no": inv.f("invoice_no").value or "",
                                     "file_name": inv.file_name})
    return sorted(agg.values(), key=lambda x: (-x["count"], x["raw"]))


def pending(limit: int = 200, with_total: bool = False):
    """待建档队列：发票上出现但尚未建档的对手方 + 查重候选（人工确认建档/并入）。

    `with_total=True` 时返回 `(list, total)`——**截断要说出来**，否则界面只显示前 N 条会被读成"就这些"。
    """
    fresh = [a for a in invoice_parties() if not resolve(a["raw"])]
    out = []
    for a in fresh[:limit]:
        item = dict(a)
        item["candidates"] = candidates(a["raw"])
        out.append(item)
    return (out, len(fresh)) if with_total else out


def overview(limit: int = 200) -> dict:
    """页面一次取数：已建档 + 待建档 + 汇总（**只扫一遍发票**）。

    此前 `/api/counterparties` 分别调 `pending()` 与 `summary()`，把"重建全部发票对象"这件重活
    做了两遍（2026-08-03 自检发现）。
    """
    parties = list_parties()
    pend, total = pending(limit=limit, with_total=True)
    return {"parties": parties, "pending": pend,
            "summary": {"parties": len(parties), "pending": total,
                        "pending_shown": len(pend), "truncated": total > len(pend)}}


def summary() -> dict:
    _pend, total = pending(with_total=True)
    return {"parties": len(list_parties()), "pending": total}


def add_role(cp_id, role, by="reviewer"):
    """在已有角色上追加一个角色（保留其它）——如已建档供应商追加"我方 self"。"""
    p = get_party(cp_id)
    if p is None:
        raise ValueError("对手方不存在：%s" % cp_id)
    if role not in _ROLES:
        raise ValueError("未知角色：%s" % role)
    return update_party(cp_id, kind=fmt_roles(parse_roles(p["kind"]) | {role}))


def remove_role(cp_id, role, by="reviewer"):
    """去除一个角色（至少保留一个；去到空则拒绝）。"""
    p = get_party(cp_id)
    if p is None:
        raise ValueError("对手方不存在：%s" % cp_id)
    roles = parse_roles(p["kind"]) - {role}
    if not roles:
        raise ValueError("至少需保留一个角色；如要停用请改状态 status=archived")
    return update_party(cp_id, kind=fmt_roles(roles))


def self_candidates(limit: int = 5) -> List[dict]:
    """侦测本方主体：扫全部发票，找**既作开票方(issuer)又作收票方(customer)**的名字——
    几乎必然是自己公司（我方开票时是开票方、收到账单时是收票方）。只返回**尚未建档**的候选，
    供界面「一键建档为我方主体(self)」。**零误报优先**：只认双向出现、且未建档的名字（已建档任何角色都不打扰，
    交给多角色手动加 self）。修「AR 发票静默默认成 AP」坑：登记 self 后方向自动判 AR/AP。"""
    from core import db
    issuers: dict = {}
    customers: dict = {}
    spell: dict = {}
    for inv in db.load_all_invoices().values():
        if (getattr(inv, "doc_type", "invoice") or "invoice") != "invoice":
            continue
        for field, bag in (("issuer_name", issuers), ("customer_name", customers)):
            raw = (inv.f(field).value or "").strip()
            if not raw:
                continue
            n = normalize(raw)
            if not n:
                continue
            bag[n] = bag.get(n, 0) + 1
            spell.setdefault(n, raw)
    out = []
    for n in set(issuers) & set(customers):
        if resolve(spell[n]):                 # 已建档（任何角色）→ 不打扰
            continue
        out.append({"name": spell[n], "norm": n,
                    "as_issuer": issuers[n], "as_customer": customers[n]})
    out.sort(key=lambda x: -(x["as_issuer"] + x["as_customer"]))
    return out[:limit]
