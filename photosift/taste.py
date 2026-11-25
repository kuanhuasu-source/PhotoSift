"""Learning what the user likes from what they have already picked and rejected.

Every pick, reject and star rating is a free label. A small linear model on
the image embeddings turns them into a score for every photo in the library,
so the next thousand photos are ordered a little more like the user would
order them. No model call, no tokens, and it improves as they keep choosing.

The same machinery scores a contest's examples ("this fits, this does not").
"""

from __future__ import annotations

import numpy as np

from .context import Ctx

MIN_EACH = 3
MIN_TOTAL = 8


def fit(pos: np.ndarray, neg: np.ndarray, min_each: int = MIN_EACH, min_total: int = MIN_TOTAL) -> dict | None:
    """Regularised logistic regression on unit vectors. Returns None when there is too little to learn from."""
    if len(pos) < min_each or len(neg) < min_each or len(pos) + len(neg) < min_total:
        return None
    X = np.vstack([pos, neg]).astype(np.float64)
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
    mu = X.mean(axis=0)
    Z = (X - mu) * 8.0  # cosine differences are small; scale so the logits have room to move
    # Start from the difference of class means, which is already a decent answer with a handful of labels.
    w = (pos.mean(axis=0) - neg.mean(axis=0)).astype(np.float64) * 8.0
    b = 0.0
    wt = np.where(y == 1, 0.5 / len(pos), 0.5 / len(neg))  # the two classes count equally however many of each
    lam = 0.02
    for _ in range(300):
        p = 1.0 / (1.0 + np.exp(-(Z @ w + b)))
        g = (p - y) * wt
        w -= 2.0 * (Z.T @ g + lam * w)
        b -= 2.0 * g.sum()
    return {"w": w, "b": b, "mu": mu, "n_pos": len(pos), "n_neg": len(neg)}


def score(model: dict, vecs: np.ndarray) -> np.ndarray:
    """Probability-like value in 0..1 for each row."""
    z = ((vecs - model["mu"]) * 8.0) @ model["w"] + model["b"]
    return 1.0 / (1.0 + np.exp(-z))


def prototype_score(pos: np.ndarray, neg: np.ndarray, vecs: np.ndarray) -> np.ndarray | None:
    """With only a few examples: closer to the liked ones than to the disliked ones."""
    if len(pos) == 0:
        return None
    s = vecs @ pos.mean(axis=0)
    if len(neg):
        s = s - vecs @ neg.mean(axis=0)
    return s


def to_percentile(x: np.ndarray) -> np.ndarray:
    if len(x) < 2:
        return np.full(len(x), 50.0)
    return x.argsort().argsort().astype(np.float32) / (len(x) - 1) * 100.0


def user_labels(ctx: Ctx) -> tuple[list[int], list[int]]:
    """Picks and high ratings on one side, rejects and low ratings on the other."""
    pos = [r[0] for r in ctx.db.all("SELECT id FROM photos WHERE flag='pick' OR rating>=4")]
    neg = [r[0] for r in ctx.db.all("SELECT id FROM photos WHERE flag='reject' OR (rating BETWEEN 1 AND 2)")]
    both = set(pos) & set(neg)  # picked but rated low, or the reverse: the flag wins
    flagged = {r[0]: r[1] for r in ctx.db.all("SELECT id, flag FROM photos WHERE flag IS NOT NULL")}
    pos = [i for i in pos if i not in both or flagged.get(i) == "pick"]
    neg = [i for i in neg if i not in both or flagged.get(i) == "reject"]
    return pos, neg


def update_library(ctx: Ctx) -> dict:
    """Recomputes the taste score of every photo. Cheap enough to run whenever the labels change."""
    pos_ids, neg_ids = user_labels(ctx)
    sig = (ctx.version, tuple(sorted(pos_ids)), tuple(sorted(neg_ids)))
    if getattr(ctx, "_taste_sig", None) == sig:
        return getattr(ctx, "_taste_info", {"ready": False})
    info = {"ready": False, "picks": len(pos_ids), "rejects": len(neg_ids), "need": MIN_TOTAL}
    if ctx.db.val("SELECT COUNT(*) FROM embeddings"):
        _, ids, vecs = ctx.vectors()
        where = {int(pid): i for i, pid in enumerate(ids)}
        pos = vecs[[where[i] for i in pos_ids if i in where]]
        neg = vecs[[where[i] for i in neg_ids if i in where]]
        model = fit(pos, neg)
        if model is not None:
            pct = to_percentile(score(model, vecs))
            ctx.db.many("UPDATE photos SET taste=? WHERE id=?", [(round(float(v), 1), int(i)) for i, v in zip(ids, pct)])
            info["ready"] = True
        else:
            ctx.db.run("UPDATE photos SET taste=NULL WHERE taste IS NOT NULL")
    ctx._taste_sig, ctx._taste_info = sig, info
    return info
