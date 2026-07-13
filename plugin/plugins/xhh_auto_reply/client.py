from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .signing import build_request_keys, ensure_xhh_token_cookie


class XHHAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class XHHClient:
    def __init__(self, settings: dict[str, Any], logger: Any):
        self.settings = settings
        self.logger = logger
        self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, trust_env=False)
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._request_lock:
            interval = max(0.2, float(self.settings.get("min_request_interval_seconds", 2.0) or 2.0))
            wait = interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

            hkey, nonce, request_time = build_request_keys(path)
            query = dict(params or {})
            query.update(
                {
                    "os_type": "web",
                    "app": "web",
                    "client_type": "web",
                    "version": str(self.settings.get("version") or "999.0.4"),
                    "web_version": str(self.settings.get("web_version") or "2.5"),
                    "x_client_type": "web",
                    "x_app": "heybox_website",
                    "x_os_type": "Windows",
                    "device_info": "Chrome",
                    "device_id": str(self.settings.get("device_id") or ""),
                    "hkey": hkey,
                    "_time": str(request_time),
                    "nonce": nonce,
                    "_notip": "true",
                }
            )
            heybox_id = str(self.settings.get("heybox_id") or "").strip()
            if heybox_id:
                query["heybox_id"] = heybox_id

            base_url = str(self.settings.get("base_url") or "https://api.xiaoheihe.cn").rstrip("/")
            headers = {"Referer": "https://www.xiaoheihe.cn/"}
            cookie = str(self.settings.get("cookie") or "").strip()
            if cookie:
                headers["Cookie"] = ensure_xhh_token_cookie(cookie)
            host = urlparse(base_url).hostname
            if host:
                headers["Host"] = host

            response = await self._client.request(
                method.upper(), f"{base_url}{path}", params=query, data=data, headers=headers
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise XHHAPIError(
                    f"小黑盒返回了非 JSON 响应（HTTP {response.status_code}）",
                    status_code=response.status_code,
                    payload=response.text[:500],
                ) from exc
            if response.status_code >= 400:
                raise XHHAPIError(
                    f"小黑盒请求失败（HTTP {response.status_code}）",
                    status_code=response.status_code,
                    payload=payload,
                )
            status = str(payload.get("status") or payload.get("stat") or "ok")
            if status not in {"ok", "success"}:
                raise XHHAPIError(
                    str(payload.get("msg") or payload.get("message") or status), payload=payload
                )
            return payload

    async def fetch_post(self, link_id: int, *, page: int = 1) -> dict[str, Any]:
        return await self.request_json(
            "GET",
            "/bbs/app/link/tree",
            params={
                "h_src": "",
                "link_id": int(link_id),
                "page": max(1, int(page)),
                "is_first": "1" if int(page) == 1 else "0",
                "index": 1,
                "limit": 20,
                "owner_only": 0,
            },
        )

    async def fetch_mentions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for message_type in (16, 17):
            payload = await self.request_json(
                "GET",
                "/bbs/app/user/message",
                params={"message_type": message_type, "offset": 0, "limit": limit, "no_more": "false"},
            )
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            for raw in result.get("messages") or []:
                if isinstance(raw, dict):
                    messages.append(normalize_mention(raw, message_type=message_type))
        return messages

    async def create_comment(
        self,
        *,
        link_id: int,
        text: str,
        reply_id: int = -1,
        root_id: int = -1,
        image_url: str = "",
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            "/bbs/app/comment/create",
            data={
                "is_cy": "0",
                "link_id": str(int(link_id)),
                "reply_id": str(int(reply_id)),
                "root_id": str(int(root_id)),
                "text": text,
                "imgs": image_url,
            },
        )


def normalize_mention(raw: dict[str, Any], *, message_type: int = 0) -> dict[str, Any]:
    user = raw.get("user_a") if isinstance(raw.get("user_a"), dict) else {}
    link = raw.get("link") if isinstance(raw.get("link"), dict) else {}
    def number(*values: Any) -> int:
        for value in values:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed:
                return parsed
        return 0

    actual_type = number(raw.get("message_type"), message_type)
    is_post = actual_type == 16
    return {
        "message_id": number(raw.get("message_id"), raw.get("msg_id")),
        "comment_id": -1 if is_post else number(raw.get("comment_a_id")),
        "root_comment_id": -1 if is_post else number(raw.get("root_comment_id")),
        "link_id": number(raw.get("linkid"), link.get("linkid")),
        "user_id": number(raw.get("userid_a"), user.get("userid")),
        "user_name": str(user.get("username") or raw.get("username") or ""),
        "text": str((link.get("description") or "") if is_post else (raw.get("comment_a_text") or "")),
        "message_type": actual_type,
        "is_post": is_post,
        "raw": raw,
    }
