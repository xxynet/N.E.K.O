from __future__ import annotations

import base64
import hashlib
import secrets
import time


_KEY = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"
_TOKEN_PHRASES = ("唉？！云朵！", "哒哒哒哒哒，好想玩原神", "云！原！神！")


def _vm(num: int) -> int:
    return (255 & ((num << 1) ^ 27)) if num & 128 else num << 1


def _qm(num: int) -> int:
    return _vm(num) ^ num


def _mm(num: int) -> int:
    return _qm(_vm(num))


def _ym(num: int) -> int:
    return _mm(_qm(_vm(num)))


def _gm(num: int) -> int:
    return _ym(num) ^ _mm(num) ^ _qm(num)


def _mixed(values: list[int]) -> list[int]:
    return [
        _gm(values[0]) ^ _ym(values[1]) ^ _mm(values[2]) ^ _qm(values[3]),
        _qm(values[0]) ^ _gm(values[1]) ^ _ym(values[2]) ^ _mm(values[3]),
        _mm(values[0]) ^ _qm(values[1]) ^ _gm(values[2]) ^ _ym(values[3]),
        _ym(values[0]) ^ _mm(values[1]) ^ _qm(values[2]) ^ _gm(values[3]),
        values[4],
        values[5],
    ]


def _av(value: str, key: str, n: int) -> str:
    pool = key[: len(key) + n]
    return "".join(pool[ord(char) % len(pool)] for char in value)


def _sv(value: str, key: str) -> str:
    return "".join(key[ord(char) % len(key)] for char in value)


def _interleave(values: list[str]) -> str:
    output: list[str] = []
    for index in range(len(values[2])):
        for value in values:
            if index < len(value):
                output.append(value[index])
    return "".join(output)


def build_request_keys(path: str, *, timestamp: int | None = None, nonce: str | None = None) -> tuple[str, str, int]:
    request_time = int(timestamp or time.time())
    request_nonce = nonce or hashlib.md5(
        f"{request_time}{secrets.randbelow(max(2, int(time.time() * 1000)))}".encode()
    ).hexdigest().upper()
    values = [
        _av(str(request_time), _KEY, -2),
        _sv(path, _KEY),
        _sv(request_nonce, _KEY),
    ]
    values.sort(key=len)
    # OpenXHH only hashes the first 20 bytes of the interleaved ASCII string.
    # Hashing the complete string produces a plausible-looking but invalid hkey.
    digest = hashlib.md5(_interleave(values).encode()[:20]).hexdigest()
    suffix_values = [ord(char) for char in digest[-6:]]
    checksum = sum(_mixed(suffix_values)) % 100
    return f"{_av(digest[:5], _KEY, -4)}{checksum:02d}", request_nonce, request_time


def build_xhh_token_id(*, timestamp: int | None = None) -> str:
    """Build the extra browser token used by OpenXHH's QR login flow."""
    current = int(timestamp or time.time())
    raw = bytearray(hashlib.md5(str(current).encode()).digest())
    for phrase in _TOKEN_PHRASES:
        raw.extend(hashlib.md5(phrase.encode()).digest())
    raw.append(0)
    return base64.b64encode(bytes(raw)).decode("ascii")


def ensure_xhh_token_cookie(cookie_header: str, *, timestamp: int | None = None) -> str:
    normalized = str(cookie_header or "").strip().rstrip(";")
    if not normalized or "x_xhh_tokenid=" in normalized:
        return normalized
    return f"{normalized};x_xhh_tokenid={build_xhh_token_id(timestamp=timestamp)}"
