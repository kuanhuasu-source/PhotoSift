"""Shared application state: database, settings and the on-disk image cache."""

from __future__ import annotations

from pathlib import Path

from . import config, imaging
from .db import DB

THUMB_PX = 512


class Ctx:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else config.data_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.home / "photosift.db")
        self.settings = config.load_settings()
        self.version = 0  # bumped whenever the library changes, so in-memory caches know to reload
        self._vecs = None

    def bump(self) -> None:
        self.version += 1

    def vectors(self):
        """(ids, matrix) of every embedding for the current model, cached until the library changes."""
        from .embedder import get_embedder

        emb = get_embedder(self.settings.embed.model, self.settings.embed.device)
        if self._vecs is None or self._vecs[0] != (self.version, emb.name):
            ids, mat = self.db.load_embeddings(emb.name)
            self._vecs = ((self.version, emb.name), ids, mat)
        return emb, self._vecs[1], self._vecs[2]

    def save_settings(self) -> None:
        config.save_settings(self.settings)

    # ------------------------------------------------------------ image cache

    def thumb_path(self, photo_id: int) -> Path:
        return self.home / "thumbs" / f"{photo_id % 256:02x}" / f"{photo_id}.jpg"

    def preview_path(self, photo_id: int, px: int) -> Path:
        return self.home / "previews" / str(px) / f"{photo_id % 256:02x}" / f"{photo_id}.jpg"

    def preview(self, photo_id: int, px: int) -> Path:
        """A metadata-free JPEG no larger than `px`, made from the original on first use."""
        p = self.preview_path(photo_id, px)
        if p.exists():
            return p
        row = self.db.one("SELECT path FROM photos WHERE id=?", (photo_id,))
        if row is None:
            raise KeyError(photo_id)
        im, _ = imaging.load_image(Path(row["path"]), max_side=px)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(imaging.jpeg_bytes(im, 86))
        tmp.replace(p)
        return p

    def preview_bytes(self, photo_id: int, px: int) -> bytes:
        return self.preview(photo_id, px).read_bytes()

    def drop_cached(self, photo_id: int) -> None:
        self.thumb_path(photo_id).unlink(missing_ok=True)
        root = self.home / "previews"
        if root.exists():
            for d in root.iterdir():
                (d / f"{photo_id % 256:02x}" / f"{photo_id}.jpg").unlink(missing_ok=True)
