"""框选取文字（原件区域 → 文本，供人工定位/补录）。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from core import config, db


def _has_fitz():
    try:
        import fitz  # noqa
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_fitz(), "需要 PyMuPDF")
class RegionTextTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._db, self._up = config.DB_PATH, config.UPLOAD_DIR
        config.DB_PATH = Path(self._dir) / "t.db"
        config.UPLOAD_DIR = Path(self._dir) / "up"
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        db._initialized = False
        db.init_db()

    def tearDown(self):
        config.DB_PATH, config.UPLOAD_DIR = self._db, self._up
        db._initialized = False
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_region_text_returns_words_in_box(self):
        import fitz
        from fastapi.testclient import TestClient
        from extraction import pipeline
        from gateway.main import app
        doc = fitz.open()
        pg = doc.new_page(width=400, height=560)
        pg.insert_text((60, 30), "ACME CORP")                         # 顶部：目标
        for i, t in enumerate(["Invoice No: X-1", "Some filler line for text density " * 2,
                               "more filler content here for the parser", "TOTAL DUE 100.00"]):
            pg.insert_text((60, 90 + i * 24), t)
        p = Path(self._dir) / "r.pdf"
        doc.save(p)
        doc.close()
        inv = pipeline.process_local(p)[0]
        c = TestClient(app)
        # 框选页面顶部一条（覆盖 ACME CORP，避开下方填充行）
        r = c.post(f"/api/review/{inv.file_hash}/region-text",
                   json={"page": 0, "x0": 0.0, "y0": 0.0, "x1": 0.7, "y1": 0.09}).json()
        self.assertIn("ACME", r["text"])
        self.assertNotIn("TOTAL", r["text"])      # 下方内容不在框内


if __name__ == "__main__":
    unittest.main()
