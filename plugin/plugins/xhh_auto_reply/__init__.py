from __future__ import annotations

import asyncio
import time
from http.cookies import SimpleCookie
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    ui,
)

from .ai_service import XHHAIService
from .client import XHHAPIError, XHHClient
from .config_store import XHHConfigStore, XHHStateStore
from .signing import ensure_xhh_token_cookie


@neko_plugin
class XHHAutoReplyPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self.config_store = XHHConfigStore(self.data_path())
        self.state_store = XHHStateStore(self.data_path())
        self._settings: dict[str, Any] = self.config_store.default_config()
        self._state: dict[str, Any] = {
            "processed_message_ids": [],
            "recent_events": [],
            "mention_baseline_initialized": False,
        }
        self._client: XHHClient | None = None
        self._ai: XHHAIService | None = None
        self._auto_reply_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()
        self._last_error = ""

    @lifecycle(id="startup")
    async def startup(self, **_):
        self._settings = await self.config_store.load()
        # v0.1.0 generated a random device_id even though OpenXHH defaults to
        # an empty value. Clear that generated value once; an explicitly
        # configured device ID is preserved by the marker below.
        if self._settings.get("device_id") and not bool(
            self._settings.get("device_id_user_configured")
        ):
            self._settings["device_id"] = ""
        if not await self.config_store.exists():
            self._settings = await self.config_store.save(self._settings)
        elif not self._settings.get("device_id"):
            self._settings = await self.config_store.save(self._settings)
        self._state = await self.state_store.load()
        await self._rebuild_services()
        self.register_static_ui("static")
        self.set_list_actions(
            [
                {
                    "id": "open_ui",
                    "label": "打开小黑盒面板",
                    "kind": "ui",
                    "target": f"/plugin/{self.plugin_id}/ui/",
                    "open_in": "new_tab",
                }
            ]
        )
        if bool(self._settings.get("auto_reply_enabled")):
            self._start_loop()
        return Ok({"status": "ready", "auto_reply_running": self._is_running()})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        await self._stop_loop()
        if self._client is not None:
            await self._client.close()
            self._client = None
        return Ok({"status": "shutdown"})

    async def _rebuild_services(self) -> None:
        old_client = self._client
        self._client = XHHClient(self._settings, self.logger)
        self._ai = XHHAIService(self._settings, self.logger)
        if old_client is not None:
            await old_client.close()

    def _is_running(self) -> bool:
        return self._auto_reply_task is not None and not self._auto_reply_task.done()

    def _start_loop(self) -> None:
        if not self._is_running():
            self._auto_reply_task = asyncio.create_task(
                self._auto_reply_loop(), name="xhh-auto-reply"
            )

    async def _stop_loop(self) -> None:
        task = self._auto_reply_task
        self._auto_reply_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _auto_reply_loop(self) -> None:
        while True:
            try:
                await self._poll_mentions_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self.logger.warning(f"小黑盒自动回帖轮询失败: {exc}")
            delay = max(15, int(self._settings.get("poll_interval_seconds", 60) or 60))
            await asyncio.sleep(delay)

    def _require_client(self) -> XHHClient:
        if self._client is None:
            raise RuntimeError("小黑盒客户端尚未初始化")
        if not str(self._settings.get("cookie") or "").strip():
            raise RuntimeError("尚未导入小黑盒 Cookie")
        return self._client

    def _require_ai(self) -> XHHAIService:
        if self._ai is None:
            raise RuntimeError("AI 服务尚未初始化")
        return self._ai

    def _dashboard(self) -> dict[str, Any]:
        cookie = str(self._settings.get("cookie") or "")
        safe_settings = dict(self._settings)
        safe_settings["cookie"] = self._mask_secret(cookie)
        events = list(self._state.get("recent_events") or [])[-50:]
        return {
            "configured": bool(cookie),
            "heybox_id": str(self._settings.get("heybox_id") or ""),
            "auto_reply_running": self._is_running(),
            "settings": safe_settings,
            "processed_count": len(self._state.get("processed_message_ids") or []),
            "recent_events": events,
            "last_error": self._last_error,
            "capabilities": {
                "top_level_comment": True,
                "nested_reply": True,
                "ai_generation": True,
                "mention_polling": True,
                "create_new_topic": False,
            },
        }

    @staticmethod
    def _mask_secret(secret: str) -> str:
        if not secret:
            return ""
        if len(secret) <= 10:
            return "*" * len(secret)
        return f"{secret[:5]}***{secret[-5:]}"

    async def _record_event(self, event: dict[str, Any]) -> None:
        events = list(self._state.get("recent_events") or [])
        events.append({"time": int(time.time()), **event})
        self._state["recent_events"] = events[-100:]
        await self.state_store.save(self._state)

    @ui.context(id="xhh_auto_reply")
    async def get_ui_context(self) -> dict[str, Any]:
        return self._dashboard()

    @plugin_entry(
        id="get_dashboard_state",
        name="获取小黑盒插件状态",
        description="读取登录配置、自动回帖状态、最近事件和功能边界。",
        input_schema={"type": "object", "properties": {}},
    )
    async def get_dashboard_state(self, **_):
        return Ok(self._dashboard())

    @plugin_entry(
        id="import_cookie",
        name="导入小黑盒 Cookie",
        description="保存从已登录小黑盒网页复制的 Cookie。Cookie 只写入插件私有数据目录。",
        input_schema={
            "type": "object",
            "properties": {
                "cookie": {"type": "string"},
                "heybox_id": {"type": "string"},
            },
            "required": ["cookie"],
        },
    )
    async def import_cookie(self, cookie: str, heybox_id: str = "", **_):
        normalized = str(cookie or "").strip().replace("\r", "").replace("\n", "")
        if "=" not in normalized:
            return Err(SdkError("Cookie 格式无效"))
        detected_id = str(heybox_id or "").strip() or self._cookie_value(
            normalized, "user_heybox_id"
        )
        self._settings["cookie"] = ensure_xhh_token_cookie(normalized)
        self._settings["heybox_id"] = detected_id
        await self.config_store.save(self._settings)
        await self._rebuild_services()
        await self._record_event({"kind": "auth", "status": "cookie_imported"})
        return Ok(self._dashboard())

    @staticmethod
    def _cookie_value(cookie_header: str, name: str) -> str:
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
            morsel = jar.get(name)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    @plugin_entry(
        id="clear_cookie",
        name="清除小黑盒登录态",
        description="删除插件保存的小黑盒 Cookie，并停止自动回帖。",
        input_schema={"type": "object", "properties": {}},
    )
    async def clear_cookie(self, **_):
        await self._stop_loop()
        self._settings["cookie"] = ""
        self._settings["heybox_id"] = ""
        self._settings["auto_reply_enabled"] = False
        await self.config_store.save(self._settings)
        await self._rebuild_services()
        return Ok(self._dashboard())

    @plugin_entry(
        id="update_config",
        name="更新小黑盒插件配置",
        description="更新 dry-run、轮询间隔、限频、白名单和 AI 回帖提示词。",
        input_schema={
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean"},
                "poll_interval_seconds": {"type": "integer"},
                "min_request_interval_seconds": {"type": "number"},
                "max_reply_chars": {"type": "integer"},
                "allowed_user_ids": {"type": "array", "items": {"type": "integer"}},
                "reply_prompt": {"type": "string"},
                "base_url": {"type": "string"},
                "device_id": {"type": "string"},
            },
        },
    )
    async def update_config(self, **kwargs):
        allowed = {
            "dry_run",
            "poll_interval_seconds",
            "min_request_interval_seconds",
            "max_reply_chars",
            "allowed_user_ids",
            "reply_prompt",
            "base_url",
            "device_id",
        }
        updates = {key: value for key, value in kwargs.items() if key in allowed}
        if "poll_interval_seconds" in updates:
            updates["poll_interval_seconds"] = max(15, int(updates["poll_interval_seconds"]))
        if "min_request_interval_seconds" in updates:
            updates["min_request_interval_seconds"] = max(
                0.2, float(updates["min_request_interval_seconds"])
            )
        if "max_reply_chars" in updates:
            updates["max_reply_chars"] = max(20, min(1000, int(updates["max_reply_chars"])))
        if "allowed_user_ids" in updates:
            updates["allowed_user_ids"] = sorted(
                {int(value) for value in updates["allowed_user_ids"] if int(value) > 0}
            )
        if "base_url" in updates:
            base_url = str(updates["base_url"] or "").strip().rstrip("/")
            if not base_url.startswith("https://"):
                return Err(SdkError("base_url 必须使用 https://"))
            updates["base_url"] = base_url
        if "device_id" in updates:
            updates["device_id"] = str(updates["device_id"] or "").strip()
            updates["device_id_user_configured"] = bool(updates["device_id"])
        self._settings.update(updates)
        await self.config_store.save(self._settings)
        await self._rebuild_services()
        return Ok(self._dashboard())

    @plugin_entry(
        id="fetch_post",
        name="读取小黑盒帖子",
        description="读取帖子正文、评论楼层和图片上下文。",
        input_schema={
            "type": "object",
            "properties": {"link_id": {"type": "integer"}, "page": {"type": "integer"}},
            "required": ["link_id"],
        },
    )
    async def fetch_post(self, link_id: int, page: int = 1, **_):
        try:
            payload = await self._require_client().fetch_post(int(link_id), page=int(page))
            return Ok(payload)
        except Exception as exc:
            return self._entry_error(exc)

    @plugin_entry(
        id="publish_post_comment",
        name="在小黑盒帖子下发表评论",
        description="向指定帖子发布顶级评论。此操作会真实修改外部平台状态。",
        input_schema={
            "type": "object",
            "properties": {
                "link_id": {"type": "integer"},
                "text": {"type": "string"},
                "image_url": {"type": "string"},
            },
            "required": ["link_id", "text"],
        },
    )
    async def publish_post_comment(self, link_id: int, text: str, image_url: str = "", **_):
        return await self._publish_comment(
            link_id=int(link_id), text=text, reply_id=-1, root_id=-1, image_url=image_url
        )

    @plugin_entry(
        id="reply_comment",
        name="回复小黑盒评论",
        description="在指定帖子中发布楼中楼回复。此操作会真实修改外部平台状态。",
        input_schema={
            "type": "object",
            "properties": {
                "link_id": {"type": "integer"},
                "comment_id": {"type": "integer"},
                "root_id": {"type": "integer"},
                "text": {"type": "string"},
                "image_url": {"type": "string"},
            },
            "required": ["link_id", "comment_id", "text"],
        },
    )
    async def reply_comment(
        self,
        link_id: int,
        comment_id: int,
        text: str,
        root_id: int = -1,
        image_url: str = "",
        **_,
    ):
        effective_root = int(root_id) if int(root_id) > 0 else int(comment_id)
        return await self._publish_comment(
            link_id=int(link_id),
            text=text,
            reply_id=int(comment_id),
            root_id=effective_root,
            image_url=image_url,
        )

    async def _publish_comment(
        self,
        *,
        link_id: int,
        text: str,
        reply_id: int,
        root_id: int,
        image_url: str = "",
    ):
        normalized = str(text or "").strip()
        if not normalized:
            return Err(SdkError("评论内容不能为空"))
        try:
            payload = await self._require_client().create_comment(
                link_id=link_id,
                text=normalized,
                reply_id=reply_id,
                root_id=root_id,
                image_url=str(image_url or "").strip(),
            )
            await self._record_event(
                {
                    "kind": "comment_sent",
                    "link_id": link_id,
                    "reply_id": reply_id,
                    "text": normalized[:200],
                }
            )
            return Ok({"sent": True, "text": normalized, "response": payload})
        except Exception as exc:
            return self._entry_error(exc)

    @plugin_entry(
        id="generate_post_comment",
        name="AI 生成帖子评论",
        description="读取帖子上下文并使用 N.E.K.O conversation 模型生成评论；publish=true 时发布。",
        input_schema={
            "type": "object",
            "properties": {
                "link_id": {"type": "integer"},
                "request": {"type": "string"},
                "instruction": {"type": "string"},
                "publish": {"type": "boolean"},
            },
            "required": ["link_id", "request"],
        },
    )
    async def generate_post_comment(
        self, link_id: int, request: str, instruction: str = "", publish: bool = False, **_
    ):
        try:
            post = await self._require_client().fetch_post(int(link_id))
            text = await self._require_ai().generate_comment(
                user_text=str(request), post_payload=post, instruction=str(instruction or "")
            )
            if not publish:
                return Ok({"generated": True, "sent": False, "text": text})
            return await self._publish_comment(
                link_id=int(link_id), text=text, reply_id=-1, root_id=-1
            )
        except Exception as exc:
            return self._entry_error(exc)

    @plugin_entry(
        id="run_poll_once",
        name="立即检查一次小黑盒 @",
        description="立即轮询帖子和评论中的 @。默认首次只建立历史基线；process_existing=true 才处理已有消息。",
        input_schema={
            "type": "object",
            "properties": {"process_existing": {"type": "boolean"}},
        },
    )
    async def run_poll_once(self, process_existing: bool = False, **_):
        try:
            return Ok(await self._poll_mentions_once(process_existing=bool(process_existing)))
        except Exception as exc:
            return self._entry_error(exc)

    @plugin_entry(
        id="start_auto_reply",
        name="启动小黑盒自动回帖",
        description="启动后台 @ 轮询。建议先保持 dry-run=true 验证生成内容。",
        input_schema={"type": "object", "properties": {}},
    )
    async def start_auto_reply(self, **_):
        try:
            self._require_client()
            self._settings["auto_reply_enabled"] = True
            await self.config_store.save(self._settings)
            self._start_loop()
            return Ok(self._dashboard())
        except Exception as exc:
            return self._entry_error(exc)

    @plugin_entry(
        id="stop_auto_reply",
        name="停止小黑盒自动回帖",
        description="停止后台 @ 轮询。",
        input_schema={"type": "object", "properties": {}},
    )
    async def stop_auto_reply(self, **_):
        await self._stop_loop()
        self._settings["auto_reply_enabled"] = False
        await self.config_store.save(self._settings)
        return Ok(self._dashboard())

    @plugin_entry(
        id="reset_processed_messages",
        name="重置已处理 @ 记录",
        description="清空消息去重记录；下一轮可能重新处理仍在接口列表中的旧消息。",
        input_schema={"type": "object", "properties": {}},
    )
    async def reset_processed_messages(self, **_):
        self._state["processed_message_ids"] = []
        self._state["mention_baseline_initialized"] = False
        await self.state_store.save(self._state)
        return Ok({"reset": True})

    async def _poll_mentions_once(self, *, process_existing: bool = False) -> dict[str, Any]:
        async with self._poll_lock:
            client = self._require_client()
            mentions = await client.fetch_mentions()
            processed = {int(value) for value in self._state.get("processed_message_ids") or []}
            if not bool(self._state.get("mention_baseline_initialized")):
                self._state["mention_baseline_initialized"] = True
                if not process_existing:
                    processed.update(
                        int(item.get("message_id") or 0)
                        for item in mentions
                        if int(item.get("message_id") or 0) > 0
                    )
                    self._state["processed_message_ids"] = sorted(processed)[-1000:]
                    await self.state_store.save(self._state)
                    self.logger.info(
                        f"小黑盒监听基线已建立: checked={len(mentions)} "
                        f"baselined={len(mentions)}"
                    )
                    return {
                        "checked": len(mentions),
                        "handled": 0,
                        "baselined": len(mentions),
                        "results": [],
                    }
            allowed = {int(value) for value in self._settings.get("allowed_user_ids") or []}
            results: list[dict[str, Any]] = []
            for mention in reversed(mentions):
                message_id = int(mention.get("message_id") or 0)
                if not message_id or message_id in processed:
                    continue
                user_id = int(mention.get("user_id") or 0)
                message_type = int(mention.get("message_type") or 0)
                link_id = int(mention.get("link_id") or 0)
                user_name = " ".join(str(mention.get("user_name") or "").split())[:10]
                message_text = " ".join(str(mention.get("text") or "").split())[:50]
                self.logger.info(
                    "小黑盒监听到新消息: "
                    f"message_id={message_id} message_type={message_type} "
                    f"link_id={link_id} user_id={user_id} "
                    f"user_name={user_name!r} text={message_text!r}"
                )
                if allowed and user_id not in allowed:
                    processed.add(message_id)
                    results.append({"message_id": message_id, "status": "filtered"})
                    continue
                if not link_id:
                    processed.add(message_id)
                    results.append({"message_id": message_id, "status": "missing_link_id"})
                    continue
                post = await client.fetch_post(link_id)
                reply_text = await self._require_ai().generate_comment(
                    user_text=str(mention.get("text") or ""), post_payload=post
                )
                dry_run = bool(self._settings.get("dry_run", True))
                if not dry_run:
                    comment_id = int(mention.get("comment_id") or -1)
                    root_id = int(mention.get("root_comment_id") or -1)
                    if mention.get("is_post"):
                        comment_id = root_id = -1
                    elif root_id <= 0:
                        root_id = comment_id
                    await client.create_comment(
                        link_id=link_id,
                        text=reply_text,
                        reply_id=comment_id,
                        root_id=root_id,
                    )
                processed.add(message_id)
                event = {
                    "kind": "auto_reply",
                    "message_id": message_id,
                    "link_id": link_id,
                    "user_id": user_id,
                    "status": "dry_run" if dry_run else "sent",
                    "text": reply_text[:200],
                }
                results.append(event)
                await self._record_event(event)

            self._state["processed_message_ids"] = sorted(processed)[-1000:]
            await self.state_store.save(self._state)
            return {"checked": len(mentions), "handled": len(results), "results": results}

    def _entry_error(self, exc: Exception):
        self._last_error = str(exc)
        if isinstance(exc, XHHAPIError):
            detail = {"status_code": exc.status_code, "payload": exc.payload}
            self.logger.warning(f"小黑盒 API 错误: {exc}; detail={detail}")
        else:
            self.logger.warning(f"小黑盒插件操作失败: {exc}")
        return Err(SdkError(str(exc)))


__all__ = ["XHHAutoReplyPlugin"]
