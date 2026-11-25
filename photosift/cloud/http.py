"""Shared POST with retries for the REST providers."""

from __future__ import annotations

import time

import httpx

from . import CloudError


def post_json(url: str, body: dict, headers: dict, label: str, timeout: float = 300.0) -> dict:
    last = ""
    for attempt in range(4):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=httpx.Timeout(timeout, connect=15.0))
        except httpx.HTTPError as e:
            last = type(e).__name__
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r.json()
        try:
            err = r.json().get("error", {})
            detail = err.get("message", "") if isinstance(err, dict) else str(err)
        except Exception:
            detail = r.text[:200]
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            wait = float(r.headers.get("retry-after") or 0) or 3.0 * (attempt + 1)
            time.sleep(min(wait, 30.0))
            last = f"{r.status_code} {detail}"
            continue
        if r.status_code in (401, 403):
            raise CloudError(f"{label} API key 無效或沒有權限，請到設定確認。")
        if r.status_code == 404:
            raise CloudError(f"找不到 {label} 模型，請到設定確認模型名稱。")
        if r.status_code == 429:
            raise CloudError(f"{label} 回報請求過於頻繁或額度不足，請稍後再試。")
        raise CloudError(f"{label} 回應錯誤（{r.status_code}）：{detail[:200]}")
    raise CloudError(f"連不上 {label}（{last}），請檢查網路後再試。")
