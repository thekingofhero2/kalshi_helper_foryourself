# -*- coding: utf-8 -*-
"""PEM 私钥与登录会话的本地持久化。

存放位置：
- 打包运行（exe）：可执行文件同级 data/ 目录（便携，拷走 exe+data 即可迁移）
- 源码运行：工程根 data/ 目录
data/ 已在 .gitignore 中，*.pem 也被忽略。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import serialization


def _data_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"


DATA_DIR = _data_dir()
PEM_PATH = DATA_DIR / "key.pem"
SESSION_PATH = DATA_DIR / "session.json"


def validate_pem(pem_text: str):
    """校验 PEM 是否为可用私钥，返回私钥对象；失败抛异常。"""
    return serialization.load_pem_private_key(pem_text.encode("utf-8"), password=None)


def stage_pem(pem_text: str) -> Path:
    """把待验证的私钥写到临时文件（验证连接成功后再落正式会话）。"""
    validate_pem(pem_text)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "staged.pem"
    path.write_text(pem_text.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def save_session(key_id: str, pem_text: str, env: str) -> dict:
    key_id = (key_id or "").strip()
    if not key_id:
        raise ValueError("API Key ID 不能为空")
    if not (pem_text or "").strip():
        raise ValueError("PEM 文件内容为空")
    validate_pem(pem_text)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PEM_PATH.write_text(pem_text.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(PEM_PATH, 0o600)
    except OSError:
        pass

    sess = {
        "key_id": key_id,
        "env": env,
        "pem_path": str(PEM_PATH),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    SESSION_PATH.write_text(json.dumps(sess, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass
    return sess


def load_session() -> Optional[dict]:
    try:
        sess = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pem = Path(sess.get("pem_path") or PEM_PATH)
    if not pem.exists():
        return None
    if sess.get("env") not in ("prod", "demo"):
        sess["env"] = "prod"
    return sess


def clear_session() -> None:
    for p in (PEM_PATH, SESSION_PATH):
        try:
            p.unlink()
        except OSError:
            pass


def mask_key(key_id: str) -> str:
    k = key_id or ""
    if len(k) <= 10:
        return k
    return f"{k[:6]}…{k[-4:]}"
