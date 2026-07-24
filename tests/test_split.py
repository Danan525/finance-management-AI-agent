"""多发票切分：按发票号边界拆分，仅在高置信时才拆（绝不误拆）。"""
import unittest

from extraction.parse import template_rules
from extraction.parse.template_rules import split_invoice_segments
from extraction.extract.pdf_text import PdfDoc, Line, pdfdoc_from_word_tuples
from core.models import Invoice


def _line(text):
    return Line(0.0, [(0.0, 1.0, text)])


def _doc(*texts):
    return PdfDoc(full_text="\n".join(texts), lines=[_line(t) for t in texts])


class TestSplit(unittest.TestCase):
    def test_two_invoices_split(self):
        doc = _doc("Invoice #: A", "TOTAL DUE $1.00", "Invoice #: B", "TOTAL DUE $2.00")
        self.assertEqual(len(split_invoice_segments(doc)), 2)

    def test_split_by_pages(self):
        """每页一张发票（页数==合计数）→ 按页切分，与发票号标签格式无关。

        关键回归：第二张发票即便用裸 'No.' 标签（起始标记匹配不到），按页也能切出。
        """
        from extraction.extract.pdf_text import PdfDoc, Line
        H = 800.0
        # 页0：No. A-1 / TOTAL DUE 1.00 ；页1（y 偏移 H+20）：No. B-2 / TOTAL DUE 2.00
        lines = [
            Line(10.0, [(0.0, 1.0, "No. A-1")]),
            Line(40.0, [(0.0, 1.0, "TOTAL DUE 1.00")]),
            Line(H + 30.0, [(0.0, 1.0, "No. B-2")]),
            Line(H + 60.0, [(0.0, 1.0, "TOTAL DUE 2.00")]),
        ]
        doc = PdfDoc(full_text="\n".join(l.text() for l in lines), lines=lines,
                     page_sizes=[[600.0, H], [600.0, H]])
        segs = split_invoice_segments(doc)
        self.assertEqual(len(segs), 2)
        self.assertIn("A-1", segs[0].full_text)
        self.assertIn("B-2", segs[1].full_text)
        self.assertNotIn("B-2", segs[0].full_text)

    def test_generalized_markers_split(self):
        """版式无关：INVOICE NO. + Amount Due 等也能识别多张。"""
        doc = _doc("INVOICE NO. A-001", "Amount Due 1.00", "INVOICE NO. B-002", "Amount Due 2.00")
        self.assertEqual(len(split_invoice_segments(doc)), 2)

    def test_multi_invoice_each_segment_extracts(self):
        """端到端：2 张发票的 PDF 切分后，每段各自抽出发票号/总额/明细。"""
        words = []
        for off, no, svc, amt_ in ((0, "A-001", "Consulting", "100.00"),
                                   (100, "B-002", "Advisory", "200.00")):
            words += [
                (60, 10 + off, 120, 18 + off, "INVOICE"), (122, 10 + off, 150, 18 + off, "NO."),
                (300, 10 + off, 360, 18 + off, no),
                (60, 30 + off, 160, 38 + off, "DESCRIPTION"), (300, 30 + off, 330, 38 + off, "QTY"),
                (450, 30 + off, 500, 38 + off, "AMOUNT"),
                (60, 50 + off, 140, 58 + off, svc), (305, 50 + off, 312, 58 + off, "1"),
                (450, 50 + off, 510, 58 + off, amt_),
                (60, 70 + off, 120, 78 + off, "TOTAL"), (122, 70 + off, 150, 78 + off, "DUE"),
                (450, 70 + off, 510, 78 + off, amt_),
            ]
        doc = pdfdoc_from_word_tuples(words, full_text="\n".join(w[4] for w in words))
        segs = split_invoice_segments(doc)
        self.assertEqual(len(segs), 2)
        results = []
        for seg in segs:
            inv = Invoice()
            template_rules.parse_pdfdoc(inv, seg)
            results.append((inv.f("invoice_no").value, str(inv.f("total_due").value),
                            len(inv.line_items), inv.line_items[0].description if inv.line_items else None))
        self.assertEqual(results[0][0], "A-001")
        self.assertEqual(results[0][1], "100.00")
        self.assertEqual(results[1][0], "B-002")
        self.assertEqual(results[1][1], "200.00")
        self.assertTrue(all(r[2] >= 1 for r in results))           # 每段都抽到明细
        self.assertIn("Consulting", results[0][3] or "")

    def test_single_invoice_not_split(self):
        doc = _doc("Invoice #: A", "TOTAL DUE $1.00")
        self.assertEqual(len(split_invoice_segments(doc)), 1)

    def test_mismatch_does_not_split(self):
        # 1 个表头但 2 个 TOTAL DUE：不确定边界 -> 不拆（退回单张 + 提示）
        doc = _doc("Invoice #: A", "TOTAL DUE $1.00", "TOTAL DUE $2.00")
        self.assertEqual(len(split_invoice_segments(doc)), 1)

    def test_segments_carry_their_own_total(self):
        doc = _doc("Invoice #: A", "TOTAL DUE $1.00", "Invoice #: B", "TOTAL DUE $2.00")
        segs = split_invoice_segments(doc)
        self.assertIn("$1.00", segs[0].full_text)
        self.assertIn("$2.00", segs[1].full_text)
        self.assertNotIn("$2.00", segs[0].full_text)

    def test_one_invoice_two_pages_restated_total_not_split(self):
        """回归：一张发票跨两页、续页重复写合计(Amount Payable) → 只有 1 个发票号，
        不得因"页数==合计数"按页误拆（"说多张、其实一张两页"的根因）。"""
        H = 800.0
        lines = [
            Line(10.0, [(0.0, 1.0, "Invoice number: INV-1")]),
            Line(40.0, [(0.0, 1.0, "TOTAL DUE 100.00")]),
            Line(H + 30.0, [(0.0, 1.0, "Continuation page")]),
            Line(H + 60.0, [(0.0, 1.0, "Amount Payable 100.00")]),   # 续页重复合计、无发票号
        ]
        doc = PdfDoc(full_text="\n".join(l.text() for l in lines), lines=lines,
                     page_sizes=[[600.0, H], [600.0, H]])
        self.assertEqual(len(split_invoice_segments(doc)), 1)   # 续页无发票起始 → 不拆


if __name__ == "__main__":
    unittest.main()
