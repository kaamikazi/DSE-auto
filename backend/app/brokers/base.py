from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Never

from app.models import Order


class BrokerAdapter(ABC):
    @abstractmethod
    def submit_order(self, order: Order, market_price: Decimal, available_volume: int) -> Order: ...

    @abstractmethod
    def cancel_order(self, order: Order) -> Order: ...

    @abstractmethod
    def replace_order(self, order: Order, quantity: int, limit_price: Decimal) -> Order: ...

    @abstractmethod
    def reconcile(self) -> dict[str, object]: ...


class OfficialBrokerAdapter(BrokerAdapter):
    """Deliberately disabled future interface; unofficial automation is prohibited."""

    def _disabled(self) -> Never:
        raise RuntimeError("Official live broker execution is disabled in Milestone 1")

    def submit_order(self, order: Order, market_price: Decimal, available_volume: int) -> Order:
        self._disabled()

    def cancel_order(self, order: Order) -> Order:
        self._disabled()

    def replace_order(self, order: Order, quantity: int, limit_price: Decimal) -> Order:
        self._disabled()

    def reconcile(self) -> dict[str, object]:
        return {"healthy": False, "reason": "live adapter disabled"}
