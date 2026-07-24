"""SQLite 持久化：发票记录、文件审计轨迹、变更日志（占位）。

仅本地文件数据库，不联网。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Optional

from . import config
from .models import Invoice

logger = logging.getLogger("finance.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash     TEXT UNIQUE,           -- 唯一：同一文件重复处理走 UPSERT 更新，不堆叠重复行
    file_name     TEXT,
    invoice_no    TEXT,
    total_due     TEXT,
    risk_score    INTEGER,
    validation_status TEXT,
    approve_status    TEXT,
    processed_at  TEXT,
    uploaded_at   TEXT,         -- 列表/队列排序键（失败置顶后按上传时间倒序）
    parse_status  TEXT,         -- 排序键（failed 置顶）+ 过滤
    review_status TEXT,
    summary       TEXT,         -- 展示摘要 JSON：列表/队列直接读它，不重建完整对象（不含大文本）
    payload       TEXT          -- 完整 JSON 快照：导出/详情/重启恢复的唯一数据源
);
CREATE INDEX IF NOT EXISTS idx_inv_no   ON invoices(invoice_no);
-- 注：uploaded_at / approve_status 上的索引在 init_db() 的列迁移(ALTER)之后创建，
-- 避免旧库（尚无这些列）在此处 executescript 时报 no such column。

CREATE TABLE IF NOT EXISTS file_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash     TEXT,
    file_name     TEXT,
    uploaded_at   TEXT,
    parse_method  TEXT,
    ocr_used      INTEGER,
    ocr_engine    TEXT,
    first_processed_at TEXT,
    recheck_at    TEXT,
    recheck_reason TEXT,
    risk_score    INTEGER,
    status        TEXT,
    review_status TEXT,
    approve_by    TEXT,
    approve_at    TEXT
);

-- 人工修改记录（本期不做交互，留空表保证结构一致）
CREATE TABLE IF NOT EXISTS change_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash     TEXT,
    field         TEXT,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    TEXT,
    changed_at    TEXT,
    reason        TEXT,
    source_file   TEXT,
    used_for_learning INTEGER,
    rule_type     TEXT,
    rule_status   TEXT
);

-- 学习规则（人工确认后沉淀，本期留空）
CREATE TABLE IF NOT EXISTS learned_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key     TEXT,
    category      TEXT,
    account       TEXT,
    source_file   TEXT,
    confirmed_by  TEXT,
    confirmed_at  TEXT,
    status        TEXT
);

-- 对账匹配：一条 match 关联一组发票 + 一组流水交易（支持 1:1 / 1:N / N:1 / N:N）
CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_key     TEXT UNIQUE,   -- 成员集合的确定性键：重跑幂等、不堆叠重复
    category      TEXT,          -- auto(高可信唯一) / confirm(中等/差额) / multi(多候选) / unmatched
    match_type    TEXT,          -- 1:1 / 1:N / N:1 / N:N / none
    match_score   INTEGER,
    currency      TEXT,
    invoice_total TEXT,
    matched_total TEXT,
    amount_delta  TEXT,
    basis         TEXT,          -- 匹配依据 JSON（命中的规则/键）
    status        TEXT,          -- proposed / confirmed / rejected
    created_at    TEXT,
    confirmed_at  TEXT,
    confirmed_by  TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_match_status ON matches(status);
CREATE INDEX IF NOT EXISTS idx_match_category ON matches(category);

CREATE TABLE IF NOT EXISTS match_members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      INTEGER,
    kind          TEXT,          -- 'invoice' | 'txn'
    invoice_hash  TEXT,          -- 发票=其 file_hash；交易=所属流水的 file_hash
    txn_index     INTEGER        -- 交易在流水 transactions 列表中的下标；发票为 NULL
);
CREATE INDEX IF NOT EXISTS idx_mm_match ON match_members(match_id);
CREATE INDEX IF NOT EXISTS idx_mm_inv ON match_members(invoice_hash);

-- 已对账成员预留表：member_key 唯一 → 数据库层原子防重复入账（并发确认也只允许一个成功）
CREATE TABLE IF NOT EXISTS reconciled_members (
    member_key TEXT PRIMARY KEY,   -- 'inv:<hash>' / 'txn:<hash>#<idx>'
    match_id   INTEGER,
    at         TEXT
);

-- 总账：会计分录头。金额一律 TEXT 存 Decimal 原文（绝不 float）。
CREATE TABLE IF NOT EXISTS journal_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_no      TEXT UNIQUE,       -- 凭证字号 YYYYMM-NNNN（过账时分配）
    date          TEXT,              -- 记账日期（ISO）
    memo          TEXT,
    source_kind   TEXT,              -- invoice/statement/manual/opening/closing/reversal
    source_hash   TEXT,              -- 来源凭证 file_hash（幂等：一来源至多一张有效分录）
    source_ref    TEXT,              -- 发票号/摘要
    status        TEXT,              -- Draft/Approved/Posted/Reversed
    total_debit   TEXT,
    total_credit  TEXT,
    period        TEXT,              -- 会计期间 YYYY-MM（软关闭用）
    reverses_id   INTEGER,           -- 红冲指向的原分录 id（普通分录为 NULL）
    settle_amount TEXT,              -- 结算分录：本次清账的票面额（明细辅助账用；非结算为 NULL）
    activity      TEXT,              -- 现金流活动类别 operating/investing/financing（动现金的分录必填；否则 NULL）
    created_by    TEXT,
    created_at    TEXT,
    posted_by     TEXT,
    posted_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_je_source ON journal_entries(source_kind, source_hash);
CREATE INDEX IF NOT EXISTS idx_je_status ON journal_entries(status);
CREATE INDEX IF NOT EXISTS idx_je_period ON journal_entries(period);

-- 总账：分录行（借/贷其一为正）。
CREATE TABLE IF NOT EXISTS journal_lines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      INTEGER,
    seq           INTEGER,           -- 行序（展示顺序稳定）
    account       TEXT,
    debit         TEXT,
    credit        TEXT,
    memo          TEXT
);
CREATE INDEX IF NOT EXISTS idx_jl_entry ON journal_lines(entry_id);
CREATE INDEX IF NOT EXISTS idx_jl_account ON journal_lines(account);
"""


@contextmanager
def connect():
    # timeout + busy_timeout：多人/线程池并发写时等待锁而非立刻报 database is locked
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _conn_or(conn):
    """复用外层传入的 conn（不 commit，由外层事务统一提交/回滚），或新开一个自动提交的连接。

    用于把"改状态/改数据"与"写审计轨迹"收进**同一事务**——同生共死，避免"已 Approved 却无
    change_log"（或反之）这类审计不一致（进程崩溃/异常中断时）。"""
    if conn is not None:
        yield conn
    else:
        with connect() as c:
            yield c


_initialized = False


def init_db() -> None:
    global _initialized
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")   # 读写不互相阻塞（并发/线程池友好）
        conn.executescript(_SCHEMA)
        # 迁移：把 learned_rules 扩成"通用人工确认规则库"（分类 + 对手方字段默认值）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(learned_rules)")}
        for col, ddl in (("rule_type", "rule_type TEXT DEFAULT 'classification'"),
                         ("target", "target TEXT"),
                         ("value", "value TEXT"),
                         ("confirm_count", "confirm_count INTEGER DEFAULT 1"),
                         ("note", "note TEXT"),      # 人工改写的整段说明（仅显示，覆盖自动大白话；不改行为）
                         ("doc_type", "doc_type TEXT DEFAULT 'invoice'"),  # 规则所属单据类型
                         ("scope", "scope TEXT")):   # 字段线索作用域：issuer(默认) / global(全局同义词)
            if col not in cols:
                conn.execute(f"ALTER TABLE learned_rules ADD COLUMN {ddl}")
        # 迁移：invoices 增列表/排序/分页用的列 + 展示摘要 JSON（列表/队列不再重建完整对象）
        icols = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)")}
        for col, ddl in (("uploaded_at", "uploaded_at TEXT"),
                         ("parse_status", "parse_status TEXT"),
                         ("review_status", "review_status TEXT"),
                         ("summary", "summary TEXT"),
                         ("doc_type", "doc_type TEXT DEFAULT 'invoice'")):   # 单据类型：invoice/statement
            if col not in icols:
                conn.execute(f"ALTER TABLE invoices ADD COLUMN {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_uploaded ON invoices(uploaded_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_approve ON invoices(approve_status)")
        # 迁移：journal_entries 增结算票面额列（增量2 结算引入；旧库无此列时补上）
        try:
            jcols = {r["name"] for r in conn.execute("PRAGMA table_info(journal_entries)")}
            if "settle_amount" not in jcols:
                conn.execute("ALTER TABLE journal_entries ADD COLUMN settle_amount TEXT")
            if "activity" not in jcols:
                conn.execute("ALTER TABLE journal_entries ADD COLUMN activity TEXT")
        except Exception:
            pass
        # 迁移：matches 增 reason（结构化未匹配/无需匹配原因）+ txn_type（交易类型）
        mcols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
        for col, ddl in (("reason", "reason TEXT"), ("txn_type", "txn_type TEXT")):
            if col not in mcols:
                conn.execute(f"ALTER TABLE matches ADD COLUMN {ddl}")
        # 回填旧行：从 payload 重建一次，填 uploaded_at/parse_status/review_status/summary（幂等，只补 NULL）
        for r in conn.execute("SELECT file_hash, payload FROM invoices WHERE summary IS NULL").fetchall():
            try:
                inv = Invoice.from_jsonable(json.loads(r["payload"]))
            except Exception:
                continue
            conn.execute(
                "UPDATE invoices SET uploaded_at=?, parse_status=?, review_status=?, summary=? WHERE file_hash=?",
                (inv.uploaded_at, inv.parse_status, inv.review_status,
                 json.dumps(_display_summary(inv), ensure_ascii=False), r["file_hash"]))
        # 回填：已确认匹配的成员写入 reconciled_members（幂等），使唯一约束对历史数据也生效
        try:
            for r in conn.execute(
                    """SELECT mm.kind, mm.invoice_hash, mm.txn_index, m.id AS mid
                       FROM match_members mm JOIN matches m ON mm.match_id=m.id
                       WHERE m.status='confirmed'""").fetchall():
                key = ("inv:" + r["invoice_hash"]) if r["kind"] == "invoice" \
                    else ("txn:%s#%s" % (r["invoice_hash"], r["txn_index"]))
                conn.execute("INSERT OR IGNORE INTO reconciled_members(member_key, match_id, at) VALUES (?,?,?)",
                             (key, r["mid"], None))
        except Exception:
            pass
    _initialized = True


def confirm_match_tx(match_id: int, member_keys, by: str, at: str):
    """**原子**确认：在一个事务里预留全部成员键（唯一约束）+ 置 matches.status='confirmed'。
    返回 None=成功；返回某 member_key=该成员已被占用（并发/重复），已回滚。"""
    _ensure_init()
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for k in member_keys:
            try:
                conn.execute("INSERT INTO reconciled_members(member_key, match_id, at) VALUES (?,?,?)",
                             (k, match_id, at))
            except sqlite3.IntegrityError:
                conn.rollback()
                return k
        conn.execute("UPDATE matches SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
                     (by, at, match_id))
        conn.commit()
        return None
    finally:
        conn.close()


def release_members(match_id: int) -> None:
    """释放某匹配预留的成员（拒绝/撤销时调用），使其可重新参与对账。"""
    _ensure_init()
    with connect() as conn:
        conn.execute("DELETE FROM reconciled_members WHERE match_id=?", (match_id,))


import re as _re


def norm_key(s) -> str:
    """对手方/匹配键规范化：小写 + 去非字母数字，使 'Halcyon Consulting Group' 稳定成同一键。"""
    return _re.sub(r"[^\w]", "", str(s or "").lower())


def _ensure_init() -> None:
    """惰性建表：任何入口（Web startup / CLI / 直接调用 pipeline）首次访问 DB 前
    确保表已存在，避免 `no such table` 报错。建表 SQL 幂等，开销仅一次。"""
    if not _initialized:
        init_db()


def find_duplicate_candidates(file_hash: str, invoice_no: Optional[str],
                              same_file: bool = True) -> list:
    """查重，返回疑似重复记录列表 [{file_hash, file_name, invoice_no, reason}]。

    两类重复：
    - **相同文件**（file_hash 已存在）：用户重复上传同一张发票。`same_file=False` 时跳过
      （供"系统内部重处理同一文件"调用，因入库 UPSERT=更新自身，不应自判重复）。
    - **相同发票号但不同文件**（同号、异 file_hash）：同一发票的另一份扫描/版本（可能多条）。
    """
    _ensure_init()
    out: list = []
    seen = set()
    with connect() as conn:
        if same_file:
            row = conn.execute(
                "SELECT file_hash, file_name, invoice_no, approve_status FROM invoices WHERE file_hash=?",
                (file_hash,)).fetchone()
            if row:
                out.append({"file_hash": row["file_hash"], "file_name": row["file_name"],
                            "invoice_no": row["invoice_no"], "approve_status": row["approve_status"],
                            "reason": "相同文件，重复上传"})
                seen.add(row["file_hash"])
        if invoice_no:
            for row in conn.execute(
                    "SELECT file_hash, file_name, invoice_no, approve_status FROM invoices "
                    "WHERE invoice_no=? AND file_hash<>?", (invoice_no, file_hash)).fetchall():
                if row["file_hash"] in seen:
                    continue
                out.append({"file_hash": row["file_hash"], "file_name": row["file_name"],
                            "invoice_no": row["invoice_no"], "approve_status": row["approve_status"],
                            "reason": f"相同发票号 {invoice_no}"})
                seen.add(row["file_hash"])
    return out


# ---- 人工确认规则库（规则即数据：人工修正→规则，后续相似情况更准）-------------
def learn_classification(match_key: str, category: Optional[str], account: Optional[str],
                         by: str = "reviewer") -> None:
    """学一条分类规则：对手方(match_key) → 科目。已存在则更新并 +1 确认次数（越确认越可信）。"""
    _ensure_init()
    if not match_key or not (category or account):
        return
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        row = conn.execute(
            "SELECT id, confirm_count FROM learned_rules WHERE rule_type='classification' AND match_key=?",
            (match_key,)).fetchone()
        if row:
            conn.execute("UPDATE learned_rules SET category=?, account=?, confirmed_by=?, "
                         "confirmed_at=?, confirm_count=? WHERE id=?",
                         (category, account, by, now, (row["confirm_count"] or 1) + 1, row["id"]))
        else:
            conn.execute("INSERT INTO learned_rules(rule_type, match_key, category, account, "
                         "confirmed_by, confirmed_at, status, confirm_count) "
                         "VALUES('classification',?,?,?,?,?,'pending',1)",
                         (match_key, category, account, by, now))


def learn_field_default(match_key: str, field: str, value: str, by: str = "reviewer") -> None:
    """学一条对手方字段默认值：对手方(match_key) + 字段 → 值（如币种恒为 EUR）。"""
    _ensure_init()
    if not match_key or not field or value in (None, ""):
        return
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        row = conn.execute("SELECT id, confirm_count FROM learned_rules WHERE rule_type='field_default' "
                           "AND match_key=? AND target=?", (match_key, field)).fetchone()
        if row:
            conn.execute("UPDATE learned_rules SET value=?, confirmed_by=?, confirmed_at=?, confirm_count=? "
                         "WHERE id=?", (value, by, now, (row["confirm_count"] or 1) + 1, row["id"]))
        else:
            conn.execute("INSERT INTO learned_rules(rule_type, match_key, target, value, "
                         "confirmed_by, confirmed_at, status, confirm_count) "
                         "VALUES('field_default',?,?,?,?,?,'pending',1)",
                         (match_key, field, value, by, now))


def learn_content_class(content: str, category: Optional[str], account: Optional[str],
                        by: str = "reviewer") -> None:
    """学一条**内容→科目**规则：从你确认分类的内容（如主导明细描述）沉淀，
    以后**相似内容**给参考建议（先 pending，启用后才作建议）。"""
    _ensure_init()
    if not content or not (category or account):
        return
    key = norm_key(content)[:200]
    if len(key) < 4:
        return
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        row = conn.execute("SELECT id, confirm_count FROM learned_rules WHERE rule_type='content_class' "
                           "AND match_key=?", (key,)).fetchone()
        if row:
            conn.execute("UPDATE learned_rules SET category=?, account=?, value=?, confirmed_by=?, "
                         "confirmed_at=?, confirm_count=? WHERE id=?",
                         (category, account, content, by, now, (row["confirm_count"] or 1) + 1, row["id"]))
        else:
            conn.execute("INSERT INTO learned_rules(rule_type, match_key, category, account, value, "
                         "confirmed_by, confirmed_at, status, confirm_count) "
                         "VALUES('content_class',?,?,?,?,?,?,'pending',1)",
                         (key, category, account, content, by, now))


def active_content_rules() -> list:
    """已启用的内容→科目规则。"""
    _ensure_init()
    with connect() as conn:
        rows = conn.execute("SELECT category, account, value, confirm_count FROM learned_rules "
                            "WHERE rule_type='content_class' AND status='active'").fetchall()
    return [dict(r) for r in rows]


def learn_line_split(match_key: str, pattern: str, sample: str,
                     by: str = "reviewer") -> None:
    """学一条**明细断句**规则：从你在明细里手工拆分大段描述的动作中，推断出
    分隔方式（pattern，如 newline/semicolon/bullet…），以后对该对手方相似的大段
    描述给**拆分建议**（先 pending，启用后才作建议；仍需人工预览采纳，不自动拆）。

    match_key=对手方(开票方) norm_key；target=分隔方式名；value=样例原文（供展示）。
    """
    _ensure_init()
    if not (match_key and pattern):
        return
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        row = conn.execute("SELECT id, confirm_count FROM learned_rules WHERE rule_type='line_split' "
                           "AND match_key=? AND target=?", (match_key, pattern)).fetchone()
        if row:
            conn.execute("UPDATE learned_rules SET value=?, confirmed_by=?, confirmed_at=?, "
                         "confirm_count=? WHERE id=?",
                         (sample, by, now, (row["confirm_count"] or 1) + 1, row["id"]))
        else:
            conn.execute("INSERT INTO learned_rules(rule_type, match_key, target, value, "
                         "confirmed_by, confirmed_at, status, confirm_count) "
                         "VALUES('line_split',?,?,?,?,?,'pending',1)",
                         (match_key, pattern, sample, by, now))


def active_line_split_rules(match_key: str) -> list:
    """某对手方已启用的明细断句规则（返回 [{pattern, sample, confirm_count}]）。"""
    _ensure_init()
    if not match_key:
        return []
    with connect() as conn:
        rows = conn.execute("SELECT target, value, confirm_count FROM learned_rules "
                            "WHERE rule_type='line_split' AND match_key=? AND status='active'",
                            (match_key,)).fetchall()
    return [{"pattern": r["target"], "sample": r["value"], "confirm_count": r["confirm_count"]}
            for r in rows]


def learn_field_locator(match_key: str, field: str, label: str, fp: str,
                        by: str = "reviewer") -> None:
    """学一条**字段定位线索**：作用域(对手方 match_key) + 字段 → **标签关键词** label（+类型指纹 fp）。
    学的是"哪个标签旁边是该字段的值"（软先验），**不是坐标/固定值**。同 (作用域,字段,标签) 再见 +1
    确认次数（越确认越可信）；同字段可学**多个**候选标签（不锁死一种）。先 pending、人工启用才生效。"""
    _ensure_init()
    if not (field and label):
        return
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        row = conn.execute("SELECT id, confirm_count FROM learned_rules WHERE rule_type='field_locator' "
                           "AND match_key=? AND target=? AND value=?",
                           (match_key or "", field, label)).fetchone()
        if row:
            conn.execute("UPDATE learned_rules SET account=?, confirmed_by=?, confirmed_at=?, confirm_count=? "
                         "WHERE id=?", (fp, by, now, (row["confirm_count"] or 1) + 1, row["id"]))
        else:
            conn.execute("INSERT INTO learned_rules(rule_type, match_key, target, value, account, "
                         "confirmed_by, confirmed_at, status, confirm_count) "
                         "VALUES('field_locator',?,?,?,?,?,?,'pending',1)",
                         (match_key or "", field, label, fp, by, now))


def active_field_locators(match_key: str, fp: Optional[str] = None) -> list:
    """已启用的字段定位线索：命中"对手方作用域"**或**"类型指纹"即返回（软匹配、多候选）。
    返回 [{field, label, fp, confirm_count}]。"""
    _ensure_init()
    with connect() as conn:
        rows = conn.execute("SELECT match_key, target, value, account, confirm_count, scope "
                            "FROM learned_rules WHERE rule_type='field_locator' AND status='active'").fetchall()
    out = []
    for r in rows:
        # 全局同义词对所有发票生效；否则按 对手方 或 类型指纹 软匹配
        if r["scope"] == "global" or (match_key and r["match_key"] == match_key) or (fp and r["account"] == fp):
            out.append({"field": r["target"], "label": r["value"], "fp": r["account"],
                        "confirm_count": r["confirm_count"] or 1, "scope": r["scope"]})
    return out


def learn_multi_invoice(fp: str, value: str, issuer: str = "", by: str = "reviewer") -> None:
    """学『该版面(指纹)倾向 单张/多张』**软先验**（pending 待启用，非死规则）。

    match_key=版面指纹 fp；value='single'|'multi'；target=开票方名（展示用）。
    同指纹再见：倾向不变则确认次数+1（越确认越可信）；倾向**翻转**则重置为待确认（新证据、需人工再启用）。
    """
    _ensure_init()
    if not fp or value not in ("single", "multi"):
        return
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        row = conn.execute("SELECT id, confirm_count, value, status FROM learned_rules "
                           "WHERE rule_type='multi_invoice' AND match_key=?", (fp,)).fetchone()
        if row:
            if row["value"] == value:
                cnt, status = (row["confirm_count"] or 1) + 1, row["status"]
            else:
                cnt, status = 1, "pending"          # 倾向翻转 → 重新待确认
            conn.execute("UPDATE learned_rules SET value=?, target=?, confirmed_by=?, confirmed_at=?, "
                         "confirm_count=?, status=? WHERE id=?",
                         (value, issuer, by, now, cnt, status, row["id"]))
        else:
            conn.execute("INSERT INTO learned_rules(rule_type, match_key, value, target, "
                         "confirmed_by, confirmed_at, status, confirm_count) "
                         "VALUES('multi_invoice',?,?,?,?,?,'pending',1)",
                         (fp, value, issuer, by, now))


def multi_invoice_prior(fp: str) -> Optional[str]:
    """已启用的『单张/多张』软先验：按版面指纹命中 → 'single'|'multi'，无则 None。"""
    _ensure_init()
    if not fp:
        return None
    with connect() as conn:
        r = conn.execute("SELECT value FROM learned_rules WHERE rule_type='multi_invoice' "
                         "AND match_key=? AND status='active' ORDER BY confirm_count DESC LIMIT 1",
                         (fp,)).fetchone()
    return r["value"] if r else None


def lookup_classification(match_key: str) -> Optional[dict]:
    _ensure_init()
    if not match_key:
        return None
    with connect() as conn:
        r = conn.execute("SELECT category, account, confirm_count FROM learned_rules "
                         "WHERE rule_type='classification' AND match_key=? AND status='active'",
                         (match_key,)).fetchone()
        return dict(r) if r else None


def lookup_field_defaults(match_key: str) -> dict:
    """返回 {字段: (值, 确认次数)}。"""
    _ensure_init()
    if not match_key:
        return {}
    with connect() as conn:
        rows = conn.execute("SELECT target, value, confirm_count FROM learned_rules "
                            "WHERE rule_type='field_default' AND match_key=? AND status='active'",
                            (match_key,)).fetchall()
    return {r["target"]: (r["value"], r["confirm_count"]) for r in rows}


def list_learned(doc_type: Optional[str] = None) -> list:
    """列出全部规则（含 pending 待确认 与 active 已启用），供管理页区分展示。可按单据类型过滤。"""
    _ensure_init()
    where = "WHERE COALESCE(doc_type,'invoice')=?" if doc_type else ""
    params = [doc_type] if doc_type else []
    with connect() as conn:
        rows = conn.execute("SELECT id, rule_type, match_key, category, account, target, value, note, "
                            "confirm_count, status, scope, confirmed_by, confirmed_at FROM learned_rules "
                            + where + " ORDER BY (status='active'), confirmed_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def learned_class_pairs() -> list:
    """已学的 (分类, 会计科目) 去重对（分类/内容规则，含 pending）——供分类下拉候选补充。"""
    _ensure_init()
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category, account FROM learned_rules "
            "WHERE rule_type IN ('classification','content_class') "
            "AND (category IS NOT NULL OR account IS NOT NULL)").fetchall()
    return [(r["category"], r["account"]) for r in rows]


def enable_learned(rule_id: int, make_global: bool = False) -> bool:
    """把一条待确认规则启用（人工审过才生效）。make_global=True 时把"字段线索"设为**全局同义词**
    （对所有发票生效，不再限同开票方/同指纹）；对其它规则类型 scope 无实际作用。"""
    _ensure_init()
    with connect() as conn:
        cur = conn.execute("UPDATE learned_rules SET status='active', scope=? WHERE id=?",
                           ("global" if make_global else "issuer", rule_id))
        return cur.rowcount > 0


def delete_learned(rule_id: int) -> bool:
    _ensure_init()
    with connect() as conn:
        cur = conn.execute("DELETE FROM learned_rules WHERE id=?", (rule_id,))
        return cur.rowcount > 0


_EDITABLE_LEARNED = ("category", "account", "target", "value", "match_key", "note")


def update_learned(rule_id: int, fields: dict) -> bool:
    """人工修正一条学习规则的可编辑字段（启用前纠正捕获不准的值/科目/标签/字段）。
    仅允许改 category/account/target/value/match_key；返回是否更新到行。"""
    _ensure_init()
    sets = {k: v for k, v in (fields or {}).items() if k in _EDITABLE_LEARNED}
    if not sets:
        return False
    cols = ", ".join(f"{k}=?" for k in sets)
    with connect() as conn:
        cur = conn.execute(f"UPDATE learned_rules SET {cols} WHERE id=?",
                           list(sets.values()) + [rule_id])
        return cur.rowcount > 0


def delete_invoice(file_hash: str) -> bool:
    """硬删除一条发票记录（仅 invoices 行）；change_log/file_audit 审计轨迹保留。返回是否删除。"""
    _ensure_init()
    with connect() as conn:
        cur = conn.execute("DELETE FROM invoices WHERE file_hash=?", (file_hash,))
        conn.commit()
        return cur.rowcount > 0


def find_duplicate(file_hash: str, invoice_no: Optional[str],
                   same_file: bool = True) -> Optional[str]:
    """查重，返回已存在记录的描述或 None（DUPLICATE 提示文案）。"""
    cands = find_duplicate_candidates(file_hash, invoice_no, same_file=same_file)
    if not cands:
        return None
    return "；".join(f"{c['file_name']} ({c['reason']})" for c in cands)


def _display_summary(inv: Invoice) -> dict:
    """列表/队列展示所需的紧凑摘要（**不含** raw_pdf_text/raw_ocr_text 等大文本）。
    存入 invoices.summary，列表/队列直接读它、不重建完整对象。"""
    total = inv.f("total_due").value
    cb = inv.f("closing_balance").value
    return {
        "file_hash": inv.file_hash,
        "file_name": inv.file_name,
        "doc_type": inv.doc_type or "invoice",
        "invoice_no": inv.f("invoice_no").value,
        "invoice_date": inv.f("invoice_date").value,
        "currency_settlement": inv.f("currency_settlement").value,
        "total_due": str(total) if total is not None else None,
        # 流水列表展示：银行/账号/期间/笔数/期末余额
        "bank_name": inv.f("bank_name").value,
        "bank_account_no": inv.f("bank_account_no").value,
        "statement_period_start": inv.f("statement_period_start").value,
        "statement_period_end": inv.f("statement_period_end").value,
        "txn_count": len(inv.transactions),
        "closing_balance": str(cb) if cb is not None else None,
        "category": inv.classification.category,
        "parse_method": inv.parse_method,
        "parse_status": inv.parse_status,
        "uploaded_at": inv.uploaded_at,
        "ocr_quality": round(inv.ocr_quality, 4),
        "risk_score": inv.risk_score,
        "validation_status": inv.validation_status,
        "review_status": inv.review_status,
        "approve_status": inv.approve_status or "Pending",
        "needs_manual_review": inv.needs_manual_review,
        "critical_review": inv.critical_review,
        # 多发票合集：同一 source_file_hash 且 segment_total>1 的记录在审核页归为一组折叠展示
        "source_file_hash": inv.source_file_hash,
        "source_file_name": inv.source_file_name,
        "segment_index": inv.segment_index,
        "segment_total": inv.segment_total,
        "is_duplicate": any(i.code == "DUPLICATE" for i in inv.issues),
        "issues": [{"severity": i.severity, "code": i.code, "message": i.message} for i in inv.issues],
    }


def load_summaries(limit: Optional[int] = None, offset: int = 0,
                   status: Optional[str] = None, doc_type: Optional[str] = None) -> list:
    """列表/队列取数：只读 summary（紧凑、不含大文本），**不重建完整 Invoice 对象**。
    排序：提取失败置顶，其次上传时间倒序。支持分页、按 approve_status 过滤、按 doc_type 过滤。"""
    _ensure_init()
    conds, params = [], []
    if status:
        conds.append("approve_status=?"); params.append(status)
    if doc_type:
        conds.append("COALESCE(doc_type,'invoice')=?"); params.append(doc_type)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = ("SELECT file_hash, summary FROM invoices " + where +
           " ORDER BY (parse_status='failed') DESC, uploaded_at DESC, id DESC")
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    out = []
    with connect() as conn:
        for row in conn.execute(sql, params):
            if row["summary"]:
                try:
                    out.append(json.loads(row["summary"]))
                except Exception as e:
                    logger.error("摘要损坏，未列出 file_hash=%s: %s", row["file_hash"], e)
                    continue
    return out


def siblings_by_source(source_file_hash: str) -> list:
    """取同一源文件切出的所有发票摘要（合集视图 / 重新切分用），按 segment_index 排序。"""
    _ensure_init()
    if not source_file_hash:
        return []
    out = []
    with connect() as conn:
        for row in conn.execute("SELECT file_hash, summary FROM invoices"):
            if not row["summary"]:
                continue
            try:
                s = json.loads(row["summary"])
            except Exception as e:
                logger.error("摘要损坏 file_hash=%s: %s", row["file_hash"], e)
                continue
            if s.get("source_file_hash") == source_file_hash:
                out.append(s)
    out.sort(key=lambda s: s.get("segment_index") or 0)
    return out


def count_invoices(status: Optional[str] = None, doc_type: Optional[str] = None) -> int:
    _ensure_init()
    conds, params = [], []
    if status:
        conds.append("approve_status=?"); params.append(status)
    if doc_type:
        conds.append("COALESCE(doc_type,'invoice')=?"); params.append(doc_type)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with connect() as conn:
        return conn.execute("SELECT count(*) c FROM invoices " + where, params).fetchone()["c"]


def status_counts(doc_type: Optional[str] = None) -> dict:
    """各审核状态计数（队列头部进度），SQL 聚合、不重建对象。可按单据类型过滤。"""
    _ensure_init()
    out: dict = {}
    where = "WHERE COALESCE(doc_type,'invoice')=?" if doc_type else ""
    params = [doc_type] if doc_type else []
    with connect() as conn:
        for r in conn.execute("SELECT COALESCE(approve_status,'Pending') s, count(*) c "
                              "FROM invoices " + where + " GROUP BY s", params):
            out[r["s"]] = r["c"]
    return out


def save_invoice(inv: Invoice) -> int:
    _ensure_init()
    payload = json.dumps(inv.to_jsonable(), ensure_ascii=False)
    summary = json.dumps(_display_summary(inv), ensure_ascii=False)
    total = inv.f("total_due").value
    total_str = str(total) if total is not None else None
    with connect() as conn:
        # invoices：按 file_hash UPSERT —— 同一文件重复处理则更新该行（不堆叠重复）
        cur = conn.execute(
            """INSERT INTO invoices
               (file_hash, file_name, invoice_no, total_due, risk_score,
                validation_status, approve_status, processed_at,
                uploaded_at, parse_status, review_status, summary, payload, doc_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(file_hash) DO UPDATE SET
                file_name=excluded.file_name, invoice_no=excluded.invoice_no,
                total_due=excluded.total_due, risk_score=excluded.risk_score,
                validation_status=excluded.validation_status,
                approve_status=excluded.approve_status,
                processed_at=excluded.processed_at,
                uploaded_at=excluded.uploaded_at, parse_status=excluded.parse_status,
                review_status=excluded.review_status, summary=excluded.summary,
                payload=excluded.payload, doc_type=excluded.doc_type""",
            (inv.file_hash, inv.file_name, inv.f("invoice_no").value,
             total_str, inv.risk_score, inv.validation_status,
             inv.approve_status, inv.processed_at,
             inv.uploaded_at, inv.parse_status, inv.review_status, summary, payload,
             inv.doc_type or "invoice"),
        )
        # file_audit：审计轨迹，每次处理都追加一行（不去重，保留完整历史）
        conn.execute(
            """INSERT INTO file_audit
               (file_hash, file_name, uploaded_at, parse_method, ocr_used, ocr_engine,
                first_processed_at, risk_score, status, review_status)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (inv.file_hash, inv.file_name, inv.uploaded_at, inv.parse_method,
             int(inv.ocr_used), inv.ocr_engine, inv.processed_at, inv.risk_score,
             inv.parse_status, inv.review_status),
        )
        return cur.lastrowid


def load_all_invoices() -> "dict[str, Invoice]":
    """从 DB 的 JSON 快照重建全部发票，返回 {file_hash: Invoice}。

    导出 / 页面列表 / 重启恢复的唯一数据源。损坏的快照跳过。
    """
    _ensure_init()
    out: "dict[str, Invoice]" = {}
    with connect() as conn:
        for row in conn.execute("SELECT file_hash, payload FROM invoices ORDER BY id"):
            try:
                inv = Invoice.from_jsonable(json.loads(row["payload"]))
            except Exception as e:
                # 损坏快照**不静默吞掉**（财务系统"凭空少一张凭证且无告警"最危险）：记 error 带 file_hash
                logger.error("发票快照损坏，已跳过 file_hash=%s: %s: %s",
                             row["file_hash"], type(e).__name__, e)
                continue
            out[inv.file_hash] = inv
    return out


# ---- 人工审核（第五模块）数据访问 -------------------------------------

def get_invoice(file_hash: str) -> Optional[Invoice]:
    """按文件哈希取单张发票（从 payload 快照重建）。"""
    _ensure_init()
    with connect() as conn:
        row = conn.execute("SELECT payload FROM invoices WHERE file_hash=?",
                           (file_hash,)).fetchone()
    if not row:
        return None
    try:
        return Invoice.from_jsonable(json.loads(row["payload"]))
    except Exception as e:
        logger.error("发票快照损坏 file_hash=%s: %s: %s", file_hash, type(e).__name__, e)
        return None


def resave_invoice(inv: Invoice, conn=None) -> None:
    """回写改后的发票快照（更新 payload 与冗余列），**不**追加 file_audit。
    用于人工审核改字段 / 改状态后回写（审核轨迹由 record_review 单独追加）。

    每次回写把乐观锁版本 rev +1：审核界面读详情时记下 rev，提交修改时带上；若期间
    别人已改（DB 里的 rev 已推进），后端据此判冲突、拒绝「后写覆盖先写」。

    传入 `conn` 可与审计留痕收进同一事务（见 `resave_and_log` / `_conn_or`）。
    """
    _ensure_init()
    inv.rev = (inv.rev or 0) + 1
    payload = json.dumps(inv.to_jsonable(), ensure_ascii=False)
    summary = json.dumps(_display_summary(inv), ensure_ascii=False)
    total = inv.f("total_due").value
    total_str = str(total) if total is not None else None
    with _conn_or(conn) as c:
        c.execute(
            """UPDATE invoices SET file_name=?, invoice_no=?, total_due=?,
               risk_score=?, validation_status=?, approve_status=?,
               uploaded_at=?, parse_status=?, review_status=?, summary=?, payload=?
               WHERE file_hash=?""",
            (inv.file_name, inv.f("invoice_no").value, total_str, inv.risk_score,
             inv.validation_status, inv.approve_status,
             inv.uploaded_at, inv.parse_status, inv.review_status, summary, payload,
             inv.file_hash),
        )


def log_change(file_hash: str, field: str, old_value, new_value,
               changed_by: str, reason: str = "", changed_at: str = "", conn=None) -> None:
    """人工修改留痕到 change_log（只追加、不可删；审计不可篡改）。

    传入 `conn` 可与状态变更收进同一事务（审计与状态同生共死）。"""
    _ensure_init()
    with _conn_or(conn) as c:
        c.execute(
            """INSERT INTO change_log
               (file_hash, field, old_value, new_value, changed_by, changed_at, reason)
               VALUES (?,?,?,?,?,?,?)""",
            (file_hash, field,
             None if old_value is None else str(old_value),
             None if new_value is None else str(new_value),
             changed_by, changed_at, reason),
        )


def resave_and_log(inv: Invoice, field: str, old_value, new_value, changed_by: str,
                   reason: str = "", changed_at: str = "") -> None:
    """**原子**：回写发票快照 + 追加一条 change_log（同一事务）。

    人工改字段/明细后调它，替代过去"先 log_change 再 resave"的两次独立提交——
    避免进程中断时出现「快照已改却无留痕」（或反之）的审计不一致。"""
    with connect() as conn:
        resave_invoice(inv, conn)
        log_change(inv.file_hash, field, old_value, new_value, changed_by, reason, changed_at, conn)


def list_changes(file_hash: str) -> list:
    """取某发票的全部人工修改 / 审核动作轨迹（时间正序）。"""
    _ensure_init()
    with connect() as conn:
        rows = conn.execute(
            """SELECT field, old_value, new_value, changed_by, changed_at, reason
               FROM change_log WHERE file_hash=? ORDER BY id""", (file_hash,)).fetchall()
    return [dict(r) for r in rows]


def record_review(file_hash: str, status: str, review_status: str,
                  approve_by: str, approve_at: str, conn=None) -> None:
    """审核动作留痕：file_audit 追加一行（审核轨迹，只追加、不可删）。

    传入 `conn` 可与状态变更收进同一事务。"""
    _ensure_init()
    with _conn_or(conn) as c:
        c.execute(
            """INSERT INTO file_audit
               (file_hash, status, review_status, approve_by, approve_at)
               VALUES (?,?,?,?,?)""",
            (file_hash, status, review_status, approve_by, approve_at),
        )


# ---- 对账匹配存储 ----------------------------------------------------------
def _match_key(inv_hashes, txn_refs) -> str:
    """成员集合的确定性键：排序后拼接，保证重跑同一组合得到同一 key（幂等去重）。"""
    a = ",".join(sorted(inv_hashes))
    b = ",".join(sorted("%s#%s" % (h, i) for h, i in txn_refs))
    return "I[" + a + "]T[" + b + "]"


def confirmed_member_refs() -> tuple:
    """已确认(confirmed)匹配占用的成员：返回 (发票 hash 集合, {(流水hash, 交易下标)} 集合)。
    重新匹配时把它们排除出候选池，避免动已定案的关联。"""
    _ensure_init()
    inv, txn = set(), set()
    with connect() as conn:
        rows = conn.execute(
            """SELECT mm.kind, mm.invoice_hash, mm.txn_index
               FROM match_members mm JOIN matches m ON mm.match_id=m.id
               WHERE m.status='confirmed'""").fetchall()
    for r in rows:
        if r["kind"] == "invoice":
            inv.add(r["invoice_hash"])
        else:
            txn.add((r["invoice_hash"], r["txn_index"]))
    return inv, txn


def confirmed_txn_kinds() -> dict:
    """已确认匹配里每笔交易的**处理类型**：{(stmt_hash, txn_index): 'reconciled'|'no_invoice'}。
    含发票的确认=对账(reconciled)；单边无发票的确认=确认无需发票(no_invoice)。"""
    _ensure_init()
    out = {}
    with connect() as conn:
        rows = conn.execute("SELECT id FROM matches WHERE status='confirmed'").fetchall()
        for row in rows:
            mem = conn.execute("SELECT kind, invoice_hash, txn_index FROM match_members WHERE match_id=?",
                               (row["id"],)).fetchall()
            has_inv = any(m["kind"] == "invoice" for m in mem)
            for m in mem:
                if m["kind"] == "txn":
                    out[(m["invoice_hash"], m["txn_index"])] = "reconciled" if has_inv else "no_invoice"
    return out


def rejected_pairs() -> set:
    """已被人工判「不成立」的匹配里的**发票×交易**配对集合：{(invoice_hash, stmt_hash, txn_index)}。
    重新匹配时作黑名单，避免又把同一对配在一起（让它们各自落回未匹配、不消失）。"""
    _ensure_init()
    out = set()
    with connect() as conn:
        rows = conn.execute("SELECT id FROM matches WHERE status='rejected'").fetchall()
        for row in rows:
            mem = conn.execute("SELECT kind, invoice_hash, txn_index FROM match_members WHERE match_id=?",
                               (row["id"],)).fetchall()
            invs = [m["invoice_hash"] for m in mem if m["kind"] == "invoice"]
            txns = [(m["invoice_hash"], m["txn_index"]) for m in mem if m["kind"] == "txn"]
            for ih in invs:
                for (th, ti) in txns:
                    out.add((ih, th, ti))
    return out


def clear_proposed_matches() -> None:
    """清掉所有未确认(proposed)匹配及其成员；确认/拒绝的保留。"""
    _ensure_init()
    with connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM matches WHERE status='proposed'")]
        if ids:
            qs = ",".join("?" * len(ids))
            conn.execute("DELETE FROM match_members WHERE match_id IN (%s)" % qs, ids)
            conn.execute("DELETE FROM matches WHERE id IN (%s)" % qs, ids)


def save_match(m: dict) -> int:
    """写入一条匹配提案。m: category/match_type/match_score/currency/invoice_total/
    matched_total/amount_delta/basis(list)/status/created_at/invoices[hash]/txns[(hash,idx)]。
    返回 match_id；match_key 冲突（同组合已存在）则忽略并返回 0。"""
    _ensure_init()
    key = _match_key(m.get("invoices", []), m.get("txns", []))
    with connect() as conn:
        exists = conn.execute("SELECT id FROM matches WHERE match_key=?", (key,)).fetchone()
        if exists:
            return 0
        cur = conn.execute(
            """INSERT INTO matches (match_key, category, match_type, match_score, currency,
                 invoice_total, matched_total, amount_delta, basis, status, created_at, reason, txn_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, m.get("category"), m.get("match_type"), int(m.get("match_score", 0)),
             m.get("currency"), _s_num(m.get("invoice_total")), _s_num(m.get("matched_total")),
             _s_num(m.get("amount_delta")), json.dumps(m.get("basis", []), ensure_ascii=False),
             m.get("status", "proposed"), m.get("created_at"), m.get("reason"), m.get("txn_type")))
        mid = cur.lastrowid
        for h in m.get("invoices", []):
            conn.execute("INSERT INTO match_members (match_id, kind, invoice_hash, txn_index) VALUES (?,?,?,?)",
                         (mid, "invoice", h, None))
        for h, i in m.get("txns", []):
            conn.execute("INSERT INTO match_members (match_id, kind, invoice_hash, txn_index) VALUES (?,?,?,?)",
                         (mid, "txn", h, int(i)))
    return mid


def _s_num(v):
    return None if v in (None, "") else str(v)


def _match_row(conn, r) -> dict:
    mem = conn.execute("SELECT kind, invoice_hash, txn_index FROM match_members WHERE match_id=?", (r["id"],)).fetchall()
    try:
        basis = json.loads(r["basis"]) if r["basis"] else []
    except Exception:
        basis = []
    return {"id": r["id"], "category": r["category"], "match_type": r["match_type"],
            "match_score": r["match_score"], "currency": r["currency"],
            "invoice_total": r["invoice_total"], "matched_total": r["matched_total"],
            "amount_delta": r["amount_delta"], "basis": basis, "status": r["status"],
            "created_at": r["created_at"], "confirmed_at": r["confirmed_at"],
            "confirmed_by": r["confirmed_by"], "note": r["note"],
            "reason": (r["reason"] if "reason" in r.keys() else None),
            "txn_type": (r["txn_type"] if "txn_type" in r.keys() else None),
            "invoices": [m["invoice_hash"] for m in mem if m["kind"] == "invoice"],
            "txns": [(m["invoice_hash"], m["txn_index"]) for m in mem if m["kind"] == "txn"]}


def list_matches(category=None, status=None) -> list:
    _ensure_init()
    conds, params = [], []
    if category:
        conds.append("category=?"); params.append(category)
    if status:
        conds.append("status=?"); params.append(status)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM matches %s ORDER BY match_score DESC, id" % where, params).fetchall()
        return [_match_row(conn, r) for r in rows]


def get_match(match_id: int) -> Optional[dict]:
    _ensure_init()
    with connect() as conn:
        r = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        return _match_row(conn, r) if r else None


def set_match_status(match_id: int, status: str, by: str = "reviewer",
                     confirmed_at: Optional[str] = None) -> None:
    _ensure_init()
    with connect() as conn:
        conn.execute("UPDATE matches SET status=?, confirmed_by=?, confirmed_at=? WHERE id=?",
                     (status, by, confirmed_at, match_id))


def delete_match(match_id: int) -> None:
    """彻底删除一条匹配及其成员/预留（撤销拒绝时用：删掉 rejected 记录=移出黑名单）。"""
    _ensure_init()
    with connect() as conn:
        conn.execute("DELETE FROM match_members WHERE match_id=?", (match_id,))
        conn.execute("DELETE FROM reconciled_members WHERE match_id=?", (match_id,))
        conn.execute("DELETE FROM matches WHERE id=?", (match_id,))


def match_counts() -> dict:
    """各类别未确认(proposed)匹配计数 + 已确认数，供对账页汇总。"""
    _ensure_init()
    out = {"auto": 0, "confirm": 0, "multi": 0, "unmatched": 0, "no_match_needed": 0,
           "confirmed": 0, "rejected": 0}
    with connect() as conn:
        for r in conn.execute("SELECT category, COUNT(*) n FROM matches WHERE status='proposed' GROUP BY category"):
            out[r["category"]] = r["n"]
        for st in ("confirmed", "rejected"):
            c = conn.execute("SELECT COUNT(*) n FROM matches WHERE status=?", (st,)).fetchone()
            out[st] = c["n"]
    return out
