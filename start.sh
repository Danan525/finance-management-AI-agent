#!/bin/bash
# 启动本地 Web 服务（FastAPI / gateway），仅监听 127.0.0.1、数据不出机、不接外部服务
cd "$(dirname "$0")" || exit 1
[ -d .venv ] && source .venv/bin/activate
exec python -m gateway.launch
