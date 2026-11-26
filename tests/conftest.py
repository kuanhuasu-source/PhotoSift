"""Test fixtures: a throw-away data dir, synthetic photos, and stand-ins for both kinds of model.

Nothing here downloads a model or touches the network.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter
from PIL.TiffImagePlugin import IFDRational as R

os.environ["PHOTOSIFT_EMBEDDER"] = "fake"


def make_photo(path: Path, seed: int, when: datetime | None = None, gps=None, blur: float = 0, size=(900, 600),
               brightness: float = 1.0, variant: int = 0, shot: dict | None = None) -> None:  # fmt: skip
    """A textured image that is unique per seed. `variant` gives a near-identical burst frame."""
    rng = np.random.default_rng(seed)
    w, h = size
    base = rng.integers(0, 255, size=(6, 9, 3), dtype=np.uint8)
    im = Image.fromarray(base).resize((w, h), Image.Resampling.BICUBIC)
    noise = np.random.default_rng(seed).integers(-40, 40, size=(h, w, 3))
    arr = np.clip(np.asarray(im).astype(int) + noise, 0, 255)
    arr = np.clip(arr * brightness, 0, 255).astype(np.uint8)
    im = Image.fromarray(arr)
    if variant:
        im = im.crop((variant, variant, w, h)).resize((w, h))
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    ex = Image.Exif()
    if when:
        ex[0x010F], ex[0x0110] = "TestCam", "T-1"
        sub = ex.get_ifd(0x8769)
        sub[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
        sub[0x829D], sub[0x829A], sub[0x8827] = R(28, 10), R(1, 125), 400
    shot = shot or {}
    if shot:  # how it was taken: aperture, shutter (num, den), iso, focal, focal35, program, bias, tz, lens, camera
        sub = ex.get_ifd(0x8769)
        if "camera" in shot:
            ex[0x010F], ex[0x0110] = shot["camera"]
        if "aperture" in shot:
            sub[0x829D] = R(int(shot["aperture"] * 10), 10)
        if "shutter" in shot:
            sub[0x829A] = R(*shot["shutter"])
        for key, tag in (("iso", 0x8827), ("program", 0x8822), ("metering", 0x9207), ("flash", 0x9209), ("focal35", 0xA405),
                         ("lens", 0xA434), ("tz", 0x9011)):  # fmt: skip
            if key in shot:
                sub[tag] = shot[key]
        if "focal" in shot:
            sub[0x920A] = R(int(shot["focal"]), 1)
        if "bias" in shot:
            sub[0x9204] = R(int(round(shot["bias"] * 10)), 10)
    if gps:
        g = ex.get_ifd(0x8825)
        lat, lon = gps
        g[1], g[2] = ("N" if lat >= 0 else "S"), (R(int(abs(lat)), 1), R(int(abs(lat) % 1 * 60), 1), R(0, 1))
        g[3], g[4] = ("E" if lon >= 0 else "W"), (R(int(abs(lon)), 1), R(int(abs(lon) % 1 * 60), 1), R(0, 1))
        if "heading" in shot:
            g[16], g[17] = "T", R(int(shot["heading"]), 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=92, exif=ex.tobytes())


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """Two events six months apart, one burst of three, one blurred frame, one undated file."""
    root = tmp_path / "photos"
    t = datetime(2026, 3, 1, 9, 0)
    for i in range(5):
        make_photo(root / "trip" / f"A{i}.jpg", seed=i, when=t + timedelta(minutes=20 * i), gps=(25.03, 121.56))
    burst = t + timedelta(hours=3)
    for k in range(3):
        make_photo(root / "trip" / f"B{k}.jpg", seed=50, when=burst + timedelta(seconds=k), gps=(25.03, 121.56),
                   variant=k * 2, blur=6 if k == 2 else 0)  # fmt: skip
    t2 = datetime(2026, 9, 10, 18, 0)
    for i in range(4):
        make_photo(root / "dinner" / f"C{i}.jpg", seed=100 + i, when=t2 + timedelta(minutes=5 * i), gps=(35.01, 135.76))
    make_photo(root / "scans" / "old.jpg", seed=200)
    (root / "trip" / "notes.txt").write_text("not a photo")
    (root / ".hidden").mkdir()
    make_photo(root / ".hidden" / "skip.jpg", seed=300)
    return root


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PHOTOSIFT_HOME", str(tmp_path / "home"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from photosift.context import Ctx

    return Ctx()


@pytest.fixture
def analysed(ctx, library):
    """A context whose library has been through ingest, embedding and grouping."""
    import time

    from photosift import pipeline
    from photosift.jobs import Job

    ctx.db.run("INSERT INTO folders(path, added_at) VALUES (?,?)", (str(library), time.time()))
    result = pipeline.analyze(ctx, Job(1, "analyze", "t"))
    return ctx, result


class FakeVLM:
    """Stands in for the on-device model: scores follow image brightness, so results are deterministic."""

    def __init__(self, *a, **k):
        self.model = "fake-vlm"
        self.calls = 0
        self.systems: list[str] = []

    def status(self):
        return {"ok": True, "error": None, "model": self.model, "models": [{"name": self.model, "vision": True, "size_gb": 1.0}]}

    def chat_json(self, system, user, images=None, schema=None, max_tokens=0, temperature=0.2):
        import io

        self.calls += 1
        self.systems.append(system)
        props = (schema or {}).get("properties", {})
        if "title" in props:
            return {"title": "測試活動"}
        level = 5
        if images:
            im = Image.open(io.BytesIO(images[0])).convert("L").resize((8, 8))
            level = int(np.clip(round(np.asarray(im).mean() / 255 * 9) + 1, 1, 10))
        if "entries" in props:  # writing descriptions without the external model
            import re

            nums = [int(n) for n in re.findall(r"^作品編號 (\d+)", user, flags=re.M)]
            return {"entries": [{"id": n, "title": f"地端名稱{n}", "description": f"地端寫的說明 {n}"} for n in nums]}
        if "verdict" in props:  # reviewing its own edit: ask for a little less once, then accept
            self.reviews = getattr(self, "reviews", 0) + 1
            first = self.reviews % 2 == 1
            return {"verdict": "less" if first else "good", **{k: 0 for k in props if props[k].get("type") == "integer"},
                    "exposure": -1 if first else 0, "comment": "有點過亮" if first else "剛好"}  # fmt: skip

        def fake(spec):
            t = spec.get("type")
            if t == "integer":
                return level
            if t == "boolean":
                return False
            if t == "array":
                self.n = getattr(self, "n", 0) + 1
                return [fake(spec["items"]), fake(spec["items"])]
            if t == "object":
                return {k: fake(v) for k, v in spec["properties"].items()}
            if "enum" in spec:
                return spec["enum"][0]
            self.k = getattr(self, "k", 0) + 1
            return f"測試文字{self.k}"  # distinct strings, like a real model's answers

        return {k: fake(spec) for k, spec in props.items()}


class FakeProvider:
    """Stands in for the external model and records what it was sent."""

    name = "fake"
    model = "fake-cloud"

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, *, system, parts, schema, max_tokens=16000):
        from photosift.cloud import CloudResult, Img

        images = [p for p in parts if isinstance(p, Img)]
        ids = [int(p.split("照片編號 ")[1].split("：")[0]) for p in parts if isinstance(p, str) and p.startswith("照片編號 ")]
        self.calls.append({"images": len(images), "ids": ids, "props": list(schema["properties"]),
                           "text": "\n".join(p for p in parts if isinstance(p, str))})  # fmt: skip
        props = schema["properties"]
        if "criteria" in props:
            data = {
                "criteria": [
                    {"name": "主題", "weight": 50, "description": "d", "high": "h", "low": "l"},
                    {"name": "意境", "weight": 30, "description": "d", "high": "h", "low": "l"},
                    {"name": "技術", "weight": 20, "description": "d", "high": "h", "low": "l"},
                ],
                "judge_brief": "評審須知", "pitfalls": ["雜亂"], "ideal": "理想作品",
                "probes": [{"zh": "安靜的街道", "en": "a quiet street"}, {"zh": "水邊的倒影", "en": "reflections on water"}],
                "avoid": [{"zh": "擁擠的人群", "en": "a crowd"}], "plan": ["最重視主題", "雜亂會扣分"],
            }  # fmt: skip
        elif "photos" in props:
            item = props["photos"]["items"]["properties"]
            data = {"photos": []}
            for n, pid in enumerate(ids):
                row = {"id": pid}
                for k, spec in item.items():
                    if k == "id":
                        continue
                    if spec["type"] == "object":  # the jury's edit for a finalist
                        row[k] = {"exposure": 1, "contrast": 0, "highlights": -1, "shadows": 1, "warmth": 0, "saturation": 0,
                                  "clarity": 1, "vignette": 0, "black_white": False, "straighten": False,
                                  "crop_left": 5, "crop_top": 0, "crop_right": 95, "crop_bottom": 100}  # fmt: skip
                    else:
                        row[k] = (3 + (pid * 7 + n) % 7) if spec["type"] == "integer" else f"評語 {pid}"
                data["photos"].append(row)
        elif "ranking" in props:  # jury: the sheet's numbers, best first; here simply reversed
            import re

            text = "\n".join(p for p in parts if isinstance(p, str))
            n = len(re.findall(r"^\d+\.《", text, flags=re.M))
            data = {"ranking": [{"no": k, "why": f"理由 {k}"} for k in range(n, 0, -1)], "overall": "總評"}
        elif "picks" in props:  # contact-sheet triage: take the first few numbers on offer
            import re

            text = "\n".join(p for p in parts if isinstance(p, str))
            want = int(re.search(r"最多 (\d+) 張", text).group(1))
            shown = len(re.findall(r"^\d+\. ", text, flags=re.M))
            data = {"picks": [{"no": k, "why": f"值得看 {k}"} for k in range(1, min(want, shown) + 1)]}
        elif "entries" in props:  # written entry: one description per work number
            import re

            text = "\n".join(p for p in parts if isinstance(p, str))
            limit = int(re.search(r"(\d+) 個字以內", text).group(1))
            nums = [int(n) for n in re.findall(r"^作品編號 (\d+)", text, flags=re.M)]
            data = {"entries": [{"id": n, "title": f"名稱{n}", "description": (f"這是作品 {n} 的說明。" * 30)[: limit - 1]} for n in nums]}
        elif "works" in props:  # exhibition curator: every other candidate, in two chapters, with flaws a real model shows
            import re

            text = "\n".join(p for p in parts if isinstance(p, str))
            want = int(re.search(r"選出剛好 (\d+) 張", text).group(1))
            shown = [int(n) for n in re.findall(r"^(\d+)\. ", text, flags=re.M)]
            picks = shown[::2][: max(want - 1, 1)]  # one short on purpose
            rest = [n for n in shown if n not in picks]
            data = {
                "title": "靜水流年", "statement": "旅途中停下來的時刻。",
                "chapters": [{"title": "開場", "idea": "把人帶進來"}, {"title": "收尾", "idea": "安靜地結束"}],
                "works": [{"no": n, "chapter": 2 if i % 2 else 1, "why": f"理由 {n}"} for i, n in enumerate(picks)] + [{"no": picks[0], "chapter": 1, "why": "重複"},
                                                                                                          {"no": 999, "chapter": 9, "why": "不存在"}],
                "reserves": [{"no": n, "chapter": 1, "why": f"可替代 {n}"} for n in rest[:4]],
                "sequence_note": "由近到遠。", "weak": [{"no": picks[-1], "why": "和系列的色調不合"}, {"no": 999, "why": "x"}],
                "gaps": ["少一張收尾的大景"],
            }
        elif "bio" in props:
            text = "\n".join(p for p in parts if isinstance(p, str))
            data = {"bio": "攝影者的簡介。", "statement": "我拍這個系列是因為" + ("有回答" if "攝影者自己的回答" in text else "沒有回答") + "。"}
        elif "headline" in props:  # mentor review: refers to photos by their number on the sheet
            import re

            text = "\n".join(p for p in parts if isinstance(p, str))
            shown = [int(n) for n in re.findall(r"^(\d+)\. ", text, flags=re.M)]
            data = {
                "headline": "構圖穩定，但常在正午拍",
                "strengths": [{"title": "主體明確", "evidence": f"第 {shown[0]} 張"}],
                "problems": [{"title": "快門太慢", "evidence": "模糊比例偏高", "fix": "把快門加快到安全快門", "photos": [shown[-1], 999]}],
                "next_time": ["黃金時刻再來", "帶腳架"],
                "photo_notes": [{"no": shown[0], "note": "這張最好"}, {"no": 999, "note": "不存在的編號"}],
                "exercises": [{"title": "練習穩定持機", "how": "同一個畫面連拍三張", "why": "模糊太多", "metric": "blur_rate"},
                              {"title": "寫拍攝筆記", "how": "每次拍完寫三行", "why": "養成習慣", "metric": "不存在的量測"}],
            }
        elif "notes" in props:
            data = {"notes": "請把技術分數打嚴一點", "observations": ["技術分數偏高"]}
        elif "verdict" in props:
            data = {"verdict": "less", "exposure": -1, "contrast": 0, "highlights": 0, "shadows": 0, "warmth": 0,
                    "saturation": 0, "comment": "有點過亮"}  # fmt: skip
        else:
            raise AssertionError(f"unexpected schema: {list(props)}")
        return CloudResult(data=data, input_tokens=1000 * max(len(images), 1), output_tokens=200, model=self.model,
                           seconds=0.01, images=len(images))  # fmt: skip

    def list_models(self):
        return ["fake-cloud"]


@pytest.fixture
def fakes(monkeypatch):
    from photosift import contest, exhibition, local_vlm, mentor, retouch, submission

    vlm = FakeVLM()
    prov = FakeProvider()
    monkeypatch.setattr(local_vlm, "LocalVLM", lambda *a, **k: vlm)
    monkeypatch.setattr(contest, "get_provider", lambda *a, **k: prov)
    monkeypatch.setattr(retouch, "get_provider", lambda *a, **k: prov)
    monkeypatch.setattr(submission, "get_provider", lambda *a, **k: prov)
    monkeypatch.setattr(mentor, "get_provider", lambda *a, **k: prov)
    monkeypatch.setattr(exhibition, "get_provider", lambda *a, **k: prov)
    return vlm, prov
