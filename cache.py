from __future__ import annotations

import hashlib
from pathlib import Path


def cache_key(text: str, voice: str, speed: float, fmt: str) -> str:
    payload = f"{voice}|{speed}|{fmt}|{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def cache_path(cache_dir: Path, key: str, fmt: str) -> Path:
    return cache_dir / f"{key}.{fmt}"
