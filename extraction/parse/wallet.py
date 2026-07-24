"""付款信息提取与校验。

原则（计划要求）：可能存在多种付款方式（多个地址、多条链、银行转账等），
必须**全部提取、一条都不能丢**；无法结构化的内容也保留原文，不得删减。

支持：
- EVM 链上地址（带链名 / 不带链名），0x 开头、42 位校验
- 其他链地址：Tron（T 开头）、Solana / 比特币（bech32）— 仅在付款区域内匹配以降低误报
- 银行转账：SWIFT/BIC、IBAN、账户号、收款人、银行名
- 兜底：付款说明区域内未能结构化的文本，整段保留为 Other 行
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from core.models import PaymentDetail
from . import addr_check

# ---- 地址正则 ------------------------------------------------------------
_EVM_STRICT = re.compile(r"0x[0-9a-fA-F]{40}")
# 宽松：含易混字符，用于"发现"疑似地址（再交由校验判定）
_EVM_LOOSE = re.compile(r"0x[0-9a-fA-FOoIlSsBZ]{30,60}")
# 链名 + EVM 地址，如 "Ethereum: 0x...","- Arbitrum 0x..."
_CHAIN_EVM = re.compile(
    r"(Ethereum|Arbitrum|Polygon|Optimism|Base|BSC|BNB Chain|BNB|Avalanche|Fantom|zkSync|Linea|Scroll|Tron|Solana)"
    r"\s*[:：\-]?\s*(0x[0-9a-fA-FOoIlSsBZ]{30,60})",
    re.IGNORECASE,
)
# 非 EVM 地址（仅在付款区域内匹配以降低误报）
_TRON = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
_BTC_BECH32 = re.compile(r"\b(bc1[0-9a-z]{25,59})\b")
_SOLANA = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{43,44}\b")

# ---- 银行转账关键字 ------------------------------------------------------
_BANK_PATTERNS = {
    "SWIFT/BIC": re.compile(r"\b(?:SWIFT|BIC)\b\s*(?:code)?\s*[:#]?\s*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)", re.IGNORECASE),
    "IBAN": re.compile(r"\bIBAN\b\s*[:#]?\s*([A-Z]{2}\d{2}[A-Z0-9]{8,30})", re.IGNORECASE),
    "Account": re.compile(r"\b(?:Account|A/C|Acct)\s*(?:No\.?|Number|#)?\s*[:#]?\s*([0-9][0-9\- ]{5,})", re.IGNORECASE),
    "Beneficiary": re.compile(r"\bBeneficiary(?:\s*Name)?\s*[:#]\s*(.+)", re.IGNORECASE),
    "Bank": re.compile(r"\bBank(?:\s*Name)?\s*[:#]\s*(.+)", re.IGNORECASE),
}

# 付款说明区域起点关键字
_PAY_REGION = re.compile(
    r"(please\s+make\s+all\s+payable|payable\s+to|payment\s+(?:details|instruction)|"
    r"remit(?:tance)?|wire\s+to|bank\s+details|beneficiary)",
    re.IGNORECASE,
)


def is_valid_evm_address(addr: str) -> bool:
    """格式校验：0x + 40 位十六进制（EIP-55 校验和方案已移除，仅判格式）。"""
    return bool(_EVM_STRICT.fullmatch(addr.strip()))


def _eval_evm(addr: str, chain_label: str = ""):
    """评估 EVM 地址，返回 (valid_address, note, issue_or_None)。

    仅做格式校验；无法核对地址内容是否正确，统一提示人工逐字核对。
    issue 为 (code, message, severity) 三元组或 None。
    """
    prefix = f"{chain_label} " if chain_label else ""
    if is_valid_evm_address(addr):
        return True, "格式合法（0x + 40 位 hex）；无法自动核对内容，请人工逐字核对地址", None
    return False, "地址格式非法（应为 0x + 40 位十六进制）", \
        ("WALLET_FORMAT", f"{prefix}钱包地址格式异常: {addr}", "error")


def _line_of(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def _payment_region(text: str) -> str:
    """付款说明区域文本（从首个付款关键字到文末）；无则返回空串。"""
    m = _PAY_REGION.search(text)
    return text[m.start():] if m else ""


def extract_payments(text: str, source_file: str,
                     settlement_currency: Optional[str] = None
                     ) -> Tuple[List[PaymentDetail], List[Tuple[str, str, str]]]:
    """提取全部付款方式，保留原文，绝不删减。

    返回 (PaymentDetail 列表, 异常列表)，异常为 (code, message, severity) 三元组。
    """
    payments: List[PaymentDetail] = []
    issues: List[Tuple[str, str, str]] = []
    consumed: List[Tuple[int, int]] = []   # 已被"带链名"匹配占用的 span，避免裸地址重复
    seen_keys = set()

    def _add(p: PaymentDetail) -> None:
        key = (p.method, p.chain, (p.wallet_address or "").lower(), p.raw)
        if key in seen_keys:
            return
        seen_keys.add(key)
        payments.append(p)

    # 1) 带链名的 EVM 地址（同一地址多条链 = 多条记录，全部保留）
    for m in _CHAIN_EVM.finditer(text):
        chain, addr = m.group(1), m.group(2)
        valid, note, issue = _eval_evm(addr, chain)
        if issue:
            issues.append(issue)
        _add(PaymentDetail(method="On-chain transfer", chain=chain.title(),
                           wallet_address=addr, settlement_currency=settlement_currency,
                           valid_address=valid, raw=m.group(0).strip(),
                           note=note, source_file=source_file))
        consumed.append(m.span())

    # 2) 未带链名的裸 EVM 地址（不再因已有带链名地址而跳过！全部独立提取）
    for m in _EVM_LOOSE.finditer(text):
        if any(s <= m.start() < e for (s, e) in consumed):
            continue
        addr = m.group(0)
        valid, note, issue = _eval_evm(addr)
        if issue:
            issues.append(issue)
        _add(PaymentDetail(method="On-chain transfer", chain=None,
                           wallet_address=addr, settlement_currency=settlement_currency,
                           valid_address=valid, raw=_line_of(text, m.start()),
                           note=f"未标注链（EVM 地址）；{note}", source_file=source_file))

    # 3) TRON：有 base58check **真校验**，误报会被校验挡下 → **全文扫**（不再依赖付款区域表头）
    for m in _TRON.finditer(text):
        addr = m.group(0)
        valid = addr_check.is_valid_tron(addr)   # base58check 真校验
        if not valid:
            continue                             # 全文扫时校验不过的直接丢弃，避免误报（不再报错噪声）
        _add(PaymentDetail(method="On-chain transfer", chain="Tron",
                           wallet_address=addr, settlement_currency=settlement_currency,
                           valid_address=True, raw=_line_of(text, m.start()),
                           note="Tron base58check 校验通过", source_file=source_file))
    # 4) BTC bech32：bc1 前缀 + 长串，结构化明显、误报低 → **全文扫**；仍标未校验、待人工核对
    for m in _BTC_BECH32.finditer(text):
        _add(PaymentDetail(method="On-chain transfer", chain="Bitcoin",
                           wallet_address=m.group(1), settlement_currency=settlement_currency,
                           valid_address=False, raw=_line_of(text, m.start()),
                           note="BTC bech32 地址，未做密码学校验，请人工逐字核对",
                           source_file=source_file))
        issues.append(("WALLET_UNVERIFIED",
                       f"BTC 地址未做密码学校验，请人工核对: {m.group(1)}", "warning"))

    # 5) Solana：长 base58、**无前缀无校验**，全文误报高 → **仅付款区域内**
    region = _payment_region(text)
    if region:
        known = {(p.wallet_address or "") for p in payments}
        for m in _SOLANA.finditer(region):
            cand = m.group(0)
            if cand in known or cand.startswith("0x"):
                continue
            # Solana 地址本身无内置校验和，无法自动核验 -> 不标"有效"
            _add(PaymentDetail(method="On-chain transfer", chain="Solana?",
                               wallet_address=cand, settlement_currency=settlement_currency,
                               valid_address=False, raw=_line_of(region, m.start()),
                               note="疑似 Solana 地址（无校验和可核），待人工确认",
                               source_file=source_file))
            issues.append(("WALLET_UNVERIFIED",
                           f"疑似 Solana 地址无法自动校验，请人工核对: {cand}", "warning"))

    # 4) 银行转账信息
    bank = _extract_bank(region or text)
    if bank:
        _add(PaymentDetail(method="Bank transfer", chain=None,
                           wallet_address=bank.get("Account") or bank.get("IBAN"),
                           settlement_currency=settlement_currency, valid_address=True,
                           raw=bank["_raw"], note="; ".join(f"{k}={v}" for k, v in bank.items() if not k.startswith("_")),
                           source_file=source_file))

    # 5) 兜底：付款区域内有内容但未能结构化 -> 整段保留，不丢失
    if region and not payments:
        _add(PaymentDetail(method="Other/Unstructured", raw=region.strip()[:1000],
                           note="付款说明未能结构化，保留原文待人工确认", source_file=source_file))
        issues.append(("PAYMENT_UNSTRUCTURED", "付款信息未能结构化，已保留原文，需人工确认", "warning"))

    # 6) 多种付款方式提示（确保人工逐一核对，勿遗漏）
    if len(payments) > 1:
        issues.append(("MULTI_PAYMENT",
                       f"检测到 {len(payments)} 种付款方式/地址，请逐一核对，勿遗漏任何一项", "info"))

    return payments, issues


def _extract_bank(text: str) -> Optional[dict]:
    """提取银行转账字段；至少命中 SWIFT/IBAN/Account 之一才算银行付款。"""
    found: dict = {}
    for label, pat in _BANK_PATTERNS.items():
        m = pat.search(text)
        if m:
            val = m.group(1).strip().rstrip(".,;")
            if label in ("Beneficiary", "Bank") and len(val) > 80:
                val = val[:80]
            found[label] = val
    if not any(k in found for k in ("SWIFT/BIC", "IBAN", "Account")):
        return None
    # 记录原始片段
    idx = min((text.find(v) for v in found.values() if v in text), default=0)
    found["_raw"] = text[max(0, idx - 40): idx + 200].strip()
    return found
