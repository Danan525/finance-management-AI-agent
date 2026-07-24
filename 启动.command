#!/bin/bash
# 财务管理系统 —— Mac 启动（双击即可）。纯本地、数据不出机、仅监听 127.0.0.1。
cd "$(dirname "$0")" || exit 1

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "未找到 Python 3。请先安装：https://www.python.org/downloads/"
  echo "（装好后重新双击本文件即可）"
  read -r -p "按回车键关闭…" _; exit 1
fi

if [ ! -d .venv ]; then
  echo "首次启动：正在创建运行环境（只需一次）…"
  "$PY" -m venv .venv || { echo "创建环境失败"; read -r -p "按回车键关闭…" _; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# OCR 依赖按平台选：Apple Silicon 用 RapidOCR（免 PaddlePaddle），其余用 PaddleOCR
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  OCR_REQ="requirements-ocr-mac.txt"
else
  OCR_REQ="requirements-ocr.txt"
fi

# 检测核心 + OCR 是否齐；任一缺失就**一次装齐全部依赖**（含 OCR）
core_ok=1; ocr_ok=1
python -c "import fastapi, fitz, pdfplumber, openpyxl, docx, PIL" 2>/dev/null || core_ok=0
python -c "import paddleocr" 2>/dev/null || python -c "import rapidocr_onnxruntime" 2>/dev/null || ocr_ok=0
if [ "$core_ok" = 0 ] || [ "$ocr_ok" = 0 ]; then
  echo "首次安装：正在安装全部依赖（含 OCR，需联网）。OCR 体积较大，可能需要几分钟到十几分钟，请耐心等待…"
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements-core.txt || {
    echo "核心依赖安装失败，请检查网络后重试"; read -r -p "按回车键关闭…" _; exit 1; }
  python -m pip install -q -r "$OCR_REQ" \
    || echo "⚠ OCR 依赖未装全：文本型 PDF/Word/Excel 仍可正常使用；扫描件/图片识别可稍后重新启动再试。"
fi

# 旧版 .doc/.xls/.ppt 等需 LibreOffice 转换才能自动识别；未装则引导安装（不阻断启动）
if ! command -v soffice >/dev/null 2>&1 && ! command -v libreoffice >/dev/null 2>&1 \
   && [ ! -x "/Applications/LibreOffice.app/Contents/MacOS/soffice" ]; then
  echo "————————————————————————————————————————————————"
  echo "提示：未检测到 LibreOffice。"
  echo "  · PDF / Word(.docx) / Excel(.xlsx) / 图片 —— 均正常识别，不受影响。"
  echo "  · 旧版 .doc/.xls/.ppt、RTF、ODF 等 —— 需 LibreOffice 才能自动识别；"
  echo "    未装时这类文件仍会进队列，可下载原件人工录入，或另存为 .docx/PDF 再传。"
  echo "  如需支持旧格式，请安装 LibreOffice（免费）：https://www.libreoffice.org/download/"
  echo "    macOS 也可用：brew install --cask libreoffice"
  echo "  装好后重新双击本文件即可自动生效。"
  echo "————————————————————————————————————————————————"
fi

echo ""
echo "启动中…浏览器将自动打开（PDF/Word/Excel/图片/扫描件均可识别）。"
echo "（关闭本窗口或按 Ctrl+C 即停止服务，数据全部留在本机）"
echo ""
python -m gateway.launch
