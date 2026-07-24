"""数据模型。

每个字段同时保留「原始文本值」与「标准化值」，并带置信度、来源、可疑标记。
金额一律用 Decimal。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any, Dict, List, Optional


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return {"__dec__": str(v)}     # 类型标签：保证反序列化能原样还原 Decimal（金额精度不丢）
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def _from_jsonable(v: Any) -> Any:
    """_jsonable 的逆操作：还原 Decimal 类型标签与嵌套结构。"""
    if isinstance(v, dict):
        if set(v.keys()) == {"__dec__"}:
            return Decimal(v["__dec__"])
        return {k: _from_jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_from_jsonable(x) for x in v]
    return v


@dataclass
class FieldValue:
    """单个字段：原始文本 + 标准化值 + 置信度 + 来源 + 可疑标记。"""
    raw: Optional[str] = None          # 原始文本值，如 "$24,946.34"
    value: Any = None                  # 标准化值，如 Decimal("24946.34")
    confidence: float = 1.0
    source: str = "pdf_text"           # pdf_text / pdf_text_cross / ocr / ocr_recheck
    suspicious: bool = False           # O/0、I/1、,/. 等易混字符
    note: str = ""
    bbox: Optional[List[float]] = None  # 原件中位置 [page, x0, y0, x1, y1]（pt，左上原点），供审核界面双向联动；缺失=未定位到

    def to_jsonable(self) -> Dict[str, Any]:
        return {
            "raw": self.raw,
            "value": _jsonable(self.value),
            "confidence": self.confidence,
            "source": self.source,
            "suspicious": self.suspicious,
            "note": self.note,
            "bbox": self.bbox,
        }

    @classmethod
    def from_jsonable(cls, d: Dict[str, Any]) -> "FieldValue":
        return cls(
            raw=d.get("raw"),
            value=_from_jsonable(d.get("value")),
            confidence=d.get("confidence", 1.0),
            source=d.get("source", "pdf_text"),
            suspicious=d.get("suspicious", False),
            note=d.get("note", ""),
            bbox=d.get("bbox"),
        )


@dataclass
class LineItem:
    item_no: Optional[str] = None
    description: Optional[str] = None
    service_period: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    tax_rate: Optional[str] = None
    amount: Optional[Decimal] = None
    amount_raw: Optional[str] = None
    line_confidence: float = 1.0
    note: Optional[str] = None              # 备注/风险提示（如未识别金额）
    source_file: Optional[str] = None
    bbox: Optional[List[float]] = None      # 明细在原件中的位置 [page,x0,y0,x1,y1]（pt），供审核界面高亮/定位；缺失=未定位
    # 尾随"类别明细附表"归属到本行的子明细 [{date, description, amount}]（已按类别勾稽），
    # 存字符串、随快照往返；主明细金额仍以本行 amount 为准（子行不参与 Σ明细==小计 校验，避免重复计）
    sub_items: List[dict] = field(default_factory=list)


@dataclass
class Transaction:
    """银行流水一笔交易：日期 / 摘要 / 收入(贷) / 支出(借) / 余额。金额用 Decimal。"""
    date: Optional[str] = None               # 标准化 ISO 日期
    date_raw: Optional[str] = None
    description: Optional[str] = None
    income: Optional[Decimal] = None         # 收入/存入/贷方
    expense: Optional[Decimal] = None        # 支出/取出/借方
    balance: Optional[Decimal] = None        # 交易后余额
    currency: Optional[str] = None           # 本笔币种（合并对账单每笔可不同；缺失时回退账户头币种）
    note: Optional[str] = None
    bbox: Optional[List[float]] = None        # 该行在原件中的位置 [page,x0,y0,x1,y1]（pt），供高亮


@dataclass
class PaymentDetail:
    method: Optional[str] = None             # On-chain / Bank transfer / Other ...
    chain: Optional[str] = None              # Ethereum / Arbitrum / Tron ...
    wallet_address: Optional[str] = None     # 链上地址，或银行账户标识
    settlement_currency: Optional[str] = None
    payment_status: Optional[str] = None
    valid_address: bool = True
    raw: Optional[str] = None                # 原始文本（保证信息不丢失）
    note: Optional[str] = None               # 备注/风险提示
    source_file: Optional[str] = None


@dataclass
class ValidationIssue:
    code: str                                # 机器可读，如 TOTAL_MISMATCH
    message: str
    field: Optional[str] = None
    severity: str = "warning"                # info / warning / error / critical


@dataclass
class Classification:
    category: Optional[str] = None           # 建议分类
    account: Optional[str] = None            # 建议会计科目
    confidence: float = 0.0
    hit_rules: List[str] = field(default_factory=list)
    needs_review: bool = True                # 默认每项都需人工复核


# 规范字段键（同时驱动解析、校验、Excel 列）
CANONICAL_FIELDS = [
    "invoice_no", "invoice_date", "payment_due_date",
    "service_start", "service_end", "fund_valuation_date",
    "invoice_ccy_raw", "currency_display_symbol", "currency_settlement",
    "issuer_name", "issuer_address", "issuer_email", "issuer_phone",
    "customer_name", "customer_address", "contact_email", "contact_phone",
    "subtotal", "tax_rate", "sales_tax", "total_due", "payment_due",
    "bank_name", "bank_account_name", "bank_account_no", "bank_swift",
]

# 银行流水的账户头字段（doc_type='statement' 时在审核界面展示；交易明细另存 transactions）
STATEMENT_FIELDS = [
    "bank_name", "bank_account_name", "bank_account_no", "bank_swift",
    "statement_period_start", "statement_period_end",
    "opening_balance", "closing_balance", "currency_settlement",
]


@dataclass
class Invoice:
    # ---- 文件 / 元数据 ----
    file_name: str = ""
    file_hash: str = ""
    file_path: str = ""
    doc_type: str = "invoice"                # 单据类型：invoice（发票）| statement（银行流水）
    parse_method: str = "pdf_text"           # pdf_text / ocr
    ocr_used: bool = False
    ocr_engine: str = ""
    raw_pdf_text: str = ""
    raw_ocr_text: str = ""
    cross_engine_text: str = ""              # 第二引擎文本（pdfplumber/二次OCR），供交叉验证
    uploaded_at: str = ""
    processed_at: str = ""
    page_sizes: List[list] = field(default_factory=list)  # 各页尺寸 [[w,h],...]（pt），供审核界面按比例叠加字段框
    # OCR 件整页词几何（归一化 0~1）：[[page, nx0, ny0, nx1, ny1, text], ...]，
    # 供框选取字直接按坐标取词（免每次实时 OCR，秒开），仅 OCR 路径填充。
    ocr_words: List[list] = field(default_factory=list)
    # ---- 多发票合集关联（一个上传文件切出多张发票时，各张记录共享同一"源文件"）----
    source_file_hash: str = ""   # 源文件（原始上传文件）哈希 = 合集分组键；单张时留空（视为自成一组）
    source_file_name: str = ""   # 源文件原始名（合集展示用）
    source_file_path: str = ""   # 源文件落盘路径（重新提取 / 重新切分用）
    segment_index: int = 0       # 该发票在源文件中的序号（1-based）；0 或 1 表示单张
    segment_total: int = 1       # 源文件切出的发票总数；1 表示单张（非合集）

    # ---- 字段 ----
    fields: Dict[str, FieldValue] = field(default_factory=dict)
    line_items: List[LineItem] = field(default_factory=list)
    transactions: List["Transaction"] = field(default_factory=list)   # 银行流水逐笔交易（doc_type='statement'）
    payments: List[PaymentDetail] = field(default_factory=list)
    issues: List[ValidationIssue] = field(default_factory=list)
    classification: Classification = field(default_factory=Classification)

    # ---- 评分 ----
    ocr_quality: float = 1.0
    ocr_quality_level: str = "Excellent"     # Excellent/Good/Warning/HighRisk
    key_field_confidence: float = 1.0
    amount_field_confidence: float = 1.0
    decimal_confidence: float = 1.0
    field_coverage: float = 1.0              # 必填字段实际抓到的比例（缺失记 0，不剔除）；与"字符清晰度"分开
    risk_score: int = 0
    recheck_count: int = 0

    # ---- 状态 ----
    parse_status: str = "parsed"             # parsed / failed
    validation_status: str = "pending"       # passed / has_issues
    needs_manual_review: bool = True
    critical_review: bool = False
    rev: int = 0                             # 乐观锁版本：每次回写 +1，多人并发编辑时防「后写覆盖先写」
    # 人工审核相关（本期占位，不做交互）
    review_status: str = "Pending Review"
    approve_status: str = "Pending"
    correction_status: str = ""
    learning_status: str = ""

    # ---- 便捷访问 ----
    def f(self, key: str) -> FieldValue:
        """取字段，缺失则返回空 FieldValue（不抛错）。"""
        return self.fields.get(key, FieldValue())

    def set(self, key: str, fv: FieldValue) -> None:
        self.fields[key] = fv

    def distinct_payment_targets(self) -> List[tuple]:
        """去重后的付款去向列表 [(方式标签, 地址/账户), ...]，保留出现顺序。

        标签为「方式（链）」，按 (标签, 地址) 去重，区分同方式不同地址的收款去向。
        """
        out: List[tuple] = []
        seen = set()
        for p in self.payments:
            label = p.method or "未知方式"
            if p.chain:
                label = f"{label}（{p.chain}）"
            key = (label, p.wallet_address)
            if key not in seen:
                seen.add(key)
                out.append((label, p.wallet_address))
        return out

    @property
    def has_multiple_payment_methods(self) -> bool:
        """是否存在两个及以上不同的付款方式/收款去向（需重点人工审核）。"""
        return len(self.distinct_payment_targets()) >= 2

    def add_issue(self, code: str, message: str, field_: Optional[str] = None,
                  severity: str = "warning") -> None:
        self.issues.append(ValidationIssue(code, message, field_, severity))

    # 完整往返所需的标量字段（DB payload 作为唯一数据源，导出/恢复都靠它重建）
    _SCALAR_FIELDS = (
        "file_name", "file_hash", "file_path", "doc_type", "parse_method", "ocr_used",
        "ocr_engine", "raw_pdf_text", "raw_ocr_text", "cross_engine_text",
        "uploaded_at", "processed_at",
        "ocr_quality", "ocr_quality_level", "key_field_confidence",
        "amount_field_confidence", "decimal_confidence", "field_coverage",
        "risk_score", "recheck_count", "parse_status", "validation_status",
        "needs_manual_review", "critical_review", "review_status",
        "approve_status", "correction_status", "learning_status",
        "page_sizes", "ocr_words", "rev",
        "source_file_hash", "source_file_name", "source_file_path",
        "segment_index", "segment_total",
    )

    def to_jsonable(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self._SCALAR_FIELDS}
        d["fields"] = {k: v.to_jsonable() for k, v in self.fields.items()}
        d["line_items"] = [_jsonable(asdict(li)) for li in self.line_items]
        d["transactions"] = [_jsonable(asdict(t)) for t in self.transactions]
        d["payments"] = [_jsonable(asdict(p)) for p in self.payments]
        d["issues"] = [asdict(i) for i in self.issues]
        d["classification"] = _jsonable(asdict(self.classification))
        return d

    @classmethod
    def from_jsonable(cls, d: Dict[str, Any]) -> "Invoice":
        """从 to_jsonable 的快照完整重建 Invoice（含 Decimal、嵌套对象）。"""
        inv = cls()
        for k in cls._SCALAR_FIELDS:
            if k in d and d[k] is not None:
                setattr(inv, k, d[k])
        inv.fields = {k: FieldValue.from_jsonable(v) for k, v in d.get("fields", {}).items()}
        inv.line_items = [LineItem(**_from_jsonable(x)) for x in d.get("line_items", [])]
        inv.transactions = [Transaction(**_from_jsonable(x)) for x in d.get("transactions", [])]
        inv.payments = [PaymentDetail(**_from_jsonable(x)) for x in d.get("payments", [])]
        inv.issues = [ValidationIssue(**x) for x in d.get("issues", [])]
        cl = d.get("classification")
        if cl:
            inv.classification = Classification(**_from_jsonable(cl))
        return inv
