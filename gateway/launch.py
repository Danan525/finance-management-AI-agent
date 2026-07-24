"""本地启动器：拉起服务并自动打开浏览器。

仅监听 127.0.0.1（财务数据不出机）。端口可用环境变量 PORT 覆盖；
若端口被占用会自动向后寻找可用端口。
"""
from __future__ import annotations

import os
import socket
import threading
import webbrowser

import uvicorn

HOST = "127.0.0.1"


def _free_port(start: int) -> int:
    for p in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, p)) != 0:   # 连接失败 = 端口空闲
                return p
    return start


def main() -> None:
    port = _free_port(int(os.environ.get("PORT", "8000")))
    url = f"http://{HOST}:{port}"
    print(f"\n财务管理系统已启动：{url}\n（按 Ctrl+C 停止）\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("gateway.main:app", host=HOST, port=port, log_level="info")


if __name__ == "__main__":
    main()
