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
               brightness: float = 1.0, variant: int = 0) -> None:  # fmt: skip
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
    if gps:
        g = ex.get_ifd(0x8825)
        lat, lon = gps
        g[1], g[2] = ("N" if lat >= 0 else "S"), (R(int(abs(lat)), 1), R(int(abs(lat) % 1 * 60), 1), R(0, 1))
        g[3], g[4] = ("E" if lon >= 0 else "W"), (R(int(abs(lon)), 1), R(int(abs(lon) % 1 * 60), 1), R(0, 1))
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
                return [fake(spec["items"])]
            if t == "object":
                return {k: fake(v) for k, v in spec["properties"].items()}
            return spec["enum"][0] if "enum" in spec else "測試文字"

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
        self.calls.append({"images": len(images), "ids": ids, "props": list(schema["properties"])})
        props = schema["properties"]
        if "criteria" in props:
            data = {
                "criteria": [
                    {"name": "主題", "weight": 50, "description": "d", "high": "h", "low": "l"},
                    {"name": "意境", "weight": 30, "description": "d", "high": "h", "low": "l"},
                    {"name": "技術", "weight": 20, "description": "d", "high": "h", "low": "l"},
                ],
                "judge_brief": "評審須知", "pitfalls": ["雜亂"], "ideal": "理想作品",
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
        elif "ranking" in props:
            data = {"ranking": [{"id": pid, "why": f"理由 {pid}"} for pid in reversed(ids)], "overall": "總評"}
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
    from photosift import contest, local_vlm, retouch

    vlm = FakeVLM()
    prov = FakeProvider()
    monkeypatch.setattr(local_vlm, "LocalVLM", lambda *a, **k: vlm)
    monkeypatch.setattr(contest, "get_provider", lambda *a, **k: prov)
    monkeypatch.setattr(retouch, "get_provider", lambda *a, **k: prov)
    return vlm, prov
