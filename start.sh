#!/bin/bash
# 启动本地 Web 服务（FastAPI / gateway），仅监听 127.0.0.1、数据不出机、不接外部服务
cd "$(dirname "$0")" || exit 1
# 直接用 venv 里的解释器：不 source activate——本 venv 曾被整体搬移，activate 里硬编码的
# 旧 VIRTUAL_ENV 路径已失效，会让 PATH 指向不存在的目录（症状：exec: python: not found）。
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
exec "$PY" -m gateway.launch
