#!/bin/bash
# 运行单元测试（标准库 unittest，无需额外依赖）
# 覆盖：金额解析 / 地址校验 / 付款提取 / 内部校验 / JSON 往返 / 多发票切分 / 人工审核
cd "$(dirname "$0")" || exit 1
[ -d .venv ] && source .venv/bin/activate
exec python -m unittest discover -s tests -p "test_*.py" -v
