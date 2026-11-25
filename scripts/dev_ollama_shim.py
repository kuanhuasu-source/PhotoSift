"""Development stand-in for Ollama, for machines that cannot run it.

It answers the three Ollama endpoints PhotoSift uses (/api/tags, /api/show,
/api/chat) and forwards chats to Gemma 4 hosted on the Gemini API. Gemma 4 is
the same open-weights family the app recommends running locally, so prompts
and JSON schemas get exercised by a real model of that class.

This is a test tool. With it, photos DO leave the machine. Never point the
real app at it for private photos; on your own computer, use Ollama itself.

    GEMINI_API_KEY=... uv run python scripts/dev_ollama_shim.py --port 11434
"""

from __future__ import annotations

import argparse
import os
import time

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

MODELS = {"gemma4:31b": "gemma-4-31b-it", "gemma4:26b": "gemma-4-26b-a4b-it"}
BASE = "https://generativelanguage.googleapis.com/v1beta"

app = FastAPI()
stats = {"calls": 0, "seconds": 0.0}


@app.get("/api/tags")
def tags():
    return {"models": [{"name": k, "model": k, "size": 20_000_000_000} for k in MODELS]}


@app.post("/api/show")
async def show(req: Request):
    body = await req.json()
    if body.get("model") not in MODELS:
        raise HTTPException(404, "model not found")
    return {"capabilities": ["completion", "vision"]}


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    model = MODELS.get(body.get("model"))
    if model is None:
        raise HTTPException(404, f"model '{body.get('model')}' not found")
    system, parts = "", []
    for m in body.get("messages", []):
        if m["role"] == "system":
            system += m["content"]
        else:
            parts.append({"text": m["content"]})
            for b64 in m.get("images") or []:
                parts.append({"inlineData": {"mimeType": "image/jpeg", "data": b64}})
    opts = body.get("options") or {}
    gen = {"temperature": opts.get("temperature", 0.2), "maxOutputTokens": max(opts.get("num_predict", 800), 256) + 2048}
    if isinstance(body.get("format"), dict):
        gen["responseMimeType"] = "application/json"
        gen["responseJsonSchema"] = body["format"]
    payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    t0 = time.time()
    last = None
    async with httpx.AsyncClient(timeout=180) as http:
        for attempt in range(4):
            r = await http.post(
                f"{BASE}/models/{model}:generateContent",
                json=payload,
                headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")},
            )
            if r.status_code == 200:
                break
            last = r.text[:300]
            if r.status_code not in (429, 500, 503):
                raise HTTPException(r.status_code, last)
            time.sleep(3 * (attempt + 1))
        else:
            raise HTTPException(503, last or "upstream unavailable")
    d = r.json()
    cand = (d.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
    stats["calls"] += 1
    stats["seconds"] += time.time() - t0
    return {"model": body["model"], "message": {"role": "assistant", "content": text}, "done": True}


@app.get("/shim/stats")
def get_stats():
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11434)
    args = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
