"""报表行定义（顺序 + 中英标签 + 小计/合计公式口径）。

科目 → 报表行的归属由 `ledger.accounts.report_line` 决定（token = "Statement:Line"）；
本文件只定义**每张报表有哪些行、按什么顺序、哪些是明细/小计/合计**，便于按 IFRS/企业政策调整。
"""
from __future__ import annotations

# ---- 利润表：明细行（对应 IncomeStatement:<Line>）+ 计算行 ----
# kind: detail(可归集科目) / subtotal / total
IS_LINES = [
    ("Revenue", "营业收入 Revenue", "detail"),
    ("COGS", "营业成本 Cost of Sales", "detail"),
    ("GrossProfit", "毛利 Gross Profit", "subtotal"),          # Revenue - COGS
    ("Opex", "营业费用 Operating Expenses", "detail"),
    ("FinanceCosts", "财务费用 Finance Costs", "detail"),
    ("NetIncome", "净利润 Net Income", "total"),                # Revenue - 全部费用
]
IS_REVENUE = {"Revenue"}
IS_EXPENSE = {"COGS", "Opex", "FinanceCosts"}

# ---- 资产负债表：分三段，明细行对应 BalanceSheet:<Line> ----
BS_ASSETS = [
    ("CashAndEquivalents", "货币资金 Cash & Equivalents"),
    ("Receivables", "应收账款 Receivables"),
    ("OtherCurrentAssets", "其它流动资产 Other Current Assets"),
    ("PPE", "固定资产 Property, Plant & Equipment"),
]
BS_LIABILITIES = [
    ("Payables", "应付账款 Payables"),
    ("TaxPayable", "应交税费 Tax Payable"),
]
BS_EQUITY = [
    ("Equity", "实收资本 Share Capital"),
    ("ShareCapital", "实收资本 Share Capital"),
    ("RetainedEarnings", "留存收益 Retained Earnings"),
]
# 本期净利润在报表期计入权益（尚无正式期末结转前，由报表侧把当期损益并入权益，保证 E1 平衡）
BS_CURRENT_NET_INCOME = ("CurrentNetIncome", "本期净利润 Current-period Net Income")
