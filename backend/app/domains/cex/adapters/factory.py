"""Factory for instantiating authenticated Binance and Bybit Exchange Adapters from Vault credentials."""

import httpx

from app.config import Settings, get_settings
from app.core.security import CredentialVault
from app.domains.cex.adapters.base import ExchangeAdapterError, IExchangeAdapter
from app.domains.cex.adapters.binance import BinanceAdapter
from app.domains.cex.adapters.bybit import BybitAdapter
from app.domains.cex.adapters.rate_limiter import AsyncTokenBucketRateLimiter
from app.models.entities import ExchangeConnection
from app.models.enums import ExchangeId


class ExchangeAdapterFactory:
    """Creates and manages authenticated IExchangeAdapter instances."""

    def __init__(
        self,
        vault: CredentialVault,
        settings: Settings | None = None,
    ) -> None:
        self._vault = vault
        self._settings = settings or get_settings()

    def create_from_connection(
        self,
        connection: ExchangeConnection,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: AsyncTokenBucketRateLimiter | None = None,
    ) -> IExchangeAdapter:
        """Decrypt ExchangeConnection credentials in memory and instantiate the adapter."""
        exch_str = connection.exchange_id.upper().strip()
        api_key = self._vault.decrypt(connection.api_key_encrypted, associated_data=exch_str)
        api_secret = self._vault.decrypt(connection.api_secret_encrypted, associated_data=exch_str)

        return self.create_direct(
            exchange_id=ExchangeId(exch_str),
            api_key=api_key,
            api_secret=api_secret,
            is_testnet=connection.is_testnet,
            http_client=http_client,
            rate_limiter=rate_limiter,
        )

    def create_direct(
        self,
        exchange_id: ExchangeId,
        api_key: str,
        api_secret: str,
        is_testnet: bool = True,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: AsyncTokenBucketRateLimiter | None = None,
    ) -> IExchangeAdapter:
        """Instantiate a BinanceAdapter or BybitAdapter directly from decrypted credentials."""
        max_lev = self._settings.default_max_leverage_cap
        if exchange_id == ExchangeId.BINANCE:
            return BinanceAdapter(
                api_key=api_key,
                api_secret=api_secret,
                is_testnet=is_testnet,
                max_leverage_cap=max_lev,
                rate_limiter=rate_limiter,
                http_client=http_client,
            )
        if exchange_id == ExchangeId.BYBIT:
            return BybitAdapter(
                api_key=api_key,
                api_secret=api_secret,
                is_testnet=is_testnet,
                max_leverage_cap=max_lev,
                rate_limiter=rate_limiter,
                http_client=http_client,
            )
        raise ExchangeAdapterError(
            f"Unsupported exchange_id: {exchange_id}",
            exchange_id=exchange_id,
        )
