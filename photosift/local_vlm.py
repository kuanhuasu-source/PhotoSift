"""The on-device vision-language model: captions, tags and rubric scoring at zero token cost.

Speaks Ollama's native API by default, or any OpenAI-compatible local server
(LM Studio, llama.cpp, mlx-vlm) when `local.api` is "openai".
"""

from __future__ import annotations

import base64
import json
import re

import httpx

from . import taxonomy
from .config import LocalSettings

VISION_HINTS = ("vl", "vision", "llava", "gemma3", "gemma4", "minicpm", "moondream", "pixtral", "bakllava", "llama4")

# Suggested Ollama tags by memory: (minimum GB, bulk annotator, contest judge).
# The annotator runs over thousands of photos, so speed wins; the judge sees a few
# hundred candidates, so it gets the largest model that fits comfortably.
RECOMMENDED = [
    (48, "gemma4:12b", "gemma4:31b"),
    (24, "gemma4:e4b", "gemma4:12b"),
    (16, "qwen3-vl:8b", "qwen3-vl:8b"),
    (0, "qwen3-vl:4b", "qwen3-vl:4b"),
]


class LocalModelError(RuntimeError):
    pass


def recommend_models(ram_gb: float) -> dict:
    for need, annotator, judge in RECOMMENDED:
        if ram_gb >= need:
            return {"annotator": annotator, "judge": judge}
    return {"annotator": RECOMMENDED[-1][1], "judge": RECOMMENDED[-1][2]}


def system_ram_gb() -> float:
    try:
        import os

        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except Exception:
        return 0.0


def parse_json(text: str) -> dict:
    """Small models sometimes wrap JSON in fences or add a sentence around it."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a : b + 1])
        raise


class LocalVLM:
    def __init__(self, s: LocalSettings, role: str = "annotator"):
        self.s = s
        self.role = role
        self.base = s.base_url.rstrip("/")
        self.http = httpx.Client(timeout=httpx.Timeout(s.timeout_s, connect=5.0))
        self._model: str | None = None
        self._caps: dict[str, list[str]] = {}

    @property
    def wanted(self) -> str:
        """The configured model for this role. The judge falls back to the annotator."""
        if self.role == "judge" and self.s.judge_model:
            return self.s.judge_model
        return self.s.model

    # ------------------------------------------------------------- discovery

    def list_models(self) -> list[dict]:
        """Installed models as {name, vision, size_gb}."""
        if self.s.api == "openai":
            r = self.http.get(f"{self.base}/v1/models", timeout=5.0)
            r.raise_for_status()
            return [{"name": m["id"], "vision": True, "size_gb": None} for m in r.json().get("data", [])]
        r = self.http.get(f"{self.base}/api/tags", timeout=5.0)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            name = m.get("name") or m.get("model")
            out.append({
                "name": name,
                "vision": "vision" in self._capabilities(name),
                "size_gb": round(m.get("size", 0) / 1e9, 1),
            })  # fmt: skip
        return out

    def _capabilities(self, name: str) -> list[str]:
        if name not in self._caps:
            caps: list[str] = []
            try:
                r = self.http.post(f"{self.base}/api/show", json={"model": name}, timeout=10.0)
                if r.status_code == 200:
                    caps = list(r.json().get("capabilities") or [])
            except httpx.HTTPError:
                pass
            if not caps and any(h in name.lower() for h in VISION_HINTS):
                caps = ["completion", "vision"]  # older Ollama: no capability list, go by name
            self._caps[name] = caps
        return self._caps[name]

    def status(self) -> dict:
        try:
            models = self.list_models()
        except Exception as e:
            return {"ok": False, "error": f"連不上地端模型服務（{self.base}）：{type(e).__name__}", "models": []}
        vision = [m for m in models if m["vision"]]
        chosen = self.wanted or (vision[0]["name"] if vision else "")
        ok = bool(chosen) and any(m["name"] == chosen for m in models)
        err = None
        if not vision and not self.wanted:
            err = "找不到支援影像的模型，請先下載一個視覺模型。"
        elif not ok:
            err = f"尚未安裝模型 {chosen}。"
        return {"ok": ok, "error": err, "models": models, "model": chosen}

    @property
    def model(self) -> str:
        if self._model is None:
            st = self.status()
            if not st["ok"]:
                raise LocalModelError(st["error"] or "地端模型無法使用")
            self._model = st["model"]
        return self._model

    # ------------------------------------------------------------------ chat

    def chat_json(
        self,
        system: str,
        user: str,
        images: list[bytes] | None = None,
        schema: dict | None = None,
        max_tokens: int = 900,
        temperature: float = 0.2,
    ) -> dict:
        images = images or []
        send = self._chat_openai if self.s.api == "openai" else self._chat_ollama
        text = ""
        # A reply cut off mid-JSON is the usual failure with small models, so try once more with room to finish.
        for budget in (max_tokens, max_tokens * 2):
            try:
                text = send(system, user, images, schema, budget, temperature)
            except httpx.HTTPStatusError as e:
                raise LocalModelError(f"地端模型回應錯誤 {e.response.status_code}：{e.response.text[:200]}") from e
            except httpx.HTTPError as e:
                raise LocalModelError(f"地端模型連線失敗：{type(e).__name__}") from e
            try:
                return parse_json(text)
            except Exception:
                continue
        raise LocalModelError(f"地端模型沒有回傳有效的 JSON：{text[:120]!r}")

    def _chat_ollama(self, system, user, images, schema, max_tokens, temperature) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.s.keep_alive,
            # Room for one image, a long rubric and the answer; Ollama's default window can be too small.
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": 8192},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user, "images": [base64.b64encode(b).decode() for b in images]},
            ],
        }
        if schema is not None:
            body["format"] = schema
        if "thinking" in self._capabilities(self.model):
            body["think"] = False  # reasoning tokens would only slow a per-photo call down
        r = self.http.post(f"{self.base}/api/chat", json=body)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")

    def _chat_openai(self, system, user, images, schema, max_tokens, temperature) -> str:
        content: list[dict] = [{"type": "text", "text": user}]
        for b in images:
            url = "data:image/jpeg;base64," + base64.b64encode(b).decode()
            content.append({"type": "image_url", "image_url": {"url": url}})
        body = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }
        r = self.http.post(f"{self.base}/v1/chat/completions", json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""


# ----------------------------------------------------------------- annotation

ANNOTATE_SYSTEM = (
    "你是一位細心的攝影編輯。仔細觀察照片後，用繁體中文填寫 JSON，不要輸出其他文字。\n"
    "評分要有鑑別度：一般的生活快照是 4～5 分，構圖光線都好的照片 6～7 分，"
    "只有真正讓人停下來多看一眼的作品才給 8 分以上。"
)

ANNOTATE_USER = (
    "請描述並評估這張照片：\n"
    "- caption：一句話說明畫面內容（20～40 字）\n"
    "- tags：5～8 個關鍵詞（主體、場景、時間、風格）\n"
    "- scene：從清單中選一個最符合的主題\n"
    "- mood：氛圍或情緒，幾個字即可\n"
    "- people：清楚可見的人數\n"
    "- composition / lighting / color / subject / story / overall：1～10 分\n"
    "  （構圖、光線、色彩、主體明確度、故事性與意境、整體印象）\n"
    "- flaws：明顯缺點，沒有就給空陣列"
)

SCENES = [label for _, label, _ in taxonomy.CATEGORIES]
SCENE_KEY = {label: key for key, label, _ in taxonomy.CATEGORIES}

SCORE_KEYS = ["composition", "lighting", "color", "subject", "story", "overall"]
SCORE_WEIGHTS = {"composition": 0.2, "lighting": 0.15, "color": 0.1, "subject": 0.15, "story": 0.2, "overall": 0.2}

ANNOTATE_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "scene": {"type": "string", "enum": SCENES},
        "mood": {"type": "string"},
        "people": {"type": "integer"},
        **{k: {"type": "integer"} for k in SCORE_KEYS},
        "flaws": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["caption", "tags", "scene", "mood", "people", *SCORE_KEYS, "flaws"],
    "additionalProperties": False,
}


NO_FLAW = {"無", "沒有", "無明顯缺點", "無明顯缺陷", "none", "None", "N/A", "-"}


def clamp_score(v, lo: int = 1, hi: int = 10) -> int:
    try:
        return int(min(max(round(float(v)), lo), hi))
    except Exception:
        return (lo + hi) // 2


def annotate(vlm: LocalVLM, image: bytes) -> dict:
    """One photo -> caption, tags and six craft scores. Returns the cleaned record."""
    d = vlm.chat_json(ANNOTATE_SYSTEM, ANNOTATE_USER, [image], ANNOTATE_SCHEMA, max_tokens=700)
    out = {
        "caption": str(d.get("caption", "")).strip()[:200],
        "tags": [str(t).strip()[:20] for t in (d.get("tags") or []) if str(t).strip()][:10],
        "scene": SCENE_KEY.get(str(d.get("scene", "")).strip()),
        "mood": str(d.get("mood", "")).strip()[:40],
        "people": clamp_score(d.get("people", 0), 0, 99),
        "flaws": [str(t).strip()[:60] for t in (d.get("flaws") or []) if str(t).strip() and str(t).strip() not in NO_FLAW][:5],
    }
    for k in SCORE_KEYS:
        out[k] = clamp_score(d.get(k))
    out["score"] = round(sum(out[k] * w for k, w in SCORE_WEIGHTS.items()) * 10, 1)
    return out


def title_event(vlm: LocalVLM, captions: list[str], place: str | None) -> str | None:
    """A short name for an event from what its photos show. Text only."""
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": False,
    }
    lines = "\n".join(f"- {c}" for c in captions[:12])
    user = (
        f"以下是同一次活動中幾張照片的描述{('，地點在' + place) if place else ''}：\n{lines}\n\n"
        "請為這次活動取一個 4～10 個字的繁體中文名稱，像相簿標題，不要包含日期。"
    )
    try:
        d = vlm.chat_json("你是相簿整理助手，只輸出 JSON。", user, None, schema, max_tokens=60)
    except LocalModelError:
        return None
    t = str(d.get("title", "")).strip().strip("「」\"'")
    return t[:16] if 2 <= len(t) <= 16 else None
