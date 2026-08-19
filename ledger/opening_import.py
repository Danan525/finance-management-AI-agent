"""期初余额批量导入：解析 Excel/CSV → post_opening 需要的 items/other_lines。

接手既有账套时科目/往来动辄几十上百行，界面手填不可行——支持拖入一张表批量建账。
本模块只做**解析 + 校验**（纯逻辑、可测），实际过账仍走 `service.post_opening`（复用建账原语与全部闸门）。
红线沿用：解析不记账；有错先返回预览让人改；提交仍是人显式触发。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from . import accounts as A

# 列名同义词（归一：小写去空格）——中英兼容，尽量吃下用户各种表头写法
_ACCOUNT_KEYS = {"account", "科目", "科目编码", "科目名称", "编码", "code", "会计科目", "科目代码"}
_AMOUNT_KEYS = {"amount", "金额", "余额", "balance", "期初余额", "期初金额"}
_SIDE_KEYS = {"side", "方向", "借贷", "借贷方向", "dr/cr", "drcr", "借/贷"}
_DEBIT_KEYS = {"debit", "借方", "借方金额", "借", "借方余额"}
_CREDIT_KEYS = {"credit", "贷方", "贷方金额", "贷", "贷方余额"}
_CP_KEYS = {"counterparty", "对手方", "往来单位", "客户", "供应商", "单位名称", "对方", "往来对象"}
_REF_KEYS = {"ref", "摘要", "备注", "memo", "单据号", "note", "说明"}


def _nk(k) -> str:
    return str(k or "").strip().lower().replace(" ", "").replace("　", "")


def _get(row: dict, keys: set):
    for k, v in row.items():
        if _nk(k) in keys:
            return v
    return None


def _num(v) -> Optional[Decimal]:
    """'1,000.00'/数字/Decimal → Decimal；空/非法 → None。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = str(v).strip().replace(",", "").replace("，", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _norm_side(v) -> str:
    s = str(v or "").strip().lower()
    if s in ("debit", "dr", "借", "借方", "d"):
        return "debit"
    if s in ("credit", "cr", "贷", "贷方", "c"):
        return "credit"
    return ""


def _resolve_account(raw: str) -> str:
    """把用户写法补全为科目表规范全名：已带编码→规范化；只写名称→**只认完全等于科目全名**。

    绝不做部分/token 匹配——`raw in n.split()` 曾把 "Tax" 静默配到第一个含该 token 的科目
    （1180 进项税额），无告警落错科目（自检发现，2026-08-11 修）。认不出就原样返回，
    让下游 `account_type` fail-closed 报错，逼用户写编码或全名，不静默猜。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if A.account_code(raw):
        return A.canonical_account(raw)
    for c, n, *_ in A.chart():
        if raw == n:                           # 仅完全等于科目全名才补编码
            return "%s %s" % (c, n)
    return raw                                  # 认不出→原样返回，交下游校验报错


def parse_opening_rows(rows: List[dict]) -> dict:
    """把逐行 dict（列名→值）分类为 items(往来)/other_lines(其它) + errors（行级）。

    往来控制账户(应付/应收)→ items（需对手方，方向由 control_side 定，不看 side）；
    其它科目 → other_lines（需借/贷方向）。方向可用 side 列，或借方/贷方两列（哪列有值定方向）。
    金额一律正数；负数/借贷双列同时有值/缺方向/无法归类 → 记为 error（不导入，先给人改）。
    """
    items: List[dict] = []
    others: List[dict] = []
    errors: List[dict] = []
    for i, raw in enumerate(rows):
        rownum = i + 2                          # 表头占第 1 行，数据从第 2 行起
        if not any(str(v).strip() for v in raw.values() if v is not None):
            continue                            # 整行空 → 跳过

        def err(msg):
            errors.append({"row": rownum, "msg": msg})

        acct = _resolve_account(str(_get(raw, _ACCOUNT_KEYS) or ""))
        if not acct:
            err("缺科目")
            continue
        cp = str(_get(raw, _CP_KEYS) or "").strip()
        dr = _num(_get(raw, _DEBIT_KEYS))
        cr = _num(_get(raw, _CREDIT_KEYS))
        if dr and cr and dr > 0 and cr > 0:
            err("借方/贷方两列不能同时有值：%s" % acct)
            continue
        if dr and dr > 0:
            amount, side = dr, "debit"
        elif cr and cr > 0:
            amount, side = cr, "credit"
        else:
            amount = _num(_get(raw, _AMOUNT_KEYS))
            side = _norm_side(_get(raw, _SIDE_KEYS))
        if amount is None or amount <= 0:
            err("金额缺失或非正（负额请用正数+借贷方向表示）：%s" % acct)
            continue
        if A.account_type(acct) is None:
            err("科目无法归类（需 1资产/2负债/3权益/4收入/5·6费用 开头）：%s" % acct)
            continue

        if A.is_control(acct):
            if not cp:
                err("往来控制账户须指定对手方：%s" % acct)
                continue
            ref = str(_get(raw, _REF_KEYS) or "").strip()
            items.append({"account": acct, "counterparty": cp,
                          "amount": str(amount), "ref": ref or cp})
        else:
            if side not in ("debit", "credit"):
                err("非往来科目须注明借/贷方向：%s" % acct)
                continue
            others.append({"account": acct, "amount": str(amount), "side": side})
    return {"items": items, "other_lines": others, "errors": errors}


def read_opening_table(data: bytes, filename: str) -> List[dict]:
    """读 xlsx/csv 首个工作表为逐行 dict（首行表头）。"""
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        return [dict(r) for r in csv.DictReader(io.StringIO(text))]
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    headers = [str(c).strip() if c is not None else "" for c in rows[0]]
    out = []
    for r in rows[1:]:
        out.append({headers[j]: r[j] for j in range(min(len(headers), len(r))) if headers[j]})
    return out


def parse_opening_file(data: bytes, filename: str) -> dict:
    """读表 + 解析，返回 {items, other_lines, errors}。读表失败抛 ValueError。"""
    try:
        rows = read_opening_table(data, filename)
    except Exception as e:
        raise ValueError("无法读取文件（需 .xlsx 或 .csv）：%s" % e)
    return parse_opening_rows(rows)
