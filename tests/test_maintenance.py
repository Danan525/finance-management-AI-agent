"""运维加固：数据库备份/完整性/WAL 收敛 + 磁盘留存清理 + 访问日志降噪。"""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core import config, db, maintenance


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._saved = {k: getattr(config, k) for k in
                       ("DATA_ROOT", "DB_PATH", "BACKUP_DIR", "PAGE_CACHE_DIR", "EXPORT_DIR")}
        config.DATA_ROOT = self._dir
        config.DB_PATH = self._dir / "app.db"
        config.BACKUP_DIR = self._dir / "backups"
        config.PAGE_CACHE_DIR = self._dir / "cache" / "pages"
        config.EXPORT_DIR = self._dir / "exports"
        db._initialized = False
        db.init_db()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    # ---- 数据库 ----
    def test_integrity_check(self):
        self.assertTrue(maintenance.integrity_check(config.DB_PATH))       # 好库
        self.assertTrue(maintenance.integrity_check(self._dir / "nope.db"))  # 不存在→True
        bad = self._dir / "bad.db"
        bad.write_bytes(b"this is not a sqlite database at all")
        self.assertFalse(maintenance.integrity_check(bad))                 # 坏文件→False

    def test_backup_creates_consistent_snapshot(self):
        dest = maintenance.backup_db(config.DB_PATH, config.BACKUP_DIR, keep=14, stamp="20260703-000000")
        self.assertIsNotNone(dest)
        self.assertTrue(dest.exists())
        self.assertTrue(maintenance.integrity_check(dest))                 # 快照本身完好、可打开
        # 快照里能查到 invoices 表（结构随主库）
        c = sqlite3.connect(str(dest))
        try:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        self.assertIn("invoices", tables)

    def test_backup_missing_source_returns_none(self):
        self.assertIsNone(maintenance.backup_db(self._dir / "gone.db", config.BACKUP_DIR))

    def test_backup_retention_keeps_newest_n(self):
        for i in range(5):
            maintenance.backup_db(config.DB_PATH, config.BACKUP_DIR, keep=3,
                                  stamp=f"20260703-0000{i}0")
        kept = sorted(config.BACKUP_DIR.glob("app-*.db"))
        self.assertEqual(len(kept), 3)                                     # 只留最近 3 份
        self.assertEqual(kept[-1].name, "app-20260703-000040.db")          # 最新在内

    def test_needs_backup_interval(self):
        stem = Path(config.DB_PATH).stem
        self.assertTrue(maintenance.needs_backup(config.BACKUP_DIR, stem, 20))   # 无快照→需要
        maintenance.backup_db(config.DB_PATH, config.BACKUP_DIR, stamp="20260703-000000")
        self.assertFalse(maintenance.needs_backup(config.BACKUP_DIR, stem, 20))  # 刚备份→不需要
        self.assertTrue(maintenance.needs_backup(config.BACKUP_DIR, stem, 0))    # 间隔 0→总需要

    def test_checkpoint_no_error(self):
        maintenance.checkpoint(config.DB_PATH)                             # 不抛即可
        maintenance.checkpoint(self._dir / "absent.db")

    # ---- 磁盘留存 ----
    def test_prune_by_count_removes_oldest(self):
        d = self._dir / "pngs"
        d.mkdir()
        files = []
        for i in range(6):
            f = d / f"p{i}.png"
            f.write_bytes(b"x")
            files.append(f)
        import os
        for i, f in enumerate(files):                                      # 递增 mtime 明确新旧
            os.utime(f, (1000 + i, 1000 + i))
        removed = maintenance.prune_by_count(d, "*.png", keep=2)
        self.assertEqual(removed, 4)
        left = {f.name for f in d.glob("*.png")}
        self.assertEqual(left, {"p4.png", "p5.png"})                       # 保留最新两个
        # 数量不超上限→不动；keep<0→不动
        self.assertEqual(maintenance.prune_by_count(d, "*.png", keep=10), 0)
        self.assertEqual(maintenance.prune_by_count(d, "*.png", keep=-1), 0)

    def test_prune_helpers_use_config(self):
        config.PAGE_CACHE_DIR.mkdir(parents=True)
        for i in range(4):
            (config.PAGE_CACHE_DIR / f"c{i}.png").write_bytes(b"x")
        config.PAGE_CACHE_MAX_FILES = 1
        self.assertEqual(maintenance.prune_page_cache(), 3)

    # ---- uploads 孤儿清理（守"原件永久保留"红线）----
    def test_prune_orphan_uploads(self):
        import os, time
        from core.models import Invoice
        saved_up = config.UPLOAD_DIR
        config.UPLOAD_DIR = self._dir / "uploads"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        try:
            db.save_invoice(Invoice(file_hash="abcdef012345aaaa", file_name="a.pdf",
                                    file_path=str(config.UPLOAD_DIR / "abcdef012345_a.pdf")))
            ref = config.UPLOAD_DIR / "abcdef012345_a.pdf"; ref.write_bytes(b"x")   # 被记录引用
            old_orphan = config.UPLOAD_DIR / "999900001111_old.pdf"; old_orphan.write_bytes(b"x")
            new_orphan = config.UPLOAD_DIR / "888800002222_new.pdf"; new_orphan.write_bytes(b"x")
            old = time.time() - 40 * 86400                          # 40 天前（超 30 天保留期）
            os.utime(old_orphan, (old, old))
            os.utime(ref, (old, old))                                             # 引用文件即使很旧也不删
            n = maintenance.prune_orphan_uploads()
            self.assertEqual(n, 1)                                                # 只删那 1 个旧孤儿
            self.assertTrue(ref.exists(), "被记录引用的原件必须保留（红线）")
            self.assertFalse(old_orphan.exists(), "无引用且超期的孤儿应删")
            self.assertTrue(new_orphan.exists(), "无引用但未超期→保留（防误删在途上传）")
        finally:
            config.UPLOAD_DIR = saved_up

    def test_prune_orphan_uploads_empty_db_deletes_nothing(self):
        """空库（取不到任何引用）→ 保守不删，绝不误清。"""
        import os, time
        saved_up = config.UPLOAD_DIR
        config.UPLOAD_DIR = self._dir / "uploads2"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        try:
            f = config.UPLOAD_DIR / "aaaa11112222_x.pdf"; f.write_bytes(b"x")
            old = time.time() - 99 * 86400; os.utime(f, (old, old))
            self.assertEqual(maintenance.prune_orphan_uploads(), 0)
            self.assertTrue(f.exists())
        finally:
            config.UPLOAD_DIR = saved_up

    # ---- 启动维护整合 ----
    def test_startup_maintenance_ok(self):
        out = maintenance.startup_maintenance()
        self.assertTrue(out["integrity_ok"])
        self.assertTrue(out["backed_up"])                                  # 首次启动→建首份快照
        self.assertTrue(any(config.BACKUP_DIR.glob("app-*.db")))


class AccessLogFilterTest(unittest.TestCase):
    def test_low_value_paths(self):
        from gateway import main
        self.assertTrue(main._is_low_value_path("/api/review/abc/page/0"))
        self.assertTrue(main._is_low_value_path("/api/review/abc/original"))
        self.assertTrue(main._is_low_value_path("/healthz"))
        self.assertFalse(main._is_low_value_path("/api/upload"))
        self.assertFalse(main._is_low_value_path("/api/review/abc/field"))


if __name__ == "__main__":
    unittest.main()
