"""分类规则的**可配置**来源：内置默认 + 可选 JSON 覆盖。

设计（对应"不要写死"）：
- 科目表 / 类别关键词 / 供应商品牌表 / 固定资产阈值 全部是**数据**，不是写死在逻辑里；
- 默认值仍在本文件（`_DEFAULTS`），保证缺配置也能开箱即用、行为与旧版一致；
- 用户改 `config/classification.json`（`config.CLASSIFY_RULES_PATH`）即可定制**自己的科目表**，
  无需改代码；文件缺失/损坏 → 静默回退默认（绝不因配置错误而崩）。
- 固定资产阈值改为**按币种**（`asset_thresholds`），不再一刀切 3000（3000 USD ≠ 3000 JPY）。

热重载：改完配置调用 `reload()`（或重启服务）生效。
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from core import config

# 类别关键词 → (分类, 会计科目)。纯同义词、通用，不绑定任何具体开票方/发票。
# 顺序讲究：具体在前、通用在后（先命中先返回）。
_DEFAULT_CATEGORY_RULES = [
    [r"management\s*fee", "Management Fee Expense", "6020 Management Fee"],
    [r"performance\s*fee|incentive\s*fee|carried\s*interest", "Performance Fee Expense", "6030 Performance Fee"],
    [r"service\s*(fee|charge)", "Service Fee Expense", "6010 Service Fee"],
    [r"professional\s*fee", "Professional Service", "6410 Professional Fees"],
    [r"legal\s*(fee|service)|attorney|solicitor|\bcounsel\b", "Legal Fees", "6420 Legal Fees"],
    [r"audit\s*(fee|service)|auditor|assurance\s*service", "Audit Fees", "6430 Audit Fees"],
    [r"account(ing|ancy)\s*(fee|service)?|bookkeep", "Accounting Fees", "6440 Accounting Fees"],
    [r"tax\s*(advisory|advis|filing|return|compliance|preparation)", "Tax Advisory", "6470 Tax Advisory"],
    [r"consult(ing|ancy|ant)|advisory|advisor", "Professional Service", "6410 Professional Fees"],
    [r"registration|incorporat|filing\s*fee|licen[sc]e\s*fee|licen[sc]ing|statutory|government\s*fee|regulatory\s*fee|annual\s*return|company\s*secretar", "Government & Registration Fees", "6460 Government & Registration Fees"],
    [r"disbursement", "Disbursements", "6910 Disbursements"],
    [r"sundry", "Sundry Expenses", "6900 Sundry Expenses"],
    [r"subscription|software|licen[sc]e|saas|cloud|hosting|domain\s*(name|renewal)?|api\s*usage|storage\s*plan|seat\s*licen", "Software & Cloud", "6110 Software & Cloud"],
    [r"bank\s*(charge|fee)|wire\s*(fee|charge)|remittance\s*(fee|charge)|processing\s*fee|transaction\s*fee|merchant\s*fee|payment\s*processing|\bfx\s*fee|exchange\s*fee", "Bank/Processing Fee", "6310 Bank Charges"],
    [r"air\s*fare|airfare|flight|\btravel\b|hotel|accommodation|lodging|per\s*diem", "Travel & Lodging", "6220 Travel & Lodging"],
    [r"taxi|ride[\s-]*hail|mileage|\btransport|parking|car\s*rental|fuel\b", "Transportation", "6210 Transportation"],
    [r"\bmeal|catering|restaurant|dining|entertainment", "Meals & Entertainment", "6230 Meals & Entertainment"],
    [r"\brent\b|lease|office\s*space|co-?working", "Rent", "6510 Rent"],
    [r"utilit|electric|water\s*(bill|charge)|gas\s*(bill|charge)|power\s*(bill|charge)", "Utilities", "6520 Utilities"],
    [r"internet|broadband|telecom|telephone|mobile\s*(plan|bill)|data\s*plan|phone\s*bill", "Telecom & Internet", "6530 Telecom & Internet"],
    [r"insurance|\bpremium\b|indemnity", "Insurance", "6540 Insurance"],
    [r"marketing|advertis|promotion|campaign|\bads?\b|sponsorship|\bseo\b|media\s*buy", "Marketing & Advertising", "6610 Marketing & Advertising"],
    [r"training|seminar|workshop|conference|course\s*fee|certification|tuition", "Training & Development", "6620 Training & Development"],
    [r"recruit|hiring|headhunt|staffing|placement\s*fee", "Recruitment", "6720 Recruitment"],
    [r"courier|shipping|postage|freight|delivery|logistics|customs\s*clearance", "Shipping & Courier", "6320 Shipping & Courier"],
    [r"interest\s*(expense|charge|payable)|loan\s*interest", "Interest Expense", "7010 Interest Expense"],
]

# 供应商品牌名 → (分类, 会计科目)。通用品牌，不绑定单一租户；可在 JSON 里增删。
_DEFAULT_SUPPLIER_RULES = [
    [r"microsoft|\baws\b|amazon\s*web|google\s*cloud|\bgcp\b|azure|digitalocean|cloudflare|heroku|vercel|github|gitlab|atlassian|slack|zoom|notion|dropbox|openai|anthropic|datadog|twilio", "Software & Cloud", "6110 Software & Cloud"],
    [r"uber|grab|didi|lyft|gojek|\bbolt\b|taxi", "Transportation", "6210 Transportation"],
    [r"booking\.com|expedia|agoda|marriott|hilton|hyatt|airbnb|\bhotel\b", "Travel & Lodging", "6220 Travel & Lodging"],
    [r"stripe|paypal|wise|adyen|\bsquare\b|payoneer", "Bank/Processing Fee", "6310 Bank Charges"],
    [r"fedex|\bdhl\b|\bups\b|sf\s*express|aramex", "Shipping & Courier", "6320 Shipping & Courier"],
    [r"linkedin|google\s*ads|meta\s*platforms|facebook|tiktok|hubspot|mailchimp", "Marketing & Advertising", "6610 Marketing & Advertising"],
]

_DEFAULTS = {
    "category_rules": _DEFAULT_CATEGORY_RULES,
    "supplier_rules": _DEFAULT_SUPPLIER_RULES,
    "asset_keywords": r"laptop|equipment|device|server|hardware",
    "asset_account": "1500 Fixed Assets",
    "asset_category": "Fixed Asset (candidate)",
    # 固定资产候选阈值：**按币种**（大额资本化门槛因币种而异）。缺的币种用 default。
    "asset_thresholds": {
        "default": 3000, "USD": 3000, "EUR": 3000, "GBP": 2500,
        "HKD": 25000, "CNY": 20000, "SGD": 4000, "TWD": 90000,
        "JPY": 400000, "KRW": 4000000, "INR": 250000,
    },
    "seed_pairs": [
        ["Professional Service", "6410 Professional Fees"],
        ["Legal Fees", "6420 Legal Fees"],
        ["Audit Fees", "6430 Audit Fees"],
        ["Fund Administration", "6050 Fund Admin"],
        ["Bank/Processing Fee", "6310 Bank Charges"],
        ["Sundry Expenses", "6900 Sundry Expenses"],
        ["Disbursements", "6910 Disbursements"],
    ],
}

_cache: Optional[dict] = None


def _load_raw() -> dict:
    """默认之上叠加 JSON 覆盖（键存在即整段替换；缺失/损坏则纯默认）。"""
    import copy
    data = copy.deepcopy(_DEFAULTS)
    p = getattr(config, "CLASSIFY_RULES_PATH", None)
    try:
        if p and Path(p).exists():
            override = json.loads(Path(p).read_text(encoding="utf-8"))
            if isinstance(override, dict):
                for k in _DEFAULTS:                 # 只认已知键；未知键（如 _README）忽略
                    if k in override and override[k] is not None:
                        data[k] = override[k]
    except Exception:
        pass                                        # 配置坏了绝不崩，回退默认
    return data


def get() -> dict:
    global _cache
    if _cache is None:
        raw = _load_raw()
        thresholds = {}
        for k, v in (raw.get("asset_thresholds") or {}).items():
            try:
                thresholds[str(k).upper()] = Decimal(str(v))
            except Exception:
                continue
        if "DEFAULT" not in thresholds:
            thresholds["DEFAULT"] = Decimal("3000")
        _cache = {
            "category_rules": [(p, c, a) for p, c, a in raw["category_rules"]],
            "supplier_rules": [(p, c, a) for p, c, a in raw["supplier_rules"]],
            "asset_re": re.compile(raw.get("asset_keywords") or r"(?!x)x", re.IGNORECASE),
            "asset_account": raw.get("asset_account") or "1500 Fixed Assets",
            "asset_category": raw.get("asset_category") or "Fixed Asset (candidate)",
            "asset_thresholds": thresholds,
            "seed_pairs": [(c, a) for c, a in raw["seed_pairs"]],
        }
    return _cache


def reload() -> None:
    """改完配置文件后调用（或重启服务）——清缓存、下次 get() 重新加载。"""
    global _cache
    _cache = None


def category_rules() -> List[Tuple[str, str, str]]:
    return get()["category_rules"]


def supplier_rules() -> List[Tuple[str, str, str]]:
    return get()["supplier_rules"]


def asset_re():
    return get()["asset_re"]


def asset_labels() -> Tuple[str, str]:
    g = get()
    return g["asset_category"], g["asset_account"]


def asset_threshold(currency: Optional[str]) -> Decimal:
    """该币种的固定资产候选阈值；未配置的币种用 default。"""
    th = get()["asset_thresholds"]
    return th.get((currency or "").upper(), th["DEFAULT"])


def seed_pairs() -> List[Tuple[str, str]]:
    return get()["seed_pairs"]
