"""Settings, data directory and API-key storage."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic import BaseModel, Field

APP_NAME = "PhotoSift"

# Environment variables checked (in order) for each cloud provider's key.
KEY_ENV = {
    "claude": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
}


def data_dir() -> Path:
    root = os.environ.get("PHOTOSIFT_HOME")
    p = Path(root).expanduser() if root else Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


class LocalSettings(BaseModel):
    """The on-device vision-language model (Ollama, LM Studio, ...)."""

    api: Literal["ollama", "openai"] = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = ""  # bulk annotator. Empty: the first installed vision model
    judge_model: str = ""  # contest judge. Empty: same as the annotator
    image_px: int = 896
    timeout_s: int = 300
    keep_alive: str = "15m"


class EmbedSettings(BaseModel):
    model: str = "hf-hub:timm/ViT-B-16-SigLIP2-256"
    device: str = "auto"  # auto | mps | cuda | cpu
    batch_size: int = 16


class CloudSettings(BaseModel):
    provider: Literal["claude", "openai", "gemini"] = "gemini"
    models: dict[str, str] = Field(
        default_factory=lambda: {
            "claude": "claude-opus-5-5",
            "openai": "",  # pick from the live model list in Settings
            "gemini": "gemini-flash-latest",
        }
    )
    openai_base_url: str = "https://api.openai.com/v1"
    image_px: int = 1024
    # Optional prices (USD per million tokens) used only to show a cost estimate.
    price_in: float = 0.0
    price_out: float = 0.0


class AnalysisSettings(BaseModel):
    event_gap_hours: float = 6.0
    event_jump_km: float = 80.0
    burst_window_s: int = 180
    burst_sim: float = 0.90
    dup_sim: float = 0.965
    blur_below: float = 22.0


class Settings(BaseModel):
    local: LocalSettings = Field(default_factory=LocalSettings)
    embed: EmbedSettings = Field(default_factory=EmbedSettings)
    cloud: CloudSettings = Field(default_factory=CloudSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)


_lock = threading.Lock()


def _settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> Settings:
    p = _settings_path()
    if p.exists():
        try:
            return Settings.model_validate(json.loads(p.read_text("utf-8")))
        except Exception:
            # A corrupt or outdated file must not stop the app from starting.
            p.rename(p.with_suffix(".broken.json"))
    return Settings()


def save_settings(s: Settings) -> None:
    with _lock:
        tmp = _settings_path().with_suffix(".tmp")
        tmp.write_text(s.model_dump_json(indent=2), "utf-8")
        tmp.replace(_settings_path())


# --------------------------------------------------------------------------- keys


def _secrets_path() -> Path:
    return data_dir() / "secrets.json"


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
        return keyring
    except Exception:
        return None


def _file_secrets() -> dict:
    p = _secrets_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def get_api_key(provider: str) -> str | None:
    """Environment first, then the OS keychain, then the private fallback file."""
    for name in KEY_ENV.get(provider, []):
        if os.environ.get(name):
            return os.environ[name]
    kr = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(APP_NAME, provider)
            if v:
                return v
        except Exception:
            pass
    return _file_secrets().get(provider) or None


def key_source(provider: str) -> str | None:
    for name in KEY_ENV.get(provider, []):
        if os.environ.get(name):
            return "env"
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password(APP_NAME, provider):
                return "keychain"
        except Exception:
            pass
    return "file" if _file_secrets().get(provider) else None


def set_api_key(provider: str, key: str) -> str:
    """Store (or, with an empty key, remove) a provider key. Returns where it went."""
    if provider not in KEY_ENV:
        raise ValueError(f"unknown provider: {provider}")
    key = key.strip()
    kr = _keyring()
    if kr is not None:
        try:
            if key:
                kr.set_password(APP_NAME, provider, key)
            else:
                try:
                    kr.delete_password(APP_NAME, provider)
                except Exception:
                    pass
            _write_file_secret(provider, "")
            return "keychain"
        except Exception:
            pass
    _write_file_secret(provider, key)
    return "file"


def _write_file_secret(provider: str, key: str) -> None:
    with _lock:
        cur = _file_secrets()
        if key:
            cur[provider] = key
        else:
            cur.pop(provider, None)
        p = _secrets_path()
        if not cur:
            p.unlink(missing_ok=True)
            return
        p.write_text(json.dumps(cur), "utf-8")
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
