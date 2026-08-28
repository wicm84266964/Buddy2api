"""TraeWork provider. Isolated TraeWork CN adapter."""

from __future__ import annotations

from typing import Optional

import auth_manager
import database as db
from providers.protocol import ChannelId, QuotaSnapshot
from providers.traework import chat, quota, store
from providers.traework.constants import ALIASES, CHANNEL_ID, DISPLAY_NAME, STATIC_MODELS
from providers.traework.token import TraeWorkAuthError, is_token_expired, refresh_account


class TraeWorkProvider:
    id: ChannelId = CHANNEL_ID
    display_name = DISPLAY_NAME
    checkin_supported = True

    def list_models(self) -> list[dict]:
        import catalog

        return catalog.models_for(self.id, [{"id": item} for item in STATIC_MODELS])

    def alias_map(self) -> dict[str, str]:
        return dict(ALIASES)

    def accepts_model(self, inner: str) -> bool:
        return chat.accepts_model(inner)

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
                except TraeWorkAuthError:
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
            except TraeWorkAuthError:
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
        return await quota.fetch_quota(account)

    async def fetch_checkin(self, account: dict, force: bool = False) -> dict:
        return await quota.fetch_checkin(account, force=force)

    async def claim_checkin(self, account: dict) -> dict:
        return await quota.claim_checkin(account)

    async def test_chat(self, account: dict, model: str = "qwen-3.7-plus", prompt: str = "ping") -> dict:
        return await chat.test_chat(account, model, prompt)

    async def refresh(self, account: dict) -> dict:
        return await refresh_account(account)


PROVIDER = TraeWorkProvider()
