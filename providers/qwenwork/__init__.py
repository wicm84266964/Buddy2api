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
        headers = openapi_headers()
        access = str(account.get("access_token") or "")
        if access:
            headers["Authorization"] = f"Bearer {access}"
        url = f"{GATEWAY}{ACCOUNT_CONTEXT_PATH}?include=user,plan,quota"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return QuotaSnapshot(
                ok=False,
                channel=self.id,
                account_id=int(account.get("id") or 0),
                unit="unknown",
                remaining=None,
                unsupported=False,
                message=str(exc)[:240],
            )
        if response.status_code >= 400:
            return QuotaSnapshot(
                ok=False,
                channel=self.id,
                account_id=int(account.get("id") or 0),
                unit="unknown",
                remaining=None,
                message=f"HTTP {response.status_code}",
            )
        try:
            data = response.json()
        except ValueError:
            data = {}
        remaining = _quota_remaining(data)
        return QuotaSnapshot(
            ok=True,
            channel=self.id,
            account_id=int(account.get("id") or 0),
            unit="unknown" if remaining is None else "credit",
            remaining=remaining,
            extra={"raw_keys": sorted(data.keys())[:12] if isinstance(data, dict) else []},
            unsupported=remaining is None,
            message="" if remaining is not None else "quota unit unknown",
        )

    async def test_chat(self, account: dict, model: str = "qwork-advanced", prompt: str = "ping") -> dict:
        return await chat.test_chat(account, model, prompt)

    async def refresh(self, account: dict) -> dict:
        return await refresh_account(account)


def _quota_remaining(data: dict) -> float | None:
    if not isinstance(data, dict):
        return None
    quota = data.get("quota") if isinstance(data.get("quota"), dict) else data
    for key in ("remaining", "remain", "available", "balance", "total_dosage"):
        value = quota.get(key) if isinstance(quota, dict) else None
        if isinstance(value, (int, float)):
            return float(value)
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    for key in ("remaining", "credits", "balance"):
        value = plan.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


PROVIDER = QwenWorkProvider()
