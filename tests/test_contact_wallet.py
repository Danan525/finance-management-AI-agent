"""联系方式/钱包 同类失败模式加固：本地电话号、TRON/BTC 无付款表头也提取。"""
import unittest

from extraction.parse import generic as g, wallet as w


class PhoneTest(unittest.TestCase):
    def test_local_and_labeled_formats(self):
        for s, exp in [("020 7946 0000", "020 7946 0000"),
                       ("(212) 555-0100", "(212) 555-0100"),
                       ("852 1234 5678", "852 1234 5678"),
                       ("212-555-0100", "212-555-0100"),
                       ("Tel: 020 7946 0000", "020 7946 0000"),
                       ("+44 20 7946 0000", "+44 20 7946 0000")]:
            self.assertEqual(g.find_phone(s), exp, s)

    def test_not_amount_date_account(self):
        # 金额/日期/账号/参考号/数量都不得被当电话
        for s in ["Total 5,000.00", "Invoice date 2026-06-02", "Account 123456789",
                  "Ref 12345", "Qty 3", "Amount 1,234.56"]:
            self.assertIsNone(g.find_phone(s), s)


class TronBtcFullTextTest(unittest.TestCase):
    _VALID_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"   # 真实有效(base58check 通过)

    def test_valid_tron_without_payment_header(self):
        pays, _ = w.extract_payments(f"Kindly remit to {self._VALID_TRON} today.", "x.pdf")
        tron = [p for p in pays if p.chain == "Tron"]
        self.assertEqual(len(tron), 1)
        self.assertEqual(tron[0].wallet_address, self._VALID_TRON)
        self.assertTrue(tron[0].valid_address)

    def test_invalid_tron_dropped(self):
        # 校验不过的假 TRON 串不得误报
        pays, _ = w.extract_payments("noise Tabcdefghijklmnopqrstuvwxyz012345 here", "y.pdf")
        self.assertFalse(any(p.chain == "Tron" for p in pays))

    def test_evm_still_full_text(self):
        pays, _ = w.extract_payments("pay 0x52908400098527886E0F7030069857D2E4169EE7", "z.pdf")
        self.assertTrue(any((p.wallet_address or "").startswith("0x") for p in pays))


if __name__ == "__main__":
    unittest.main()
