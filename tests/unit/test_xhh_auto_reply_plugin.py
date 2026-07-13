from __future__ import annotations

import logging
import re

import httpx
import pytest

from plugin.plugins.xhh_auto_reply import ai_service as xhh_ai_module
from plugin.plugins.xhh_auto_reply.ai_service import XHHAIService, normalize_generated_comment
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


@pytest.mark.asyncio
async def test_ai_generation_uses_qq_style_omni_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class FakeConfigManager:
        def get_model_api_config(self, model_type: str) -> dict[str, str]:
            assert model_type == "conversation"
            return {
                "base_url": "https://example.invalid/v1",
                "api_key": "secret",
                "model": "test-model",
                "provider_type": "openai_compatible",
            }

        def get_character_data(self) -> tuple[object, ...]:
            return (
                "主人",
                "兰兰",
                None,
                {},
                None,
                {"兰兰": "你是 {LANLAN_NAME}，正在帮助 {MASTER_NAME}。"},
                None,
                None,
                None,
            )

    class FakeOmniOfflineClient:
        _is_responding = False

        def __init__(self, **kwargs: object):
            calls["init"] = kwargs
            self.on_text_delta = kwargs["on_text_delta"]
            self.on_response_done = kwargs["on_response_done"]

        async def connect(self, instructions: str) -> None:
            calls["instructions"] = instructions

        async def stream_text(self, text: str) -> None:
            calls["prompt"] = text
            await self.on_text_delta("评论：流式", True)
            await self.on_text_delta("回复", False)
            await self.on_response_done()

        async def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(xhh_ai_module, "get_config_manager", lambda: FakeConfigManager())
    monkeypatch.setattr(xhh_ai_module, "OmniOfflineClient", FakeOmniOfflineClient)

    service = XHHAIService(
        {"reply_prompt": "小黑盒回复提示", "max_reply_chars": 30},
        logging.getLogger(__name__),
    )
    result = await service.generate_comment(
        user_text="请生成评论",
        post_payload={"result": {"title": "测试帖子"}},
    )

    assert result == "流式回复"
    instructions = str(calls["instructions"])
    assert "兰兰" in instructions
    assert "主人" in instructions
    assert "小黑盒回复提示" in instructions
    assert "小黑盒社区环境" in instructions
    assert "测试帖子" in str(calls["prompt"])
    assert calls["closed"] is True
    init = calls["init"]
    assert isinstance(init, dict)
    assert init["model"] == "test-model"
    assert init["max_response_length"] == 30
