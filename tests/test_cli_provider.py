"""The subscription route: PhotoSift drives the vendor's command-line tool.

The real tools need a logged-in account, so these tests use small stand-in
programs that accept the same arguments and print the same shapes of output.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photosift import cloud, config, contest
from photosift.cloud import CloudError, Img
from photosift.cloud import cli as cli_mod
from photosift.server import create_app

from .test_server import analyse, wait

FAKER = r'''
import json, os, re, sys

def fake(spec, ids):
    t = spec.get("type")
    if t == "object":
        return {k: fake(v, ids) for k, v in spec["properties"].items()}
    if t == "array":
        item = spec["items"]
        if item.get("type") == "object" and "id" in item.get("properties", {}):
            return [dict(fake(item, ids), id=i) for i in ids]
        return [fake(item, ids), fake(item, ids), fake(item, ids)]
    if t == "integer":
        return 6
    if t == "boolean":
        return True
    return spec["enum"][0] if "enum" in spec else "測試"

def record(**kw):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")
'''

FAKE_CLAUDE = FAKER + r'''
argv = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")
if argv[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": mode != "loggedout", "subscriptionType": "ultra"}))
    sys.exit(0 if mode != "loggedout" else 1)
prompt = sys.stdin.read()
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
jpegs = sorted(f for f in os.listdir(".") if f.endswith(".jpg"))
assert all(open(f, "rb").read(2) == b"\xff\xd8" for f in jpegs)
record(tool="claude", argv=argv, prompt=prompt, files=jpegs, cwd=os.getcwd(),
       api_key_visible="ANTHROPIC_API_KEY" in os.environ)
if mode == "garbage":
    print("Something went wrong before any JSON"); sys.exit(0)
if mode == "error":
    print(json.dumps({"type": "result", "is_error": True, "result": "Invalid API key · Please run /login"})); sys.exit(1)
ids = [int(x) for x in re.findall(r"照片編號 (\d+)", prompt)]
data = fake(json.loads(opt("--json-schema")), ids)
out = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
       "usage": {"input_tokens": 1200, "cache_read_input_tokens": 300, "output_tokens": 150},
       "modelUsage": {"claude-opus-5-5": {}}}
if mode == "noschema":
    out["result"] = json.dumps(data, ensure_ascii=False)
else:
    out["result"] = ""; out["structured_output"] = data
print("warning: something harmless")
print(json.dumps(out, ensure_ascii=False))
'''

FAKE_CODEX = FAKER + r'''
argv = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")
if argv[:2] == ["login", "status"]:
    print("Logged in using ChatGPT" if mode != "loggedout" else "Not logged in")
    sys.exit(0 if mode != "loggedout" else 1)
assert argv[0] == "exec"
prompt = sys.stdin.read()
images = [argv[i + 1] for i, a in enumerate(argv) if a == "-i"]
assert all(open(f, "rb").read(2) == b"\xff\xd8" for f in images)
schema = json.load(open(argv[argv.index("--output-schema") + 1], encoding="utf-8"))
record(tool="codex", argv=argv, prompt=prompt, images=[os.path.basename(f) for f in images],
       api_key_visible="OPENAI_API_KEY" in os.environ)
if mode == "error":
    sys.stderr.write("ERROR: not logged in\n"); sys.exit(1)
ids = [int(x) for x in re.findall(r"照片編號 (\d+)", prompt)]
with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as f:
    json.dump(fake(schema, ids), f, ensure_ascii=False)
print(json.dumps({"type": "thread.started"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 0, "output_tokens": 80}}))
'''

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
SCHEMA = {
    "type": "object",
    "properties": {"caption": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["caption", "score"],
    "additionalProperties": False,
}


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Stand-in `claude` and `codex` programs, plus a log of how they were called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        f = bin_dir / name
        f.write_text(f"#!{sys.executable}\n# -*- coding: utf-8 -*-\n{body}", "utf-8")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")

    class Tools:
        claude, codex = str(bin_dir / "claude"), str(bin_dir / "codex")

        @staticmethod
        def calls():
            return [json.loads(x) for x in log.read_text("utf-8").splitlines()] if log.exists() else []

    return Tools


def test_find_cli(tools, monkeypatch, tmp_path):
    assert cli_mod.find_cli("claude", tools.claude) == tools.claude
    assert cli_mod.find_cli("claude", str(tmp_path / "missing")) is None
    monkeypatch.setenv("PATH", str(Path(tools.claude).parent))
    assert cli_mod.find_cli("openai") == tools.codex  # "openai" means the codex program
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(cli_mod, "SEARCH_DIRS", [str(Path(tools.claude).parent)])
    assert cli_mod.find_cli("claude") == tools.claude  # found even with the bare PATH of a Dock-launched app
    monkeypatch.setattr(cli_mod, "SEARCH_DIRS", [])
    assert cli_mod.find_cli("claude") is None


def test_claude_cli_reads_photos_as_files(tools, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
    p = cli_mod.ClaudeCLIProvider(model="opus", path=tools.claude)
    res = p.generate(system="你是評審", parts=["說明", "照片編號 12：", Img(JPEG), "照片編號 7：", Img(JPEG)], schema=SCHEMA)
    assert res.data == {"caption": "測試", "score": 6}
    assert res.images == 2 and res.input_tokens == 1500 and res.output_tokens == 150
    assert res.model == "claude-opus-5-5（訂閱）"
    call = tools.calls()[0]
    a = call["argv"]
    assert a[0] == "-p" and a[a.index("--output-format") + 1] == "json" and a[a.index("--model") + 1] == "opus"
    assert json.loads(a[a.index("--json-schema") + 1]) == SCHEMA and a[a.index("--system-prompt") + 1] == "你是評審"
    assert a[a.index("--tools") + 1] == "Read" and "--no-session-persistence" in a  # nothing but reading files
    assert call["files"] == ["photo_12.jpg", "photo_7.jpg"]  # file name carries the photo number
    assert "photo_12.jpg" in call["prompt"] and "photo_7.jpg" in call["prompt"] and "Read" in call["prompt"]
    assert call["api_key_visible"] is False  # the subscription login is used, not a pay-per-token key
    assert not Path(call["cwd"]).exists()  # the temporary copies are gone afterwards


def test_claude_cli_text_only_gets_no_tools(tools):
    cli_mod.ClaudeCLIProvider(path=tools.claude).generate(system="s", parts=["只有文字"], schema=SCHEMA)
    a = tools.calls()[0]["argv"]
    assert a[a.index("--tools") + 1] == "" and "--model" not in a and "--allowedTools" not in a


def test_claude_cli_failures_are_explained(tools, monkeypatch, tmp_path):
    p = cli_mod.ClaudeCLIProvider(path=tools.claude)
    monkeypatch.setenv("FAKE_MODE", "error")
    with pytest.raises(CloudError, match="/login"):
        p.generate(system="s", parts=["x"], schema=SCHEMA)
    monkeypatch.setenv("FAKE_MODE", "garbage")
    with pytest.raises(CloudError, match="沒有回傳結果"):
        p.generate(system="s", parts=["x"], schema=SCHEMA)
    monkeypatch.setenv("FAKE_MODE", "noschema")  # older builds: JSON arrives as text
    assert p.generate(system="s", parts=["x"], schema=SCHEMA).data["score"] == 6
    with pytest.raises(CloudError, match="找不到 claude"):
        cli_mod.ClaudeCLIProvider(path=str(tmp_path / "nope")).generate(system="s", parts=["x"], schema=SCHEMA)


def test_codex_cli_attaches_images(tools, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    p = cli_mod.CodexCLIProvider(model="", path=tools.codex)
    res = p.generate(system="你是評審", parts=["照片編號 3：", Img(JPEG), "請評分"], schema=SCHEMA)
    assert res.data == {"caption": "測試", "score": 6} and res.images == 1
    assert (res.input_tokens, res.output_tokens) == (900, 80) and res.model == "codex（訂閱）"
    call = tools.calls()[0]
    a = call["argv"]
    assert a[0] == "exec" and a[a.index("-s") + 1] == "read-only" and "--skip-git-repo-check" in a and "-m" not in a
    assert call["images"] == ["photo_3.jpg"] and a[-2:] == ["--color", "never"]  # prompt comes on stdin, not as an argument
    assert call["prompt"].startswith("你是評審") and "請評分" in call["prompt"]
    assert call["api_key_visible"] is False
    monkeypatch.setenv("FAKE_MODE", "error")
    with pytest.raises(CloudError, match="codex login"):
        p.generate(system="s", parts=["x"], schema=SCHEMA)


def test_login_status(tools, monkeypatch):
    s = cli_mod.cli_status("claude", tools.claude)
    assert s["found"] == tools.claude and s["logged_in"] is True and "ultra" in s["detail"]
    assert cli_mod.cli_status("openai", tools.codex)["logged_in"] is True
    monkeypatch.setenv("FAKE_MODE", "loggedout")
    assert cli_mod.cli_status("claude", tools.claude)["logged_in"] is False
    assert cli_mod.cli_status("openai", tools.codex)["logged_in"] is False
    missing = cli_mod.cli_status("claude", "/nonexistent/claude")
    assert missing["found"] is None and "claude" in missing["detail"]


def test_provider_choice_follows_settings(tools):
    s = config.Settings()
    assert type(cloud.get_provider(s, "claude")).__name__ == "ClaudeProvider"
    s.cloud.access["claude"] = "cli"
    s.cloud.access["gemini"] = "cli"  # not offered for Gemini: stays on the API
    assert type(cloud.get_provider(s, "claude")).__name__ == "ClaudeCLIProvider"
    assert type(cloud.get_provider(s, "gemini")).__name__ == "GeminiProvider"
    s.cloud.access["openai"] = "cli"
    assert type(cloud.get_provider(s, "openai")).__name__ == "CodexCLIProvider"


def test_contest_runs_through_the_subscription_tool(ctx, library, tools, monkeypatch):
    """Coach, calibration and finals over HTTP with the command-line tool as the external model."""
    from photosift import local_vlm

    from .conftest import FakeVLM

    vlm = FakeVLM()
    monkeypatch.setattr(local_vlm, "LocalVLM", lambda *a, **k: vlm)
    ctx.settings.cloud.provider = "claude"
    ctx.settings.cloud.access["claude"] = "cli"
    ctx.settings.cloud.cli_paths["claude"] = tools.claude
    app = create_app(ctx)
    with TestClient(app) as client:
        client.library = library
        analyse(client)
        st = client.get("/api/status").json()["cloud"]
        assert st == {"provider": "claude", "label": "Claude", "via": "cli", "model": "", "has_key": True}
        assert client.get("/api/cli/claude").json()["logged_in"] is True
        assert client.get("/api/cli/gemini").status_code == 404

        t = client.post("/api/cloud/test").json()
        assert t["ok"] and t["answer"] == {"colour": "測試", "ok": True} and t["model"].endswith("（訂閱）")

        cid = client.post("/api/contests", json={"name": "c", "theme": "旅途", "sizes": {"pool": 8, "calibration": 4, "finals": 3, "picks": 1}}).json()["id"]
        rubric = client.post(f"/api/contests/{cid}/coach", json={"use_cloud": True}).json()["rubric"]
        assert len(rubric["criteria"]) == 3 and rubric["source"].startswith("claude:")
        client.post(f"/api/contests/{cid}/local")
        wait(client)
        assert client.get(f"/api/contests/{cid}/outgoing/calibrate").json()["via"] == "cli"
        client.post(f"/api/contests/{cid}/calibrate")
        assert wait(client)["status"] == "done"
        client.post(f"/api/contests/{cid}/finals")
        assert wait(client)["status"] == "done"
        c = client.get(f"/api/contests/{cid}").json()
        assert c["scored_teacher"] == 4 and c["scored_final"] == 3 and len(c["summary"]["picks"]) == 1

    calls = [x for x in tools.calls() if x["tool"] == "claude"]
    with_photos = [x for x in calls if x["files"]]
    assert sum(len(x["files"]) for x in with_photos) == 1 + 4 + 3 + 3  # test swatch, calibration, critique, jury
    assert all(x["api_key_visible"] is False for x in calls)
    usage = ctx.db.all("SELECT provider, model, purpose FROM cloud_calls ORDER BY id")
    assert {r["provider"] for r in usage} == {"claude"} and all(r["model"].endswith("（訂閱）") for r in usage)
    assert [r["purpose"] for r in usage][:2] == ["test", "coach"]
