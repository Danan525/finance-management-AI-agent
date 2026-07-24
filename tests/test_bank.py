"""银行信息提取加固：无需表头的正则回填 + 表头/子标签放宽 + 打通到 bank_ 字段。"""
import unittest

from extraction.parse import generic as g


class BankFromTextTest(unittest.TestCase):
    def test_full_block_no_header(self):
        out = g.bank_from_text("Beneficiary: ACME Ltd\nBank: HSBC Hong Kong\n"
                               "Account No: 123-456-789\nSWIFT: HSBCHKHH")
        self.assertEqual(out["bank_swift"], "HSBCHKHH")
        self.assertEqual(out["bank_account_name"], "ACME Ltd")
        self.assertEqual(out["bank_name"], "HSBC Hong Kong")
        self.assertTrue(out["bank_account_no"])

    def test_variants_ac_bic_bankname(self):
        out = g.bank_from_text("Account Name: Beta Corp\nBank Name: DBS\nA/C 9876543210\nBIC: DBSSSGSG")
        self.assertEqual(out["bank_swift"], "DBSSSGSG")
        self.assertEqual(out["bank_account_no"], "9876543210")
        self.assertEqual(out["bank_account_name"], "Beta Corp")
        self.assertEqual(out["bank_name"], "DBS")

    def test_sort_code_fallback_to_swift_field(self):
        out = g.bank_from_text("Beneficiary Bank: Citibank\nSort Code: 12-34-56\nAccount Number: 55667788")
        self.assertEqual(out["bank_name"], "Citibank")
        self.assertEqual(out["bank_account_no"], "55667788")
        self.assertEqual(out["bank_swift"], "12-34-56")     # 无 SWIFT 时 sort code 兜到该字段

    def test_inline_single_line_cut_at_gap(self):
        out = g.bank_from_text("Our Bank: Standard Chartered  Payee: Gamma LLC  IBAN GB33BUKB20201555555555")
        self.assertEqual(out["bank_name"], "Standard Chartered")   # 列间隙处切断，不吞后面
        self.assertEqual(out["bank_account_name"], "Gamma LLC")
        self.assertEqual(out["bank_account_no"], "GB33BUKB20201555555555")

    def test_no_bank_info(self):
        self.assertEqual(g.bank_from_text("Thank you for your business."), {})

    # ---- 户名/行名无冒号（列对齐/下一行）也要抓到 ----
    def test_account_name_whitespace_aligned(self):
        out = g.bank_from_text("Account Name    ACME Ltd\nSWIFT   HSBCHKHH")
        self.assertEqual(out["bank_account_name"], "ACME Ltd")

    def test_account_name_next_line(self):
        out = g.bank_from_text("Account Name\nACME Ltd\nSWIFT\nHSBCHKHH")
        self.assertEqual(out["bank_account_name"], "ACME Ltd")

    def test_column_bank_block_all_fields(self):
        out = g.bank_from_text("Bank Name       HSBC\nAccount Name    ACME Ltd\n"
                               "Account No      12345678\nSWIFT           HSBCHKHH")
        self.assertEqual(out["bank_account_name"], "ACME Ltd")
        self.assertEqual(out["bank_name"], "HSBC")
        self.assertEqual(out["bank_account_no"], "12345678")
        self.assertEqual(out["bank_swift"], "HSBCHKHH")

    def test_no_prose_false_positive(self):
        # 单空格散文 / "Bank charges" 不得被当户名/行名
        self.assertEqual(g.bank_from_text("The beneficiary should be notified before payment."), {})
        self.assertEqual(g.bank_from_text("Bank charges of 5.00 apply."), {})


class BankHeaderTest(unittest.TestCase):
    def test_broadened_headers_match(self):
        for h in ["Bank Details", "Banking Details", "Payment Instructions", "Account Details",
                  "Our Bank", "Beneficiary Details", "Bank Information", "Wire Instructions"]:
            self.assertTrue(g._BANK_HEADER.search(h), h)


class BankIntegrationTest(unittest.TestCase):
    def _L(self, y, *w):
        from extraction.extract.pdf_text import Line
        return Line(y, list(w))

    def test_extract_generic_fills_bank_without_header(self):
        # 无"Bank Details"表头，仍应经正则兜底填出银行字段
        lines = [
            self._L(10, (40, 200, "Invoice number: INV-1")),
            self._L(30, (40, 300, "Beneficiary: ACME Ltd")),
            self._L(50, (40, 300, "Bank: HSBC")),
            self._L(70, (40, 300, "Account No: 123456789")),
            self._L(90, (40, 300, "SWIFT: HSBCHKHH")),
        ]
        out = g.extract_generic(lines)
        self.assertEqual(out.get("bank_swift"), "HSBCHKHH")
        self.assertEqual(out.get("bank_name"), "HSBC")
        self.assertTrue(out.get("bank_account_no"))


if __name__ == "__main__":
    unittest.main()
