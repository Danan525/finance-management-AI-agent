"""文件落盘 + SHA256 哈希 + 重复文件识别。"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Optional

from . import config


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_upload(data: bytes, original_name: str) -> tuple[Path, str]:
    """保存上传文件到本地 uploads 目录。

    返回 (落盘路径, 文件哈希)。文件以哈希前缀命名避免覆盖与冲突。
    """
    file_hash = sha256_of_bytes(data)
    safe_name = Path(original_name).name  # 去掉任何路径成分
    dest = config.UPLOAD_DIR / f"{file_hash[:12]}_{safe_name}"
    if not dest.exists():
        with open(dest, "wb") as f:
            f.write(data)
    return dest, file_hash


def import_local_file(src: Path) -> tuple[Path, str]:
    """把一个已存在的本地文件拷入 uploads（供 CLI/自测用）。

    若 src 本就在 uploads 内（如对已入库文件重处理），直接复用、不再加前缀重拷
    （否则会层层叠加前缀、并产生重复文件/记录）。
    """
    src = Path(src).resolve()
    file_hash = sha256_of_file(src)
    if src.parent == config.UPLOAD_DIR.resolve():
        return src, file_hash
    dest = config.UPLOAD_DIR / f"{file_hash[:12]}_{src.name}"
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest, file_hash
