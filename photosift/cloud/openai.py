"""OpenAI (GPT) through the Chat Completions REST API."""

from __future__ import annotations

import json
import time

import httpx

from . import CloudError, CloudResult, Img, Part, Provider
from .http import post_json


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str, api_key: str | None, base_url: str):
        self.model = model
        self.key = api_key
        self.base = base_url.rstrip("/")

    def _headers(self) -> dict:
        if not self.key:
            raise CloudError("尚未設定 OpenAI API key，請到設定輸入。")
        return {"Authorization": f"Bearer {self.key}"}

    def generate(self, *, system: str, parts: list[Part], schema: dict, max_tokens: int = 16000) -> CloudResult:
        if not self.model:
            raise CloudError("尚未選擇 GPT 模型，請到設定按「列出可用模型」後選一個。")
        content = []
        for p in parts:
            if isinstance(p, Img):
                url = "data:image/jpeg;base64," + p.b64
                content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})
            else:
                content.append({"type": "text", "text": p})
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "result", "strict": True, "schema": schema}},
            "max_completion_tokens": max_tokens,
        }
        t0 = time.time()
        d = post_json(f"{self.base}/chat/completions", body, self._headers(), "GPT")
        choice = (d.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        if msg.get("refusal"):
            raise CloudError("GPT 拒絕處理這批照片。可以換一批照片或改用其他外部模型。")
        if choice.get("finish_reason") == "length":
            raise CloudError("GPT 的回覆被截斷，請減少一次送出的照片數量。")
        try:
            data = json.loads(msg.get("content") or "")
        except json.JSONDecodeError as e:
            raise CloudError("GPT 沒有回傳有效的 JSON。") from e
        u = d.get("usage") or {}
        return CloudResult(
            data=data,
            input_tokens=u.get("prompt_tokens", 0),
            output_tokens=u.get("completion_tokens", 0),
            model=d.get("model", self.model),
            seconds=time.time() - t0,
            images=sum(isinstance(p, Img) for p in parts),
        )

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(f"{self.base}/models", headers=self._headers(), timeout=20)
        except httpx.HTTPError as e:
            raise CloudError("連不上 OpenAI，請檢查網路。") from e
        if r.status_code == 401:
            raise CloudError("OpenAI API key 無效，請到設定重新輸入。")
        if r.status_code != 200:
            raise CloudError(f"無法取得 GPT 模型清單（{r.status_code}）。")
        ids = [m["id"] for m in r.json().get("data", [])]
        skip = ("embedding", "whisper", "tts", "dall-e", "image", "audio", "realtime", "moderation", "transcribe", "sora")
        return sorted(i for i in ids if not any(s in i for s in skip))
