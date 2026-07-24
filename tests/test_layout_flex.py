"""版面/标签"写太死"整改回归：列分界自适应、模板值不截断、Bill-to 锚点放宽。"""
import unittest

from extraction.extract import pdf_text as p
from extraction.parse import template_rules as t
from extraction.parse import generic as g


class ColSplitTest(unittest.TestCase):
    def test_portrait_keeps_300(self):
        # 标准竖版零回归：A4 595 / Letter 612 仍用调校过的 300
        self.assertEqual(p.col_split_for(595), 300.0)
        self.assertEqual(p.col_split_for(612), 300.0)
        self.assertEqual(p.col_split_for(0), 300.0)

    def test_wide_page_adapts(self):
        # 横版/宽页按页宽取中线（固定 300 会严重偏左）
        self.assertEqual(p.col_split_for(842), 421.0)   # A4 横版
        self.assertEqual(p.col_split_for(1000), 500.0)

    def test_line_uses_col_split(self):
        # 词 x=350：竖版(split 300)算右栏；宽页(split 421)算左栏
        ln1 = p.Line(10, [(350, 380, "X")], 300.0)
        self.assertEqual(ln1.right_text(), "X")
        self.assertEqual(ln1.left_text(), "")
        ln2 = p.Line(10, [(350, 380, "X")], 421.0)
        self.assertEqual(ln2.left_text(), "X")
        self.assertEqual(ln2.right_text(), "")


class TemplateNoTruncationTest(unittest.TestCase):
    def _m(self, blob):
        return t._match(t._HEADER_PATTERNS, blob)

    def test_slashed_invoice_no(self):
        self.assertEqual(self._m("Invoice #: INV/2026/001")["invoice_no"], "INV/2026/001")

    def test_spaced_invoice_no(self):
        self.assertEqual(self._m("Invoice #: INV 001")["invoice_no"], "INV 001")

    def test_spaced_date_not_truncated(self):
        self.assertEqual(self._m("Invoice date: 28 December 2025")["invoice_date"], "28 December 2025")

    def test_two_fields_one_line_split_at_gap(self):
        out = self._m("Invoice date: 2025-06-26    Invoice #: X-9")
        self.assertEqual(out["invoice_date"], "2025-06-26")   # 在 4 空格列间隙处止住
        self.assertEqual(out["invoice_no"], "X-9")


class BillToAnchorTest(unittest.TestCase):
    def test_broadened_anchors(self):
        for s in ["Bill To", "Sold To", "Ship To", "Buyer", "Attention", "Attn", "Recipient", "Invoice To"]:
            self.assertTrue(g._BILLTO.match(s), f"{s!r} 应被识别为收票方锚点")

    def test_non_anchor_rejected(self):
        for s in ["Total Due", "Description", "From"]:
            self.assertFalse(g._BILLTO.match(s))


if __name__ == "__main__":
    unittest.main()
