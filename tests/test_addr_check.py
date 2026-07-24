"""链上地址校验：Tron base58check（EIP-55/Keccak 已撤除）。"""
import unittest

from extraction.parse import addr_check


class TestTronBase58Check(unittest.TestCase):
    REAL = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

    def test_valid_address(self):
        self.assertTrue(addr_check.is_valid_tron(self.REAL))

    def test_tampered_last_char(self):
        self.assertFalse(addr_check.is_valid_tron(self.REAL[:-1] + "X"))

    def test_garbage(self):
        self.assertFalse(addr_check.is_valid_tron("not-an-address"))
        self.assertFalse(addr_check.is_valid_tron(""))

    def test_b58_decode_roundtrip_length(self):
        # 主网地址解码为 25 字节（0x41 + 20 + 4 校验位）
        self.assertEqual(len(addr_check.b58_decode(self.REAL)), 25)


class TestEip55Removed(unittest.TestCase):
    def test_no_keccak_residue(self):
        # EIP-55 校验和（纯 Python Keccak）已撤除，防止回退
        self.assertFalse(hasattr(addr_check, "keccak256"))
        self.assertFalse(hasattr(addr_check, "evm_check"))


if __name__ == "__main__":
    unittest.main()
