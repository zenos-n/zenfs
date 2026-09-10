"""Shared token contract for ZenOS application registries."""

from __future__ import annotations

import hashlib
import re


REGISTRY_SCHEMA = 1
TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def app_token(source: str, desktop_id: str) -> str:
    identity = f"zenos-app\0{source}\0{desktop_id}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def valid_token(value: object) -> bool:
    return isinstance(value, str) and TOKEN_PATTERN.fullmatch(value) is not None
