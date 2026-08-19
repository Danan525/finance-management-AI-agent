"""科目表（默认）+ 关键控制/税/往来科目 + 从发票取费用科目。

科目表是分录与报表之间的桥。默认预置一套对齐 IFRS 报表行的基础科目（软件公司），
费用明细科目复用 `extraction/classify/rules.py` 的"类别→科目"（规则即数据、可配置）。
"""
from __future__ import annotations

import re as _re
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
EXCHANGE_GL = "6607 汇兑损益 FX Gain/Loss"               # 外币结算：票面(入账日汇率)−现金(结算日汇率) 的已实现汇兑损益

# ---- 往来控制账户（明细辅助账 == 控制账户余额，动它须有对手方与明细依据）----
CONTROL_ACCOUNTS = (AP, AR)
CONTROL_CODES = ("2100", "1100")     # 与上面两个常量的编码一致（见 control_side / is_control）


def is_control(account: str) -> bool:
    """是否往来控制账户（应付/应收）。手工凭证动它需显式确认 + 对手方（软护栏）。"""
    return account_code(account) in CONTROL_CODES


def control_side(account: str) -> Optional[str]:
    """控制账户归属："AP"/"AR"；非控制账户 → None。

    **按编码判定，不比较科目全名**——同编码不同写法（`"2100 应付账款"` vs 常量全名）若用字符串相等
    判断，会被误算到另一侧（2026-08-03 自检发现的假告警根因）。
    """
    code = account_code(account)
    if code == account_code(AP):
        return "AP"
    if code == account_code(AR):
        return "AR"
    return None


# ---- 期末结转 ----
CY_PROFIT = "3300 本年利润 Current-Year Profit"          # 过渡科目：结转损益归集，结转后归零
RETAINED = "3200 未分配利润 Retained Earnings"           # 留存收益：接收本年利润

# 差额原因 → 承接科目（差额落在借/贷哪一方由 direction+符号机械决定，不在此配）
DIFF_REASONS = {
    "fee": FEE,
    "withholding_ar": WHT_PREPAID,     # 收款被代扣
    "withholding_ap": WHT_PAYABLE,     # 付款代扣
    "discount": CASH_DISCOUNT,
    "rounding": ROUNDING,
    "fx_gain_loss": EXCHANGE_GL,        # 外币结算汇率变动的已实现汇兑损益
}

# ---- 默认科目表：编码/名称/类别/正常余额方向/IFRS 报表归属 ----
# type: asset|liability|equity|revenue|expense ；normal: debit|credit
_BUILTIN_CHART = [
    ("1001", "现金 Cash on Hand", "asset", "debit", "BalanceSheet:CashAndEquivalents"),
    ("1002", "银行存款 Bank", "asset", "debit", "BalanceSheet:CashAndEquivalents"),
    ("1100", "应收账款 Accounts Receivable", "asset", "debit", "BalanceSheet:Receivables"),
    ("1180", "进项税额 Input Tax", "asset", "debit", "BalanceSheet:OtherCurrentAssets"),
    ("1500", "固定资产 Fixed Assets", "asset", "debit", "BalanceSheet:PPE"),
    ("2100", "应付账款 Accounts Payable", "liability", "credit", "BalanceSheet:Payables"),
    ("2210", "销项税额 Output Tax", "liability", "credit", "BalanceSheet:TaxPayable"),
    ("3000", "实收资本 Share Capital", "equity", "credit", "BalanceSheet:Equity"),
    ("3200", "未分配利润 Retained Earnings", "equity", "credit", "BalanceSheet:RetainedEarnings"),
    ("3300", "本年利润 Current-Year Profit", "equity", "credit", "BalanceSheet:RetainedEarnings"),
    ("4000", "营业收入 Sales Revenue", "revenue", "credit", "IncomeStatement:Revenue"),
    ("1221", "预缴所得税 Prepaid Income Tax", "asset", "debit", "BalanceSheet:OtherCurrentAssets"),
    ("2221", "应交税费-代扣税款 Withholding Tax Payable", "liability", "credit", "BalanceSheet:TaxPayable"),
    ("6603", "财务费用-手续费 Bank/Platform Fees", "expense", "debit", "IncomeStatement:FinanceCosts"),
    ("6604", "现金折扣 Cash Discount", "expense", "debit", "IncomeStatement:Opex"),
    ("6605", "财务费用-舍入差异 Rounding Difference", "expense", "debit", "IncomeStatement:FinanceCosts"),
    ("6900", "其它费用 Other Expenses", "expense", "debit", "IncomeStatement:Opex"),
    # ---- 补全常用科目(2026-08-11)----
    # 资产
    ("1012", "其它货币资金 Other Cash Equivalents", "asset", "debit", "BalanceSheet:CashAndEquivalents"),
    ("1122", "其它应收款 Other Receivables", "asset", "debit", "BalanceSheet:Receivables"),
    ("1123", "预付账款 Prepayments", "asset", "debit", "BalanceSheet:OtherCurrentAssets"),
    ("1405", "存货 Inventory", "asset", "debit", "BalanceSheet:OtherCurrentAssets"),
    ("1509", "累计折旧 Accumulated Depreciation", "asset", "credit", "BalanceSheet:PPE"),   # 抵减固定资产
    ("1601", "无形资产 Intangible Assets", "asset", "debit", "BalanceSheet:PPE"),
    ("1602", "累计摊销 Accumulated Amortization", "asset", "credit", "BalanceSheet:PPE"),
    # 负债
    ("2202", "预收账款 Advances from Customers", "liability", "credit", "BalanceSheet:Payables"),
    ("2211", "应交税费-应交增值税 VAT Payable", "liability", "credit", "BalanceSheet:TaxPayable"),
    ("2220", "应交税费-应交所得税 Income Tax Payable", "liability", "credit", "BalanceSheet:TaxPayable"),
    ("2241", "应付职工薪酬 Salaries Payable", "liability", "credit", "BalanceSheet:Payables"),
    ("2280", "其它应付款 Other Payables", "liability", "credit", "BalanceSheet:Payables"),
    # 收入 / 其它收益
    ("4001", "主营业务收入 Operating Revenue", "revenue", "credit", "IncomeStatement:Revenue"),
    ("4100", "利息收入 Interest Income", "revenue", "credit", "IncomeStatement:OtherIncome"),
    ("4200", "营业外收入 Other Income", "revenue", "credit", "IncomeStatement:OtherIncome"),
    # 成本 / 费用
    ("6401", "主营业务成本 Cost of Sales", "expense", "debit", "IncomeStatement:COGS"),
    ("6601", "工资费用 Salaries Expense", "expense", "debit", "IncomeStatement:Opex"),
    ("6602", "折旧摊销费 Depreciation & Amortization", "expense", "debit", "IncomeStatement:Opex"),
    ("6606", "利息支出 Interest Expense", "expense", "debit", "IncomeStatement:FinanceCosts"),
    ("6607", "汇兑损益 FX Gain/Loss", "expense", "debit", "IncomeStatement:FinanceCosts"),
    ("6610", "租金费用 Rent Expense", "expense", "debit", "IncomeStatement:Opex"),
    ("6620", "办公及行政费用 Office & Admin", "expense", "debit", "IncomeStatement:Opex"),
    ("6630", "差旅费 Travel Expense", "expense", "debit", "IncomeStatement:Opex"),
    ("6640", "专业服务费 Professional Fees", "expense", "debit", "IncomeStatement:Opex"),
    ("6650", "营销费用 Marketing Expense", "expense", "debit", "IncomeStatement:Opex"),
    ("6801", "所得税费用 Income Tax Expense", "expense", "debit", "IncomeStatement:IncomeTax"),
]

# ---- 科目表可配置（规则即数据，计划 §3.7）----------------------------------
# 内置默认之上叠加 JSON 覆盖：用户改 `config/chart_of_accounts.json`（`config.CHART_PATH`）即可
# 加/改科目与报表行归属,无需改代码;缺失/损坏则纯用内置默认(行为不变)。按编码合并:同编码覆盖、新编码追加。
_ACCT_TYPES = {"asset", "liability", "equity", "revenue", "expense"}
# 利润表实际消费的行 token（P&L 科目只能映射到这些,否则会从净利漏掉却仍在账 → E1 破,见报表中心）
_IS_LINES_OK = {"Revenue", "COGS", "GrossProfit", "Opex", "FinanceCosts", "OtherIncome", "IncomeTax", "NetIncome"}
_chart_cache = None


def _valid_report_line(rep: str) -> bool:
    if not isinstance(rep, str) or ":" not in rep:
        return False
    stmt, line = rep.split(":", 1)
    if stmt == "BalanceSheet":
        return bool(line)
    if stmt == "IncomeStatement":
        return line in _IS_LINES_OK          # 未知损益行会静默漏进净利,拒绝
    return False


def chart() -> list:
    """生效科目表（内置默认 + JSON 覆盖，按编码合并）。缓存;改配置后调 reload_chart()。"""
    global _chart_cache
    if _chart_cache is not None:
        return _chart_cache
    import json
    from pathlib import Path
    from core import config
    by_code = {c: (c, n, t, s, r) for c, n, t, s, r in _BUILTIN_CHART}
    order = [c for c, *_ in _BUILTIN_CHART]
    p = getattr(config, "CHART_PATH", None)
    try:
        if p and Path(p).exists():
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            rows = data.get("accounts") if isinstance(data, dict) else data
            for row in (rows or []):
                if isinstance(row, dict):
                    code, name = str(row.get("code", "")).strip(), str(row.get("name", "")).strip()
                    typ, side, rep = row.get("type"), row.get("side", "debit"), row.get("report_line")
                else:
                    code, name, typ, side, rep = (list(row) + [None] * 5)[:5]
                    code = str(code).strip(); name = str(name).strip()
                if not code or typ not in _ACCT_TYPES or not _valid_report_line(rep):
                    continue                 # 无效条目跳过(不崩),坏配置整体回退默认由外层 except 兜底
                if code not in by_code:
                    order.append(code)
                by_code[code] = (code, name, typ, (side or "debit"), rep)
    except Exception:
        by_code = {c: (c, n, t, s, r) for c, n, t, s, r in _BUILTIN_CHART}
        order = [c for c, *_ in _BUILTIN_CHART]
    _chart_cache = [by_code[c] for c in order]
    return _chart_cache


def reload_chart() -> None:
    """改完 config/chart_of_accounts.json 后调用（或重启服务）——清缓存、下次 chart() 重载。"""
    global _chart_cache
    _chart_cache = None


CHART = chart()      # 模块级快照（外部遍历用；reload 后应改用 chart()）


_CODE_RE = _re.compile(r"^\s*(\d+)")


def account_code(account: str) -> str:
    """取科目编码=字符串**开头的数字串**，与后面是否有空格无关。

    不能用 `split()[0]`：用户在界面手填的 `"1002银行存款"`（编码贴着名字、无空格）会整串成为"编码"，
    于是 `account_type`/`report_line`/`is_cash`/`is_control` 全部认不出它——曾导致
    **动现金却不必标活动类别、现金流量表漏计该笔、而 E1/E3 因两边都漏仍假通过**（2026-08-03 自检发现）。
    """
    m = _CODE_RE.match(account or "")
    return m.group(1) if m else ""


def canonical_account(account: str) -> str:
    """把科目名规范化为科目表里的**规范全名**（按编码匹配）；表外编码原样返回（去首尾空白）。

    科目字符串同时是账簿里的**余额键**，所以 `"1002银行存款"` 与 `"1002 银行存款 Bank"` 不规范化就是
    两个科目（"影子科目"）：试算平衡多出一行、控制账户/现金口径也各算一半。过账时统一规范化即可根除。
    """
    code = account_code(account)
    if code:
        for c, n, _t, _s, _r in chart():
            if code == c:
                return "%s %s" % (c, n)
    return (account or "").strip()


def account_type(account: str) -> Optional[str]:
    """按科目编码前缀查类别（编码即字符串开头的数字）。"""
    code = account_code(account)
    for c, _n, typ, _s, _r in chart():
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
    code = account_code(account)
    for c, _n, _t, _s, rep in chart():
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


# ---- 过账科目角色可配置（规则即数据 §3.7 收尾）----
# 上面这些"角色→科目"常量（AP/AR/税/收入/银行/差额/结转…）是**默认**；不同辖区 COA 编码不同，
# 可用 `config/posting_accounts.json` 覆盖（借贷结构仍在代码、是会计恒等；这里只换"角色用哪个科目"）。
# CONTROL_CODES 从 AP/AR 角色**推导**（消除与常量手工保持一致的脆弱点）。无配置文件时行为完全不变。
_ROLE_DEFAULTS = {
    "AP": AP, "AR": AR, "BANK": BANK, "REVENUE": REVENUE,
    "INPUT_TAX": INPUT_TAX, "OUTPUT_TAX": OUTPUT_TAX, "EXPENSE_DEFAULT": EXPENSE_DEFAULT,
    "FEE": FEE, "WHT_PREPAID": WHT_PREPAID, "WHT_PAYABLE": WHT_PAYABLE,
    "CASH_DISCOUNT": CASH_DISCOUNT, "ROUNDING": ROUNDING, "EXCHANGE_GL": EXCHANGE_GL,
    "CY_PROFIT": CY_PROFIT, "RETAINED": RETAINED,
}


def _load_roles() -> dict:
    """内置默认 + `config/posting_accounts.json` 覆盖（只认已知角色键、非空字符串、编码合法）。"""
    roles = dict(_ROLE_DEFAULTS)
    try:
        import json
        from pathlib import Path
        from core import config
        p = getattr(config, "POSTING_ROLES_PATH", None)
        if p and Path(p).exists():
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            for k, v in (data or {}).items():
                if k in _ROLE_DEFAULTS and isinstance(v, str) and v.strip() and account_code(v):
                    roles[k] = v.strip()          # 须有编码（否则 is_control/report_line 认不出）→ 无效覆盖忽略
    except Exception:
        pass                                       # 坏文件整体回退默认，绝不崩
    return roles


def reload_roles() -> None:
    """（重新）加载过账角色并重设模块级角色变量 + 派生（CONTROL_*、DIFF_REASONS）。"""
    global AP, AR, BANK, REVENUE, INPUT_TAX, OUTPUT_TAX, EXPENSE_DEFAULT
    global FEE, WHT_PREPAID, WHT_PAYABLE, CASH_DISCOUNT, ROUNDING, EXCHANGE_GL
    global CY_PROFIT, RETAINED, CONTROL_ACCOUNTS, CONTROL_CODES, DIFF_REASONS
    r = _load_roles()
    AP = r["AP"]; AR = r["AR"]; BANK = r["BANK"]; REVENUE = r["REVENUE"]
    INPUT_TAX = r["INPUT_TAX"]; OUTPUT_TAX = r["OUTPUT_TAX"]; EXPENSE_DEFAULT = r["EXPENSE_DEFAULT"]
    FEE = r["FEE"]; WHT_PREPAID = r["WHT_PREPAID"]; WHT_PAYABLE = r["WHT_PAYABLE"]
    CASH_DISCOUNT = r["CASH_DISCOUNT"]; ROUNDING = r["ROUNDING"]; EXCHANGE_GL = r["EXCHANGE_GL"]
    CY_PROFIT = r["CY_PROFIT"]; RETAINED = r["RETAINED"]
    CONTROL_ACCOUNTS = (AP, AR)
    CONTROL_CODES = (account_code(AP), account_code(AR))       # 从角色推导，不再手工保持一致
    DIFF_REASONS = {
        "fee": FEE, "withholding_ar": WHT_PREPAID, "withholding_ap": WHT_PAYABLE,
        "discount": CASH_DISCOUNT, "rounding": ROUNDING, "fx_gain_loss": EXCHANGE_GL,
    }


reload_roles()      # 模块加载即应用（无配置文件时值与上面默认完全相同）
