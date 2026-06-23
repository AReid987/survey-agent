"""Withdrawal automation — configurable per-site withdrawal manager."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sites.base import SiteAdapter, WithdrawalResult


@dataclass
class WithdrawalConfig:
    """Withdrawal configuration for a site."""

    mode: str = "auto"  # auto | accumulate | manual
    threshold: float = 5.00
    method: str = "paypal"


class WithdrawalManager:
    """Manage withdrawals across sites."""

    def __init__(self, config: dict | WithdrawalConfig | None = None):
        if isinstance(config, dict):
            self._config = WithdrawalConfig(**config)
        elif isinstance(config, WithdrawalConfig):
            self._config = config
        else:
            self._config = WithdrawalConfig()

    @property
    def mode(self) -> str:
        return self._config.mode

    @property
    def threshold(self) -> float:
        return self._config.threshold

    @property
    def method(self) -> str:
        return self._config.method

    async def check_and_withdraw(self, site: SiteAdapter, page) -> WithdrawalResult | None:
        """Check balance and withdraw if conditions are met.

        Returns None if no withdrawal was attempted.
        """
        if self._config.mode == "manual":
            return None

        balance = await site.get_balance(page)

        if self._config.mode == "auto":
            if balance.available >= self._config.threshold:
                return await site.withdraw(page, balance.available, self._config.method)

        elif self._config.mode == "accumulate":
            # Just report, don't withdraw
            return None

        return None

    async def force_withdraw(self, site: SiteAdapter, page, amount: float | None = None) -> WithdrawalResult:
        """Force a withdrawal regardless of mode/threshold."""
        if amount is None:
            balance = await site.get_balance(page)
            amount = balance.available
        return await site.withdraw(page, amount, self._config.method)
