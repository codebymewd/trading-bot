"""Tiny .env loader: KEY=VALUE lines, '#' comments, never overrides variables that are already set."""
import os
from typing import Dict


def parse_env(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if k.strip():
            out[k.strip()] = v
    return out


def load_env_file(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        values = parse_env(fh.read())
    n = 0
    for k, v in values.items():
        if k not in os.environ and v:
            os.environ[k] = v
            n += 1
    return n
