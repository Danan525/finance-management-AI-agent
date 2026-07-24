"""金额解析：严格正则 + Decimal。

金额字段只允许数字、小数点、千分位、负号、币种符号。
对易混字符（O/0、I/1、,/.）标记可疑，不自动修正。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

from core import config

# 阿拉伯-印度(٠-٩)、波斯(۰-۹)数字 + 阿拉伯小数点(٫)/千分位(٬) → ASCII，供海湾/波斯地区单据
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}       # U+0660–0669
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})  # U+06F0–06F9
_DIGIT_MAP[0x066B] = "."     # 阿拉伯小数点 ٫
_DIGIT_MAP[0x066C] = ","     # 阿拉伯千分位 ٬

# 币种符号字符类（不再只认 $€£；补 ¥ 元/円、₹ 卢比、₩ 韩元、฿ 泰铢、₱ ₦ ₫ ₨ ₪ 等）。
# 供本模块与 generic 复用，避免各处白名单不一致。
_SYM = "$€£¥₹₩฿₱₦₫₨₪"
SYM_CLASS = "[" + _SYM + "]"

# 允许的金额表达：可带币种符号前缀、千分位、小数（任意位数，保留真实精度）、负号或括号负数
_AMOUNT_RE = re.compile(
    r"^\s*\(?(?P<sign>[-+])?\s*" + SYM_CLASS + r"?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\)?\s*$")

# 易混字符：金额里出现字母 O/o/l/I 等
_SUSPICIOUS_CHARS = re.compile(r"[OoIlSsBZ]")
_MULTI_DOT = re.compile(r"\..*\.")



def _normalize_seps(text: str) -> str:
    """把金额文本的币种前缀/千分位/小数分隔归一到"点作小数点、无千分位空格"的形式。
    parse_amount 与 decimal_places **共用**，保证"解析出的值"与"小数位判定"一致（欧式/空格千分位）。"""
    # 去前导币种标识：符号前缀字母 US$/HK$ → 去字母留符号；3 字母 ISO 码 + 空格 → 去码
    text = re.sub(r"^\s*[A-Za-z]{1,3}\s*(?=[$\u20ac\u00a3\u00a5\u20b9\u20a9\u0e3f\u20b1\u20a6\u20ab\u20a8\u20aa])", "", text)
    text = re.sub(r"^\s*[A-Za-z]{3}\b\s+", "", text)
    # 去**尾部** 3 字母 ISO 币种码（"4000.00 USD" / "1,500.00 EUR"）——值语境下数字后的 3 字母即币种码
    text = re.sub(r"(?<=\d)\s+[A-Za-z]{3}\s*$", "", text)
    # 空格千分位（1 234,56 / 1 234.56）——去数字间普通/不断行空格（此时已是单个金额 token，安全）
    text = re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d)", "", text)
    # 瑞士撇号千分位：1'234.56 → 1234.56（撇号/右单引号在数字间只作千分位）
    text = re.sub(r"(?<=\d)['\u2019](?=\d)", "", text)
    # 小数分隔：同含 . 和 , → 靠后者为小数点（欧式 1.234,56→1234.56）；仅含 , 且末组非 3 位 → 小数点
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")     # 欧式：逗号在后=小数点
        else:
            # 美式/印度：点在后=小数点，逗号是分组——**仅当分组合法（末组恰 3 位、
            # 中间 2~3 位：美式 1,234 / 印度 lakh 1,23,456）才去逗号**；非法如 1,00 保留、交正则拒绝。
            ic = re.sub(r"[^\d,]", "", text.split(".", 1)[0])
            if "," not in ic or re.fullmatch(r"\d{1,3}(,\d{2,3})*,\d{3}", ic):
                text = text.replace(",", "")
    elif "," in text and "." not in text:
        tail = text.rsplit(",", 1)[-1]
        if text.count(",") == 1 and not re.fullmatch(r"\d{3}", tail):
            text = text.replace(",", ".")
    return text


def parse_amount(raw: Optional[str]) -> Tuple[Optional[Decimal], bool, str]:
    """解析金额字符串，**保留真实小数精度**（不强制两位）。

    返回 (Decimal 或 None, 是否可疑, 备注)。解析失败返回 (None, True, 原因)。
    小数位是否为两位的判断与"是否提取错误"的验证交由校验层处理，本函数不截断、不臆改。
    """
    if raw is None:
        return None, False, ""
    text = raw.translate(_DIGIT_MAP).strip()   # 阿拉伯-印度/波斯数字 → ASCII（含 ٫→. ٬→,）
    if not text:
        return None, False, ""
    # 会计式括号负数：(1,234.56) → 负（银行/流水常用括号表示支出/负额）
    paren_neg = text.startswith("(") and text.endswith(")")
    # 尾部负号：1,234.56- → 负（SAP/德式等把负号放在数字后面）
    trail_neg = bool(re.search(r"\d\s*-\s*$", text))
    if trail_neg:
        text = re.sub(r"\s*-\s*$", "", text)
    text = _normalize_seps(text)

    suspicious = False
    notes = []

    # 含字母等易混字符 -> 可疑，但仍尝试解析数字部分
    if _SUSPICIOUS_CHARS.search(text):
        suspicious = True
        notes.append("含易混字符(O/0,I/1等)")

    # 多个小数点 -> 可疑
    core = re.sub(r"[$€£¥₹₩฿₱₦₫₨₪,\s\(\)]", "", text)
    if _MULTI_DOT.search(core):
        suspicious = True
        notes.append("多个小数点")

    m = _AMOUNT_RE.match(text)
    if not m:
        return None, True, "金额格式不符合标准正则: " + "; ".join(notes)

    num = m.group("num").replace(",", "")
    try:
        d = Decimal(num)          # 原样保留精度，不 quantize
    except InvalidOperation:
        return None, True, "无法转为 Decimal"
    if m.group("sign") == "-" or paren_neg or trail_neg:   # 前导 "+" 仅表正、不翻号
        d = -d

    return d, suspicious, "; ".join(notes)


def decimal_places(raw: Optional[str]) -> Optional[int]:
    """返回金额原始文本的小数位数；无小数点返回 None。

    先按 `_normalize_seps` 归一（与 parse_amount 一致），再数小数位——否则欧式 `59.400,00`
    会被当成 5 位小数、空格千分位 `88 400,00` 数不到小数位，误触 DECIMAL_NONSTANDARD/低置信。"""
    if not raw:
        return None
    core = _normalize_seps(raw.strip())
    core = re.sub(r"[$€£¥₹₩฿₱₦₫₨₪,\s\(\)-]", "", core)   # 归一后剩余千分位逗号/符号一并去掉
    if "." not in core:
        return None
    return len(core.split(".")[-1])


def normalize_for_match(raw: Optional[str]) -> str:
    """规整金额文本用于双引擎字符串匹配：去掉币种符号/千分位/空格/括号。"""
    if not raw:
        return ""
    return re.sub(r"[$€£¥₹₩฿₱₦₫₨₪,\s\(\)]", "", raw.strip())
