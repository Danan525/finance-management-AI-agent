"""科目表（默认）+ 关键控制/税/往来科目 + 从发票取费用科目。

科目表是分录与报表之间的桥。默认预置一套对齐 IFRS 报表行的基础科目（软件公司），
费用明细科目复用 `extraction/classify/rules.py` 的"类别→科目"（规则即数据、可配置）。
"""
from __future__ import annotations

from typing import Optional

# ---- 关键控制/往来/税/资金科目（accrual + settlement 用）----
AP = "2100 应付账款 Accounts Payable"        # 我方收票（负债）
AR = "1100 应收账款 Accounts Receivable"     # 我方开票（资产）
BANK = "1002 银行存款 Bank"                   # 资金（结算用）
REVENUE = "4000 营业收入 Sales Revenue"       # 我方开票的收入
INPUT_TAX = "1180 进项税额 Input Tax"         # 我方收票的可抵扣税（资产）
OUTPUT_TAX = "2210 销项税额 Output Tax"       # 我方开票的销项税（负债）
EXPENSE_DEFAULT = "6900 其它费用 Other Expenses"   # 分类未给科目时的费用兜底

# ---- 结算净额差承接科目（第二段：应付/应收 ↔ 银行）----
FEE = "6603 财务费用-手续费 Bank/Platform Fees"          # 银行/平台手续费（费用）
WHT_PREPAID = "1221 预缴所得税 Prepaid Income Tax"       # 我方收款被代扣预扣税（资产）
WHT_PAYABLE = "2221 应交税费-代扣税款 Withholding Tax Payable"  # 我方付款代扣预扣税（负债）
CASH_DISCOUNT = "6604 现金折扣 Cash Discount"            # 现金折扣（冲减成本/费用）
ROUNDING = "6605 财务费用-舍入差异 Rounding Difference"   # 舍入微差兜底（有容差阈值）

# 差额原因 → 承接科目（差额落在借/贷哪一方由 direction+符号机械决定，不在此配）
DIFF_REASONS = {
    "fee": FEE,
    "withholding_ar": WHT_PREPAID,     # 收款被代扣
    "withholding_ap": WHT_PAYABLE,     # 付款代扣
    "discount": CASH_DISCOUNT,
    "rounding": ROUNDING,
}

# ---- 默认科目表：编码/名称/类别/正常余额方向/IFRS 报表归属 ----
# type: asset|liability|equity|revenue|expense ；normal: debit|credit
CHART = [
    ("1001", "现金 Cash on Hand", "asset", "debit", "BalanceSheet:CashAndEquivalents"),
    ("1002", "银行存款 Bank", "asset", "debit", "BalanceSheet:CashAndEquivalents"),
    ("1100", "应收账款 Accounts Receivable", "asset", "debit", "BalanceSheet:Receivables"),
    ("1180", "进项税额 Input Tax", "asset", "debit", "BalanceSheet:OtherCurrentAssets"),
    ("1500", "固定资产 Fixed Assets", "asset", "debit", "BalanceSheet:PPE"),
    ("2100", "应付账款 Accounts Payable", "liability", "credit", "BalanceSheet:Payables"),
    ("2210", "销项税额 Output Tax", "liability", "credit", "BalanceSheet:TaxPayable"),
    ("3000", "实收资本 Share Capital", "equity", "credit", "BalanceSheet:Equity"),
    ("3200", "未分配利润 Retained Earnings", "equity", "credit", "BalanceSheet:RetainedEarnings"),
    ("4000", "营业收入 Sales Revenue", "revenue", "credit", "IncomeStatement:Revenue"),
    ("1221", "预缴所得税 Prepaid Income Tax", "asset", "debit", "BalanceSheet:OtherCurrentAssets"),
    ("2221", "应交税费-代扣税款 Withholding Tax Payable", "liability", "credit", "BalanceSheet:TaxPayable"),
    ("6603", "财务费用-手续费 Bank/Platform Fees", "expense", "debit", "IncomeStatement:FinanceCosts"),
    ("6604", "现金折扣 Cash Discount", "expense", "debit", "IncomeStatement:Opex"),
    ("6605", "财务费用-舍入差异 Rounding Difference", "expense", "debit", "IncomeStatement:FinanceCosts"),
    ("6900", "其它费用 Other Expenses", "expense", "debit", "IncomeStatement:Opex"),
]


def account_type(account: str) -> Optional[str]:
    """按科目编码前缀查类别（编码即字符串开头的数字）。"""
    code = (account or "").split(None, 1)[0]
    for c, _n, typ, _s, _r in CHART:
        if code == c:
            return typ
    # 未在默认表里：按编码首位约定（1 资产 2 负债 3 权益 4 收入 5/6 费用）
    return {"1": "asset", "2": "liability", "3": "equity",
            "4": "revenue", "5": "expense", "6": "expense"}.get(code[:1])


# ---- 现金流量表：现金及等价物口径 + 活动类别 ----
OPERATING, INVESTING, FINANCING = "operating", "investing", "financing"
ACTIVITIES = (OPERATING, INVESTING, FINANCING)
ACTIVITY_LABEL = {OPERATING: "经营活动 Operating", INVESTING: "投资活动 Investing",
                  FINANCING: "筹资活动 Financing"}


def is_cash(account: str) -> bool:
    """是否现金及等价物科目（报表行归属为 CashAndEquivalents）。"""
    return report_line(account) == "BalanceSheet:CashAndEquivalents"


def report_line(account: str) -> Optional[str]:
    """科目 → 报表行归属 token（"Statement:Line"）。默认表未收录则按科目类别兜底；
    类别也判不出（未知科目）→ None（报表侧据此列入"未归类"并阻止出表，E6）。"""
    code = (account or "").split(None, 1)[0]
    for c, _n, _t, _s, rep in CHART:
        if code == c:
            return rep
    typ = account_type(account)
    return {
        "asset": "BalanceSheet:OtherCurrentAssets",
        "liability": "BalanceSheet:Payables",
        "equity": "BalanceSheet:RetainedEarnings",
        "revenue": "IncomeStatement:Revenue",
        "expense": "IncomeStatement:Opex",
    }.get(typ)


def expense_account(inv) -> str:
    """发票的费用科目：优先分类给的科目（classify/rules 的 类别→科目），否则兜底。"""
    acct = getattr(inv.classification, "account", None) if getattr(inv, "classification", None) else None
    return acct or EXPENSE_DEFAULT
