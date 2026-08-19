"""全局配置：路径、阈值、安全基线。

所有财务数据只留本地，后端默认只监听 127.0.0.1。
"""
from __future__ import annotations

from pathlib import Path

# ---- 目录 ----------------------------------------------------------------
# core/config.py -> 项目根；运行期数据统一放 项目根/data/（gitignore）
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_ROOT = BASE_DIR / "data"
STORAGE_DIR = DATA_ROOT / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
EXPORT_DIR = DATA_ROOT / "exports"
DATA_DIR = DATA_ROOT
DB_PATH = DATA_ROOT / "app.db"
BACKUP_DIR = DATA_ROOT / "backups"          # 本地数据库快照（不出机、非 git 跟踪）
PAGE_CACHE_DIR = DATA_ROOT / "cache" / "pages"
# 分类规则可配置文件（科目表/关键词/供应商/固定资产阈值）——缺省或损坏则用代码内置默认，
# 行为完全不变；用户改这一个 JSON 即可定制自己的科目表，无需改代码。见 config/classification.json。
CLASSIFY_RULES_PATH = BASE_DIR / "config" / "classification.json"
# 总账科目表可配置文件（编码/名称/类别/方向/报表行归属）——缺省/损坏则用内置默认，行为不变；
# 用户改这一个 JSON 即可加/改科目与报表行,无需改代码(规则即数据,计划 §3.7)。见 config/chart_of_accounts.example.json。
CHART_PATH = BASE_DIR / "config" / "chart_of_accounts.json"
FX_RATES_PATH = BASE_DIR / "config" / "fx_rates.json"   # 外币→功能货币 汇率表（Frankfurter 按日拉取更新的本地缓存）
FX_AUTO_FETCH = True               # 本地缺当日汇率时是否自动向 provider 拉一次（测试/离线可置 False）；只拉公开汇率、不发内部数据
BUSINESS_TIMEZONE = "Asia/Shanghai"  # 业务日期/汇率调度统一按北京时间；审计时间戳仍存 UTC
FX_UPDATE_STATE_PATH = DATA_ROOT / "fx-update-state.json"  # 去重：最后已推送的汇率有效日（运行时状态，不入 git）
POSTING_ROLES_PATH = BASE_DIR / "config" / "posting_accounts.json"   # 关键过账科目角色（AP/AR/税/收入/银行/差额…）可配置覆盖

for _d in (UPLOAD_DIR, EXPORT_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---- 总账/记账口径 ------------------------------------------------------
FUNCTIONAL_CURRENCY = "USD"        # 功能货币；MVP 单一币种,外币发票入账前须换算(否则拒绝,防静默当 USD)
INPUT_TAX_DEDUCTIBLE = True        # 收票进项税默认可抵扣(VAT/GST 辖区);置 False=不可抵扣、税并入成本(美国销售税)
                                   # 可按发票覆盖(post_invoice(tax_deductible=...))。计划 §3.2 保守默认为不可抵扣,
                                   # 本工具面向多国(多数辖区可抵扣)故默认 True,US 等取消勾选或置 False。

# ---- 数据安全 / 磁盘留存（运维加固）------------------------------------
BACKUP_KEEP = 14             # 保留最近 N 份数据库快照（默认每日一份 → 约两周）
BACKUP_MIN_INTERVAL_H = 20   # 启动时若最近快照已超过该小时数才再备份（避免频繁重启狂备份）
PAGE_CACHE_MAX_FILES = 1000  # 页面图片缓存 PNG 上限（超出按最旧删，防磁盘只增不减）
EXPORT_KEEP = 20             # 保留最近 N 份导出 xlsx
MAX_CONCURRENT_PROCESS = 2   # 同时处理（OCR/渲染/转换）的上限（200% CPU/8GB 配额下的背压）

# ---- 服务（本地安全基线）-------------------------------------------------
HOST = "127.0.0.1"          # 严禁绑定 0.0.0.0；财务数据不出机
PORT = 8000

# ---- 金额 ----------------------------------------------------------------
AMOUNT_DECIMALS = 2          # 期望小数位（仅作校验参考，不强制截断，保留真实精度）

# ---- 上传限制（后端强制，前端 accept 仅提示，可绕过）--------------------
MAX_UPLOAD_BYTES = 30 * 1024 * 1024          # 单文件上限 30MB，防止超大文件占满内存
# 图像安全：像素上限（防"解压炸弹"——小文件解码成上亿像素撑爆内存，共享机 OOM 会波及所有服务）；
# OCR 前把超大图按最长边封顶缩小，限制 OCR 内存/耗时（超出对识别无益）。
MAX_IMAGE_PIXELS = 80_000_000                # 约 A3@600dpi；超过按解压炸弹拒绝（PIL 抛错→兜底 failed 记录）
OCR_MAX_SIDE = 4500                          # OCR 输入最长边上限（≈A4@380dpi，够清晰且内存可控）
PREVIEW_MAX_SIDE = 2000                      # 图片件原件预览最长边上限：超大图缩小后再传，避免每次显示卡顿
# 放大版 DoS 防护：单文件页数 / Word·Excel 内嵌图数上限（防千页 PDF 或几百张内嵌图 → 海量渲染/OCR）
MAX_PDF_PAGES = 200                          # 超过 → 兜底 failed 记录，提示拆分后上传
MAX_EMBEDDED_IMAGES = 50                     # 超过则只处理前 N 张并记日志（绝不静默全量处理）
MAX_INVOICES_PER_FILE = 100                  # 单文件拆分出的发票数上限（xlsx多表/docx块/文本段）——
#                                              防"小文件放大成海量记录"（如 300 表 xlsx→300 条、
#                                              1KB 多 TOTAL DUE 文本→上百条）；超出只取前 N 并记日志
UPLOAD_RETENTION_DAYS = 30                    # uploads/ 里**无任何记录引用**的孤儿文件（撤销/重切后
#                                              被删记录的遗留、转换中间件）保留天数，超期才 GC；
#                                              被记录引用的原件（含 failed 记录）永久保留、绝不删
MAX_STATEMENT_ROWS = 100000                  # 流水 CSV/xlsx/HTML 行数上限（防 50 万行拖垮解析）
# 允许上传的扩展名 = 各处理路径**真实支持的并集**（上传落盘前强制，拒真·垃圾如 .exe/.zip/.mp3；
# process_path 仍按 doc_type 做权威判定）。**新增支持格式时须同步这里**——否则会误拒（可见、非静默）。
# 来源：pipeline._KNOWN + is_image + office._CONVERTIBLE + statement_structured.STRUCTURED_EXTS。
ALLOWED_UPLOAD_EXTS = {
    ".pdf", ".docx", ".docm", ".xlsx", ".xlsm",                                  # 直接支持
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif",           # 图片(OCR)
    ".doc", ".dot", ".xls", ".xlt", ".ppt", ".pot", ".pps",                      # LibreOffice 可转
    ".rtf", ".odt", ".ods", ".odp", ".fodt", ".fods",
    ".csv", ".tsv", ".json", ".ndjson", ".jsonl", ".qif",                        # 流水结构化
    ".mt940", ".sta", ".ofx", ".qfx", ".xml", ".camt053", ".htm", ".html",
}

# ---- PDF 判型 ------------------------------------------------------------
# 每页可抽取字符数低于该阈值，判定为扫描型 / 需要 OCR 兜底
MIN_TEXT_CHARS_PER_PAGE = 100

# 纯数字日期 日/月 有歧义时（如 05/06，两位都 ≤12）的默认解读：
#   False = 月/日在前(美式 MM/DD，保持历史默认)；True = 日/月在前(DD/MM，港/欧/多数非美地区)。
# 无论取哪种，歧义日期都会**标记待复核**（不静默猜）。
DATE_DAYFIRST = False

# ---- 置信度阈值（计划第六节 11~13）--------------------------------------
# 整体 OCR 质量分级
OCR_QUALITY_EXCELLENT = 0.98
OCR_QUALITY_GOOD = 0.95
OCR_QUALITY_WARNING = 0.90   # 低于此 -> High Risk -> Needs Review

# 关键字段置信度
KEY_FIELD_PASS = 0.99
KEY_FIELD_WARNING = 0.97
KEY_FIELD_RECHECK = 0.95     # 低于此 -> Needs Review

# 金额字段置信度
AMOUNT_FIELD_NORMAL = 0.995
AMOUNT_FIELD_RECHECK = 0.98  # 低于此 -> 重点人工审核

# 小数点字符置信度
DECIMAL_NORMAL = 0.99
DECIMAL_RECHECK = 0.95       # 低于此 -> Critical Review

# 文本型 PDF 直抽时的默认置信度（非 OCR，视为高可信）
PDF_TEXT_CONFIDENCE = 1.0

# ---- 风险评分（计划第六节 15）------------------------------------------
RISK_OCR_LOW = 20            # OCR < 95%
RISK_AMOUNT_LOW = 30         # 金额字段 < 98%
RISK_DECIMAL_LOW = 50        # 小数点 < 95%
RISK_TOTAL_FAIL = 100        # Total 校验失败
RISK_OCR_PDF_MISMATCH = 80   # OCR 与 PDF 文本不一致
RISK_DUAL_OCR_MISMATCH = 60  # 双 OCR 不一致
RISK_FIELD_MISSING = 40      # 每缺失一个必填身份字段（invoice_no/日期/total）加分；缺一个即超阈值进人工
RISK_THRESHOLD = 30          # >30 触发二次识别；二次后仍 >30 进人工审核

# 对账：金额 ≥ 此值的交易，即便被判为"无需发票"类型也**不自动跳过**，一律进人工确认（防误判漏票）。
RECONCILE_NO_MATCH_MAX_AMOUNT = 50000

# ---- 关键字段集合 --------------------------------------------------------
KEY_FIELDS = (
    "invoice_no", "invoice_date", "currency_settlement",
    "subtotal", "sales_tax", "total_due",
)
AMOUNT_FIELDS = ("subtotal", "sales_tax", "total_due")
# 必填身份字段：缺任一即视为"提取不完整"，强制人工、不得评 Excellent（提取完整性闸门）
REQUIRED_FIELDS = ("invoice_no", "invoice_date", "total_due")
# 通用兜底（版式无关启发式）抽到的字段置信度：低于模板精确命中，触发关键字段复核
GENERIC_FIELD_CONFIDENCE = 0.90
