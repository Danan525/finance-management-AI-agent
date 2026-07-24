"""日期解析：支持多格式，统一转为 ISO (YYYY-MM-DD)。

无法确定的保留原始文本并标记待复核。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

from core import config

# 纯数字 日[分隔]月[分隔]年（如 05/06/2026、13-6-26）——用于识别 日/月 歧义
_NUM_DMY = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\s*$")

# 支持的格式（宽容白名单）。注意：strptime 的 %Y 会吞 2 位年，故下方用"年份<1000 则跳过"
# 的守卫强制 4 位年格式让位给 %y 格式，避免 "06/26/25" 被 %m/%d/%Y 误解析成 0025 年。
_FORMATS = [
    "%d-%b-%Y",     # 22-Apr-2026
    "%d-%B-%Y",     # 22-April-2026
    "%d-%b-%y",     # 22-Apr-26（两位年）
    "%m/%d/%Y",     # 06/26/2025
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%m-%d-%Y",     # 06-26-2025（短横数字，美式在前）
    "%d-%m-%Y",     # 26-06-2025（短横数字，日在前）
    "%m/%d/%y",     # 06/26/25（两位年）
    "%d/%m/%y",
    "%d %b %Y",     # 28 Dec 2025
    "%d %B %Y",     # 28 December 2025（多供应商表头常见的竖排日期）
    "%d %b %y",     # 28 Dec 25（两位年）
    "%d %B, %Y",    # 28 December, 2025
    "%B %d, %Y",    # March 1, 2026
    "%B %d,%Y",     # March 1,2026 (样例描述里的写法)
    "%b %d, %Y",
    "%b %d,%Y",
    "%Y/%m/%d",
    "%Y.%m.%d",     # 2025.06.26（点分 ISO）
    "%d.%m.%Y",
]

# 描述中 "March 1,2026" 这类无空格逗号写法，先规整
_MONTHWORD = re.compile(
    r"([A-Za-z]+)\s+(\d{1,2})\s*,\s*(\d{4})")
# 预处理正则：中文年月日 / 序数词后缀 / 尾部时间
_CJK_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
_ORDINAL = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)
_TIME_TAIL = re.compile(r"[T\s]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:Z|[+-]\d{2}:?\d{2})?\s*$",
                        re.IGNORECASE)


def normalize_date(raw: Optional[str], dayfirst: Optional[bool] = None) -> Tuple[Optional[str], bool]:
    """返回 (ISO 日期字符串 或 None, 是否需复核)。

    解析成功 -> (iso, False)；失败 -> (None, True)。
    宽容处理常见变体：中文 年月日、序数词(1st/2nd/…)、尾部时间(T10:00:00)、两位年、
    短横/点分数字日期等——白名单外仍失败并标待复核（绝不臆造）。
    dayfirst：日/月歧义（两位都 ≤12）时是否日在前；None=用 `config.DATE_DAYFIRST`（默认月在前）。
    流水解析按整列推断出的序传入（如 UK/德式 DD/MM），避免逐个默认成 MM/DD 误解。
    """
    if raw is None:
        return None, False
    text = raw.strip().strip(".,")
    if not text:
        return None, False

    # 中文日期直接构造（最明确、无歧义）
    mc = _CJK_DATE.search(text)
    if mc:
        try:
            return datetime(int(mc.group(1)), int(mc.group(2)), int(mc.group(3))).strftime("%Y-%m-%d"), False
        except ValueError:
            pass
    # 去尾部时间（ISO 带时刻）+ 去序数词后缀（1st→1）
    text = _TIME_TAIL.sub("", text).strip()
    text = _ORDINAL.sub("", text)

    # 日/月歧义（05/06 这类，两位都 ≤12 且不等）：两种解读都合法 → 按配置默认解读，但**标待复核**（不静默猜）
    mnum = _NUM_DMY.match(text)
    if mnum:
        a, b, y = int(mnum.group(1)), int(mnum.group(2)), int(mnum.group(3))
        if y < 100:
            y += 2000
        if 1 <= a <= 12 and 1 <= b <= 12 and a != b:
            _df = config.DATE_DAYFIRST if dayfirst is None else dayfirst
            mo, da = (b, a) if _df else (a, b)
            try:
                return datetime(y, mo, da).strftime("%Y-%m-%d"), True
            except ValueError:
                pass

    candidates = [text]
    m = _MONTHWORD.search(text)
    if m:
        candidates.append(f"{m.group(1)} {m.group(2)}, {m.group(3)}")

    for cand in candidates:
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(cand, fmt)
            except ValueError:
                continue
            if dt.year < 1000:      # %Y 误吞了 2 位年 → 让位给后面的 %y 格式
                continue
            return dt.strftime("%Y-%m-%d"), False
    return None, True


def extract_period(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """从描述中抽取服务期间起止日期（标准化为 ISO）。

    支持:
      "from March 1,2026 to March 31,2026"
      "for period 06/26/2025 - 07/31/2025"
    返回 (start_iso, end_iso)，无法解析的项为 None。
    """
    if not text:
        return None, None

    # from X to Y
    m = re.search(r"from\s+(.+?)\s+to\s+(.+?)(?:\.|$|\s{2,})", text, re.IGNORECASE)
    if not m:
        # 带标签的期间：Billing period / Service dates / For the period / Period (of)：A to/through/– B
        m = re.search(
            r"(?:billing\s*period|service\s*(?:period|dates?)|for\s*the\s*period|period(?:\s*of)?)"
            r"\s*[:：]?\s*(.+?)\s*(?:to|through|thru|[-–—])\s*(.+?)(?:\.|$|\s{2,})",
            text, re.IGNORECASE)
    if not m:
        # 裸日期范围 A - B
        m = re.search(
            r"(\d{1,2}[/-][A-Za-z0-9]{1,9}[/-]\d{2,4}|\w+\s+\d{1,2}\s*,\s*\d{4})"
            r"\s*[-–—]\s*"
            r"(\d{1,2}[/-][A-Za-z0-9]{1,9}[/-]\d{2,4}|\w+\s+\d{1,2}\s*,\s*\d{4})",
            text,
        )
    if not m:
        return None, None
    start, _ = normalize_date(m.group(1))
    end, _ = normalize_date(m.group(2))
    return start, end
