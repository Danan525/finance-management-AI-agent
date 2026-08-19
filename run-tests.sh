#!/bin/bash
# 运行单元测试（标准库 unittest，无需额外依赖）
# 覆盖：金额解析 / 地址校验 / 付款提取 / 内部校验 / JSON 往返 / 多发票切分 / 人工审核
cd "$(dirname "$0")" || exit 1
# 直接用 venv 里的解释器：不 source activate——本 venv 曾被整体搬移，activate 里硬编码的
# 旧 VIRTUAL_ENV 路径已失效，会让 PATH 指向不存在的目录（症状：exec: python: not found）。
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
exec "$PY" -m unittest discover -s tests -p "test_*.py" -v
