"""付款信息提取：EVM 格式校验、Tron base58check、多付款方式提示、不给虚假"有效"。"""
import unittest

from extraction.parse import wallet


class TestEvmFormat(unittest.TestCase):
    def test_valid_format(self):
        pays, _ = wallet.extract_payments("Ethereum: 0x" + "a" * 40, "f.pdf")
        self.assertTrue(pays)
        self.assertTrue(pays[0].valid_address)

    def test_bad_format_flagged(self):
        pays, issues = wallet.extract_payments("Ethereum: 0x" + "a" * 39 + " end", "f.pdf")
        self.assertTrue(pays)
        self.assertFalse(pays[0].valid_address)
        self.assertIn("WALLET_FORMAT", [c for c, _m, _s in issues])

    def test_is_valid_evm_address(self):
        self.assertTrue(wallet.is_valid_evm_address("0x" + "A" * 40))
        self.assertFalse(wallet.is_valid_evm_address("0x" + "A" * 39))


class TestTronInRegion(unittest.TestCase):
    def test_valid_tron(self):
        txt = "please make all payable to TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        pays, _ = wallet.extract_payments(txt, "f.pdf")
        tron = [p for p in pays if p.chain == "Tron"]
        self.assertTrue(tron and tron[0].valid_address)


class TestUnverifiedNotTrustedBlindly(unittest.TestCase):
    def test_btc_marked_unverified(self):
        # BTC bech32 未做密码学校验：不能标 valid=True
        txt = "payment details: bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
        pays, _ = wallet.extract_payments(txt, "f.pdf")
        btc = [p for p in pays if p.chain == "Bitcoin"]
        self.assertTrue(btc)
        self.assertFalse(btc[0].valid_address)


class TestMultiPayment(unittest.TestCase):
    def test_multi_payment_issue(self):
        txt = "Ethereum: 0x" + "a" * 40 + "\nArbitrum: 0x" + "b" * 40
        pays, issues = wallet.extract_payments(txt, "f.pdf")
        self.assertGreaterEqual(len(pays), 2)
        self.assertIn("MULTI_PAYMENT", [c for c, _m, _s in issues])


if __name__ == "__main__":
    unittest.main()
