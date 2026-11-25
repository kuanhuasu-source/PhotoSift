"""Scene categories and aesthetic anchors, scored zero-shot against the embeddings."""

from __future__ import annotations

import numpy as np

from .embedder import Embedder

# key, label shown in the UI, text prompts (several phrasings are averaged)
CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("landscape", "風景", ["a landscape photograph of nature scenery", "a scenic view of mountains, valleys or fields"]),
    ("water", "水岸與海景", ["a photo of the sea, a beach or a coastline", "a photo of a lake, river or waterfall"]),
    ("city", "城市與建築", ["a photo of buildings and architecture", "a city skyline or urban cityscape photo"]),
    ("street", "街拍", ["a candid street photography shot of people in a city street", "a photo of everyday street life, shops and alleys"]),
    ("portrait", "人像", ["a portrait photo of one person", "a close-up photo of a person's face"]),
    ("group", "合照與聚會", ["a group photo of several people posing together", "people at a party, gathering or family event"]),
    ("food", "美食", ["a photo of food on a plate", "a photo of a meal, dessert or drink on a table"]),
    ("animal", "動物", ["a photo of an animal or a pet", "a wildlife photograph of a bird or a mammal"]),
    ("plant", "花卉與植物", ["a close-up photo of flowers", "a photo of plants, leaves or trees"]),
    ("night", "夜景", ["a night photograph with city lights", "a photo taken at night of illuminated streets or stars"]),
    ("sunset", "日出日落", ["a photo of a sunset or sunrise sky", "golden hour light with the sun near the horizon"]),
    ("indoor", "室內空間", ["a photo of an indoor room interior", "the inside of a building, hall, temple or museum"]),
    ("event", "活動與表演", ["a photo of a concert, stage performance or festival", "a photo of a ceremony, parade or sports event"]),
    ("vehicle", "交通工具", ["a photo of a car, train, airplane or boat", "a photo of vehicles and transportation"]),
    ("macro", "微距與靜物", ["a macro close-up photo of a small object or insect", "a still life photo of objects on a table"]),
    ("document", "文件與截圖", ["a screenshot of a phone or computer screen", "a photo of a document, receipt, whiteboard or text page"]),
]
LABELS = {k: label for k, label, _ in CATEGORIES}

AESTHETIC_POS = [
    "an award-winning photograph",
    "a stunning professional photograph with beautiful composition",
    "a breathtaking fine art photograph with dramatic light",
    "a masterful photo with a strong mood and atmosphere",
    "a beautifully composed photograph with a clear subject",
]
AESTHETIC_NEG = [
    "a boring ordinary snapshot",
    "a poorly composed amateur photo",
    "a blurry low quality photo",
    "a cluttered messy photo with no clear subject",
    "a dull badly lit photo",
]


class Scorer:
    """Text anchors embedded once per model, then applied to any batch of image vectors."""

    def __init__(self, emb: Embedder):
        self.keys = [k for k, _, _ in CATEGORIES]
        cat = []
        for _, _, prompts in CATEGORIES:
            v = emb.texts(prompts).mean(axis=0)
            cat.append(v / (np.linalg.norm(v) + 1e-9))
        self.cat = np.stack(cat)
        self.pos = emb.texts(AESTHETIC_POS)
        self.neg = emb.texts(AESTHETIC_NEG)

    def categories(self, vecs: np.ndarray, top: int = 3) -> list[list[tuple[str, float]]]:
        sims = vecs @ self.cat.T
        # Sharpen cosine gaps into a readable confidence; only the ordering matters downstream.
        z = (sims - sims.max(axis=1, keepdims=True)) * 60.0
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        out = []
        for row in p:
            idx = np.argsort(-row)[:top]
            out.append([(self.keys[i], round(float(row[i]), 3)) for i in idx])
        return out

    def aesthetic_raw(self, vecs: np.ndarray) -> np.ndarray:
        return (vecs @ self.pos.T).mean(axis=1) - (vecs @ self.neg.T).mean(axis=1)


def to_percentile(raw: np.ndarray) -> np.ndarray:
    """Rank within the library, 0-100. The raw anchor difference has no absolute meaning."""
    if len(raw) == 0:
        return raw
    if len(raw) == 1:
        return np.array([50.0])
    order = raw.argsort().argsort().astype(np.float32)
    return order / (len(raw) - 1) * 100.0
