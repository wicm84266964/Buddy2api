"""QwenWork provider. Isolated COSY adapter."""

from __future__ import annotations

from typing import Optional

import httpx

import auth_manager
import database as db
from providers.protocol import ChannelId, QuotaSnapshot
from providers.qwenwork import chat, store
from providers.qwenwork.constants import (
    ACCOUNT_CONTEXT_PATH,
    ALIASES,
    CHANNEL_ID,
    DISPLAY_NAME,
    GATEWAY,
    STATIC_MODELS,
)
from providers.qwenwork.token import QwenWorkAuthError, is_token_expired, openapi_headers, refresh_account


class QwenWorkProvider:
    id: ChannelId = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = False

    def list_models(self) -> list[dict]:
        import catalog

        return catalog.models_for(self.id, [{"id": item} for item in STATIC_MODELS])

    def alias_map(self) -> dict[str, str]:
        return dict(ALIASES)

    def accepts_model(self, inner: str) -> bool:
        value = (inner or "").strip()
        if value in ALIASES:
            return True
        ids = {str(item.get("id")) for item in self.list_models() if isinstance(item, dict)}
        return value in ids

    def translate_model(self, model: str) -> str:
        return chat.translate_model(model)

    def pick_account(self, exclude_ids: set[int] | None = None) -> Optional[dict]:
        return auth_manager.pick_account(exclude_ids, provider=self.id)

    async def pick_account_with_fallback(
        self, exclude_ids: set[int] | None = None
    ) -> Optional[dict]:
        exclude = exclude_ids or set()
        account = self.pick_account(exclude)
        if account:
            if is_token_expired(account):
                try:
                    return await refresh_account(account)
                except QwenWorkAuthError:
                    pass
            else:
                return account
        expired = [
            row
            for row in db.list_accounts(provider=self.id)
            if row.get("status") == "expired" and row.get("id") not in exclude
        ]
        for row in expired:
            try:
                return await refresh_account(row)
            except QwenWorkAuthError:
                continue
        return None

    async def has_usable_account(self) -> bool:
        return await self.pick_account_with_fallback() is not None

    async def chat_completions(self, payload: dict, api_key_info: dict | None) -> tuple:
        return await chat.chat_completions(payload, api_key_info)

    def parse_credentials(self, body: dict) -> dict:
        return store.parse_credentials(body)

    def discover(self) -> dict:
        return store.discover()

    def import_path(self, path: str) -> dict:
        return store.import_discovered(path)

    def upsert_account(self, parsed: dict) -> dict:
        return store.upsert_account(parsed)

    async def fetch_quota(self, account: dict) -> QuotaSnapshot:
        account_id = int(account.get("id") or 0)
        try:
            if is_token_expired(account):
                account = await refresh_account(account)
            response = await _account_context(account)
            if response.status_code in {401, 403}:
                first = _http_error_message(response)
                try:
                    account = await refresh_account(account)
                    response = await _account_context(account)
                except QwenWorkAuthError:
                    return QuotaSnapshot(
                        ok=False,
                        channel=self.id,
                        account_id=account_id,
                        unit="credit",
                        remaining=None,
                        message=f"{first}；请在官方客户端登录后重新导入",
                    )
                if response.status_code >= 400:
                    return QuotaSnapshot(
                        ok=False,
                        channel=self.id,
                        account_id=account_id,
                        unit="credit",
                        remaining=None,
                        message=f"{first}；请在官方客户端登录后重新导入",
                    )
        except QwenWorkAuthError as exc:
            return QuotaSnapshot(
                ok=False,
                channel=self.id,
                account_id=account_id,
                unit="credit",
                remaining=None,
                message=str(exc)[:240],
            )
        except httpx.HTTPError as exc:
            return QuotaSnapshot(
                ok=False,
                channel=self.id,
                account_id=account_id,
                unit="credit",
                remaining=None,
                message=str(exc)[:240],
            )
        if response.status_code >= 400:
            return QuotaSnapshot(
                ok=False,
                channel=self.id,
                account_id=account_id,
                unit="credit",
                remaining=None,
                message=_http_error_message(response),
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        remaining = _quota_remaining(data)
        return QuotaSnapshot(
            ok=True,
            channel=self.id,
            account_id=account_id,
            unit="credit" if remaining is not None else "unknown",
            remaining=remaining,
            extra={"raw_keys": sorted(data.keys())[:12] if isinstance(data, dict) else []},
            unsupported=remaining is None,
            message="" if remaining is not None else "quota unit unknown",
        )

    async def test_chat(self, account: dict, model: str = "qwork-advanced", prompt: str = "ping") -> dict:
        return await chat.test_chat(account, model, prompt)

    async def refresh(self, account: dict) -> dict:
        return await refresh_account(account)


async def _account_context(account: dict):
    headers = openapi_headers()
    access = str(account.get("access_token") or "")
    if access:
        headers["Authorization"] = f"Bearer {access}"
    url = f"{GATEWAY}{ACCOUNT_CONTEXT_PATH}?include=user,plan,quota"
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.get(url, headers=headers)


def _http_error_message(response) -> str:
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        detail = str(payload.get("errorMessage") or payload.get("errorCode") or payload.get("message") or "")
    text = f"HTTP {response.status_code}"
    if detail:
        text += f" {detail}"
    return text[:240]


_CREDIT_KEYS = (
    "remaining",
    "remain",
    "available",
    "balance",
    "credits",
    "total_dosage",
    "quota_remain",
    "left_quota",
)


def _quota_number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            return None
        return number
    return None


def _quota_remaining(data: dict) -> float | None:
    if not isinstance(data, dict):
        return None
    found: list[float] = []

    def walk(obj, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(obj, list):
            for item in obj[:24]:
                walk(item, depth + 1)
            return
        if not isinstance(obj, dict):
            return
        lower = {str(key).lower(): value for key, value in obj.items()}
        for key in _CREDIT_KEYS:
            number = _quota_number(lower.get(key))
            if number is not None:
                found.append(number)
                return
        for value in obj.values():
            if isinstance(value, (dict, list)):
                walk(value, depth + 1)

    walk(data)
    return found[0] if found else None


PROVIDER = QwenWorkProvider()
