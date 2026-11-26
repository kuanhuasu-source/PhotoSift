"""Stage 2: one image-text embedding model, run on-device, used for four jobs.

Near-duplicate grouping, scene categories, text search and a first aesthetic
guess all come from the same vectors, so the whole library costs one model pass.
The default is SigLIP 2, whose text side is multilingual (Chinese queries work).
"""

from __future__ import annotations

import os
import zlib
import threading
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image


class Embedder:
    name: str
    dim: int

    def images(self, ims: list[Image.Image]) -> np.ndarray: ...
    def texts(self, texts: list[str]) -> np.ndarray: ...

    def match_prob(self, sims: np.ndarray) -> np.ndarray | None:
        """Probability that image and text really match, when the model is trained to give one."""
        return None


class OpenClipEmbedder(Embedder):
    def __init__(self, model: str, device: str = "auto"):
        import open_clip
        import torch

        self.torch = torch
        if device == "auto":
            if torch.backends.mps.is_available():
                device = "mps"
            elif torch.cuda.is_available():
                device = "cuda"
            else:
                device = "cpu"
        self.device = device
        self.name = model
        self.model, self.preprocess = open_clip.create_model_from_pretrained(model)
        self.model.eval().to(device)
        self.tokenizer = open_clip.get_tokenizer(model)
        self.dim = int(self.texts(["x"]).shape[1])
        # SigLIP models are trained with a sigmoid loss, so scale and bias turn a cosine into a probability.
        bias = getattr(self.model, "logit_bias", None)
        self._calib = (float(self.model.logit_scale.detach().exp()), float(bias.detach())) if bias is not None else None

    def match_prob(self, sims: np.ndarray) -> np.ndarray | None:
        if self._calib is None:
            return None
        scale, bias = self._calib
        return 1.0 / (1.0 + np.exp(-(scale * sims + bias)))

    def images(self, ims: list[Image.Image]) -> np.ndarray:
        t = self.torch
        batch = t.stack([self.preprocess(im) for im in ims]).to(self.device)
        with t.inference_mode():
            f = self.model.encode_image(batch)
            f = f / f.norm(dim=-1, keepdim=True)
        return f.float().cpu().numpy()

    def texts(self, texts: list[str]) -> np.ndarray:
        t = self.torch
        tok = self.tokenizer(texts).to(self.device)
        with t.inference_mode():
            f = self.model.encode_text(tok)
            f = f / f.norm(dim=-1, keepdim=True)
        return f.float().cpu().numpy()


class FakeEmbedder(Embedder):
    """Deterministic colour/layout signature. For tests only: no model download, no semantics."""

    name = "fake-v1"
    dim = 96

    def images(self, ims: list[Image.Image]) -> np.ndarray:
        out = []
        for im in ims:
            a = np.asarray(im.convert("RGB").resize((4, 4), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
            v = np.concatenate([a.ravel() - 0.5, a.mean(axis=(0, 1)) - 0.5, a.std(axis=(0, 1))])
            v = np.resize(v, self.dim)
            out.append(v / (np.linalg.norm(v) + 1e-9))
        return np.stack(out).astype(np.float32)

    def texts(self, texts: list[str]) -> np.ndarray:
        out = []
        for s in texts:
            rng = np.random.default_rng(zlib.crc32(s.encode("utf-8")))  # not hash(): that changes from one run to the next
            v = rng.normal(size=self.dim)
            out.append(v / np.linalg.norm(v))
        return np.stack(out).astype(np.float32)


_cache: dict[tuple[str, str], Embedder] = {}
_cache_lock = threading.Lock()


def get_embedder(model: str, device: str = "auto") -> Embedder:
    if os.environ.get("PHOTOSIFT_EMBEDDER") == "fake" or model == "fake":
        return _cache.setdefault(("fake", ""), FakeEmbedder())
    with _cache_lock:
        key = (model, device)
        if key not in _cache:
            _cache[key] = OpenClipEmbedder(model, device)
        return _cache[key]


def embed_images(
    emb: Embedder,
    items: Iterable[tuple[int, Path]],
    batch_size: int,
    on_batch: Callable[[list[tuple[int, np.ndarray]]], None],
    cancelled: Callable[[], bool] = lambda: False,
) -> int:
    """Embeds thumbnails in batches. Unreadable files are skipped, not fatal."""
    n = 0
    batch_ids: list[int] = []
    batch_ims: list[Image.Image] = []

    def flush():
        nonlocal n
        if not batch_ims:
            return
        vecs = emb.images(batch_ims)
        on_batch(list(zip(batch_ids, vecs)))
        n += len(batch_ids)
        batch_ids.clear()
        batch_ims.clear()

    for pid, path in items:
        if cancelled():
            break
        try:
            with Image.open(path) as im:
                batch_ims.append(im.convert("RGB"))
            batch_ids.append(pid)
        except Exception:
            continue
        if len(batch_ims) >= batch_size:
            flush()
    flush()
    return n
