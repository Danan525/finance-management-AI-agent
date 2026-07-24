"""地址识别整改回归：明细行(含金额)紧跟 party 块时不再被吞进地址；地址数字不误判。"""
import unittest

from extraction.parse import generic as g


def _rows(*lines):
    """把 ['文本', ...] 变成收集器要的 rows（每行一个整格，x 固定在右半区）。"""
    return [[(300.0, 500.0, t)] for t in lines]


class LineItemRowGuardTest(unittest.TestCase):
    def test_is_line_item_row_true(self):
        for s in ["Consulting services rendered   5,000.00", "Management fee $1,200.00",
                  "1 Advisory 200.00", "¥10,000", "Description  Qty  Amount", "Item # Description Amount"]:
            self.assertTrue(g.is_line_item_row(s), s)

    def test_is_line_item_row_false_for_addresses(self):
        for s in ["Suite 1200, Central Tower", "28 Queen's Road Central", "P.O. Box 12345",
                  "Hong Kong", "1,234 Main Street", "London SW1A 1AA", "Unit 5, Level 3"]:
            self.assertFalse(g.is_line_item_row(s), s)


class BillToStopsAtLineItemsTest(unittest.TestCase):
    def test_line_items_not_absorbed_into_address(self):
        rows = _rows(
            "Bill To",
            "Acme Client Ltd",
            "42 Market Street",
            "London EC1A 1BB",
            "Consulting services rendered   5,000.00",   # 明细行——不应进地址
            "Advisory fee   1,200.00",
        )
        name, addr, email, phone = g.extract_billto(rows)
        self.assertEqual(name, "Acme Client Ltd")
        self.assertEqual(addr, ["42 Market Street", "London EC1A 1BB"])
        self.assertNotIn("Consulting services rendered   5,000.00", addr)

    def test_clean_address_still_collected_fully(self):
        rows = _rows("Bill To", "Beta Corp", "5 King Road", "Suite 900", "Singapore 049315")
        name, addr, _e, _p = g.extract_billto(rows)
        self.assertEqual(name, "Beta Corp")
        self.assertEqual(addr, ["5 King Road", "Suite 900", "Singapore 049315"])


class AddrNoisyTest(unittest.TestCase):
    def test_price_makes_address_noisy(self):
        self.assertTrue(g.addr_noisy("42 Market Street, Consulting 5,000.00"))
        self.assertFalse(g.addr_noisy("42 Market Street, London EC1A 1BB"))
        self.assertFalse(g.addr_noisy("P.O. Box 12345, Singapore 049315"))


class LineItemsRejectAddressTest(unittest.TestCase):
    """明细侧对称守卫：左下角地址不再被当明细；门牌号里的裸逗号数不当金额；真金额仍收。"""
    def _L(self, y, *w):
        from extraction.extract.pdf_text import Line
        return Line(y, list(w))

    def test_bottom_address_not_line_item_with_subtotal(self):
        lines = [
            self._L(10, (40, 120, "Description"), (400, 460, "Amount")),
            self._L(30, (40, 300, "Consulting services"), (400, 470, "5,000.00")),
            self._L(50, (40, 300, "Advisory fee"), (400, 470, "1,200.00")),
            self._L(70, (40, 300, "Subtotal"), (400, 470, "6,200.00")),
            self._L(120, (40, 320, "1,234 Main Street")),
            self._L(140, (40, 320, "Suite 400, Tower Plaza")),
        ]
        items = g.extract_line_items(lines)
        descs = [it["description"] for it in items]
        self.assertEqual(len(items), 2)
        self.assertNotIn("1,234 Main Street", descs)
        self.assertTrue(all("Street" not in (d or "") for d in descs))

    def test_address_after_items_without_subtotal(self):
        # 无 Subtotal 兜停：靠"门牌号裸逗号数不当金额 + 地址关键词止收"
        lines = [
            self._L(10, (40, 120, "Description"), (400, 460, "Amount")),
            self._L(30, (40, 300, "Consulting services"), (400, 470, "5,000.00")),
            self._L(60, (40, 320, "1,234 Main Street")),
            self._L(80, (40, 320, "Suite 400, London")),
        ]
        items = g.extract_line_items(lines)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["description"], "Consulting services")

    def test_integer_amount_line_item_kept(self):
        # 保真：真·整数金额（金额格内）仍应识别为明细
        lines = [
            self._L(10, (40, 120, "Description"), (400, 460, "Amount")),
            self._L(30, (40, 300, "Item A"), (400, 470, "1,000")),
        ]
        items = g.extract_line_items(lines)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["amount"], "1,000")


if __name__ == "__main__":
    unittest.main()
