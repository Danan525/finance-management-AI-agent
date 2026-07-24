@echo off
REM 财务管理系统 —— Windows 启动（双击即可）。纯本地、数据不出机、仅监听 127.0.0.1。
cd /d "%~dp0"
chcp 65001 >nul

where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python 3。请先安装：https://www.python.org/downloads/
  echo 安装时请勾选 "Add Python to PATH"，装好后重新双击本文件。
  pause
  exit /b 1
)

if not exist .venv (
  echo 首次启动：正在创建运行环境（只需一次）...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

REM 检测核心 + OCR 是否齐；任一缺失就一次装齐全部依赖（含 OCR）
set NEED=0
python -c "import fastapi, fitz, pdfplumber, openpyxl, docx, PIL" 2>nul || set NEED=1
python -c "import paddleocr" 2>nul || python -c "import rapidocr_onnxruntime" 2>nul || set NEED=1
if "%NEED%"=="1" (
  echo 首次安装：正在安装全部依赖（含 OCR，需联网）。OCR 体积较大，可能需要几分钟到十几分钟，请耐心等待...
  python -m pip install -q --upgrade pip
  python -m pip install -q -r requirements-core.txt
  if errorlevel 1 ( echo 核心依赖安装失败，请检查网络后重试 & pause & exit /b 1 )
  python -m pip install -q -r requirements-ocr.txt
  if errorlevel 1 ( echo OCR 依赖未装全：文本型 PDF/Word/Excel 仍可用；扫描件/图片识别可稍后重启再试。 )
)

REM 旧版 .doc/.xls/.ppt 等需 LibreOffice 才能自动识别；未装则引导安装（不阻断启动）
set _LO=0
where soffice >nul 2>nul && set _LO=1
if exist "C:\Program Files\LibreOffice\program\soffice.exe" set _LO=1
if exist "C:\Program Files (x86)\LibreOffice\program\soffice.exe" set _LO=1
if "%_LO%"=="0" (
  echo ------------------------------------------------------------
  echo 提示：未检测到 LibreOffice。
  echo   - PDF / Word(.docx) / Excel(.xlsx) / 图片 —— 均正常识别，不受影响。
  echo   - 旧版 .doc/.xls/.ppt、RTF、ODF 等 —— 需 LibreOffice 才能自动识别；
  echo     未装时这类文件仍会进队列，可下载原件人工录入，或另存为 .docx/PDF 再传。
  echo   如需支持旧格式，请安装 LibreOffice（免费）：https://www.libreoffice.org/download/
  echo   装好后重新双击本文件即可自动生效。
  echo ------------------------------------------------------------
)

echo.
echo 启动中...浏览器将自动打开（PDF/Word/Excel/图片/扫描件均可识别）。
echo （关闭本窗口或按 Ctrl+C 即停止服务，数据全部留在本机）
echo.
python -m gateway.launch
pause
