"""Claude through the official Anthropic SDK."""

from __future__ import annotations

import json
import time

import anthropic

from . import CloudError, CloudResult, Img, Part, Provider

# Models that accept the server-side refusal fallback. Others (for example Haiku) reject the parameter.
FALLBACK_PREFIXES = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5", "claude-mythos-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider(Provider):
    name = "claude"

    def __init__(self, model: str, api_key: str | None):
        self.model = model or "claude-opus-5-5"
        # Without a stored key the SDK still resolves ANTHROPIC_API_KEY or an `ant auth login` profile.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def generate(self, *, system: str, parts: list[Part], schema: dict, max_tokens: int = 16000) -> CloudResult:
        content = []
        for p in parts:
            if isinstance(p, Img):
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": p.b64}})
            else:
                content.append({"type": "text", "text": p})
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        t0 = time.time()
        try:
            if self.model.startswith(FALLBACK_PREFIXES):
                # If a safety classifier declines, the API retries on its recommended fallback model.
                resp = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise CloudError("Claude API key 無效，請到設定重新輸入。") from e
        except anthropic.PermissionDeniedError as e:
            raise CloudError("這把 Claude API key 沒有使用此模型的權限。") from e
        except anthropic.NotFoundError as e:
            raise CloudError(f"找不到 Claude 模型「{self.model}」，請到設定確認模型名稱。") from e
        except anthropic.RateLimitError as e:
            raise CloudError("Claude 回報請求過於頻繁，請稍後再試。") from e
        except anthropic.BadRequestError as e:
            raise CloudError(f"Claude 拒絕了這個請求：{e.message}") from e
        except anthropic.APIStatusError as e:
            raise CloudError(f"Claude 服務錯誤（{e.status_code}），請稍後再試。") from e
        except anthropic.APIConnectionError as e:
            raise CloudError("連不上 Claude，請檢查網路。") from e

        if resp.stop_reason == "refusal":
            raise CloudError("Claude 拒絕處理這批照片（安全政策）。可以換一批照片或改用其他外部模型。")
        if resp.stop_reason == "max_tokens":
            raise CloudError("Claude 的回覆被截斷，請減少一次送出的照片數量。")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CloudError("Claude 沒有回傳有效的 JSON。") from e
        u = resp.usage
        tokens_in = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
        tokens_in += getattr(u, "cache_creation_input_tokens", 0) or 0
        return CloudResult(
            data=data,
            input_tokens=tokens_in,
            output_tokens=u.output_tokens or 0,
            model=resp.model,
            seconds=time.time() - t0,
            images=sum(isinstance(p, Img) for p in parts),
        )

    def list_models(self) -> list[str]:
        try:
            return [m.id for m in self.client.models.list()]
        except anthropic.AuthenticationError as e:
            raise CloudError("Claude API key 無效，請到設定重新輸入。") from e
        except anthropic.APIError as e:
            raise CloudError(f"無法取得 Claude 模型清單：{type(e).__name__}") from e
