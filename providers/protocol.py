"""Provider protocol types for Buddy2api 2.0 channel isolation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

ChannelId = Literal["workbuddy", "qclaw", "qwenwork", "qoderwork", "traework"]

KNOWN_CHANNEL_IDS: tuple[ChannelId, ...] = ("workbuddy", "qclaw", "qwenwork", "qoderwork", "traework")
KNOWN_CHANNEL_SET = frozenset(KNOWN_CHANNEL_IDS)


class CheckinUnsupported(Exception):
    """Channel has no daily check-in claim API."""


class ChannelUnavailable(Exception):
    def __init__(self, channel: str, message: str = ""):
        self.channel = channel
        super().__init__(message or f"No usable accounts for channel '{channel}'")


class UnknownChannel(Exception):
    def __init__(self, channel: str, message: str = ""):
        self.channel = channel
        super().__init__(message or f"Unknown or disabled channel '{channel}'")


class InvalidModel(Exception):
    def __init__(self, model: str, message: str = ""):
        self.model = model
        super().__init__(message or f"Invalid model '{model}'")


class UnknownModel(Exception):
    def __init__(self, model: str, message: str = ""):
        self.model = model
        super().__init__(message or f"Unknown model '{model}'")


class KeyChannelMismatch(Exception):
    def __init__(self, channel: str, key_channel: str, message: str = ""):
        self.channel = channel
        self.key_channel = key_channel
        super().__init__(
            message
            or f"API key is bound to '{key_channel}', not '{channel}'"
        )


@dataclass(frozen=True)
class BindResult:
    channel: ChannelId
    inner: str
    original: str


@dataclass(frozen=True)
class DiscoveredFile:
    channel: ChannelId
    path: str
    valid: bool
    reason: str
    account_name: str
    uid_masked: str
    already_imported: bool
    extra_preview: dict = field(default_factory=dict)


@dataclass
class DiscoverResult:
    dirs: list[dict]
    files: list[DiscoveredFile]
    file_count: int
    valid_count: int
    importable_count: int
    preview_token: str = ""


@dataclass
class QuotaSnapshot:
    ok: bool
    channel: ChannelId
    account_id: int
    unit: str  # "credit" | "token" | "unknown"
    remaining: float | None
    extra: dict = field(default_factory=dict)
    unsupported: bool = False
    message: str = ""


@runtime_checkable
class Provider(Protocol):
    id: ChannelId
    display_name: str
    checkin_supported: bool

    def list_models(self) -> list[dict]: ...

    def alias_map(self) -> dict[str, str]: ...

    def accepts_model(self, inner: str) -> bool: ...

    def translate_model(self, model: str) -> str: ...

    def pick_account(self, exclude_ids: set[int] | None = None) -> dict | None: ...

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> dict | None: ...

    async def has_usable_account(self) -> bool: ...

    async def chat_completions(
        self, payload: dict, api_key_info: dict | None
    ) -> tuple: ...
