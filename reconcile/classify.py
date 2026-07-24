"""银行流水**交易类型识别**（规则/关键词，纯函数）。

正式财务对账里"未匹配 ≠ 异常"：先判交易类型，再决定该匹配什么、是否需要发票。
非采购/销售类（手续费、工资、税款、内部划转、借还款、押金预付…）本就**无需发票匹配**，
不应堆进"异常/未匹配"队列。参见 计划/对账升级方向。
"""
from __future__ import annotations

import re

# 每个类型：key -> (中文标签, 需要发票匹配?, 主要匹配对象说明)
TYPES = {
    "vendor_payment":   ("供应商付款", True,  "采购发票/合同/验收/应付单"),
    "customer_receipt": ("客户收款",   True,  "销售发票/销售订单/应收单"),
    "reimbursement":    ("员工报销",   False, "报销单+发票+审批"),
    "payroll":          ("工资社保",   False, "工资表/社保申报"),
    "tax":              ("税款",       False, "纳税申报表/完税凭证"),
    "internal_transfer":("内部划转",   False, "另一账户的对应流水"),
    "loan":             ("借款还款",   False, "借款合同/还款计划"),
    "prepayment":       ("押金预付",   False, "合同/付款申请（不立即作费用）"),
    "refund":           ("退款冲正",   False, "原交易/红字发票"),
    "bank_fee":         ("手续费利息", False, "银行电子回单"),
    "unknown":          ("未分类",     True,  "需人工判断"),
}

# 判定不需要发票、可直接"无需匹配"的类型（其余进结构化未匹配原因）
NO_INVOICE_TYPES = {"payroll", "tax", "internal_transfer", "loan", "prepayment", "bank_fee"}

# 强关键词（含义明确、几乎不会是应开票业务）——命中才算高置信、才允许"自动判无需匹配"。
# 弱关键词（如 服务费/commission/advance/transfer/return）可能是应开票服务或含糊表述 → 低置信、不自动跳过。
_STRONG = {
    "bank_fee":          ["手续费", "利息", "bank fee", "wire fee", "interest charge", "overdraft"],
    "payroll":           ["工资", "薪资", "薪酬", "社保", "公积金", "payroll", "salary", "wage",
                          "social security", "provident fund"],
    "tax":               ["纳税", "完税", "增值税", "个税", "withholding tax", "vat payment",
                          "gst payment", "income tax", "税款缴纳"],
    "internal_transfer": ["内部划转", "账户划转", "备用金", "book transfer", "own account", "intra-account"],
    "loan":              ["借款", "还款", "贷款", "本金", "loan disbursement", "loan repayment", "principal"],
    "prepayment":        ["押金", "保证金", "定金", "security deposit", "retainer"],
}
# 单据号/发票引用迹象：含则不自动判"无需匹配"（它引用了单据 → 应核对/匹配，交人工）
_REF_RE = re.compile(r"(发票|invoice|\binv\b|\bpo\b|采购单|订单|合同|contract|[A-Za-z]{2,}-\d{3,})", re.I)


def looks_referenced(description) -> bool:
    return bool(_REF_RE.search(str(description or "")))


# 关键词（小写；中英兼顾）。按优先级从上到下匹配。
_RULES = [
    ("bank_fee",          ["手续费", "利息", "服务费", "bank fee", "service charge", "interest",
                            " fee ", "commission", "wire fee", "charge off"]),
    ("payroll",           ["工资", "薪资", "薪酬", "社保", "公积金", "payroll", "salary", "wage",
                            "social security", "provident fund"]),
    ("tax",               ["纳税", "完税", "税款", "增值税", "个税", "withholding tax", "vat", "gst",
                            "tax payment", "income tax"]),
    ("reimbursement",     ["报销", "reimburse", "expense claim", "expense reimb"]),
    ("internal_transfer", ["内部划转", "账户划转", "备用金", "internal transfer", "book transfer",
                            "own account", "intra-account", "sweep"]),
    ("loan",              ["借款", "还款", "贷款", "本金", "loan", "repayment", "principal", "drawdown"]),
    ("prepayment",        ["押金", "保证金", "预付", "定金", "deposit", "prepay", "prepayment",
                            "retainer", "advance payment"]),
    ("refund",            ["退款", "冲正", "红字", "refund", "reversal", "chargeback", "return"]),
]


def classify_txn(description, direction: str = "out") -> dict:
    """按摘要/方向判交易类型。返回 {type, label, needs_invoice, no_match_ok, target}。
    direction: 'out'=支出/付款, 'in'=收入/收款。"""
    d = " " + re.sub(r"\s+", " ", str(description or "").lower()) + " "
    t = None
    for key, kws in _RULES:
        if any(k in d for k in kws):
            t = key
            break
    if t is None:
        # 无明确关键词：按方向落到 供应商付款(支出) / 客户收款(收入)
        t = "vendor_payment" if direction != "in" else "customer_receipt"
    label, needs_inv, target = TYPES[t]
    # 高置信 = 命中该类型的**强关键词**；否则低置信（弱关键词/按方向兜底）——低置信不允许自动判"无需匹配"
    strong = any(k.lower() in d for k in _STRONG.get(t, []))
    return {"type": t, "label": label, "needs_invoice": needs_inv,
            "no_match_ok": t in NO_INVOICE_TYPES, "target": target,
            "confidence": "high" if strong else "low"}
