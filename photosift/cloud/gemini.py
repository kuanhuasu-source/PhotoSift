"""Google Gemini through the generateContent REST API."""

from __future__ import annotations

import json
import time

import httpx

from . import CloudError, CloudResult, Img, Part, Provider
from .http import post_json

BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, model: str, api_key: str | None):
        self.model = model or "gemini-flash-latest"
        self.key = api_key

    def _headers(self) -> dict:
        if not self.key:
            raise CloudError("尚未設定 Gemini API key，請到設定輸入。")
        return {"x-goog-api-key": self.key}

    def generate(self, *, system: str, parts: list[Part], schema: dict, max_tokens: int = 16000) -> CloudResult:
        gparts = []
        for p in parts:
            if isinstance(p, Img):
                gparts.append({"inlineData": {"mimeType": "image/jpeg", "data": p.b64}})
            else:
                gparts.append({"text": p})
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": gparts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "maxOutputTokens": max_tokens,
            },
        }
        t0 = time.time()
        d = post_json(f"{BASE}/models/{self.model}:generateContent", body, self._headers(), "Gemini")
        cands = d.get("candidates") or []
        if not cands:
            reason = (d.get("promptFeedback") or {}).get("blockReason", "")
            raise CloudError(f"Gemini 沒有回覆（{reason or '內容被擋下'}）。可以換一批照片或改用其他外部模型。")
        c = cands[0]
        if c.get("finishReason") == "MAX_TOKENS":
            raise CloudError("Gemini 的回覆被截斷，請減少一次送出的照片數量。")
        text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CloudError(f"Gemini 沒有回傳有效的 JSON（{c.get('finishReason')}）。") from e
        u = d.get("usageMetadata") or {}
        return CloudResult(
            data=data,
            input_tokens=u.get("promptTokenCount", 0),
            # Reasoning tokens are billed as output, so they belong in the total.
            output_tokens=u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
            model=d.get("modelVersion", self.model),
            seconds=time.time() - t0,
            images=sum(isinstance(p, Img) for p in parts),
        )

    def list_models(self) -> list[str]:
        try:
            r = httpx.get(f"{BASE}/models", params={"pageSize": 300}, headers=self._headers(), timeout=20)
        except httpx.HTTPError as e:
            raise CloudError("連不上 Gemini，請檢查網路。") from e
        if r.status_code in (400, 401, 403):
            raise CloudError("Gemini API key 無效，請到設定重新輸入。")
        if r.status_code != 200:
            raise CloudError(f"無法取得 Gemini 模型清單（{r.status_code}）。")
        out = []
        skip = ("tts", "image", "audio", "embedding", "robotics", "live", "lyria", "veo", "imagen", "banana",
                "transcribe", "omni", "antigravity", "deep-research", "computer", "customtools")  # fmt: skip
        for m in r.json().get("models", []):
            mid = m["name"].split("/")[-1]
            if "generateContent" in m.get("supportedGenerationMethods", []) and mid.startswith("gemini"):
                if not any(s in mid for s in skip):
                    out.append(mid)
        return sorted(out)
