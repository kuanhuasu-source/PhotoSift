"""External models. One small interface, three interchangeable providers.

Every call returns JSON matching a schema plus the tokens it cost, so the app
can show exactly what the cloud was used for.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from .. import config


class CloudError(RuntimeError):
    """A message that is safe and useful to show the user (already in Chinese)."""


@dataclass
class Img:
    data: bytes  # metadata-free JPEG

    @property
    def b64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")


@dataclass
class CloudResult:
    data: dict
    input_tokens: int
    output_tokens: int
    model: str
    seconds: float
    images: int


Part = str | Img

PROVIDERS = ("claude", "openai", "gemini")
LABELS = {"claude": "Claude", "openai": "GPT", "gemini": "Gemini"}


class Provider:
    name: str
    model: str

    def generate(self, *, system: str, parts: list[Part], schema: dict, max_tokens: int = 16000) -> CloudResult:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError


CLI_CAPABLE = ("claude", "openai")


def via_cli(s: config.Settings, name: str) -> bool:
    return name in CLI_CAPABLE and s.cloud.access.get(name) == "cli"


def get_provider(s: config.Settings, name: str | None = None) -> Provider:
    name = name or s.cloud.provider
    if via_cli(s, name):
        from .cli import ClaudeCLIProvider, CodexCLIProvider

        cls = ClaudeCLIProvider if name == "claude" else CodexCLIProvider
        return cls(s.cloud.cli_models.get(name, ""), s.cloud.cli_paths.get(name, ""))
    model = s.cloud.models.get(name, "")
    key = config.get_api_key(name)
    if name == "claude":
        from .claude import ClaudeProvider

        return ClaudeProvider(model, key)
    if name == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider(model, key, s.cloud.openai_base_url)
    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(model, key)
    raise CloudError(f"不支援的外部模型：{name}")


def estimate_image_tokens(px: int) -> int:
    """Rough tokens for one photo at `px` on its long side (3:2 frame, about 750 pixels per token)."""
    return int(px * (px * 2 / 3) / 750)
