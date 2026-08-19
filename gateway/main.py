"""FastAPI 入口：最小上传页 + 处理 + Excel 导出。

本地财务工具：仅监听 127.0.0.1，数据不出机，不接外部服务。
人工审核界面为后续阶段，本期不含。
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import json
from decimal import Decimal

import anyio
from fastapi import FastAPI, UploadFile, File, Body, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.responses import JSONResponse as _BaseJSONResponse
from starlette.concurrency import run_in_threadpool


def _json_default(o):
    """兜底：任何漏转字符串的 Decimal 一律转字符串（金额展示语义），
    杜绝个别端点因 dict 里塞了原始 Decimal 而整条 500（曾致「待入账」端点崩、前端显示空）。"""
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError("Object of type %s is not JSON serializable" % type(o).__name__)


class JSONResponse(_BaseJSONResponse):
    """全局 JSON 响应：在 starlette 默认基础上加 Decimal 兜底编码，防序列化 500。"""

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False,
                          indent=None, separators=(",", ":"),
                          default=_json_default).encode("utf-8")

from core import config, counterparty, db, maintenance
from extraction import pipeline
from review import service as review
from reconcile import service as reconcile
from ledger import service as ledger
from reports import service as reports
from core.models import Invoice

# 处理（OCR/渲染/转换）并发上限：200% CPU / 8GB 配额下的背压，防一次传大量大文件打满配额。
_process_limiter = anyio.CapacityLimiter(config.MAX_CONCURRENT_PROCESS)

logger = logging.getLogger("finance")


def _setup_logging() -> None:
    """结构化日志：只记方法/路径/状态/耗时/错误类型——**不记发票内容、金额、姓名、
    地址、钱包地址**（隐私红线，计划规定日志只留处理状态/文件编号/错误类型/时间）。
    幂等，避免重复挂 handler。"""
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(h)
    root.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: "FastAPI"):
    """应用生命周期（替代弃用的 on_event）：起时建表 + 配日志，停时无需清理。"""
    _setup_logging()
    db.init_db()   # 数据全部落 SQLite；列表/导出按需查询，不在内存常驻历史
    summary = maintenance.startup_maintenance()   # 完整性校验 + WAL 收敛 + 每日快照 + 清理缓存/导出
    logger.info("启动维护：%s", summary)
    _warm_ocr_engine()   # 后台预热主 OCR 引擎，消除首张图片/扫描件提取的冷启动卡顿（不预热 rapid，省内存）
    yield


def _warm_ocr_engine() -> None:
    """后台线程预加载主 OCR 引擎（paddle）：跑一次小图 OCR 触发模型加载，
    使首次提取/框选不必等冷启动。守护线程、异常吞掉，不阻塞启动、装不上 OCR 也无碍。"""
    import threading

    def _run():
        try:
            from extraction.extract import ocr as ocr_mod
            if not ocr_mod.ocr_available():
                return
            from PIL import Image
            ocr_mod.run_ocr(Image.new("RGB", (2000, 1400), "white"))   # 触发主引擎模型加载
            logger.info("OCR 引擎预热完成")
        except Exception as e:
            logger.info("OCR 预热跳过：%s", e)

    threading.Thread(target=_run, daemon=True).start()


app = FastAPI(title="财务管理系统", version="0.1.0", lifespan=lifespan)


# ---- 全站统一顶部导航栏（注入到每个页面；链接用 window.APP_BASE 运行时生成，主站/演示子路径自适配）----
_NAV_CSS = """<style>
  .appnav{position:sticky;top:0;z-index:200;background:#12325a;display:flex;align-items:center;
    gap:2px;padding:0 12px;min-height:48px;flex-wrap:wrap;box-shadow:0 2px 8px rgba(0,0,0,.18);
    font-family:-apple-system,Segoe UI,"Microsoft YaHei",sans-serif}
  .appnav .brand{color:#fff;font-weight:800;font-size:15px;margin-right:14px;white-space:nowrap;
    text-decoration:none;display:flex;align-items:center;gap:6px}
  .appnav a.navlink{color:#c7d5e8;text-decoration:none;font-size:13.5px;font-weight:600;
    padding:9px 13px;border-radius:8px;white-space:nowrap;line-height:1}
  .appnav a.navlink:hover{background:rgba(255,255,255,.13);color:#fff}
  .appnav a.navlink.on{background:#fff;color:#12325a}
  /* 页面自带 header 不再各自 sticky，避免与全局导航叠压 */
  body > header{position:static !important}
</style>"""

_NAV_HTML = """<nav class="appnav" id="appnav"></nav>
<script>(function(){
  var B = window.APP_BASE || '';
  var M = [["/","🏠 上传识别"],["/review","📝 审核"],["/reconcile","🔗 对账"],
           ["/ledger","📒 总账"],["/reports","📊 报表"],["/counterparties","🏢 对手方"],
           ["/learned","🧠 学习规则"],
           ["/help","📖 使用说明"]];
  var p = location.pathname;
  if (B && p.indexOf(B) === 0) p = p.slice(B.length) || '/';
  function on(path){ return path === '/' ? (p === '/' || p === '') : (p === path || p.indexOf(path + '/') === 0 || p === path); }
  var html = '<a class="brand" href="' + B + '/">💰 财务系统</a>';
  html += M.map(function(it){
    var tgt = it[0] === '/help' ? ' target="_blank"' : '';
    return '<a class="navlink' + (on(it[0]) ? ' on' : '') + '" href="' + B + it[0] + '"' + tgt + '>' + it[1] + '</a>';
  }).join('');
  document.getElementById('appnav').innerHTML = html;
})();</script>"""


def _page(html: str) -> str:
    """给整页 HTML 注入全站统一顶部导航（CSS 进 <head>、导航条紧跟 <body>）。"""
    if "</head>" in html:
        html = html.replace("</head>", _NAV_CSS + "\n</head>", 1)
    html = html.replace("<body>", "<body>\n" + _NAV_HTML, 1)
    return html


@app.middleware("http")
async def _access_log(request: Request, call_next):
    """访问日志：只记方法/路径/状态/耗时。路径里的 file_hash 是哈希、非敏感内容；
    绝不记录请求体（发票内容/金额/地址一律不入日志）。"""
    start = time.perf_counter()
    resp = await call_next(request)
    dur_ms = (time.perf_counter() - start) * 1000
    path = request.url.path
    # HTML 页面外壳不做浏览器缓存：每次导航都取最新页（避免部署/改导航后仍用旧页里的旧链接 → 404）。
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    # 高频低价值请求（页面图片瓦片 / 取原件 / 健康探活）降到 DEBUG，避免日志被刷爆、磁盘暴涨
    lvl = logging.DEBUG if _is_low_value_path(path) else logging.INFO
    logger.log(lvl, "%s %s -> %s %.0fms", request.method, path, resp.status_code, dur_ms)
    return resp


def _is_low_value_path(path: str) -> bool:
    return ("/page/" in path) or path.endswith("/original") or path == "/healthz"


@app.get("/healthz")
def healthz() -> JSONResponse:
    """健康检查：进程存活 + DB 可连（供运维/监控探活，无敏感数据）。"""
    db_ok = True
    try:
        with db.connect() as c:
            c.execute("SELECT 1")
    except Exception:
        db_ok = False
    return JSONResponse({"status": "ok" if db_ok else "degraded", "db": db_ok})


_PAGE_SIZE = 100          # 列表/队列默认分页大小（前端可「加载更多」翻页）


def _fmt_summary(s: dict) -> dict:
    """把 DB 紧凑摘要（db._display_summary/summary 列）格式化成列表页所需形状。"""
    fh = s.get("file_hash") or ""
    return {
        "file_name": s.get("file_name"),
        "file_hash": fh[:12],
        "file_hash_full": fh,
        "doc_type": s.get("doc_type") or "invoice",
        "invoice_no": s.get("invoice_no"),
        "invoice_date": s.get("invoice_date"),
        "currency_settlement": s.get("currency_settlement"),
        "total_due": s.get("total_due"),
        "category": s.get("category"),
        # 银行流水专用摘要字段（发票记录为空）
        "bank_name": s.get("bank_name"),
        "bank_account_no": s.get("bank_account_no"),
        "statement_period_start": s.get("statement_period_start"),
        "statement_period_end": s.get("statement_period_end"),
        "txn_count": s.get("txn_count"),
        "closing_balance": s.get("closing_balance"),
        "parse_method": s.get("parse_method"),
        "parse_status": s.get("parse_status"),
        "parse_failed": s.get("parse_status") == "failed",
        "uploaded_at": s.get("uploaded_at"),
        "ocr_quality": s.get("ocr_quality"),
        "risk_score": s.get("risk_score"),
        "validation_status": s.get("validation_status"),
        "review_status": s.get("review_status"),
        "issues": s.get("issues") or [],
    }


def _summary(inv: Invoice) -> dict:
    """单张摘要（上传后即时构建；与列表页共用同一摘要来源，形状一致）。"""
    return _fmt_summary(db._display_summary(inv))


def _error_row(file_name: str, msg: str) -> dict:
    return {"file_name": file_name or "（未命名）", "error": msg}


@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...), doc_type: str = Form("invoice")) -> JSONResponse:
    dt = "statement" if doc_type == "statement" else "invoice"
    results = []
    for uf in files:
        fname = uf.filename or "（未命名）"
        try:
            # 有界读取：最多读 上限+1 字节即可判超限，避免超大上传在大小检查前就把整文件读进内存（OOM）
            data = await uf.read(config.MAX_UPLOAD_BYTES + 1)
            if len(data) > config.MAX_UPLOAD_BYTES:
                mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
                results.append(_error_row(fname, f"文件过大，上限 {mb}MB"))
                continue
            if not data:
                results.append(_error_row(fname, "空文件"))
                continue
            # 扩展名白名单（落盘前拒绝真·垃圾类型，防任意文件写盘/被下载；支持格式的并集见 config）
            ext = Path(fname).suffix.lower()
            if ext not in config.ALLOWED_UPLOAD_EXTS:
                results.append(_error_row(fname, f"不支持的文件类型：{ext or '无扩展名'}"
                                                 f"（请上传 PDF / 图片 / Office / CSV 等）"))
                continue
            # 单文件容错：一个文件失败不影响其余文件。
            # 重活（OCR/LibreOffice/PDF 渲染 + 写库）丢线程池，避免阻塞事件循环——
            # 一个人传大发票时不再卡住其他请求。
            # 限流处理：并发处理数受 _process_limiter 约束（超出的请求排队而非一拥而上）
            for inv in await anyio.to_thread.run_sync(pipeline.process_upload, data, fname, dt,
                                                      limiter=_process_limiter):
                results.append(_summary(inv))
        except Exception as e:
            results.append(_error_row(fname, f"处理失败: {type(e).__name__}: {e}"))
    return JSONResponse({"count": len(results), "results": results})


@app.get("/api/invoices")
def list_invoices(limit: int = _PAGE_SIZE, offset: int = 0, doc_type: str = "invoice") -> JSONResponse:
    """记录列表：只读 DB 紧凑摘要（不重建完整对象、不载大文本），SQL 排序 + 分页。
    按 doc_type 隔离（发票/银行流水各自一份清单，互不混显）。
    排序：提取失败在最前，其次最新上传在前。count=总数，has_more 指示是否还有下一页。"""
    dt = "statement" if doc_type == "statement" else "invoice"
    rows = db.load_summaries(limit=limit, offset=offset, doc_type=dt)
    total = db.count_invoices(doc_type=dt)
    return JSONResponse({"count": total, "results": [_fmt_summary(r) for r in rows],
                         "limit": limit, "offset": offset,
                         "has_more": offset + len(rows) < total})


@app.get("/api/recent-transactions")
def recent_transactions_api(limit: int = 10) -> JSONResponse:
    """上传页「识别进度」卡片：最近成功识别的 N 笔流水交易（按最新上传的流水排序）。"""
    return JSONResponse({"transactions": review.recent_statement_transactions(limit)})


@app.post("/api/export")
def export(approved_only: bool = False) -> JSONResponse:
    """导出 Excel 工作底稿。

    默认导出全部记录（最新快照，人工修改即时生效，不以审核状态过滤）。
    approved_only=true 时只导出已通过（Approved）的记录——把审核当入账闸门、未审核的不进表。
    """
    invs = db.load_all_invoices()
    # 只导发票：Excel 工作底稿是发票形状，银行流水另有清单，不混进来（否则流水行整排空）
    items = [inv for inv in invs.values() if (inv.doc_type or "invoice") != "statement"]
    if approved_only:
        items = [inv for inv in items if (inv.approve_status or "") == review.APPROVED]
        if not items:
            return JSONResponse({"error": "没有已通过(Approved)的发票可导出"}, status_code=400)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = pipeline.export_excel(items, filename=f"invoices_approved_{stamp}.xlsx")
    else:
        if not items:
            return JSONResponse({"error": "暂无已处理发票"}, status_code=400)
        out = pipeline.export_excel(items)
    maintenance.prune_exports()   # 只保留最近 N 份导出，防 exports 目录只增不减
    return JSONResponse({"file": out.name, "path": str(out),
                         "download": f"/download/{out.name}", "count": len(items),
                         "approved_only": approved_only})


@app.get("/download/{filename}")
def download(filename: str):
    # 防目录穿越：只允许 exports 目录内的文件
    safe = Path(filename).name
    fp = config.EXPORT_DIR / safe
    if not fp.exists():
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return FileResponse(fp, filename=safe,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---- 人工审核（第五模块）API ----------------------------------------

@app.get("/api/review/queue")
def review_queue_api(status: Optional[str] = None, doc_type: str = "invoice",
                     limit: int = _PAGE_SIZE, offset: int = 0, fix_first: int = 0) -> JSONResponse:
    """待审队列 + 各状态计数。status 可筛选；doc_type 选发票/流水；支持分页。
    fix_first=1：把"未通过提取校验（需纠错）"的记录排到最前（供对账页横幅跳转后自动定位）。"""
    dt = "statement" if doc_type == "statement" else "invoice"
    q = review.review_queue(status, limit=limit, offset=offset, doc_type=dt, fix_first=bool(fix_first))
    total = review.queue_count(status, doc_type=dt)
    return JSONResponse({"queue": q, "summary": review.queue_summary(dt), "doc_type": dt,
                         "limit": limit, "offset": offset, "total": total,
                         "has_more": offset + len(q) < total})


@app.post("/api/review/reapply-all")
def review_reapply_all_api(body: dict = Body(default={})) -> JSONResponse:
    """对未 Approve 的记录，按最新已启用规则批量补齐（只补空/弱、不覆盖已确认/手改）。
    body.doc_type 限定范围：省略/all=全部、invoice=只发票、statement=只流水。"""
    dt = body.get("doc_type")
    dt = dt if dt in ("invoice", "statement") else None
    return JSONResponse(review.reapply_learned_all(body.get("by", "reviewer"), doc_type=dt))


# ---- 对账匹配（发票 ↔ 银行流水）--------------------------------------------
@app.post("/api/reconcile/run")
def reconcile_run_api() -> JSONResponse:
    """自动匹配：清未确认匹配 → 建候选池（仅提取通过者）→ 匹配入库。返回统计。"""
    return JSONResponse(reconcile.run_matching())


@app.get("/api/reconcile/summary")
def reconcile_summary_api() -> JSONResponse:
    """各类别匹配计数 + 纠错队列计数 + 候选池规模。"""
    return JSONResponse(reconcile.summary())


@app.get("/api/reconcile/matches")
def reconcile_matches_api(category: Optional[str] = None, status: str = "proposed") -> JSONResponse:
    """匹配列表（带发票/交易展示信息）。category: auto/confirm/multi/unmatched。"""
    return JSONResponse({"matches": reconcile.matches_view(category=category, status=status)})


@app.post("/api/reconcile/confirm")
def reconcile_confirm_api(body: dict = Body(default={})) -> JSONResponse:
    try:
        return JSONResponse(reconcile.confirm_match(int(body.get("match_id")), body.get("by", "reviewer")))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/reconcile/confirm-batch")
def reconcile_confirm_batch_api(body: dict = Body(default={})) -> JSONResponse:
    """一键批量确认某类别（默认 auto=高可信唯一）的全部未确认匹配。"""
    cat = body.get("category", "auto")
    return JSONResponse(reconcile.confirm_batch(category=cat, by=body.get("by", "reviewer")))


@app.post("/api/reconcile/reject")
def reconcile_reject_api(body: dict = Body(default={})) -> JSONResponse:
    try:
        return JSONResponse(reconcile.reject_match(int(body.get("match_id")), body.get("by", "reviewer")))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/reconcile/unreject")
def reconcile_unreject_api(body: dict = Body(default={})) -> JSONResponse:
    """撤销「不成立」：移出黑名单并重跑匹配。"""
    try:
        return JSONResponse(reconcile.unreject_match(int(body.get("match_id")), body.get("by", "reviewer")))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/reconcile/ack-no-match")
def reconcile_ack_api(body: dict = Body(default={})) -> JSONResponse:
    """确认『无需发票』：该单边交易确实无需发票，标记为已处理（可撤销）。"""
    try:
        return JSONResponse(reconcile.ack_no_match(int(body.get("match_id")), body.get("by", "reviewer")))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/reconcile/unack-no-match")
def reconcile_unack_api(body: dict = Body(default={})) -> JSONResponse:
    """撤销『确认无需发票』：退回待核。"""
    try:
        return JSONResponse(reconcile.unack_no_match(int(body.get("match_id")), body.get("by", "reviewer")))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.post("/api/reconcile/unconfirm")
def reconcile_unconfirm_api(body: dict = Body(default={})) -> JSONResponse:
    """撤销『已确认对账』(反做)：退回待确认、释放占用、解锁对应发票/流水。"""
    try:
        return JSONResponse(reconcile.unconfirm_match(int(body.get("match_id")), body.get("by", "reviewer")))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@app.get("/api/reconcile/manual-candidates")
def reconcile_manual_candidates_api(stmt_hash: str, index: int, q: str = "") -> JSONResponse:
    """供未匹配流水手工选发票：未匹配发票优先 + 关键词搜全部已提取发票。"""
    return JSONResponse(reconcile.manual_match_candidates(stmt_hash, int(index), q or ""))


@app.post("/api/reconcile/manual-match")
def reconcile_manual_match_api(body: dict = Body(default={})) -> JSONResponse:
    """人工把一笔未匹配流水关联到一张发票（建匹配→复用确认护栏；发票未审核会提示先去审核）。"""
    return JSONResponse(reconcile.manual_match(
        body.get("stmt_hash", ""), int(body.get("index", -1)),
        body.get("invoice_hash", ""), body.get("by", "reviewer")))


@app.get("/api/reconcile/{match_id}/stmt/{stmt_hash}.png")
def reconcile_stmt_png(match_id: int, stmt_hash: str):
    """该匹配里此流水的高亮框**已烧入图片**（同坐标系绘制 + 裁剪到匹配行附近），前端直接显示不叠框。"""
    png = reconcile.statement_marked_png(match_id, stmt_hash)
    if png is None:
        return JSONResponse({"error": "无法渲染"}, status_code=404)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/reconcile", response_class=HTMLResponse)
def reconcile_page() -> str:
    """对账确认界面（静态页，调用 /api/reconcile/*）。"""
    return _page((config.BASE_DIR / "web" / "reconcile.html").read_text(encoding="utf-8"))


# ---------- 总账（module 6）：人工触发入账 + 分录/试算平衡查看 ----------

@app.get("/ledger", response_class=HTMLResponse)
def ledger_page() -> str:
    """总账界面（静态页，调用 /api/ledger/*）。"""
    return _page((config.BASE_DIR / "web" / "ledger.html").read_text(encoding="utf-8"))


@app.get("/api/ledger/summary")
def ledger_summary_api() -> JSONResponse:
    return JSONResponse(ledger.summary())


@app.get("/api/ledger/entries")
def ledger_entries_api() -> JSONResponse:
    return JSONResponse({"entries": ledger.entries_view()})


@app.get("/api/ledger/postable")
def ledger_postable_api() -> JSONResponse:
    """已审核通过、待人工触发入账的发票（含建议分录预览）。"""
    return JSONResponse({"invoices": ledger.postable_invoices()})


@app.get("/api/ledger/trial-balance")
def ledger_trial_balance_api() -> JSONResponse:
    return JSONResponse(ledger.trial_balance_view())


@app.get("/api/ledger/open")
def ledger_open_api() -> JSONResponse:
    """待结算：有未结余额的已过账发票（未结额取自明细辅助账）。

    富化：若该发票有**已确认对账匹配**，带上匹配到的银行现金 + 日期（供「据对账结算」一键带入）。
    """
    invs = ledger.open_view()
    for x in invs:
        try:
            m = reconcile.matched_cash_for_invoice(x.get("file_hash", ""))
        except Exception:
            m = None
        if m:
            x["matched"] = m
    return JSONResponse({"invoices": invs, "control": ledger.control_view()})


@app.post("/api/ledger/settle-from-match")
def ledger_settle_from_match_api(body: dict = Body(default={})) -> JSONResponse:
    """据已确认对账匹配结算：用匹配到的银行交易金额清账。body: {file_hash, diff_reason?, by?}

    连接 reconcile → 结算：省去人工重填现金额。仍是人显式触发（按钮），差额(手续费/预扣税等)照常需指定原因。
    """
    fh = body.get("file_hash", "")
    try:
        m = reconcile.matched_cash_for_invoice(fh)
        if not m:
            return JSONResponse({"error": "该发票无已确认对账匹配，无法据此结算"}, status_code=400)
        if m.get("already_posted"):
            return JSONResponse({"error": "该匹配的银行流水已作「流水入账」，不能再据对账结算（避免现金双记）；"
                                          "如需改用结算请先在「已过账分录」红冲那笔流水入账"}, status_code=400)
        no = ledger.settle_invoice(
            fh, cash_amount=m["cash"], date=m.get("date", "") or "",
            diff_reason=body.get("diff_reason") or None, by=body.get("by", "reviewer"))
        return JSONResponse({"entry_no": no, "cash": m["cash"]})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/ledger/settle")
def ledger_settle_api(body: dict = Body(default={})) -> JSONResponse:
    """人工触发：对一张已入账发票做资金结算。
    body: {file_hash, cash_amount, diff_reason?, diff_account?, settle_amount?, tolerance?, by?}"""
    try:
        no = ledger.settle_invoice(
            body.get("file_hash", ""), cash_amount=body.get("cash_amount"),
            diff_reason=body.get("diff_reason") or None,
            diff_account=body.get("diff_account") or None,
            settle_amount=body.get("settle_amount") or None,
            tolerance=body.get("tolerance") or None,
            activity=body.get("activity") or None,
            date=body.get("date") or "",
            cash_currency=body.get("cash_currency") or "",
            by=body.get("by", "reviewer"))
        return JSONResponse({"entry_no": no})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/ledger/post")
def ledger_post_api(body: dict = Body(default={})) -> JSONResponse:
    """人工触发：把一张已审核通过的发票过账为应计分录。
    body: {file_hash, direction?, by?, as_of?}（外币汇率取录入日 as_of，默认今天；补录可指定）"""
    try:
        td = body.get("tax_deductible")
        no = ledger.post_invoice_by_hash(
            body.get("file_hash", ""), by=body.get("by", "reviewer"),
            direction=body.get("direction") or None,
            tax_deductible=(None if td is None else bool(td)),
            as_of=body.get("as_of") or None)
        return JSONResponse({"entry_no": no})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/ledger/accounts")
def ledger_accounts_api() -> JSONResponse:
    """默认科目表（供手工凭证下拉）。"""
    return JSONResponse({"accounts": ledger.chart_accounts()})


@app.get("/api/ledger/periods")
def ledger_periods_api() -> JSONResponse:
    """会计期间 + 关账状态。"""
    return JSONResponse({"periods": ledger.periods_view()})


@app.post("/api/ledger/opening")
def ledger_opening_api(body: dict = Body(default={})) -> JSONResponse:
    """建账期初余额。body: {items:[{account,counterparty,amount}], other_lines:[{account,amount,side}], date, by?}"""
    try:
        return JSONResponse(ledger.post_opening(
            items=body.get("items") or [], other_lines=body.get("other_lines") or [],
            date=body.get("date", ""), by=body.get("by", "admin")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/ledger/close")
def ledger_close_api(body: dict = Body(default={})) -> JSONResponse:
    """人工期末关账：结转损益 + 锁定该期。body: {period 'YYYY-MM', by?}"""
    try:
        return JSONResponse(ledger.close_period(body.get("period", ""), by=body.get("by", "admin")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/ledger/reopen")
def ledger_reopen_api(body: dict = Body(default={})) -> JSONResponse:
    """重开已关账期（红冲结转）。body: {period, by?}"""
    try:
        return JSONResponse(ledger.reopen_period(body.get("period", ""), by=body.get("by", "admin")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/ledger/manual")
def ledger_manual_api(body: dict = Body(default={})) -> JSONResponse:
    """人工新建并过账一张手工记账凭证。
    body: {lines:[{account,debit,credit,memo}], date, memo?, activity?, by?,
           allow_control?, counterparty?}
    含应付/应收往来控制账户时须 allow_control=true + counterparty（软护栏）。"""
    try:
        no = ledger.post_manual_entry(
            body.get("lines") or [], date=body.get("date", ""),
            memo=body.get("memo", ""), activity=body.get("activity") or None,
            by=body.get("by", "reviewer"),
            allow_control=bool(body.get("allow_control")),
            counterparty=body.get("counterparty", "") or "")
        return JSONResponse({"entry_no": no})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/ledger/statement-lines")
def ledger_statement_lines_api() -> JSONResponse:
    """待入账银行流水（无对应发票的现金收支），供「流水入账」页。"""
    return JSONResponse({"lines": ledger.statement_lines_view(only_open=True)})


@app.post("/api/ledger/opening-import")
async def ledger_opening_import_api(
        file: UploadFile = File(...), dry_run: str = Form("1"),
        date: str = Form(""), by: str = Form("admin")) -> JSONResponse:
    """期初余额批量导入：上传 Excel/CSV。dry_run=1 只预览解析结果，dry_run=0 才过账。"""
    fname = file.filename or "opening"
    try:
        data = await file.read(config.MAX_UPLOAD_BYTES + 1)
        if len(data) > config.MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "文件过大"}, status_code=400)
        if not data:
            return JSONResponse({"error": "空文件"}, status_code=400)
        if str(dry_run).lower() in ("0", "false", "no"):
            res = ledger.commit_opening_import(data, fname, date=date, by=by)
            return JSONResponse({**res, "committed": True})
        parsed = ledger.preview_opening_import(data, fname)
        return JSONResponse({**parsed, "committed": False})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/fx/rates")
def fx_rates_api() -> JSONResponse:
    """汇率表（外币→功能货币，人工录入·固定）。"""
    from core import fx
    return JSONResponse({"functional": config.FUNCTIONAL_CURRENCY, "rates": fx.rates()})


@app.get("/api/fx/revaluation")
def fx_revaluation_api(as_of: str = "") -> JSONResponse:
    """期末外币敞口重估报告（诊断，不记账）：从未结外币发票回溯原币敞口、按 as_of 汇率重估。"""
    return JSONResponse(ledger.fx_revaluation_view(as_of))


@app.post("/api/fx/rate")
def fx_add_rate_api(body: dict = Body(default={})) -> JSONResponse:
    """人工录入/更新一条汇率（离线兜底或修正）。body: {currency, date, rate}。"""
    from core import fx
    try:
        res = fx.add_rate(body.get("currency", ""), body.get("date", ""), body.get("rate"))
        return JSONResponse(res)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/fx/update")
def fx_update_api(body: dict = Body(default={})) -> JSONResponse:
    """按日从 provider（默认 Frankfurter）拉取并更新本地汇率表。body: {date?}（默认今天）。
    只拉公开汇率、写本地；不上传任何内部数据。"""
    from core import fx
    try:
        n, eff = fx.update_rates(body.get("date") or None)
        return JSONResponse({"updated": n, "effective_date": eff, "provider": fx.get_provider().name})
    except Exception as e:
        return JSONResponse({"error": f"拉取汇率失败（{type(e).__name__}）：{e}"}, status_code=502)


@app.get("/api/ledger/bank-recon")
def ledger_bank_recon_api() -> JSONResponse:
    """银行余额调节：逐张流水单自洽校验 + 入账进度 + 总账银行余额（诊断，不记账）。"""
    return JSONResponse(ledger.bank_reconciliation_view())


@app.post("/api/ledger/post-statement")
def ledger_post_statement_api(body: dict = Body(default={})) -> JSONResponse:
    """把一笔无发票的银行流水入账（选对方科目 → Dr/Cr 银行）。
    body: {stmt_hash, index, counter_account, activity, date?, memo?, by?}"""
    try:
        no = ledger.post_statement_entry(
            body.get("stmt_hash", ""), int(body.get("index")),
            counter_account=body.get("counter_account", ""),
            activity=body.get("activity", ""),
            date=body.get("date") or None, memo=body.get("memo", ""),
            by=body.get("by", "reviewer"))
        return JSONResponse({"entry_no": no})
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/counterparties", response_class=HTMLResponse)
def counterparties_page() -> str:
    """对手方主数据界面（建档 / 查重归并 / 维护）。"""
    return _page((config.BASE_DIR / "web" / "counterparties.html").read_text(encoding="utf-8"))


@app.get("/api/counterparties")
def counterparties_api(include_archived: bool = False) -> JSONResponse:
    """已建档对手方 + 待建档队列（发票上出现但未建档，带查重候选）。"""
    if include_archived:      # 含归档的列表另算，其余复用 overview（只扫一遍发票）
        data = counterparty.overview()
        data["parties"] = counterparty.list_parties(include_archived=True)
        return JSONResponse(data)
    return JSONResponse(counterparty.overview())


@app.get("/api/counterparties/suggest")
def counterparties_suggest_api(name: str = "") -> JSONResponse:
    """按名字查已建档对手方 + 相似候选（供建档查重、对手方输入提示）。"""
    return JSONResponse({"match": counterparty.resolve(name),
                         "candidates": counterparty.candidates(name)})


@app.get("/api/counterparties/self-candidates")
def counterparties_self_candidates_api() -> JSONResponse:
    """侦测"既开票又收票"的名字=很可能是本方主体（尚未建档者），供「一键建档为我方主体」提示。"""
    return JSONResponse({"candidates": counterparty.self_candidates()})


@app.post("/api/counterparties/register")
def counterparties_register_api(body: dict = Body(default={})) -> JSONResponse:
    """人工建档新对手方。body: {name, kind?, tax_id?, note?, default_account?, aliases?, force?, by?}
    疑似与已建档重复且未 force → 400（请先判断并入已有还是确为另一家）。"""
    try:
        p = counterparty.register(
            body.get("name", ""), kind=(",".join(body["kind"]) if isinstance(body.get("kind"), list) else (body.get("kind") or counterparty.KIND_VENDOR)),
            tax_id=body.get("tax_id", "") or "", note=body.get("note", "") or "",
            default_account=body.get("default_account", "") or "",
            aliases=body.get("aliases") or [], force=bool(body.get("force")),
            by=body.get("by", "reviewer"))
        return JSONResponse({"party": p})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/counterparties/alias")
def counterparties_alias_api(body: dict = Body(default={})) -> JSONResponse:
    """把发票上的一种写法并入已建档对手方（人工确认的查重结果）。body: {cp_id, raw, by?}"""
    try:
        p = counterparty.add_alias(int(body.get("cp_id") or 0), body.get("raw", ""),
                                   by=body.get("by", "reviewer"))
        return JSONResponse({"party": p})
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/counterparties/update")
def counterparties_update_api(body: dict = Body(default={})) -> JSONResponse:
    """维护对手方字段。body: {cp_id, kind?, tax_id?, note?, default_account?, status?}"""
    try:
        p = counterparty.update_party(
            int(body.get("cp_id") or 0), kind=((",".join(body["kind"]) or None) if isinstance(body.get("kind"), list) else body.get("kind")), tax_id=body.get("tax_id"),
            note=body.get("note"), default_account=body.get("default_account"),
            status=body.get("status"))
        return JSONResponse({"party": p})
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/ledger/reverse")
def ledger_reverse_api(body: dict = Body(default={})) -> JSONResponse:
    """红冲一张已过账分录。body: {entry_no, by?}"""
    import datetime as _dt
    at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rev = ledger.store.reverse_entry(body.get("entry_no", ""),
                                         by=body.get("by", "reviewer"), at=at)
        return JSONResponse({"reversal_no": rev})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- 报表中心（module 7）：利润表 + 资产负债表 + 勾稽 ----------

@app.get("/reports", response_class=HTMLResponse)
def reports_page() -> str:
    """报表界面（静态页，调用 /api/reports/*）。"""
    return _page((config.BASE_DIR / "web" / "reports.html").read_text(encoding="utf-8"))


@app.get("/api/reports")
def reports_api() -> JSONResponse:
    """完整报表包：利润表 + 资产负债表 + 现金流量表 + 勾稽结论（勾稽不过 can_issue=False）。"""
    return JSONResponse(reports.generate())


@app.post("/api/reports/export")
def reports_export_api() -> JSONResponse:
    """导出三张报表 Excel（封面+三表+勾稽+审计轨迹）。勾稽不过则 400、不出表。"""
    try:
        out = reports.export_excel()
        return JSONResponse({"file": out.name, "download": f"/download/{out.name}"})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/review/{file_hash}/reapply")
def review_reapply_api(file_hash: str, body: dict = Body(default={})) -> JSONResponse:
    """对单条未 Approve 记录按最新已启用规则补齐。"""
    try:
        r = review.reapply_learned(file_hash, body.get("by", "reviewer"))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(r)


@app.get("/api/review/{file_hash}")
def review_detail_api(file_hash: str) -> JSONResponse:
    """单张详情：字段 + 置信度 + 原文 + 校验问题 + 人工修改轨迹。"""
    d = review.review_detail(file_hash)
    if d is None:
        return JSONResponse({"error": "未找到该记录"}, status_code=404)
    return JSONResponse(d)


@app.post("/api/review/{file_hash}/field")
def review_field_api(file_hash: str, body: dict = Body(...)) -> JSONResponse:
    """人工修改一个字段（留痕）。body: {field, value, by?, reason?, base_rev?}"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    field = body.get("field")
    if not field:
        return JSONResponse({"error": "缺少 field"}, status_code=400)
    try:
        r = review.change_field(file_hash, field, body.get("value"),
                                body.get("by", "reviewer"), body.get("reason", ""),
                                region=body.get("region"))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/transaction")
def review_transaction_api(file_hash: str, body: dict = Body(...)) -> JSONResponse:
    """银行流水交易行改/增/删。body: {index, field, value, by?}
    field=date/description/income/expense/balance 改某笔；'__add__' 增行；'__del__' 删行。"""
    try:
        r = review.save_transaction(file_hash, int(body.get("index", -1)),
                                    body.get("field", ""), body.get("value"),
                                    body.get("by", "reviewer"))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/clear-locate")
def review_clear_locate_api(file_hash: str, body: dict = Body(...)) -> JSONResponse:
    """清除某字段的原件定位框（识别错位置时去掉赖在错处的高亮）。body: {field, by?, reason?, base_rev?}"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    field = body.get("field")
    if not field:
        return JSONResponse({"error": "缺少 field"}, status_code=400)
    try:
        r = review.clear_field_locate(file_hash, field, body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/delete")
def review_delete_api(file_hash: str, body: dict = Body(default={})) -> JSONResponse:
    """删除一条发票记录（审核前清理）。body: {by?, reason?, base_rev?}"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    try:
        r = review.delete_invoice(file_hash, body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/resplit")
def review_resplit_api(file_hash: str, body: dict = Body(default={})) -> JSONResponse:
    """重新切分多发票文件并替换整组记录。
    body: {mode:"single"|"auto"|"manual", cuts?, by?, reason?, base_rev?}
    - single：识别成多张但其实一张 → 合并回单张；
    - auto  ：识别成单张但其实多张 → 重新自动检测（找不到边界返回 resplit=false，前端转人工画线）；
    - manual：按人工画线边界 cuts 切分。"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    try:
        r = review.resplit(file_hash, body.get("mode", "single"),
                           body.get("by", "reviewer"), body.get("reason", ""),
                           cuts=body.get("cuts"))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.get("/learned", response_class=HTMLResponse)
def learned_page() -> str:
    """已学规则管理页（查看 / 删除人工确认沉淀的规则）。"""
    return _page((config.BASE_DIR / "web" / "learned.html").read_text(encoding="utf-8"))


@app.get("/api/learned")
def learned_list_api(doc_type: str = "invoice") -> JSONResponse:
    """列出人工确认沉淀的规则（分类 / 对手方字段默认值），供查看与管理。按单据类型过滤。"""
    dt = "statement" if doc_type == "statement" else "invoice"
    return JSONResponse({"rules": db.list_learned(doc_type=dt), "doc_type": dt})


@app.post("/api/learned/{rule_id}/enable")
def learned_enable_api(rule_id: int) -> JSONResponse:
    """启用一条待确认规则（仅此对手方/此类版式生效）。"""
    return JSONResponse({"enabled": db.enable_learned(rule_id, make_global=False)})


@app.post("/api/learned/{rule_id}/enable-global")
def learned_enable_global_api(rule_id: int) -> JSONResponse:
    """把一条"字段线索"启用为**全局同义词**（对所有发票生效；仍逐张按标签校验、只补空/弱字段）。"""
    return JSONResponse({"enabled": db.enable_learned(rule_id, make_global=True)})


@app.post("/api/learned/{rule_id}/delete")
def learned_delete_api(rule_id: int) -> JSONResponse:
    """删除一条学到的规则（学错了可撤；待确认的不想要也删）。"""
    return JSONResponse({"deleted": db.delete_learned(rule_id)})


@app.post("/api/learned/{rule_id}/update")
def learned_update_api(rule_id: int, body: dict = Body(...)) -> JSONResponse:
    """人工修正一条学到的规则（启用前纠正捕获不准的值/科目/标签/字段/作用域）。
    body 可含：category / account / target / value / match_key / note。"""
    return JSONResponse({"updated": db.update_learned(rule_id, body or {})})


@app.post("/api/learned/{rule_id}/parse")
def learned_parse_api(rule_id: int, body: dict = Body(...)) -> JSONResponse:
    """把人工自由写的整段说明，就地（纯规则/关键词，不调模型）解析成结构化字段。
    body: {text}。返回 {fields, understood, missing}——读懂的填进空格、没读出的明确提示，
    交人工确认后再 update+enable 才生效。"""
    from review import rule_text
    cur = next((r for r in db.list_learned() if r["id"] == rule_id), None)
    if cur is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    text = (body or {}).get("text", "")
    return JSONResponse(rule_text.parse(text, cur["rule_type"], cur))


@app.get("/api/review/{file_hash}/duplicates")
def review_duplicates_api(file_hash: str) -> JSONResponse:
    """取疑似重复候选（供对比确认界面）。"""
    d = review.duplicate_candidates(file_hash)
    if d is None:
        return JSONResponse({"error": "未找到该记录"}, status_code=404)
    return JSONResponse(d)


@app.post("/api/review/{file_hash}/duplicate")
def review_resolve_dup_api(file_hash: str, body: dict = Body(...)) -> JSONResponse:
    """人工确认某候选是否真重复。body: {against, is_duplicate: bool, by?, reason?}"""
    try:
        r = review.resolve_duplicate(file_hash, body.get("against", ""),
                                     bool(body.get("is_duplicate")),
                                     body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(r)


@app.post("/api/review/dedupe")
def review_dedupe_api(body: dict = Body(...)) -> JSONResponse:
    """重复去重：一组里只保留 keep、删除其余。body: {keep, group:[hash…], by?, reason?}
    已 Approved 的会跳过（在 skipped 里返回）。"""
    keep = body.get("keep")
    group = body.get("group") or []
    if not keep or not group:
        return JSONResponse({"error": "缺少 keep/group"}, status_code=400)
    return JSONResponse(review.dedupe(keep, group, body.get("by", "reviewer"), body.get("reason", "")))


@app.post("/api/review/drop-unapproved")
def review_drop_unapproved_api(body: dict = Body(...)) -> JSONResponse:
    """删除一组里所有未入账重复、保留已入账。body: {group:[hash…], by?, reason?}"""
    group = body.get("group") or []
    if not group:
        return JSONResponse({"error": "缺少 group"}, status_code=400)
    return JSONResponse(review.drop_unapproved(group, body.get("by", "reviewer"), body.get("reason", "")))


@app.get("/compare", response_class=HTMLResponse)
def compare_page() -> str:
    """重复发票对比确认界面（左=本次上传，右=疑似重复，可横向滑动）。"""
    return _page((config.BASE_DIR / "web" / "compare.html").read_text(encoding="utf-8"))


@app.post("/api/review/{file_hash}/line-item/add")
def review_line_item_add_api(file_hash: str, body: dict = Body(default={})) -> JSONResponse:
    """新增一条空白服务明细（漏识别时手工补）。注意：须在 /{index} 路由之前声明，
    否则 'add' 会被当作 index:int 解析而 422。"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    try:
        r = review.add_line_item(file_hash, body.get("by", "reviewer"), body.get("reason", ""),
                                 description=body.get("description"), amount=body.get("amount"),
                                 region=body.get("region"))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/line-item/{index}")
def review_line_item_api(file_hash: str, index: int, body: dict = Body(...)) -> JSONResponse:
    """改一条服务明细。body: {field, value, by?, reason?, base_rev?}"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    field = body.get("field")
    if not field:
        return JSONResponse({"error": "缺少 field"}, status_code=400)
    try:
        r = review.change_line_item(file_hash, index, field, body.get("value"),
                                    body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/line-item/{index}/delete")
def review_line_item_delete_api(file_hash: str, index: int, body: dict = Body(default={})) -> JSONResponse:
    """删一条服务明细（多识别/错误的行）。"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    try:
        r = review.delete_line_item(file_hash, index, body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/line-item/{index}/split")
def review_line_item_split_api(file_hash: str, index: int, body: dict = Body(...)) -> JSONResponse:
    """把一条大段描述的明细按人工分段拆成多条，并学习断句方式（pending）。
    body: {pieces:[...], by?, reason?, base_rev?}"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    pieces = body.get("pieces")
    if not isinstance(pieces, list):
        return JSONResponse({"error": "缺少 pieces 列表"}, status_code=400)
    try:
        r = review.split_line_item(file_hash, index, pieces,
                                   body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/line-item/{index}/sub/add")
def review_sub_add_api(file_hash: str, index: int, body: dict = Body(default={})) -> JSONResponse:
    """给某明细行加一条空白勾稽子明细。须在 /{sub_index} 之前声明（否则 'add' 被当 int 而 422）。"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    try:
        r = review.add_sub_item(file_hash, index, body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/line-item/{index}/sub/{sub_index}/delete")
def review_sub_delete_api(file_hash: str, index: int, sub_index: int,
                          body: dict = Body(default={})) -> JSONResponse:
    """删一条勾稽子明细（识别错的）。"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    try:
        r = review.delete_sub_item(file_hash, index, sub_index,
                                   body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/line-item/{index}/sub/{sub_index}")
def review_sub_change_api(file_hash: str, index: int, sub_index: int,
                          body: dict = Body(...)) -> JSONResponse:
    """改一条勾稽子明细的 日期/描述/金额。body: {field, value, by?, reason?, base_rev?}。
    返回含该行最新勾稽状态 reconcile（供前端在改金额时提醒是否对上）。"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    field = body.get("field")
    if not field:
        return JSONResponse({"error": "缺少 field"}, status_code=400)
    try:
        r = review.change_sub_item(file_hash, index, sub_index, field, body.get("value"),
                                   body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/classify")
def review_classify_api(file_hash: str, body: dict = Body(...)) -> JSONResponse:
    """人工确认/修正分类（建议科目，留痕）。body: {category, account, by?, reason?, base_rev?}"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    try:
        r = review.set_classification(file_hash, body.get("category", ""), body.get("account", ""),
                                      body.get("by", "reviewer"), body.get("reason", ""))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse(r)


@app.post("/api/review/{file_hash}/action")
def review_action_api(file_hash: str, body: dict = Body(...)) -> JSONResponse:
    """审核动作（确认/拒绝/待定）。body: {action: Approved|Rejected|Hold, by?, reason?, base_rev?}"""
    conflict = _rev_conflict(file_hash, body)
    if conflict:
        return conflict
    try:
        r = review.act(file_hash, body.get("action", ""),
                       body.get("by", "reviewer"), body.get("reason", ""),
                       force=bool(body.get("force")))
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(r)


_IMG_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")
_RENDER_DPI = 144   # PDF 页渲染分辨率；前端按页 pt 尺寸百分比叠框，与此 DPI 无关

# ---- 原件页渲染缓存（内容由 file_hash+页码+DPI 唯一决定 → 可安全缓存，翻页/多人复看不重渲染）----
import re as _re


def _cache_key(file_hash: str) -> str:
    return _re.sub(r"[^A-Za-z0-9]", "_", file_hash)   # 派生 hash 可能含 ':' 等 → 清成安全文件名


def _page_cache_path(file_hash: str, n: int) -> Path:
    # 动态读 config.DATA_ROOT（随部署/测试隔离生效），落 <DATA_ROOT>/cache/pages/
    return config.DATA_ROOT / "cache" / "pages" / f"{_cache_key(file_hash)}_{n}_{_RENDER_DPI}.png"


def _page_cache_get(file_hash: str, n: int) -> Optional[bytes]:
    p = _page_cache_path(file_hash, n)
    try:
        return p.read_bytes() if p.exists() else None
    except OSError:
        return None


_page_put_count = 0


def _page_cache_put(file_hash: str, n: int, png: bytes) -> None:
    global _page_put_count
    try:
        p = _page_cache_path(file_hash, n)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(png)
    except OSError:
        return
    # 每写 50 张顺带清一次，把页面缓存 PNG 控制在上限内（按最旧删），防磁盘只增不减
    _page_put_count += 1
    if _page_put_count % 50 == 0:
        maintenance.prune_page_cache()


def _png_headers(etag: str) -> dict:
    return {"ETag": etag, "Cache-Control": "private, max-age=86400"}


def _rev_conflict(file_hash: str, body: dict):
    """乐观锁：若请求带 base_rev 且与 DB 当前 rev 不一致 → 返回 409（别人已改）。
    否则返回 None（放行）。base_rev 缺省（如脚本/批量）则不校验，向后兼容。"""
    base = body.get("base_rev") if isinstance(body, dict) else None
    if base is None:
        return None
    inv = db.get_invoice(file_hash)
    if inv is not None and int(base) != (inv.rev or 0):
        return JSONResponse(
            {"error": "该记录刚被他人修改；已为你刷新到最新，请在最新版本上重做本次操作。",
             "code": "conflict", "current_rev": inv.rev or 0}, status_code=409)
    return None


def _safe_original(file_hash: str) -> Optional[Path]:
    """由 file_hash 取原件路径，并确保它落在 UPLOAD_DIR 内（防目录穿越）。"""
    inv = db.get_invoice(file_hash)
    if inv is None or not inv.file_path:
        return None
    p = Path(inv.file_path).resolve()
    root = config.UPLOAD_DIR.resolve()
    if root not in p.parents or not p.exists():
        return None
    return p


def _safe_source(source_file_hash: str) -> Optional[Path]:
    """由 source_file_hash 取"源文件"（原始上传文件）路径，并校验落在 UPLOAD_DIR 内。
    兜底：无源链接（旧记录）时传进来的其实是记录自身 hash → 用它自己的 file_path。"""
    sibs = db.siblings_by_source(source_file_hash)
    inv = db.get_invoice(sibs[0]["file_hash"]) if sibs else db.get_invoice(source_file_hash)
    if inv is None:
        return None
    path_str = inv.source_file_path or inv.file_path   # 旧记录无源路径 → 回退自身原件
    if not path_str:
        return None
    p = Path(path_str).resolve()
    root = config.UPLOAD_DIR.resolve()
    if root not in p.parents or not p.exists():
        return None
    return p


@app.get("/api/review/collection/{source_file_hash}")
def review_collection(source_file_hash: str) -> JSONResponse:
    """多发票合集详情：源文件名 + 原件页数 + 组内各单张发票（审核页折叠展开用）。"""
    d = review.collection_detail(source_file_hash)
    if d is None:
        return JSONResponse({"error": "未找到合集"}, status_code=404)
    return JSONResponse(d)


@app.get("/api/collection/{source_file_hash}/page/{n}")
def collection_page_image(source_file_hash: str, n: int, request: Request):
    """渲染"源文件"（合并前的原始上传文件）第 n 页为图片，供合集视图展示原件样子。"""
    p = _safe_source(source_file_hash)
    if p is None:
        return JSONResponse({"error": "未找到源文件"}, status_code=404)
    if p.suffix.lower() in _IMG_SUFFIXES:
        if n != 0:
            return JSONResponse({"error": "页码越界"}, status_code=404)
        return FileResponse(p)
    ck = "src-" + source_file_hash
    etag = f'"{_cache_key(ck)}-{n}-{_RENDER_DPI}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=_png_headers(etag))
    cached = _page_cache_get(ck, n)
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers=_png_headers(etag))
    from extraction.extract import excel, textrender
    if excel.is_excel(p):
        if n != 0:
            return JSONResponse({"error": "页码越界"}, status_code=404)
        png = excel.render_png(p)
    elif textrender.can_preview(p):        # 文本/结构化源文件 → 等宽文本图片
        if n != 0:
            return JSONResponse({"error": "页码越界"}, status_code=404)
        png = textrender.render_text_png(p)
        if png is None:
            return JSONResponse({"error": "该格式无法在线预览"}, status_code=404)
    else:
        import fitz
        try:
            doc = fitz.open(p)
            try:
                if n < 0 or n >= doc.page_count:
                    return JSONResponse({"error": "页码越界"}, status_code=404)
                zoom = _RENDER_DPI / 72.0
                png = doc[n].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
            finally:
                doc.close()
        except Exception:
            return JSONResponse({"error": "该格式无法在线预览"}, status_code=404)
    _page_cache_put(ck, n, png)
    return Response(content=png, media_type="image/png", headers=_png_headers(etag))


@app.get("/api/collection/{source_file_hash}/original")
def collection_original_file(source_file_hash: str):
    """下载源文件（合并前的原始上传文件）。"""
    p = _safe_source(source_file_hash)
    if p is None:
        return JSONResponse({"error": "未找到源文件"}, status_code=404)
    d = review.collection_detail(source_file_hash)
    return FileResponse(p, filename=(d.get("source_file_name") if d else p.name))


@app.get("/api/review/{file_hash}/raw.png")
def review_raw_png(file_hash: str):
    """流水审核「原件」视图：渲染**原件源文件**（真实字节、完整不截断）为图片，供核对——
    区别于 /page/0 的「整理视图」（规范交易表，会截断长附言）。仅文本/结构化原件可用。"""
    p = _safe_original(file_hash)
    if p is None:
        return JSONResponse({"error": "未找到原件"}, status_code=404)
    from extraction.extract import textrender as _tr
    if not _tr.can_preview(p):
        return JSONResponse({"error": "该格式无原件文本视图"}, status_code=404)
    png = _tr.render_text_png(p, max_cols=110)   # 收窄折行，画布更窄 → 左栏「适应宽度」时更易读、再配缩放
    if png is None:
        return JSONResponse({"error": "无法渲染"}, status_code=404)
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/review/{file_hash}/page/{n}")
def review_page_image(file_hash: str, n: int, request: Request):
    """渲染原件第 n 页为图片（纯本地，PDF→PNG / 图片直发）。供审核界面左栏显示原件。

    渲染结果按 file_hash+页码+DPI **缓存**（内容不变、可安全缓存）：带 ETag + max-age，
    浏览器翻页/多人复看走 304 或磁盘缓存，不重复渲染。
    """
    # 银行流水（已解析出逐笔交易）：左栏渲染「规范交易表」，每笔带行级 bbox → 点交易行可高亮对应行。
    # 不依赖原始文件（CSV/结构化导入无页面图也能预览），故置于「找原件」检查之前。
    # 不走页面缓存：交易被人工改动后需即时反映（渲染开销小）。
    _sinv = db.get_invoice(file_hash)
    if _sinv is not None and _sinv.doc_type == "statement" and _sinv.transactions:
        if n != 0:
            return JSONResponse({"error": "页码越界"}, status_code=404)
        from extraction.extract import textrender as _tr
        png = _tr.render_statement_png(_sinv)
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})
    p = _safe_original(file_hash)
    if p is None:
        return JSONResponse({"error": "未找到原件"}, status_code=404)
    if p.suffix.lower() in _IMG_SUFFIXES:
        if n != 0:
            return JSONResponse({"error": "页码越界"}, status_code=404)
        # 小图直发（无损、自带 ETag）；超大图缩到预览上限并缓存，避免每次显示/缩放传渲染整张大图卡顿。
        # bbox 用归一化坐标叠框，缩放不影响对齐。
        from PIL import Image
        try:
            with Image.open(p) as _im:
                too_big = max(_im.size) > config.PREVIEW_MAX_SIDE
        except Exception:
            too_big = False
        if not too_big:
            return FileResponse(p)
        etag = f'"{_cache_key(file_hash)}-prev-{config.PREVIEW_MAX_SIDE}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=_png_headers(etag))
        cached = _page_cache_get(file_hash, 0)
        if cached is not None:
            return Response(content=cached, media_type="image/png", headers=_png_headers(etag))
        import io as _io
        with Image.open(p) as im:
            im = im.convert("RGB")
            f = config.PREVIEW_MAX_SIDE / max(im.size)
            im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
            buf = _io.BytesIO(); im.save(buf, "PNG"); png = buf.getvalue()
        _page_cache_put(file_hash, 0, png)
        return Response(content=png, media_type="image/png", headers=_png_headers(etag))
    # 渲染类（Excel/PDF/Word）——先走缓存与条件请求
    etag = f'"{_cache_key(file_hash)}-{n}-{_RENDER_DPI}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=_png_headers(etag))
    cached = _page_cache_get(file_hash, n)
    if cached is not None:
        return Response(content=cached, media_type="image/png", headers=_png_headers(etag))
    # Excel(.xlsx) → 自渲染（保留加粗/字色/填充，与字段 bbox 对齐可高亮）
    from extraction.extract import excel, textrender
    if excel.is_excel(p):
        if n != 0:
            return JSONResponse({"error": "页码越界"}, status_code=404)
        png = excel.render_png(p)
    elif textrender.can_preview(p):
        # 文本/结构化流水（CSV/JSON/MT940/OFX/QIF/XML/定宽txt/HTML-xls）→ 渲染成等宽文本图片
        if n != 0:
            return JSONResponse({"error": "页码越界"}, status_code=404)
        png = textrender.render_text_png(p)
        if png is None:
            return JSONResponse({"error": "该格式无法在线预览"}, status_code=404)
    else:
        # PDF / Word(.docx) → fitz 渲染指定页；无法渲染（如旧版 .doc）→ 404，前端降级为下载原件
        import fitz
        try:
            doc = fitz.open(p)
            try:
                if n < 0 or n >= doc.page_count:
                    return JSONResponse({"error": "页码越界"}, status_code=404)
                zoom = _RENDER_DPI / 72.0
                png = doc[n].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
            finally:
                doc.close()
        except Exception:
            return JSONResponse({"error": "该格式无法在线预览"}, status_code=404)
    _page_cache_put(file_hash, n, png)
    return Response(content=png, media_type="image/png", headers=_png_headers(etag))


@app.get("/api/review/{file_hash}/original")
def review_original_file(file_hash: str):
    """下载原始上传文件（无法在线预览的格式，如旧版 .doc，供人工下载后用本地软件打开对照录入）。"""
    p = _safe_original(file_hash)
    if p is None:
        return JSONResponse({"error": "未找到原件"}, status_code=404)
    inv = db.get_invoice(file_hash)
    return FileResponse(p, filename=(inv.file_name if inv else p.name))


def _pick_words_text(ocr_words, page: int, x0: float, y0: float, x1: float, y1: float) -> str:
    """从已存的整页归一化词几何里，取与框选区重叠的词并按 (y,x) 拼接。零 OCR、毫秒返回。"""
    rx0, rx1 = sorted((x0, x1))
    ry0, ry1 = sorted((y0, y1))
    picks = []
    for w in ocr_words or []:
        try:
            pno, wx0, wy0, wx1, wy1, txt = w
        except Exception:
            continue
        if int(pno) != int(page) or not (txt or "").strip():
            continue
        if wx1 < rx0 or wx0 > rx1 or wy1 < ry0 or wy0 > ry1:   # 无重叠
            continue
        picks.append((round(wy0, 3), wx0, txt))
    picks.sort()
    return " ".join(t for _, _, t in picks)


def _ocr_region_text(img, x0: float, y0: float, x1: float, y1: float) -> str:
    """裁剪图片的框选区域并 OCR 取字（仅在无已存词几何时的兜底）。归一化坐标→像素。

    按比例向外扩边——**水平扩得多**(框常从数字中间切断，如把 "7,000.00" 切成 "000.00"→OCR 碎成
    "000. .00")，**垂直扩得少**(避免带入上下相邻行)。这样切偏的框也能圈回完整数字。
    """
    from extraction.extract import ocr as ocr_mod
    W, H = img.size
    bx0, bx1 = sorted((x0 * W, x1 * W))
    by0, by1 = sorted((y0 * H, y1 * H))
    bw, bh = bx1 - bx0, by1 - by0
    if bw < 3 or bh < 3:
        return ""
    # 水平扩边取「35% 框宽」与「2× 框高」的较大者——窄框(切掉半个数字)靠后者也能够回整数字，
    # 宽框则按比例；封顶 40% 页宽避免过度扩。垂直只扩 12%，少带上下行。
    padx = min(W * 0.4, max(bw * 0.35, bh * 2.0))
    pady = max(4, bh * 0.12)
    crop = img.crop((max(0, int(bx0 - padx)), max(0, int(by0 - pady)),
                     min(W, int(bx1 + padx)), min(H, int(by1 + pady))))
    r = ocr_mod.run_ocr(crop)                 # 兜底用单引擎（快）；正常路径已走 _pick_words_text
    return r.text.strip() if r else ""


@app.post("/api/review/{file_hash}/region-text")
def review_region_text_api(file_hash: str, body: dict = Body(...)) -> JSONResponse:
    """取原件某框选区域内的文字（供人工框选→复制/填入字段）。
    body: {page:int, x0,y0,x1,y1}（归一化 0~1，相对该页）。
    文本型 PDF 用 fitz 词坐标；图片件/扫描型 PDF（无文本层）用"裁剪区域→OCR"取字。"""
    p = _safe_original(file_hash)
    if p is None:
        return JSONResponse({"text": "", "note": "找不到原件"})
    try:
        n = int(body.get("page", 0))
        x0, y0, x1, y1 = (float(body[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "参数不全"}, status_code=400)

    # 快路径：OCR 件在提取时已存整页词几何 → 直接按坐标取词，免实时 OCR（框选弹窗秒开）。
    # 取到就返回；**取不到则不早退**，继续走下方实时 OCR 兜底——这样旧记录（词几何为空/为修复前的
    # 旋转坐标）框选也能读出来，不至于显示"没有文字"。
    inv = db.get_invoice(file_hash)
    if inv is not None and getattr(inv, "ocr_words", None):
        text = _pick_words_text(inv.ocr_words, n, x0, y0, x1, y1)
        if text:
            return JSONResponse({"text": text})

    # 图片件：裁剪框选区 → OCR（此前直接返回空 = "未提取到文字"的根因）
    if p.suffix.lower() in _IMG_SUFFIXES:
        from PIL import Image
        try:
            img = Image.open(p)
        except Exception:
            return JSONResponse({"text": "", "note": "无法打开图片"})
        text = _ocr_region_text(img, x0, y0, x1, y1)
        return JSONResponse({"text": text, "note": "" if text else "该区域未识别到文字"})

    # Excel(.xlsx)：原件预览是 excel 自渲染 PNG（fitz 取不到 xlsx 文本、坐标也不一致），
    # 故用同一张 render_png 裁剪框选区→OCR，保证"所见即所裁"。
    from extraction.extract import excel as _excel
    if _excel.is_excel(p):
        import io as _io
        from PIL import Image
        try:
            img = Image.open(_io.BytesIO(_excel.render_png(p)))
        except Exception:
            return JSONResponse({"text": "", "note": "无法渲染该 Excel"})
        text = _ocr_region_text(img, x0, y0, x1, y1)
        return JSONResponse({"text": text, "note": "" if text else "该区域未识别到文字"})

    # 文本/结构化流水（CSV/JSON/MT940/OFX/XML/定宽txt/HTML-xls）：预览是等宽文本渲染 PNG，
    # 框选区在同一张图上裁剪→OCR，保证"所见即所裁"；也避免 fitz 打不开这些格式而 500。
    from extraction.extract import textrender as _tr
    if _tr.can_preview(p):
        import io as _io
        from PIL import Image
        png = _tr.render_text_png(p)
        if png is None:
            return JSONResponse({"text": "", "note": "无法渲染该文件"})
        img = Image.open(_io.BytesIO(png))
        text = _ocr_region_text(img, x0, y0, x1, y1)
        return JSONResponse({"text": text, "note": "" if text else "该区域未识别到文字"})

    import fitz
    doc = fitz.open(p)
    pimg = None
    try:
        if n < 0 or n >= doc.page_count:
            return JSONResponse({"error": "页码越界"}, status_code=404)
        page = doc[n]
        W, H = page.rect.width, page.rect.height
        rx0, rx1 = sorted((x0 * W, x1 * W))
        ry0, ry1 = sorted((y0 * H, y1 * H))
        picks = []
        for w in page.get_text("words"):
            wx0, wy0, wx1, wy1, txt = w[0], w[1], w[2], w[3], w[4]
            # 词框与选区**有重叠**即取（比"词中心落框内"宽容：细长横条选区也能取到该行数字/金额）
            overlaps = not (wx1 < rx0 or wx0 > rx1 or wy1 < ry0 or wy0 > ry1)
            if overlaps and txt.strip():
                picks.append((round(wy0, 1), wx0, txt))
        picks.sort()
        text = " ".join(t for _, _, t in picks)
        if text:
            return JSONResponse({"text": text})
        # 无文本层（扫描型 PDF）→ 渲染该页为图片，走 OCR 兜底取字
        import io as _io
        pix = page.get_pixmap(matrix=fitz.Matrix(300 / 72.0, 300 / 72.0))
        from PIL import Image
        pimg = Image.open(_io.BytesIO(pix.tobytes("png")))
    finally:
        doc.close()
    text = _ocr_region_text(pimg, x0, y0, x1, y1) if pimg is not None else ""
    return JSONResponse({"text": text, "note": "" if text else "该区域未识别到文字"})


@app.get("/review", response_class=HTMLResponse)
def review_page() -> str:
    """人工审核界面（静态页，调用 /api/review/* 接口）。"""
    return _page((config.BASE_DIR / "web" / "review.html").read_text(encoding="utf-8"))


def _md_to_html(md: str) -> str:
    """把使用说明的 Markdown 轻量渲染成 HTML（只覆盖本文用到的语法：标题/列表/表格/加粗/代码/引用/分隔线）。"""
    import html as _h
    import re
    out: List[str] = []
    in_code = in_ul = False
    tbuf: List[List[str]] = []

    def inline(s: str) -> str:
        s = _h.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return s

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>"); in_ul = False

    def flush_table():
        nonlocal tbuf
        rows = [r for r in tbuf if not all(set(c) <= {"-", ":", " "} for c in r)]
        if rows:
            out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in rows[0]) + "</tr>")
            for r in rows[1:]:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</table>")
        tbuf = []

    for raw in md.split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            flush_table()
            if in_code:
                out.append("</pre>"); in_code = False
            else:
                close_ul(); out.append('<pre class="cb">'); in_code = True
            continue
        if in_code:
            out.append(_h.escape(line)); continue
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            close_ul(); tbuf.append([c.strip() for c in s.strip("|").split("|")]); continue
        flush_table()
        if not s:
            close_ul(); continue
        if s.startswith("#"):
            close_ul(); lvl = min(len(s) - len(s.lstrip("#")), 4)
            out.append(f"<h{lvl}>{inline(s.lstrip('#').strip())}</h{lvl}>"); continue
        if set(s) <= {"-"} and len(s) >= 3:
            close_ul(); out.append("<hr>"); continue
        if s.startswith(">"):
            close_ul(); out.append(f"<blockquote>{inline(s[1:].strip())}</blockquote>"); continue
        if s.startswith("- "):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{inline(s[2:])}</li>"); continue
        close_ul(); out.append(f"<p>{inline(s)}</p>")
    close_ul(); flush_table()
    if in_code:
        out.append("</pre>")
    body = "\n".join(out)
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>使用说明</title>'
        "<style>body{max-width:860px;margin:0 auto;padding:24px 18px 60px;font:15px/1.7 -apple-system,"
        "Segoe UI,Roboto,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2937}"
        "h1{font-size:24px;border-bottom:2px solid #1f4e78;padding-bottom:6px}h2{font-size:19px;color:#1f4e78;"
        "margin-top:28px}h3{font-size:16px}h4{font-size:15px;color:#374151}"
        "table{border-collapse:collapse;width:100%;margin:10px 0;font-size:14px}"
        "th,td{border:1px solid #d1d5db;padding:6px 10px;text-align:left}th{background:#eef4fb}"
        "code{background:#f1f5f9;padding:1px 5px;border-radius:4px;font-size:13px}"
        ".cb{background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:8px;overflow:auto;font-size:13px}"
        "blockquote{margin:10px 0;padding:8px 14px;background:#fbf7ef;border-left:4px solid #d19a3d;border-radius:4px}"
        "hr{border:0;border-top:1px solid #e5e7eb;margin:22px 0}ul{padding-left:22px}li{margin:3px 0}"
        "a.top{display:inline-block;margin-bottom:16px;color:#1f4e78;font-size:14px;font-weight:600;text-decoration:none;"
        "border:1px solid #1f4e78;padding:7px 14px;border-radius:8px}a.top:hover{background:#eef4fb}</style></head><body>"
        '<a class="top" href="review">← 回到审核</a>\n' + body + "</body></html>")


@app.get("/help", response_class=HTMLResponse)
def help_page() -> str:
    """使用说明（识别 / 审核 / 学习规则）——渲染 extraction/使用说明.md。"""
    p = config.BASE_DIR / "extraction" / "使用说明.md"
    if not p.exists():
        return "<p>使用说明文件缺失。</p>"
    return _md_to_html(p.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _page(_INDEX_HTML)


_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>财务管理系统</title>
<style>
  :root{--bd:#e2e8f0;--pri:#1f4e78;--bg:#f7f9fc;}
  *{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,"PingFang SC","Microsoft YaHei",sans-serif;}
  body{margin:0;background:var(--bg);color:#1a202c;font-size:14px;line-height:1.5;}
  header{background:var(--pri);color:#fff;padding:18px 28px;position:sticky;top:0;z-index:20;}
  header h1{margin:0;font-size:18px;font-weight:700;}
  header p{margin:4px 0 0;font-size:12px;opacity:.85;}
  main{max-width:1180px;margin:24px auto;padding:0 20px;}
  .drop{border:2px dashed #94a3b8;border-radius:12px;background:#fff;padding:44px;text-align:center;cursor:pointer;transition:.15s;}
  .drop:hover{border-color:var(--pri);background:#fbfdff;}
  .drop.drag{border-color:var(--pri);background:#eef4fb;}
  .drop p{margin:6px 0;color:#475569;}
  .drop strong{font-size:16px;color:#1f2d3d;}
  .btn{background:var(--pri);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer;}
  .btn:disabled{opacity:.5;cursor:not-allowed;}
  .bar{display:flex;gap:12px;align-items:center;margin:18px 0;}
  table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;font-size:13px;box-shadow:0 1px 4px rgba(0,0,0,.06);}
  th,td{padding:10px 11px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:top;}
  th{background:#eef2f7;font-weight:600;}
  tbody tr:hover{background:#f5f8fd;}
  .tabbtn{padding:8px 16px;border:1px solid var(--bd);background:#fff;color:#475569;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;}
  .tabbtn.on{background:var(--pri);color:#fff;border-color:var(--pri);}
  .risk-hi{color:#b91c1c;font-weight:700;}
  .failedrow td{background:#fff4f4;}
  .risk-ok{color:#15803d;}
  .tag{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;}
  .tag.warn{background:#fef3c7;color:#92400e;}
  .tag.err{background:#fee2e2;color:#991b1b;}
  .tag.crit{background:#b91c1c;color:#fff;}
  .tag.info{background:#e0e7ff;color:#3730a3;}
  .muted{color:#64748b;font-size:12px;}
  .card{background:#fff;border:1px solid var(--bd);border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.06);margin:18px 0;overflow:hidden;}
  .card .chead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:11px 16px;border-bottom:1px solid var(--bd);background:#f8fafc;}
  .card .chead b{font-size:14px;color:#1f2d3d;}
  table.rtx{box-shadow:none;border-radius:0;font-size:12.5px;margin:0;}
  table.rtx th,table.rtx td{padding:8px 12px;}
  table.rtx tbody tr{cursor:pointer;}
  table.rtx .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}
  table.rtx .in{color:#15803d;font-weight:600;}
  table.rtx .out{color:#b91c1c;font-weight:600;}
  table.rtx .desc{max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .issues{margin:4px 0 0;padding-left:0;list-style:none;}
  .issues li{margin:2px 0;}
  .cmpbtn{display:inline-block;margin-top:4px;padding:3px 8px;background:#b91c1c;color:#fff;border-radius:4px;text-decoration:none;font-size:12px;}
  #status{font-size:13px;color:#475569;}
</style>
</head>
<body>
<header style="display:flex;justify-content:space-between;align-items:center">
  <div>
    <h1>财务管理系统</h1>
    <p>纯本地运行 · 数据不出机 · PDF文本优先 / OCR兜底 / 规则解析 / 多重校验</p>
  </div>
</header>
<main>
  <div class="bar" style="margin-bottom:12px">
    <span style="font-weight:600;color:#334155">上传类型：</span>
    <button id="tab-invoice" class="tabbtn on" onclick="setDocType('invoice')">📄 发票</button>
    <button id="tab-statement" class="tabbtn" onclick="setDocType('statement')">🏦 银行流水</button>
  </div>
  <div id="drop" class="drop">
    <p><strong id="drophint">拖拽发票文件到这里</strong> 或点击选择</p>
    <p class="muted" id="dropdesc">自动识别：PDF / Word（.docx）/ Excel（.xlsx）/ 图片（PNG·JPG）；旧版 .doc/.xls/.ppt/.rtf/.odt 等在装了 LibreOffice 时也自动识别，否则可下载原件人工录入或另存为 .docx/PDF。可多选</p>
    <input id="file" type="file" multiple style="display:none"/>
  </div>
  <!-- 识别进度卡片：最近成功识别的流水交易（按最新上传排序，点行进详情核对） -->
  <section id="recentTxCard" class="card" style="display:none">
    <div class="chead">
      <b>🏦 最近识别的流水交易</b>
      <span class="muted">最新上传的流水 · 最近 <span id="rtxN">0</span> 笔 · 点任一行进入详情核对</span>
      <a id="rtxAll" href="/review" style="margin-left:auto;font-size:12px;color:var(--pri);text-decoration:none;font-weight:600">流水审核 →</a>
    </div>
    <table class="rtx">
      <thead><tr><th>日期</th><th>摘要</th><th class="num">收入</th><th class="num">支出</th><th class="num">余额</th><th>来源</th></tr></thead>
      <tbody id="rtxBody"></tbody>
    </table>
  </section>
  <div class="bar">
    <button id="export" class="btn" disabled>导出全部 Excel（8 Sheet 工作底稿）</button>
    <button id="exportApproved" class="btn" disabled title="只导出审核已通过(Approved)的记录">仅导出已通过的</button>
    <span id="status"></span>
  </div>
  <table id="tbl" style="display:none">
    <thead><tr>
      <th>文件名</th><th>发票号</th><th>日期</th><th>结算币种</th><th>总金额</th>
      <th>建议分类</th><th>解析方式</th><th>风险评分</th><th>校验/复核</th><th>校验问题</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <div id="morebar" style="margin:14px 0;display:none;text-align:center">
    <button id="moreBtn" class="btn">加载更多</button>
  </div>
</main>
<script>
const drop=document.getElementById('drop'),file=document.getElementById('file'),
  tbl=document.getElementById('tbl'),tb=tbl.querySelector('tbody'),
  status=document.getElementById('status'),exportBtn=document.getElementById('export'),
  exportApprovedBtn=document.getElementById('exportApproved');

drop.onclick=()=>file.click();
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('drag');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('drag');}));
drop.addEventListener('drop',ev=>upload(ev.dataTransfer.files));
file.addEventListener('change',()=>upload(file.files));

function sevTag(s){const m={warning:'warn',error:'err',critical:'crit',info:'info'};return m[s]||'info';}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function rlink(h,ti){ return '/review?hash='+encodeURIComponent(h)+(ti!=null?'&txn='+ti:''); }   // 深链进流水详情、定位到具体交易行（demo 子路径由 sync-demo 补前缀）

// 识别进度卡片：拉取最近成功识别的流水交易，渲染小表格（点行进详情核对）
// 仅在「🏦 银行流水」上传标签下显示；切到「📄 发票」标签时隐藏。
async function loadRecentTx(){
  const card=document.getElementById('recentTxCard');
  if(DOC_TYPE!=='statement'){ card.style.display='none'; return; }
  try{
    const r=await fetch('/api/recent-transactions?limit=10');
    const j=await r.json();
    const txns=(j&&j.transactions)||[];
    if(!txns.length){ card.style.display='none'; return; }
    document.getElementById('rtxBody').innerHTML = txns.map(t=>{
      // 收入/支出已分两列，数字一律正数、不带 +/- 符号（方向由所在列表示）；仅用绿/红上色区分
      const inc=t.income!=null?('<span class="in">'+esc(t.income)+'</span>'):'';
      const out=t.expense!=null?('<span class="out">'+esc(t.expense)+'</span>'):'';
      const bal=t.balance!=null?esc(t.balance):'';
      const src=[t.currency,t.source].filter(Boolean).map(esc).join(' · ');   // 币种并入来源列，不占金额列
      return '<tr onclick="location.href=rlink(\\''+t.file_hash+'\\','+(t.txn_index!=null?t.txn_index:'null')+')">'
        +'<td>'+esc(t.date||'—')+'</td>'
        +'<td class="desc" title="'+esc(t.description||'')+'">'+esc(t.description||'—')+'</td>'
        +'<td class="num">'+inc+'</td><td class="num">'+out+'</td>'
        +'<td class="num">'+bal+'</td>'
        +'<td class="muted">'+src+'</td></tr>';
    }).join('');
    document.getElementById('rtxN').textContent=txns.length;
    document.getElementById('rtxAll').href=rlink(txns[0].file_hash);
    card.style.display='';
  }catch(e){ card.style.display='none'; }
}

let DOC_TYPE='invoice';
const _THEAD_INVOICE='<tr>'
  +'<th>文件名</th><th>发票号</th><th>日期</th><th>结算币种</th><th>总金额</th>'
  +'<th>建议分类</th><th>解析方式</th><th>风险评分</th><th>校验/复核</th><th>校验问题</th></tr>';
const _THEAD_STATEMENT='<tr>'
  +'<th>文件名</th><th>银行/账号</th><th>对账期间</th><th>币种</th><th>交易笔数</th>'
  +'<th>期末余额</th><th>解析方式</th><th>风险评分</th><th>校验/复核</th><th>校验问题</th></tr>';
function setDocType(t){
  DOC_TYPE=t;
  document.getElementById('tab-invoice').classList.toggle('on', t==='invoice');
  document.getElementById('tab-statement').classList.toggle('on', t==='statement');
  document.getElementById('drophint').textContent = t==='statement' ? '拖拽银行流水文件到这里' : '拖拽发票文件到这里';
  document.getElementById('dropdesc').textContent = t==='statement'
    ? '识别银行对账单/流水：账户信息 + 逐笔交易（日期/摘要/收入/支出/余额）。支持 CSV/Excel/PDF/OFX/MT940/CAMT053 等格式。可多选'
    : '自动识别：PDF / Word（.docx）/ Excel（.xlsx）/ 图片（PNG·JPG）；旧版 .doc/.xls/.ppt/.rtf/.odt 等在装了 LibreOffice 时也自动识别。可多选';
  // 切换表头列 + 隔离记录清单：流水不显示发票记录，反之亦然
  tbl.querySelector('thead').innerHTML = t==='statement' ? _THEAD_STATEMENT : _THEAD_INVOICE;
  // 导出仅适用于发票（流水导出暂未提供）——流水页隐藏导出按钮
  const expWrap = t==='statement' ? 'none' : '';
  exportBtn.style.display = expWrap; exportApprovedBtn.style.display = expWrap;
  if(t==='statement'){ exportBtn.disabled=true; exportApprovedBtn.disabled=true; }
  initFromDb(true);   // 按当前类型重新拉取记录
  loadRecentTx();     // 「最近识别的流水交易」卡片：仅流水标签显示，切标签即更新可见性
}

async function upload(files){
  if(!files.length)return;
  status.textContent='正在识别 '+files.length+' 个文件…';
  const fd=new FormData();
  for(const f of files)fd.append('files',f);
  fd.append('doc_type', DOC_TYPE);   // 发票 / 银行流水
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const data=await r.json();
    await initFromDb(true);   // 重新拉取（首页）并按「提取失败在前、最新上传在前」排序展示
    loadRecentTx();           // 刷新「最近识别的流水交易」卡片（新传流水即时体现）
    // 本次未入库的问题文件（过大/空等）也置顶提示出来，避免"上传了却看不到"
    (data.results||[]).filter(x=>x.error).reverse().forEach(x=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${esc(x.file_name||'—')}</td><td colspan="9" class="risk-hi">⚠ ${esc(x.error)}</td>`;
      tb.insertBefore(tr, tb.firstChild);
    });
    status.textContent='已处理 '+data.count+' 个文件（提取失败的已置顶，可点「人工录入」）。';
    exportBtn.disabled=false;
    exportApprovedBtn.disabled=false;
  }catch(e){status.textContent='出错: '+e;}
}

function render(rows){
  tbl.style.display='table';
  for(const x of rows){
    const tr=document.createElement('tr');
    if(x.error){
      tr.innerHTML=`<td>${esc(x.file_name||'—')}</td><td colspan="9" class="risk-hi">⚠ ${esc(x.error)}</td>`;
      tb.appendChild(tr);
      continue;
    }
    if(x.parse_failed){   // 提取失败：置顶、红底，可点「人工录入」进审核页对照原件填写
      tr.className='failedrow';
      tr.innerHTML=`<td>⚠ ${esc(x.file_name)}</td>
        <td colspan="8" class="risk-hi">自动提取失败——请对照原件人工录入（已置顶）</td>
        <td><a class="cmpbtn" href="/review?hash=${encodeURIComponent(x.file_hash_full)}">→ 人工录入</a></td>`;
      tb.appendChild(tr);
      continue;
    }
    const riskCls=x.risk_score>30?'risk-hi':'risk-ok';
    const hasDup=(x.issues||[]).some(i=>i.code==='DUPLICATE');
    const dupBtn=hasDup&&x.file_hash_full?`<a class="cmpbtn" href="/compare?hash=${encodeURIComponent(x.file_hash_full)}" target="_blank">⚖ 对比确认重复 →</a>`:'';
    const issues=(x.issues||[]).map(i=>`<li><span class="tag ${sevTag(i.severity)}">${esc(i.severity)}</span> ${esc(i.code)}: ${esc(i.message)}</li>`).join('')+dupBtn;
    const method=`${x.parse_method||'—'}${x.parse_method==='ocr'?(' '+(x.ocr_quality*100).toFixed(1)+'%'):''}`;
    const tail=`<td>${esc(method)}</td>
      <td class="${riskCls}">${esc(x.risk_score)}</td>
      <td>${esc(x.validation_status)}<br><span class="muted">${esc(x.review_status)}</span></td>
      <td><ul class="issues">${issues||'<li class="muted">无</li>'}</ul></td>`;
    if(x.doc_type==='statement'){   // 银行流水记录：账户/期间/笔数/期末余额
      const bank=[x.bank_name,x.bank_account_no].filter(Boolean).join(' · ')||'—';
      const period=(x.statement_period_start||x.statement_period_end)
        ? `${x.statement_period_start||'?'} ~ ${x.statement_period_end||'?'}` : '—';
      tr.innerHTML=`<td>${esc(x.file_name)}</td><td>${esc(bank)}</td>
        <td>${esc(period)}</td><td>${esc(x.currency_settlement||'—')}</td>
        <td>${esc(x.txn_count!=null?x.txn_count:'—')}</td><td>${esc(x.closing_balance||'—')}</td>${tail}`;
    }else{
      tr.innerHTML=`<td>${esc(x.file_name)}</td><td>${esc(x.invoice_no||'—')}</td>
        <td>${esc(x.invoice_date||'—')}</td><td>${esc(x.currency_settlement||'—')}</td>
        <td>${esc(x.total_due||'—')}</td><td>${esc(x.category||'—')}</td>${tail}`;
    }
    tb.appendChild(tr);
  }
}

// 页面加载即拉取库内已有记录（分页：默认每页 100，可「加载更多」）；有记录就启用导出。
let listOffset=0; const LIST_PAGE=100; let listTotal=0;
async function initFromDb(reset){
  if(reset){ listOffset=0; tb.innerHTML=''; }
  try{
    const r=await fetch('/api/invoices?limit='+LIST_PAGE+'&offset='+listOffset+'&doc_type='+DOC_TYPE);
    const d=await r.json();
    listTotal=d.count||0;
    if((d.results||[]).length){
      render(d.results);
      listOffset += d.results.length;
      if(DOC_TYPE==='invoice'){ exportBtn.disabled=false; exportApprovedBtn.disabled=false; }
    }
    tbl.style.display = listTotal ? 'table' : 'none';
    document.getElementById('morebar').style.display = d.has_more ? 'block' : 'none';
    const noun = DOC_TYPE==='statement' ? '流水' : '发票';
    if(listTotal>0) status.textContent='库内共 '+listTotal+' 条'+noun
      +(listOffset<listTotal?('，已加载 '+listOffset+' 条（点「加载更多」）'):'')
      +(DOC_TYPE==='invoice'?'（导出为全部）':'')+'。';
    else status.textContent='暂无'+noun+'记录，拖拽文件到上方开始识别。';
  }catch(e){/* 库为空或接口不可用时保持禁用，上传后再启用 */}
}
document.getElementById('moreBtn').onclick=()=>initFromDb(false);
initFromDb(true);
loadRecentTx();

async function doExport(approvedOnly){
  status.textContent='正在生成 Excel…';
  const url='/api/export'+(approvedOnly?'?approved_only=true':'');
  const r=await fetch(url,{method:'POST'});
  const d=await r.json();
  if(d.download){
    status.textContent='已生成: '+d.file+'（'+d.count+' 条'+(approvedOnly?'，仅已通过':'，全部')+'）';
    window.location=d.download;
  }else{status.textContent='导出失败: '+(d.error||'');}
}
exportBtn.onclick=()=>doExport(false);
exportApprovedBtn.onclick=()=>doExport(true);
</script>
<button id="toTop" title="回到顶部" onclick="__toTop()">↑ 顶部</button>
<style>#toTop{position:fixed;right:18px;bottom:18px;z-index:9999;display:none;padding:9px 13px;border:0;border-radius:22px;background:#1f4e78;color:#fff;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.3)}#toTop:hover{background:#163a5a}</style>
<script>
(function(){
  var SC='#queue,.qcol,.detail,.fields,.orig,#list,main,.list,.layout';
  function conts(){return [document.scrollingElement||document.documentElement].concat([].slice.call(document.querySelectorAll(SC)));}
  window.__toTop=function(){conts().forEach(function(el){try{el.scrollTo({top:0,behavior:'smooth'});}catch(e){if(el)el.scrollTop=0;}});};
  function upd(){var b=document.getElementById('toTop');if(!b)return;var on=false;conts().forEach(function(el){if(el&&el.scrollTop>200)on=true;});b.style.display=on?'block':'none';}
  document.addEventListener('scroll',upd,true);window.addEventListener('resize',upd);setInterval(upd,600);
})();
</script>
</body>
</html>"""
