"""One background job at a time, with progress the UI can poll and a cooperative cancel."""

from __future__ import annotations

import threading
import time
import traceback
from typing import Callable

from .cloud import CloudError
from .local_vlm import LocalModelError


class Busy(RuntimeError):
    pass


class Job:
    def __init__(self, jid: int, kind: str, title: str):
        self.id, self.kind, self.title = jid, kind, title
        self.status = "running"  # running | done | error | cancelled
        self.stage = ""
        self.done = 0
        self.total = 0
        self.message = ""
        self.started = time.time()
        self.finished: float | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self._cancel = threading.Event()

    def progress(self, done: int, total: int, message: str = "") -> None:
        self.done, self.total = done, total
        if message:
            self.message = message

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "status": self.status, "stage": self.stage,
            "done": self.done, "total": self.total, "message": self.message, "started": self.started,
            "finished": self.finished, "result": self.result, "error": self.error,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
        }  # fmt: skip


class JobRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._n = 0
        self.current: Job | None = None
        self.last: Job | None = None

    def submit(self, kind: str, title: str, fn: Callable[[Job], dict | None]) -> Job:
        with self._lock:
            if self.current is not None and self.current.status == "running":
                raise Busy(f"「{self.current.title}」還在進行中，請等它完成或先取消。")
            self._n += 1
            job = Job(self._n, kind, title)
            self.current = job

        def run():
            try:
                job.result = fn(job) or {}
                job.status = "cancelled" if job.cancelled() else "done"
            except (CloudError, LocalModelError, ValueError, FileNotFoundError) as e:
                job.status, job.error = "error", str(e)  # expected failures: the message is for the user
            except Exception as e:
                traceback.print_exc()
                job.status, job.error = "error", f"發生未預期的錯誤：{type(e).__name__}: {e}"
            finally:
                job.finished = time.time()
                self.last = job

        threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        return job

    def cancel(self) -> bool:
        j = self.current
        if j is not None and j.status == "running":
            j._cancel.set()
            return True
        return False

    def snapshot(self) -> dict | None:
        j = self.current
        return j.to_dict() if j else None

    def wait(self, timeout: float = 600.0) -> Job | None:
        """Blocks until the current job ends. Used by tests and the CLI."""
        t0 = time.time()
        while self.current is not None and self.current.status == "running" and time.time() - t0 < timeout:
            time.sleep(0.05)
        return self.current
