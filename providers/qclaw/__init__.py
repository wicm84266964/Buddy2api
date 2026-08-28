"""QClaw provider. Isolated aizone/jprx adapter."""

from __future__ import annotations

from typing import Optional

import auth_manager
import database as db
from providers.protocol import ChannelId, QuotaSnapshot
from providers.qclaw import chat, jprx, oauth, store
from providers.qclaw.constants import (
    ALIASES,
    CHANNEL_ID,
    DISPLAY_NAME,
    STATIC_MODELS,
)


class QClawProvider:
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
        if value in ALIASES or value.startswith("pool-"):
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
        account = self.pick_account(exclude_ids)
        if account:
            return account
        expired = [
            row
            for row in db.list_accounts(provider=self.id)
            if row.get("status") == "expired"
            and row.get("id") not in (exclude_ids or set())
        ]
        for row in expired:
            try:
                await jprx.refresh_channel(row)
                fresh = db.get_account(row["id"])
                if fresh:
                    db.update_account(fresh["id"], {"status": "active"})
                    return db.get_account(fresh["id"])
            except jprx.JprxError:
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

    async def fetch_quota(self, account: dict) -> QuotaSnapshot:
        # Official balance column is credit-only. QClaw's daily token cap is not 积分.
        return QuotaSnapshot(
            ok=True,
            channel=self.id,
            account_id=int(account.get("id") or 0),
            unit="credit",
            remaining=None,
            unsupported=True,
            message="no credit balance",
        )

    async def test_chat(self, account: dict, model: str = "default", prompt: str = "ping") -> dict:
        return await chat.test_chat(account, model, prompt)

    async def start_login(self, guid: str) -> dict:
        return await oauth.start_login(guid)

    async def complete_login(self, guid: str, code: str, state: str) -> dict:
        return await oauth.complete_login(guid, code, state)

    async def refresh(self, account: dict) -> dict:
        await jprx.refresh_channel(account)
        import database as db

        fresh = db.get_account(account["id"])
        if fresh:
            db.update_account(fresh["id"], {"status": "active"})
            return db.get_account(fresh["id"]) or fresh
        return account


PROVIDER = QClawProvider()
