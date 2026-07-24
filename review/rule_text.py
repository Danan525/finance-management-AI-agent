"""把人工**自由写的整段规则说明**，就地（纯规则 / 关键词，绝不调用任何模型）解析成结构化字段。

用途：用户在「✎ 修改」框里随便写一句话（如"以后老王家的发票都记 6400 咨询费"），
点「解析这句话」→ 本模块抽取出 match_key / category / account / target / value 等，
把**读懂的**填进下方结构化空格、**没读出的**明确标 missing，交人工确认后才启用。
解析不到就如实说"没读出"，绝不假装生效——契合"软先验、人工把关"的红线。
"""
import re
from typing import Optional

from core import db
from extraction.classify import rules as classify_rules

# 中文字段名 → 字段 key（越长越先匹配，避免"开票方"吃掉"开票方地址"）
_FIELD_CN = {
    "开票方地址": "issuer_address", "开票方邮箱": "issuer_email", "开票方电话": "issuer_phone",
    "客户地址": "customer_address", "客户邮箱": "contact_email", "客户电话": "contact_phone",
    "结算币种": "currency_settlement", "币种符号": "currency_display_symbol",
    "税前金额": "subtotal", "应付金额": "payment_due", "总金额": "total_due",
    "发票日期": "invoice_date", "发票号": "invoice_no", "到期日": "payment_due_date",
    "服务起": "service_start", "服务止": "service_end", "估值日": "fund_valuation_date",
    "开户行": "bank_name", "户名": "bank_account_name", "账号": "bank_account_no",
    "开票方": "issuer_name", "客户": "customer_name", "币种": "invoice_ccy_raw",
    "税额": "sales_tax", "税率": "tax_rate", "SWIFT": "bank_swift", "swift": "bank_swift",
}
# 按中文名长度降序，长名优先
_FIELD_CN_ORDER = sorted(_FIELD_CN.items(), key=lambda kv: -len(kv[0]))

_SEP = r"[=＝:：是为]"                       # 允许 = ： 是 为 等赋值口吻


def _find_field(text: str) -> Optional[str]:
    """文本里提到的目标字段 key（取最先出现、名字最长的那个）。"""
    best = None
    for cn, key in _FIELD_CN_ORDER:
        idx = text.find(cn)
        if idx >= 0 and (best is None or idx < best[0] or (idx == best[0] and len(cn) > best[2])):
            best = (idx, key, len(cn))
    return best[1] if best else None


def _known_pairs():
    """已知 (分类, 科目) 候选：已学的 + 种子对。"""
    pairs = list(db.learned_class_pairs()) + list(classify_rules.seed_pairs())
    out = []
    seen = set()
    for cat, acc in pairs:
        k = (cat or "", acc or "")
        if k not in seen:
            seen.add(k)
            out.append((cat, acc))
    return out


def _match_account(text: str):
    """从文本里认出 (分类, 科目)。先整体匹配已知对里的科目/分类名，再退到显式 '科目=X / 分类=Y'。"""
    cat = acc = None
    low = text.lower()
    # 1) 已知库：文本里出现某个科目名或分类名，就采用该对（科目名更长更具体，优先）
    cands = _known_pairs()
    for c, a in sorted(cands, key=lambda p: -len(p[1] or "")):
        if a and a.lower() in low:
            return c, a
    for c, a in sorted(cands, key=lambda p: -len(p[0] or "")):
        if c and c.lower() in low:
            cat, acc = c, a
            break
    # 2) 显式赋值："科目=6400 咨询费"、"分类：办公费"
    m = re.search(r"科目\s*" + _SEP + r"\s*(.+?)(?:[；;，。\n]|$)", text)
    if m:
        acc = m.group(1).strip()
    m = re.search(r"分类\s*" + _SEP + r"\s*(.+?)(?:[；;，。\n]|$)", text)
    if m:
        cat = m.group(1).strip()
    # 3) 纯科目代码（4~6 位数字）——补进 account（若还没有）
    if not acc:
        m = re.search(r"\b(\d{4,6})\b", text)
        if m:
            acc = m.group(1)
    return cat, acc


_LEAD = r"^(?:以后|今后|以後|之后|往后|以\s|接下来|凡是|所有)\s*"


def _clean_issuer(s: str) -> str:
    s = re.sub(_LEAD, "", (s or "").strip())
    return s.strip()


def _match_issuer(text: str, fallback: Optional[str]) -> Optional[str]:
    """认出开票方（对手方）。支持 '开票方=X'、'X 家'、'X 的发票'；认不出退回原值。"""
    m = re.search(r"开票方\s*" + _SEP + r"\s*(.+?)(?:\s|的|[，,；;。\n]|$)", text)
    if m and m.group(1).strip():
        return _clean_issuer(m.group(1))
    m = re.search(r"([^\s，,；;。的]+?)\s*家(?:的)?", text)     # "老王家"、"Acme 家的"
    if m and _clean_issuer(m.group(1)):
        return _clean_issuer(m.group(1))
    m = re.search(r"([^\s，,；;。]+?)\s*的发票", text)          # "Acme Labs 的发票"
    if m and _clean_issuer(m.group(1)):
        return _clean_issuer(m.group(1))
    return fallback


def _match_value(text: str) -> Optional[str]:
    """认出要填入的值：优先「…」/ "…" 引号内，其次 '填为 X' / '填成 X'。"""
    m = re.search(r"[「\"“](.+?)[」\"”]", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"填(?:为|成|入|进)\s*" + _SEP + r"?\s*(.+?)(?:[，,；;。\n]|$)", text)
    if m:
        return m.group(1).strip()
    return None


def parse(text: str, rule_type: str, current: Optional[dict] = None) -> dict:
    """把自由文本解析成该规则类型的字段。
    返回 {fields:{...解析到的...}, understood:[中文项], missing:[中文项]}。
    current 提供原规则字段做兜底（如开票方认不出就沿用原开票方）。
    """
    text = (text or "").strip()
    current = current or {}
    fields, understood, missing = {}, [], []

    if rule_type in ("classification", "content_class"):
        cat, acc = _match_account(text)
        if rule_type == "classification":
            issuer = _match_issuer(text, current.get("match_key"))
            if issuer:
                fields["match_key"] = issuer
                understood.append(f"开票方＝{issuer}")
            else:
                missing.append("开票方")
        else:                                   # content_class：内容特征就是整句话
            fields["value"] = text
        if cat:
            fields["category"] = cat
            understood.append(f"分类＝{cat}")
        else:
            missing.append("分类")
        if acc:
            fields["account"] = acc
            understood.append(f"科目＝{acc}")
        else:
            missing.append("会计科目")

    elif rule_type == "field_default":
        issuer = _match_issuer(text, current.get("match_key"))
        if issuer:
            fields["match_key"] = issuer
            understood.append(f"开票方＝{issuer}")
        else:
            missing.append("开票方")
        tgt = _find_field(text)
        if tgt:
            fields["target"] = tgt
            understood.append(f"填入字段＝{tgt}")
        else:
            missing.append("填入哪个字段")
        val = _match_value(text)
        if val:
            fields["value"] = val
            understood.append(f"默认值＝{val}")
        else:
            missing.append("默认值")

    elif rule_type == "field_locator":
        val = _match_value(text)
        if val:
            fields["value"] = val
            understood.append(f"标签词＝{val}")
        else:
            missing.append("标签词（请用「」括起来）")
        tgt = _find_field(text)
        if tgt:
            fields["target"] = tgt
            understood.append(f"填入字段＝{tgt}")
        else:
            missing.append("填入哪个字段")

    elif rule_type == "multi_invoice":
        if re.search(r"单张|不.*拆|别.*拆|当作一张|合并", text):
            fields["value"] = "single"
            understood.append("倾向＝当作单张·不自动拆")
        elif re.search(r"多张|多.*拆|积极.*拆|拆开|分开", text):
            fields["value"] = "multi"
            understood.append("倾向＝当作多张·更积极拆")
        else:
            missing.append("倾向（写“单张/不拆”或“多张/拆开”）")

    else:  # line_split 等：整段作为值，交人工核对
        if text:
            fields["value"] = text
            understood.append("已记录（拆分规律仍建议用示例确认）")

    fields["note"] = text                        # 原句始终存为显示说明
    return {"fields": fields, "understood": understood, "missing": missing}
