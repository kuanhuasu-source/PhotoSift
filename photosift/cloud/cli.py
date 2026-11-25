"""External models through the vendor's own command-line tool, signed in with a subscription.

Experimental. Instead of an API key, PhotoSift runs the official `claude` or
`codex` program that the user has already logged in to, in its non-interactive
mode, and reads the JSON it prints. Photos are handed over as files in a
private temporary folder that is deleted afterwards.

Nothing here touches credentials: the tool uses its own stored login.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import CloudError, CloudResult, Img, Part, Provider

TIMEOUT_S = 900
SEARCH_DIRS = ["~/.local/bin", "~/.claude/local", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/bin"]
# With one of these set the tool would bill an API account instead of using the subscription login.
STRIP_ENV = {"claude": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"], "codex": ["OPENAI_API_KEY"]}
PROGRAM = {"claude": "claude", "openai": "codex"}
LOGIN_HINT = {
    "claude": "請在終端機執行 claude，輸入 /login 用你的訂閱帳號登入。",
    "openai": "請在終端機執行 codex login，選擇用 ChatGPT 帳號登入。",
}


def find_cli(provider: str, override: str = "") -> str | None:
    """Full path of the tool. An app started from the Dock has a bare PATH, so common folders are searched too."""
    if override:
        p = Path(override).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    name = PROGRAM[provider]
    found = shutil.which(name)
    if found:
        return found
    for d in SEARCH_DIRS:
        p = Path(d).expanduser() / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _env(kind: str, exe: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV[kind]}
    # node-based tools need their own folder (and the usual ones) on PATH to find `node`.
    extra = [str(Path(exe).parent), *(str(Path(d).expanduser()) for d in SEARCH_DIRS)]
    env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    env["NO_COLOR"] = "1"
    return env


def _run(argv: list[str], *, cwd: str, env: dict, stdin: str, label: str, timeout: float = TIMEOUT_S):
    try:
        return subprocess.run(argv, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise CloudError(f"{label} 超過 {int(timeout // 60)} 分鐘沒有回應，已中止。可以減少一次送出的照片數量再試。") from e
    except OSError as e:
        raise CloudError(f"無法執行 {label}：{e}") from e


def cli_status(provider: str, override: str = "") -> dict:
    """Is the tool installed, and does it report being signed in?"""
    exe = find_cli(provider, override)
    if exe is None:
        return {"found": None, "logged_in": None, "detail": f"找不到 {PROGRAM[provider]} 指令。", "hint": LOGIN_HINT[provider]}
    kind = "claude" if provider == "claude" else "codex"
    argv = [exe, "auth", "status"] if provider == "claude" else [exe, "login", "status"]
    try:
        r = subprocess.run(argv, env=_env(kind, exe), capture_output=True, text=True, timeout=20)
        text = (r.stdout or r.stderr).strip()
        logged_in = r.returncode == 0
        detail = text.splitlines()[0][:200] if text else ""
        try:  # `claude auth status` prints JSON
            info = json.loads(r.stdout)
            logged_in = bool(info.get("loggedIn", logged_in))
            detail = "已登入" + (f"（{info['subscriptionType']}）" if info.get("subscriptionType") else "") if logged_in else "尚未登入"
        except (json.JSONDecodeError, AttributeError):
            pass
    except (OSError, subprocess.TimeoutExpired):
        logged_in, detail = None, "無法確認登入狀態。"
    return {"found": exe, "logged_in": logged_in, "detail": detail, "hint": LOGIN_HINT[provider]}


def _split(parts: list[Part], folder: Path) -> tuple[str, list[Path]]:
    """Writes each image to `folder` and returns the prompt with a file reference where each image was.

    A photo introduced as "照片編號 12：" is saved as photo_12.jpg, so number and file cannot drift apart.
    """
    text: list[str] = []
    files: list[Path] = []
    for p in parts:
        if isinstance(p, Img):
            m = re.search(r"照片編號 (\d+)", text[-1]) if text else None
            f = folder / (f"photo_{m.group(1)}.jpg" if m else f"image_{len(files) + 1:02d}.jpg")
            f.write_bytes(p.data)
            files.append(f)
            text.append(f"〔第 {len(files)} 張照片，檔案 {f.name}〕")
        else:
            text.append(p)
    return "\n".join(text), files


class ClaudeCLIProvider(Provider):
    """`claude -p`: Claude Code's non-interactive mode. It reads the photos with its file-reading tool."""

    name = "claude"
    via = "cli"

    def __init__(self, model: str = "", path: str = ""):
        self.model = model
        self.path = path

    def _exe(self) -> str:
        exe = find_cli("claude", self.path)
        if exe is None:
            raise CloudError("找不到 claude 指令。請先安裝 Claude Code 並登入，或在設定裡填入它的完整路徑。")
        return exe

    def generate(self, *, system: str, parts: list[Part], schema: dict, max_tokens: int = 16000) -> CloudResult:
        exe = self._exe()
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="photosift-") as tmp:
            prompt, files = _split(parts, Path(tmp))
            if files:
                prompt += (
                    "\n\n照片都在目前的資料夾裡。請先用 Read 工具把上面提到的每一個檔案都讀過，看完全部照片之後再回答。"
                    "不要讀取其他檔案，也不要執行任何指令。"
                )
            argv = [exe, "-p", "--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False),
                    "--system-prompt", system, "--no-session-persistence", "--strict-mcp-config"]  # fmt: skip
            if self.model:
                argv += ["--model", self.model]
            # Only the read tool, and only when there is something to read.
            argv += ["--tools", "Read", "--allowedTools", "Read"] if files else ["--tools", ""]
            r = _run(argv, cwd=tmp, env=_env("claude", exe), stdin=prompt, label="Claude Code")
        out = _last_json(r.stdout)
        if out is None:
            raise CloudError(f"Claude Code 沒有回傳結果：{_tail(r.stderr or r.stdout)}　{LOGIN_HINT['claude']}")
        if out.get("is_error") or r.returncode != 0:
            raise CloudError(f"Claude Code 回報錯誤：{_tail(str(out.get('result') or r.stderr))}　{LOGIN_HINT['claude']}")
        data = out.get("structured_output")
        if not isinstance(data, dict):
            try:
                data = json.loads(out.get("result") or "")
            except (json.JSONDecodeError, TypeError) as e:
                raise CloudError("Claude Code 沒有回傳符合格式的 JSON。") from e
        u = out.get("usage") or {}
        tokens_in = sum(u.get(k, 0) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        models = list((out.get("modelUsage") or {}).keys())
        return CloudResult(
            data=data,
            input_tokens=tokens_in,
            output_tokens=u.get("output_tokens", 0) or 0,
            model=(models[0] if models else self.model or "claude") + "（訂閱）",
            seconds=time.time() - t0,
            images=len(files),
        )

    def list_models(self) -> list[str]:
        return ["opus", "sonnet", "fable", "haiku"]


class CodexCLIProvider(Provider):
    """`codex exec`: Codex's non-interactive mode, with photos attached as images."""

    name = "openai"
    via = "cli"

    def __init__(self, model: str = "", path: str = ""):
        self.model = model
        self.path = path

    def _exe(self) -> str:
        exe = find_cli("openai", self.path)
        if exe is None:
            raise CloudError("找不到 codex 指令。請先安裝 Codex CLI 並用 ChatGPT 帳號登入，或在設定裡填入它的完整路徑。")
        return exe

    def generate(self, *, system: str, parts: list[Part], schema: dict, max_tokens: int = 16000) -> CloudResult:
        exe = self._exe()
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="photosift-") as tmp:
            folder = Path(tmp)
            prompt, files = _split(parts, folder)
            (folder / "schema.json").write_text(json.dumps(schema, ensure_ascii=False), "utf-8")
            answer = folder / "answer.json"
            argv = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-C", tmp,
                    "--output-schema", str(folder / "schema.json"), "-o", str(answer), "--json"]  # fmt: skip
            if self.model:
                argv += ["-m", self.model]
            for f in files:
                argv += ["-i", str(f)]
            # `-i` takes several values, so a plain option goes last and the prompt comes in on stdin.
            argv += ["--color", "never"]
            attached = "\n\n照片已經依照上面的順序附在這則訊息裡。" if files else ""
            full = f"{system}\n\n{prompt}{attached}\n\n只回答問題本身，不需要執行任何指令或修改任何檔案。"
            r = _run(argv, cwd=tmp, env=_env("codex", exe), stdin=full, label="Codex")
            text = answer.read_text("utf-8") if answer.exists() else ""
        if r.returncode != 0 or not text.strip():
            raise CloudError(f"Codex 回報錯誤：{_tail(r.stderr or r.stdout)}　{LOGIN_HINT['openai']}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CloudError("Codex 沒有回傳符合格式的 JSON。") from e
        tokens_in = tokens_out = 0
        for line in r.stdout.splitlines():  # usage arrives as JSON lines; absent in some versions, so best effort
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = ev.get("usage") if isinstance(ev, dict) else None
            if isinstance(u, dict):
                tokens_in += (u.get("input_tokens") or 0)
                tokens_out += (u.get("output_tokens") or 0)
        return CloudResult(data=data, input_tokens=tokens_in, output_tokens=tokens_out,
                           model=(self.model or "codex") + "（訂閱）", seconds=time.time() - t0, images=len(files))  # fmt: skip

    def list_models(self) -> list[str]:
        return []


def _last_json(stdout: str) -> dict | None:
    """The result object. Some versions print warnings before it, so take the last line that parses."""
    try:
        v = json.loads(stdout)
        return v if isinstance(v, dict) else (v[-1] if isinstance(v, list) and v and isinstance(v[-1], dict) else None)
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.strip().splitlines()):
        try:
            v = json.loads(line)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    return None


def _tail(text: str, n: int = 240) -> str:
    text = (text or "").strip()
    return text[-n:] if text else "（沒有輸出）"
