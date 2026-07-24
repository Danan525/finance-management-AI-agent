"""运维加固：数据库备份 / 完整性校验 / WAL 收敛 / 磁盘留存清理。

动机（对应体检项 ①②③）：
- 「不出机」意味着没有云端兜底——数据库损坏/误删就永久丢失，故需本地定时快照 + 保留 N 份；
- WAL 文件与页面图片缓存、导出 xlsx 都会**只增不减**，需按数量上限清理，防磁盘占满拖垮服务。

全部**纯本地、无外部依赖**，函数参数化（便于测试与脚本复用），异常不外抛以免拖垮启动。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("finance")


# ---- 数据库：完整性 / 收敛 / 备份 ---------------------------------------

def integrity_check(db_path: Path) -> bool:
    """PRAGMA integrity_check：库文件是否完好。文件不存在视为「无需检查」→ True。"""
    db_path = Path(db_path)
    if not db_path.exists():
        return True
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def checkpoint(db_path: Path) -> None:
    """WAL 收敛（TRUNCATE）：把 -wal 合并回主库并截断，避免 -wal 无限增长。"""
    db_path = Path(db_path)
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()


def backup_db(db_path: Path, backup_dir: Path, keep: int = 14,
              stamp: Optional[str] = None) -> Optional[Path]:
    """用 SQLite 在线备份 API 生成一致快照（WAL 下也安全），并保留最近 keep 份。

    返回快照路径；源库不存在则返回 None。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{db_path.stem}-{stamp}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)          # 在线备份：即使有并发写也得到一致快照
        finally:
            dst.close()
    finally:
        src.close()
    prune_by_count(backup_dir, f"{db_path.stem}-*.db", keep)
    return dest


def _newest_mtime(directory: Path, pattern: str) -> Optional[float]:
    files = list(Path(directory).glob(pattern))
    return max((f.stat().st_mtime for f in files), default=None)


def needs_backup(backup_dir: Path, stem: str, min_interval_h: float) -> bool:
    """最近快照是否已超过 min_interval_h 小时（无快照 → 需要）。"""
    newest = _newest_mtime(Path(backup_dir), f"{stem}-*.db")
    if newest is None:
        return True
    return datetime.now() - datetime.fromtimestamp(newest) >= timedelta(hours=min_interval_h)


# ---- 磁盘留存：按数量上限清理（保留最新）--------------------------------

def prune_by_count(directory: Path, pattern: str, keep: int) -> int:
    """保留 directory 下匹配 pattern 的最新 keep 个文件，其余按 mtime 从旧到新删除。

    返回删除数量。目录不存在或 keep<0 时不动。
    """
    directory = Path(directory)
    if keep < 0 or not directory.exists():
        return 0
    files = [f for f in directory.glob(pattern) if f.is_file()]
    if len(files) <= keep:
        return 0
    files.sort(key=lambda f: f.stat().st_mtime)          # 最旧在前
    removed = 0
    for f in files[:len(files) - keep]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def prune_page_cache(max_files: Optional[int] = None) -> int:
    return prune_by_count(config.PAGE_CACHE_DIR, "*.png",
                          config.PAGE_CACHE_MAX_FILES if max_files is None else max_files)


def prune_exports(keep: Optional[int] = None) -> int:
    return prune_by_count(config.EXPORT_DIR, "*.xlsx",
                          config.EXPORT_KEEP if keep is None else keep)


def prune_orphan_uploads(retention_days: Optional[int] = None) -> int:
    """清理 uploads/ 里**无任何记录引用**且**超过保留期**的孤儿文件。

    孤儿来源：撤销/重切后被 `delete_invoice` 删掉记录的遗留、转换中间件（office→pdf 等）。
    **红线：被记录引用的原件（含 failed 记录）永久保留、绝不删**——判据用 **file_hash 列前缀**
    （文件恒命名 `<hash12>_原名`；凡前缀在 `invoices.file_hash` 集合里即保留），**不依赖 payload
    解码**（避免某条 payload 损坏就误删其原件）。保留期避免误删在途上传（刚落盘、记录未写完）。
    """
    from . import db
    up = config.UPLOAD_DIR
    if not up.exists():
        return 0
    days = config.UPLOAD_RETENTION_DAYS if retention_days is None else retention_days
    try:
        with db.connect() as conn:
            prefixes = {(r["file_hash"] or "")[:12]
                        for r in conn.execute("SELECT file_hash FROM invoices")}
    except Exception:
        logger.exception("prune_orphan_uploads 取引用集失败，跳过（安全优先，不删）")
        return 0
    if not prefixes:                       # 取不到任何引用（空库/异常）→ 保守不删，避免误清
        return 0
    cutoff = datetime.now().timestamp() - days * 86400
    removed = 0
    for p in up.iterdir():
        if not p.is_file():
            continue
        prefix = p.name.split("_", 1)[0]   # 命名 `<hash12>_原名`；取哈希前缀
        if prefix in prefixes:             # 被某条记录引用 → 保留（含 failed 记录的原件）
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info("prune_orphan_uploads: 清理孤儿上传文件 %d 个（无记录引用且超 %d 天）", removed, days)
    return removed


# ---- 启动/定时统一入口 ---------------------------------------------------

def startup_maintenance() -> dict:
    """服务启动时跑一遍：完整性校验（异常仅告警）→ WAL 收敛 → 每日快照 → 清理缓存/导出。

    best-effort：任何一步失败都不应阻断启动。返回执行摘要（便于日志/测试）。
    """
    out = {"integrity_ok": True, "backed_up": False,
           "pages_pruned": 0, "exports_pruned": 0, "uploads_pruned": 0}
    try:
        out["integrity_ok"] = integrity_check(config.DB_PATH)
        if not out["integrity_ok"]:
            logger.error("数据库完整性校验未通过：%s（请尽快用最近备份恢复）", config.DB_PATH)
        checkpoint(config.DB_PATH)
        if needs_backup(config.BACKUP_DIR, Path(config.DB_PATH).stem, config.BACKUP_MIN_INTERVAL_H):
            dest = backup_db(config.DB_PATH, config.BACKUP_DIR, config.BACKUP_KEEP)
            out["backed_up"] = dest is not None
        out["pages_pruned"] = prune_page_cache()
        out["exports_pruned"] = prune_exports()
        out["uploads_pruned"] = prune_orphan_uploads()
    except Exception:
        logger.exception("启动维护任务出错（不影响服务启动）")
    return out
