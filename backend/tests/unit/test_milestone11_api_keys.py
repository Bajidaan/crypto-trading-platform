"""Milestone 11: API keys are checked with Binance, tied to an account, and stored safely."""

from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from app.api.deps import AppState, set_app_state
from app.config import Settings
from app.core.database import init_database
from app.core.security import DEFAULT_DEV_VAULT_SECRET, CredentialVault, build_credential_vault
from app.domains.cex.adapters.base import AccountBalance, ExchangeAuthenticationError, ExchangeNetworkError
from app.main import create_app
from app.models.entities import ExchangeConnection
from app.models.enums import ExchangeId, MarketType
from tests.unit.test_milestone8_live_execution import LiveMockAdapter, StubAdapterFactory


class KeyCheckAdapter(LiveMockAdapter):
    """Answers balance reads like Binance would for a key valid on `valid_on` (DEMO/LIVE)."""

    def __init__(self, is_demo: bool, valid_on: set[str], markets: set[MarketType] | None = None,
                 unreachable: bool = False) -> None:
        super().__init__()
        self.env = "DEMO" if is_demo else "LIVE"
        self.valid_on = valid_on
        self.markets = markets if markets is not None else {MarketType.FUTURES_USDT, MarketType.SPOT}
        self.unreachable = unreachable

    async def fetch_balance(self, market_type: MarketType) -> AccountBalance:
        if self.unreachable:
            raise ExchangeNetworkError("connection refused")
        if self.env not in self.valid_on or market_type not in self.markets:
            raise ExchangeAuthenticationError("Invalid API-key, IP, or permissions for action (-2015)")
        return AccountBalance(exchange_id=ExchangeId.BINANCE, market_type=market_type,
                              total_equity=Decimal("5000"), available_balance=Decimal("5000"))


@asynccontextmanager
async def _client(**key_behaviour: Any) -> AsyncIterator[tuple[httpx.AsyncClient, AppState, StubAdapterFactory]]:
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:",
                        master_vault_secret="milestone11-test-master-key-32-bytes!", market_stream_enabled=False)
    state = AppState(settings)
    await init_database(state.engine)
    factory = StubAdapterFactory(LiveMockAdapter())
    factory.direct = lambda **kw: KeyCheckAdapter(is_demo=kw["is_testnet"], **key_behaviour)  # type: ignore[attr-defined]
    state.strategy_runner.adapter_factory = factory  # type: ignore[assignment]
    set_app_state(state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as client:
        yield client, state, factory
    await state.engine.dispose()


def _payload(environment: str, **extra: Any) -> dict[str, Any]:
    return {"api_key": "AbCdEfGh12345678", "api_secret": "SeCrEt12345678", "environment": environment, **extra}


async def _count_keys(state: AppState) -> int:
    from sqlalchemy import func, select

    async with state.session_factory() as s:
        return int((await s.execute(select(func.count(ExchangeConnection.id)))).scalar() or 0)


async def test_live_key_entered_as_demo_is_refused_with_a_hint() -> None:
    async with _client(valid_on={"LIVE"}) as (client, state, _):
        resp = await client.post("/api/v1/connections", json=_payload("DEMO"))
        assert resp.status_code == 400
        assert "belongs to your real Binance account" in resp.json()["detail"]
        assert await _count_keys(state) == 0


async def test_demo_key_entered_as_live_is_refused_with_a_hint() -> None:
    async with _client(valid_on={"DEMO"}) as (client, state, _):
        resp = await client.post("/api/v1/connections", json=_payload("LIVE"))
        assert resp.status_code == 400
        assert "belongs to Binance Demo Trading" in resp.json()["detail"]
        assert await _count_keys(state) == 0


async def test_invalid_key_is_refused() -> None:
    async with _client(valid_on=set()) as (client, state, _):
        resp = await client.post("/api/v1/connections", json=_payload("DEMO"))
        assert resp.status_code == 400 and "rejected this key" in resp.json()["detail"]
        assert await _count_keys(state) == 0


async def test_unreachable_binance_is_not_reported_as_a_bad_key() -> None:
    async with _client(valid_on={"DEMO"}, unreachable=True) as (client, state, _):
        resp = await client.post("/api/v1/connections", json=_payload("DEMO"))
        assert resp.status_code == 502 and "Could not reach Binance" in resp.json()["detail"]
        assert await _count_keys(state) == 0


async def test_valid_key_is_saved_trimmed_with_balances_and_warnings() -> None:
    async with _client(valid_on={"DEMO"}, markets={MarketType.SPOT}) as (client, state, factory):
        resp = await client.post(
            "/api/v1/connections",
            json={"api_key": "  AbCdEfGh\n12345678 ", "api_secret": " SeCrEt12345678\n",
                  "environment": "DEMO", "spot_capital_cap_usd": 300},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["label"] == "Binance Demo"  # default label
        assert data["api_key_masked"] == "AbCd****5678"
        assert data["in_use"] is True
        assert data["balances"] == {"SPOT": 5000.0}
        assert any("Futures are not enabled" in w for w in data["warnings"])
        assert data["spot_capital_cap_usd"] == 300.0
        # The key was checked on the demo servers.
        assert factory.direct_calls[0]["is_testnet"] is True
        async with state.session_factory() as s:
            conn = await s.get(ExchangeConnection, data["id"])
            assert state.vault.decrypt(conn.api_key_encrypted, associated_data="BINANCE") == "AbCdEfGh12345678"
            assert state.vault.decrypt(conn.api_secret_encrypted, associated_data="BINANCE") == "SeCrEt12345678"


async def test_newest_key_per_account_is_the_one_in_use() -> None:
    async with _client(valid_on={"DEMO", "LIVE"}) as (client, _, _):
        old_demo = (await client.post("/api/v1/connections", json=_payload("DEMO", label="old demo"))).json()
        new_demo = (await client.post("/api/v1/connections", json=_payload("DEMO", label="new demo"))).json()
        live = (await client.post("/api/v1/connections", json=_payload("LIVE", label="real"))).json()
        listed = {c["label"]: c for c in (await client.get("/api/v1/connections")).json()}
        status = (await client.get("/api/v1/system/status")).json()
    assert listed["new demo"]["in_use"] is True
    assert listed["old demo"]["in_use"] is False
    assert listed["real"]["in_use"] is True
    assert {old_demo["environment"], new_demo["environment"], live["environment"]} == {"DEMO", "LIVE"}
    # The active environment (DEMO) shows the new key and its balance immediately (read through
    # the saved key's client, which here is the standard $10,000 mock account).
    assert status["connection_label"] == "new demo" and status["equity_usd"] == 10000.0


async def test_bybit_and_negative_caps_are_refused() -> None:
    async with _client(valid_on={"DEMO"}) as (client, _, _):
        bybit = await client.post("/api/v1/connections", json=_payload("DEMO", exchange_id="BYBIT"))
        negative = await client.post("/api/v1/connections", json=_payload("DEMO", futures_capital_cap_usd=-1))
    assert bybit.status_code == 400 and negative.status_code == 400


def test_default_secret_is_replaced_by_a_local_vault_key(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'trading.db').as_posix()}"
    vault = build_credential_vault(DEFAULT_DEV_VAULT_SECRET, db_url)
    key_file = tmp_path / "vault.key"
    assert key_file.exists() and len(key_file.read_text()) >= 32

    token = vault.encrypt("my-api-secret", associated_data="BINANCE")
    # Not readable with the public development secret alone...
    public_only = CredentialVault(DEFAULT_DEV_VAULT_SECRET)
    try:
        public_only.decrypt(token, associated_data="BINANCE")
        raise AssertionError("token must not decrypt with the public secret")
    except Exception as exc:
        assert "Credential authentication failed" in str(exc)
    # ...but readable after a restart, which reuses the same file.
    assert build_credential_vault(DEFAULT_DEV_VAULT_SECRET, db_url).decrypt(token, associated_data="BINANCE") == "my-api-secret"

    # Keys saved earlier with the public secret still open (fallback), so nothing is lost.
    legacy = public_only.encrypt("old-secret", associated_data="BINANCE")
    assert vault.decrypt(legacy, associated_data="BINANCE") == "old-secret"


def test_explicit_master_secret_is_used_as_is(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'trading.db').as_posix()}"
    vault = build_credential_vault("my-own-very-long-master-secret-123", db_url)
    assert not (tmp_path / "vault.key").exists()
    token = vault.encrypt("x", associated_data="BINANCE")
    assert CredentialVault("my-own-very-long-master-secret-123").decrypt(token, associated_data="BINANCE") == "x"
