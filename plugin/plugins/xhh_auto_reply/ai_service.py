from __future__ import annotations

import json
import re
from typing import Any

from utils.config_manager import get_config_manager
from utils.llm_client import create_chat_llm_async
from utils.token_tracker import set_call_type


class XHHAIService:
    def __init__(self, settings: dict[str, Any], logger: Any):
        self.settings = settings
        self.logger = logger

    async def generate_comment(
        self,
        *,
        user_text: str,
        post_payload: dict[str, Any] | None,
        instruction: str = "",
    ) -> str:
        model_config = get_config_manager().get_model_api_config("conversation")
        base_url = str(model_config.get("base_url") or "").strip()
        model = str(model_config.get("model") or "").strip()
        api_key = str(model_config.get("api_key") or "").strip()
        if not base_url or not model:
            raise RuntimeError("N.E.K.O conversation 模型尚未配置")

        max_chars = max(20, min(1000, int(self.settings.get("max_reply_chars", 300) or 300)))
        system_prompt = str(instruction or self.settings.get("reply_prompt") or "").strip()
        context = compact_post_context(post_payload or {})
        prompt = (
            f"帖子与评论上下文：\n{context}\n\n"
            f"当前用户内容：\n{user_text.strip()}\n\n"
            f"请直接输出准备发布的评论正文，最多 {max_chars} 个字符。"
        )
        llm = await create_chat_llm_async(
            model=model,
            base_url=base_url,
            api_key=api_key,
            max_completion_tokens=400,
            timeout=60.0,
            provider_type=model_config.get("provider_type"),
        )
        try:
            set_call_type("conversation")
            response = await llm.ainvoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
            )
            content = str(getattr(response, "content", "") or "")
            normalized = normalize_generated_comment(content, max_chars=max_chars)
            if not normalized:
                raise RuntimeError("AI 未生成可发布的评论")
            return normalized
        finally:
            aclose = getattr(llm, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass


def compact_post_context(payload: dict[str, Any], *, max_chars: int = 8000) -> str:
    if not payload:
        return "（未提供帖子上下文）"
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > max_chars:
        serialized = serialized[:max_chars] + "…"
    return serialized


def normalize_generated_comment(content: str, *, max_chars: int = 300) -> str:
    text = str(content or "").strip()
    text = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:回复|评论|答案)\s*[：:]\s*", "", text)
    text = text.strip().strip('"“”')
    return text[:max_chars].rstrip()
