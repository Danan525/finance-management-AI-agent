"""链上地址校验（纯标准库，无第三方依赖）。

- Tron：base58check（0x41 前缀 + 双 SHA256 校验位），能发现单字符识别错误，
  且基于 hashlib（C 实现），速度快、无性能负担。

注：EVM 的 EIP-55 校验和方案已移除——真实发票地址多为全大写/全小写、本就无校验和可核，
该方案对实际数据无效；且纯 Python Keccak-256 会带来额外计算负担。EVM 地址改为仅做
格式校验（见 wallet.py 的 is_valid_evm_address），未能验证之处一律提示人工逐字核对。
"""
from __future__ import annotations

import hashlib

# ---- base58check（Tron）-------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58_decode(s: str) -> bytes:
    num = 0
    for ch in s:
        if ch not in _B58_INDEX:
            raise ValueError(f"非 base58 字符: {ch}")
        num = num * 58 + _B58_INDEX[ch]
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def is_valid_tron(addr: str) -> bool:
    """Tron 主网地址：base58check，解码为 25 字节、0x41 前缀、双 SHA256 校验位匹配。"""
    try:
        raw = b58_decode(addr.strip())
    except ValueError:
        return False
    if len(raw) != 25 or raw[0] != 0x41:
        return False
    checksum = hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4]
    return checksum == raw[21:]
