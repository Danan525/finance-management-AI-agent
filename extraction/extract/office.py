"""用 LibreOffice(headless) 把旧版/其它办公格式转成**带文本层的 PDF**，再复用成熟的 PDF 提取路径
（文本提取 + 原件预览 + 字段 bbox 高亮 + 多发票拆分，效果最好）。

仅用于 fitz/openpyxl **不能直接处理**的格式（旧二进制 .doc/.xls/.ppt、RTF、ODF 等）；
`.docx/.xlsx` 保持各自专用路径（已针对多发票/日期/图片优化）。LibreOffice 未装则不可用，
上层降级为 failed 记录 + 下载原件人工录入。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

# 需要 LibreOffice 转换的格式（不含已直接支持的 .docx/.xlsx；.pdf/图片另走各自路径）
_CONVERTIBLE = {".doc", ".dot", ".xls", ".xlt", ".ppt", ".pot", ".pps",
                ".rtf", ".odt", ".ods", ".odp"}

# 电子表格类：转 PDF 会按“可见列宽”裁剪单元格文字（Descriptio…/值散成独立行），
# 应转成 .xlsx 用 openpyxl 读**全量单元格**（不裁剪），再走成熟的 Excel 路径。
_SPREADSHEET = {".xls", ".xlt", ".ods", ".fods"}

# 文书类：LibreOffice 转 PDF 时表格单元格常被 fitz 粘连成一行（DescriptionQtyAmount…），
# 改转 .docx 让 fitz 原生读表（与 .docx 发票同路径，逐格分明）。幻灯片(.ppt/.odp)无此问题、仍走 PDF。
_WORDDOC = {".doc", ".dot", ".rtf", ".odt", ".fodt"}


# 装了但不在 PATH 的常见安装位置（Windows/macOS）也探测，免得"装了却识别不到"
_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",        # macOS
    r"C:\Program Files\LibreOffice\program\soffice.exe",           # Windows 64 位
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",     # Windows 32 位
    "/usr/bin/soffice", "/usr/bin/libreoffice",                    # Linux
]


def soffice_bin():
    b = shutil.which("soffice") or shutil.which("libreoffice")
    if b:
        return b
    for c in _CANDIDATES:
        if Path(c).exists():
            return c
    return None


def available() -> bool:
    return soffice_bin() is not None


def is_convertible(suffix: str) -> bool:
    return (suffix or "").lower() in _CONVERTIBLE


def is_spreadsheet(suffix: str) -> bool:
    return (suffix or "").lower() in _SPREADSHEET


def is_worddoc(suffix: str) -> bool:
    return (suffix or "").lower() in _WORDDOC


def _run_soffice(cmd, timeout: int = 120) -> None:
    """跑 LibreOffice 转换。超时杀**整个进程组**——soffice 会派生 soffice.bin 孙进程，
    `subprocess.run(timeout=)` 只杀直接子进程会留孤儿累积。用新会话隔离进程组、超时 `killpg`。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)          # 自成进程组，便于整组终止
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # 杀整组（含 soffice.bin 孙进程）
        except Exception:
            proc.kill()                                      # 兜底（如 Windows 无 killpg）
        proc.communicate()
        raise RuntimeError(f"LibreOffice 转换超时（>{timeout}s，已终止进程组）")
    if proc.returncode != 0:
        raise RuntimeError(f"LibreOffice 转换失败（退出码 {proc.returncode}）")


def _convert(path, target_ext: str) -> bytes:
    """通用 LibreOffice 转换：把 path 转成 target_ext，返回字节。失败/超时抛异常。"""
    binp = soffice_bin()
    if not binp:
        raise RuntimeError("LibreOffice 不可用")
    src = Path(path)
    with tempfile.TemporaryDirectory() as prof, tempfile.TemporaryDirectory() as out:
        cmd = [binp, "--headless", "--norestore", "--nolockcheck",
               f"-env:UserInstallation=file://{prof}",
               "--convert-to", target_ext, "--outdir", out, str(src)]
        _run_soffice(cmd)
        dst = Path(out) / (src.stem + "." + target_ext.split(":")[0])
        if not dst.exists():
            raise RuntimeError(f"LibreOffice 未产出 {target_ext}")
        return dst.read_bytes()


def to_xlsx(path) -> bytes:
    """把电子表格（.xls/.ods 等）转成 .xlsx 字节，供 openpyxl 读全量单元格（不裁剪列宽）。"""
    return _convert(path, "xlsx")


def to_docx(path) -> bytes:
    """把文书（.doc/.rtf/.odt 等）转成 .docx 字节，供 fitz 原生读表（不粘连单元格）。"""
    return _convert(path, "docx:MS Word 2007 XML")


def to_pdf(path) -> bytes:
    """把 path 转成 PDF，返回 PDF 字节。失败/超时抛异常（上层兜底为 failed 记录）。

    每次用独立 UserInstallation profile，避免并发/已存 profile 的锁冲突。
    """
    binp = soffice_bin()
    if not binp:
        raise RuntimeError("LibreOffice 不可用")
    src = Path(path)
    with tempfile.TemporaryDirectory() as prof, tempfile.TemporaryDirectory() as out:
        cmd = [binp, "--headless", "--norestore", "--nolockcheck",
               f"-env:UserInstallation=file://{prof}",
               "--convert-to", "pdf", "--outdir", out, str(src)]
        _run_soffice(cmd)
        pdf = Path(out) / (src.stem + ".pdf")
        if not pdf.exists():
            raise RuntimeError("LibreOffice 未产出 PDF")
        return pdf.read_bytes()
