"""Signed EA <-> backend requests: HMAC-SHA256 over method, path, timestamp, nonce and body hash.
The same recipe is implemented on the EA side (WebRequest). Replays are rejected by nonce and clock skew."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional, Tuple

MAX_SKEW_SECONDS = 60
H_TS, H_NONCE, H_SIG = "X-TB-Timestamp", "X-TB-Nonce", "X-TB-Signature"


def _msg(method: str, path: str, ts: str, nonce: str, body: bytes) -> bytes:
    return f"{method.upper()}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()


def sign(secret: str, method: str, path: str, body: bytes, ts: Optional[int] = None, nonce: Optional[str] = None) -> dict:
    import time
    ts_s = str(int(time.time()) if ts is None else ts)
    n = nonce or secrets.token_hex(12)
    sig = hmac.new(secret.encode(), _msg(method, path, ts_s, n, body), hashlib.sha256).hexdigest()
    return {H_TS: ts_s, H_NONCE: n, H_SIG: sig}


def verify(secret: str, method: str, path: str, body: bytes, headers, now: int, db) -> Tuple[bool, str]:
    """Returns (ok, reason). Records the nonce only after the signature is valid."""
    ts, nonce, sig = headers.get(H_TS), headers.get(H_NONCE), headers.get(H_SIG)
    if not (ts and nonce and sig):
        return False, "missing_auth_headers"
    try:
        ts_i = int(ts)
    except ValueError:
        return False, "bad_timestamp"
    if abs(now - ts_i) > MAX_SKEW_SECONDS:
        return False, "clock_skew"
    expect = hmac.new(secret.encode(), _msg(method, path, ts, nonce, body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return False, "bad_signature"
    if not db.add_nonce(nonce, ts_i):
        return False, "replayed_nonce"
    db.purge_nonces(now - 10 * MAX_SKEW_SECONDS)
    return True, ""
