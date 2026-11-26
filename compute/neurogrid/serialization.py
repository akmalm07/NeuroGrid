"""Canonical JSON and artifact digests shared by proof backends."""

import json
from hashlib import file_digest
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest_file(path: Path) -> str:
    with path.open("rb") as data:
        return file_digest(data, "sha256").hexdigest()


def write_json(path: Path, value: Any) -> None:
    with path.open("xb") as data:
        data.write(canonical(value))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
