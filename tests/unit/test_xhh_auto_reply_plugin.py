from __future__ import annotations

import logging
import re

import httpx
import pytest

from plugin.plugins.xhh_auto_reply.ai_service import normalize_generated_comment
from plugin.plugins.xhh_auto_reply.client import WEB_USER_AGENT, XHHClient, normalize_mention
from plugin.plugins.xhh_auto_reply.config_store import XHHConfigStore
from plugin.plugins.xhh_auto_reply.signing import (
    build_request_keys,
    build_xhh_token_id,
    ensure_xhh_token_cookie,
)


def test_request_signing_is_deterministic_with_fixed_inputs() -> None:
    first = build_request_keys(
        "/bbs/app/comment/create",
        timestamp=1_700_000_000,
        nonce="0123456789ABCDEF0123456789ABCDEF",
    )
    second = build_request_keys(
        "/bbs/app/comment/create",
        timestamp=1_700_000_000,
        nonce="0123456789ABCDEF0123456789ABCDEF",
    )

    assert first == second
    assert first[0] == "YT27P47"
    assert first[1] == "0123456789ABCDEF0123456789ABCDEF"
    assert first[2] == 1_700_000_000
    assert re.fullmatch(r"[A-Z0-9]{5}\d{2}", first[0])


def test_request_signing_regression_for_user_message_log() -> None:
    hkey, nonce, request_time = build_request_keys(
        "/bbs/app/user/message",
        timestamp=1_783_922_133,
        nonce="C2D70C48DE90C025ADCCD6D755076E90",
    )

    assert (hkey, nonce, request_time) == (
        "2ZVUS61",
        "C2D70C48DE90C025ADCCD6D755076E90",
        1_783_922_133,
    )


def test_xhh_token_cookie_is_added_once() -> None:
    token = build_xhh_token_id(timestamp=1_700_000_000)
    assert token == "JJIN7Pg/aWC4D3N6KO6vwWQtpGeo85pa12zIObKvyTrPUfGe8Sz8T4KDVn+jqSREvfbiRNT6c/W5eBXG8riADQA="

    cookie = ensure_xhh_token_cookie("user_heybox_id=123", timestamp=1_700_000_000)
    assert cookie == f"user_heybox_id=123;x_xhh_tokenid={token}"
    assert ensure_xhh_token_cookie(cookie, timestamp=1_800_000_000) == cookie


def test_default_device_id_matches_openxhh() -> None:
    config = XHHConfigStore.default_config()
    assert config["device_id"] == ""
    assert config["device_id_user_configured"] is False


@pytest.mark.asyncio
async def test_request_matches_openxhh_web_fingerprint_without_device_id() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"status": "ok", "result": {}})

    settings = XHHConfigStore.default_config()
    settings.update({"cookie": "user_heybox_id=123", "heybox_id": "123"})
    client = XHHClient(settings, logging.getLogger(__name__))
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client.request_json("GET", "/bbs/app/user/message")
    finally:
        await client.close()

    request = captured[0]
    assert "device_id" not in request.url.params
    assert request.headers["user-agent"] == WEB_USER_AGENT
    assert request.headers["referer"] == "https://www.xiaoheihe.cn/"
    assert "x_xhh_tokenid=" in request.headers["cookie"]


def test_normalize_comment_mention() -> None:
    mention = normalize_mention(
        {
            "message_id": 10,
            "comment_a_id": 20,
            "root_comment_id": 19,
            "linkid": 30,
            "userid_a": "40",
            "comment_a_text": "@机器人 你好",
            "user_a": {"username": "测试用户"},
        },
        message_type=17,
    )

    assert mention == {
        "message_id": 10,
        "comment_id": 20,
        "root_comment_id": 19,
        "link_id": 30,
        "user_id": 40,
        "user_name": "测试用户",
        "text": "@机器人 你好",
        "message_type": 17,
        "is_post": False,
        "raw": {
            "message_id": 10,
            "comment_a_id": 20,
            "root_comment_id": 19,
            "linkid": 30,
            "userid_a": "40",
            "comment_a_text": "@机器人 你好",
            "user_a": {"username": "测试用户"},
        },
    }


def test_normalize_post_mention_uses_link_description() -> None:
    mention = normalize_mention(
        {
            "message_id": 11,
            "linkid": 31,
            "userid_a": 41,
            "link": {"linkid": 31, "description": "帖子里提到了机器人"},
        },
        message_type=16,
    )

    assert mention["is_post"] is True
    assert mention["comment_id"] == -1
    assert mention["root_comment_id"] == -1
    assert mention["text"] == "帖子里提到了机器人"


def test_normalize_generated_comment_removes_wrappers_and_limits_length() -> None:
    assert normalize_generated_comment("```text\n评论：你好呀\n```", max_chars=10) == "你好呀"
    assert normalize_generated_comment("abcdefghijkl", max_chars=5) == "abcde"
