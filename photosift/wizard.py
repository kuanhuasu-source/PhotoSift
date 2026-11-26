"""Defining a contest by conversation instead of a form.

What the contest is, who judges it and what the entrant wants decide how every
photo is scored, so this is worth more care than three text boxes. Three layers:

1. fixed multiple-choice questions about the things that change the scoring;
2. follow-up questions the on-device model writes after reading the answers;
3. a look at real photos from the library: the user marks which ones fit, the
   on-device model says what the choices have in common, and the user confirms.

Everything here runs on-device. The external model only sees the finished
transcript, as text, when it writes the rubric.
"""

from __future__ import annotations

import json

import numpy as np
from PIL import Image

from . import contest, editing, imaging, local_vlm, taste
from .context import Ctx

QUESTIONS = [
    {
        "id": "occasion", "title": "這是什麼樣的比賽？", "multi": False,
        "options": [
            {"label": "公司或團體的內部活動", "hint": "同事之間的比賽，氣氛輕鬆"},
            {"label": "攝影社團或專業比賽", "hint": "參賽者多半有攝影經驗"},
            {"label": "機關或品牌的主題徵件", "hint": "作品可能被拿去宣傳或展出"},
            {"label": "社群平台上的活動", "hint": "以分享和互動為主"},
        ],
    },
    {
        "id": "judges", "title": "誰來評審？", "multi": False,
        "why": "專業評審看創意和手法，一般人看的是共鳴和第一眼的感覺，票選則是親和力取勝。",
        "options": [
            {"label": "專業攝影師或評審團", "hint": "重視創意、手法和完成度"},
            {"label": "主管或同事", "hint": "重視切題、有共鳴、看得懂"},
            {"label": "大眾票選", "hint": "第一眼的衝擊力和親和力最重要"},
            {"label": "不確定", "hint": "會採用兼顧各方的標準"},
        ],
    },
    {
        "id": "strictness", "title": "主題要多切題？", "multi": False,
        "options": [
            {"label": "要一眼就看出主題", "hint": "離題的照片再好也不選"},
            {"label": "可以延伸詮釋", "hint": "有巧思的聯想會加分"},
            {"label": "沒有指定主題", "hint": "只看照片本身的好壞"},
        ],
    },
    {
        "id": "goal", "title": "你參加的目標是什麼？", "multi": False,
        "why": "想得獎和想展現自己，選出來的照片常常不一樣。",
        "options": [
            {"label": "想得獎，投評審所好", "hint": "選最穩、最符合評審口味的"},
            {"label": "想展現個人風格", "hint": "選最有個人特色的，不怕冒險"},
            {"label": "參與為主，選自己最喜歡的", "hint": "以你的喜好為準"},
        ],
    },
    {
        "id": "styles", "title": "你希望作品給人什麼感覺？", "multi": True,
        "options": [
            {"label": "溫馨有人情味"}, {"label": "壯麗、有氣勢"}, {"label": "寧靜、有餘韻"}, {"label": "幽默有趣"},
            {"label": "簡約、有設計感"}, {"label": "紀實、有故事"},
        ],
    },
]  # fmt: skip

LLM_SYSTEM = "你是協助攝影比賽參賽者釐清需求的助手。只輸出 JSON，文字用繁體中文，語氣自然、簡短。"


def _llm(ctx: Ctx) -> local_vlm.LocalVLM:
    return local_vlm.LocalVLM(ctx.settings.local, role="judge")


def draft_text(draft: dict) -> str:
    """What is known so far, as plain lines for a prompt."""
    lines = [f"比賽名稱：{draft.get('name') or '（未命名）'}", f"主題：{draft.get('theme') or '（沒有指定）'}"]
    if draft.get("rules"):
        lines.append(f"規則：{draft['rules']}")
    for k, label in contest.PROFILE_ZH.items():
        v = (draft.get("profile") or {}).get(k)
        if v:
            lines.append(f"{label}：{'、'.join(v) if isinstance(v, list) else v}")
    for qa in draft.get("qa") or []:
        if qa.get("q") and qa.get("a"):
            lines.append(f"問：{qa['q']}　答：{qa['a']}")
    return "\n".join(lines)


# ------------------------------------------------------- reading an announcement

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "theme": {"type": "string"},
        "rules": {"type": "string"},
        "date_from": {"type": "string"},
        "date_to": {"type": "string"},
        "max_entries": {"type": "integer"},
        "orientation": {"type": "string", "enum": ["any", "landscape", "portrait", "square"]},
        "min_long_edge": {"type": "integer"},
        "device": {"type": "string", "enum": ["any", "phone", "camera"]},
        "people": {"type": "string", "enum": ["any", "none"]},
        "judging": {"type": "string"},
    },
    "required": ["name", "theme", "rules", "date_from", "date_to", "max_entries", "orientation", "min_long_edge",
                 "device", "people", "judging"],
    "additionalProperties": False,
}  # fmt: skip


def _date(s) -> str:
    s = str(s or "").strip()[:10]
    try:
        from datetime import date

        return date.fromisoformat(s).isoformat()
    except ValueError:
        return ""


def parse_announcement(ctx: Ctx, text: str) -> dict:
    """Pulls the fields PhotoSift can act on out of a pasted contest announcement."""
    user = (
        "下面是一份攝影比賽的簡章或公告。請擷取資訊，簡章沒有提到的就留空字串、0，或選 any：\n"
        "- name：比賽名稱\n- theme：主題（一句話）\n- rules：參賽規則的重點摘要，150 字以內（後製限制、授權、投稿方式等）\n"
        "- date_from、date_to：照片必須在什麼期間拍攝，格式 YYYY-MM-DD。只有投稿截止日而沒有拍攝期間限制時留空\n"
        "- max_entries：每人最多投稿幾張\n- orientation：限定橫式 landscape、直式 portrait、正方形 square，沒限制選 any\n"
        "- min_long_edge：照片長邊至少幾個像素\n- device：限手機 phone、限相機 camera，沒限制選 any\n"
        "- people：若不可出現可辨識的人物選 none，否則 any\n"
        "- judging：評分方式、評審是誰、各項比重，照簡章寫的摘要\n\n"
        f"簡章內容：\n{text[:6000]}"
    )
    d = _llm(ctx).chat_json(LLM_SYSTEM, user, None, PARSE_SCHEMA, max_tokens=900)
    cons = {
        "date_from": _date(d.get("date_from")), "date_to": _date(d.get("date_to")),
        "orientation": d.get("orientation") if d.get("orientation") in ("landscape", "portrait", "square") else "",
        "min_long_edge": max(int(d.get("min_long_edge") or 0), 0) if str(d.get("min_long_edge") or "0").lstrip("-").isdigit() else 0,
        "device": d.get("device") if d.get("device") in ("phone", "camera") else "",
        "people": "none" if d.get("people") == "none" else "",
    }  # fmt: skip
    try:
        entries = min(max(int(d.get("max_entries") or 0), 0), 50)
    except (TypeError, ValueError):
        entries = 0
    return {
        "name": str(d.get("name", "")).strip()[:60],
        "theme": str(d.get("theme", "")).strip()[:120],
        "rules": str(d.get("rules", "")).strip()[:400],
        "judging": str(d.get("judging", "")).strip()[:300],
        "max_entries": entries,
        "constraints": {k: v for k, v in cons.items() if v},
    }


# ----------------------------------------------------------------- follow-ups

FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "why": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question", "why", "options"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def followups(ctx: Ctx, draft: dict) -> list[dict]:
    """Two to four questions about whatever is still ambiguous, each with choices to click."""
    user = (
        f"參賽者目前提供的資訊：\n{draft_text(draft)}\n\n"
        "你要幫他從幾千張照片裡挑出最適合投稿的。請找出目前還模糊、而且答案不同會讓你挑出不同照片的地方，"
        "提出 2～4 個選擇題。例如主題的某個詞可以怎麼解讀、有沒有人物、要寫實還是寫意。\n"
        "- question：問題本身，一句話\n- why：為什麼這會影響選片，一句話\n"
        "- options：3～4 個具體、互斥的選項，每個 15 字以內\n"
        "不要問已經回答過的事，也不要問規則或截止日期這類和選片無關的事。"
    )
    d = _llm(ctx).chat_json(LLM_SYSTEM, user, None, FOLLOWUP_SCHEMA, max_tokens=900)
    out = []
    for q in d.get("questions") or []:
        opts = list(dict.fromkeys(str(o).strip()[:40] for o in (q.get("options") or []) if str(o).strip()))[:4]
        text = str(q.get("question", "")).strip()[:120]
        if text and len(opts) >= 2:
            out.append({"question": text, "why": str(q.get("why", "")).strip()[:120], "options": opts})
    return out[:4]


# ------------------------------------------------------------- photo discussion

PROBE_SCHEMA = {
    "type": "object",
    "properties": {"probes": {"type": "array", "items": contest._PROBE}},
    "required": ["probes"],
    "additionalProperties": False,
}


def quick_probes(ctx: Ctx, draft: dict) -> list[dict]:
    """A handful of picture descriptions for the theme, to find candidate photos worth showing."""
    theme = (draft.get("theme") or "").strip()
    fallback = [{"zh": theme, "en": theme}] if theme else []
    if not theme:
        return fallback
    user = (
        f"{draft_text(draft)}\n\n請寫出 6 個符合這個主題的照片可能呈現的畫面，涵蓋不同的詮釋方式。"
        "每個是一句具體的畫面描述：zh 用繁體中文，en 用英文（像圖片說明，例如 an empty street at dawn with soft light）。"
    )
    try:
        d = _llm(ctx).chat_json(LLM_SYSTEM, user, None, PROBE_SCHEMA, max_tokens=600)
        return contest._probes(d.get("probes"), 8) or fallback
    except local_vlm.LocalModelError:
        return fallback


def _eligible(ctx: Ctx, draft: dict):
    brief = {"scope": draft.get("scope") or {}, "constraints": draft.get("constraints") or {}}
    w, p = contest.scope_sql(brief)
    rows = ctx.db.all(
        f"SELECT p.id, p.prefilter FROM photos p WHERE {w} AND p.is_best=1 AND COALESCE(p.flag,'') != 'reject' "
        "AND p.prefilter IS NOT NULL AND p.flags='[]'",
        p,
    )
    return rows


def _spread(vecs: np.ndarray, order: list[int], k: int) -> list[int]:
    """Greedy farthest-point choice: start with the best, then keep adding whatever looks least like the rest."""
    if not order:
        return []
    chosen = [order[0]]
    while len(chosen) < min(k, len(order)):
        sims = vecs[order] @ vecs[chosen].T
        nearest = sims.max(axis=1)
        nearest[[order.index(c) for c in chosen]] = 9.0
        chosen.append(order[int(nearest.argmin())])
    return chosen


def examples(ctx: Ctx, draft: dict, round_no: int, labels: dict[str, str], k: int = 8) -> dict:
    """Photos to put in front of the user.

    Round 1: varied candidates that fit the theme in different ways.
    Round 2: the ones the first answers leave most in doubt, where one more answer teaches the most.
    """
    if not ctx.db.val("SELECT COUNT(*) FROM embeddings"):
        raise ValueError("請先分析相片庫，才能挑出範例照片。")
    rows = _eligible(ctx, draft)
    if len(rows) < 4:
        raise ValueError("符合條件的照片太少，請放寬範圍或硬性規則。")
    _, ids, vecs = ctx.vectors()
    where = {int(pid): i for i, pid in enumerate(ids)}
    rows = [r for r in rows if r["id"] in where]
    idx = [where[r["id"]] for r in rows]
    base = np.array([r["prefilter"] for r in rows], dtype=np.float32)
    probes = draft.get("probes") or quick_probes(ctx, draft)
    ts = contest.theme_scores(ctx, {"probes": probes}, vecs[idx]) if probes else None
    score = 0.6 * ts[0] + 0.4 * base if ts is not None else base
    seen = {int(k_) for k_ in labels}
    yes = [where[int(p)] for p, v in labels.items() if v == "yes" and int(p) in where]
    no = [where[int(p)] for p, v in labels.items() if v == "no" and int(p) in where]

    if round_no >= 2 and yes:
        pref = taste.prototype_score(vecs[yes], vecs[no], vecs[idx])
        # Most informative: plausible on theme, yet neither clearly like the liked ones nor like the disliked ones.
        doubt = -np.abs(taste.to_percentile(pref) - 50.0)
        top = [i for i in np.argsort(-score)[: max(k * 12, 60)] if rows[int(i)]["id"] not in seen]
        top.sort(key=lambda i: -doubt[int(i)])
        pool = [idx[int(i)] for i in top[: k * 3]]
    else:
        pool = [idx[int(i)] for i in np.argsort(-score)[: max(k * 8, 40)] if rows[int(i)]["id"] not in seen]
    chosen = _spread(vecs, pool, k)
    back = {i: int(pid) for pid, i in where.items()}
    return {"photo_ids": [back[i] for i in chosen], "probes": probes}


INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "array", "items": {"type": "string"}},
        "hypothesis": {"type": "string"},
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["observations", "hypothesis", "question", "options"],
    "additionalProperties": False,
}


def _sheet(ctx: Ctx, ids: list[int]) -> bytes:
    tiles = [Image.open(ctx.thumb_path(pid)) for pid in ids[:8]]
    return imaging.jpeg_bytes(editing.contact_sheet(tiles, list(range(1, len(tiles) + 1)), cols=4, cell=320), 85)


def insight(ctx: Ctx, draft: dict, labels: dict[str, str]) -> dict:
    """The on-device model looks at what the user accepted and rejected, and says what it thinks they are after."""
    yes = [int(p) for p, v in labels.items() if v == "yes"]
    no = [int(p) for p, v in labels.items() if v == "no"]
    if len(yes) < 2:
        raise ValueError("至少要標記兩張「符合」的照片，模型才看得出共通點。")
    images = [_sheet(ctx, yes)] + ([_sheet(ctx, no)] if no else [])
    user = (
        f"{draft_text(draft)}\n\n"
        f"第一張圖是參賽者認為「符合他想投的感覺」的 {min(len(yes), 8)} 張照片。"
        + (f"第二張圖是他認為「不符合」的 {min(len(no), 8)} 張照片。" if no else "")
        + "\n請仔細比較，找出他的偏好：\n"
        "- observations：2～4 點具體觀察。符合的照片有什麼共通點？和不符合的差在哪裡？"
        "要講畫面上看得到的事（有沒有人、留白多寡、光線、色調、距離感），不要講空泛的形容詞。\n"
        "- hypothesis：用一句話歸納「所以他想找的是……的照片」\n"
        "- question：針對你最沒把握的一點，問他一個問題來確認\n"
        "- options：這個問題的 3 個選項，每個 15 字以內"
    )
    d = _llm(ctx).chat_json(LLM_SYSTEM, user, images, INSIGHT_SCHEMA, max_tokens=800)
    opts = list(dict.fromkeys(str(o).strip()[:40] for o in (d.get("options") or []) if str(o).strip()))[:4]
    return {
        "observations": [str(o).strip()[:200] for o in (d.get("observations") or []) if str(o).strip()][:4],
        "hypothesis": str(d.get("hypothesis", "")).strip()[:300],
        "question": str(d.get("question", "")).strip()[:160] if len(opts) >= 2 else "",
        "options": opts if len(opts) >= 2 else [],
    }


# -------------------------------------------------------------------- summary


def suggest_sizes(eligible: int, max_entries: int = 0) -> dict:
    """Funnel widths that scale with the library: a wider local pass for a bigger library, the same few uploads."""
    pool = int(min(max(round(eligible * 0.08), 40), 400))
    pool = min(pool, eligible) if eligible else pool
    picks = max_entries or 5
    finals = max(12, picks * 3)
    return {
        "pool": pool,
        "calibration": 16 if pool >= 40 else max(4, pool // 3),
        "triage": 48 if pool >= 96 else (32 if pool >= 48 else 0),
        "finals": min(finals, max(pool // 2, 3)),
        "picks": picks,
    }


def preview(ctx: Ctx, draft: dict) -> dict:
    """How many photos survive the scope and the hard rules, and the funnel sizes that follow."""
    brief = {"scope": draft.get("scope") or {}, "constraints": draft.get("constraints") or {}}
    library = ctx.db.val("SELECT COUNT(*) FROM photos WHERE error IS NULL") or 0
    in_scope = contest.scope_count(ctx, {"scope": brief["scope"]})
    after_rules = contest.scope_count(ctx, brief)
    eligible = len(_eligible(ctx, draft))
    return {
        "library": library, "in_scope": in_scope, "after_rules": after_rules, "eligible": eligible,
        "sizes": suggest_sizes(eligible, int(draft.get("max_entries") or 0)),
    }  # fmt: skip


def to_contest(draft: dict, sizes: dict | None = None) -> dict:
    """The wizard's draft as the fields a contest is created from."""
    labels = draft.get("labels") or {}
    notes = "；".join(x for x in (draft.get("judging"), draft.get("notes")) if x)
    return {
        "name": draft.get("name") or draft.get("theme") or "未命名比賽",
        "theme": draft.get("theme") or "",
        "rules": draft.get("rules") or "",
        "notes": notes,
        "scope": draft.get("scope") or {},
        "sizes": sizes or {},
        "constraints": {k: v for k, v in (draft.get("constraints") or {}).items() if v},
        "profile": {k: v for k, v in (draft.get("profile") or {}).items() if v},
        "qa": [q for q in (draft.get("qa") or []) if q.get("q") and q.get("a")],
        "examples": {
            "liked": [int(p) for p, v in labels.items() if v == "yes"],
            "disliked": [int(p) for p, v in labels.items() if v == "no"],
        },
        "insight": draft.get("insight") or "",
    }
