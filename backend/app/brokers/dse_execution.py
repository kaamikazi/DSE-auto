from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


@dataclass(frozen=True)
class DSEExecutionContext:
    previous_close: Decimal
    available_volume: int
    queue_ahead: int = 0
    suspended: bool = False
    no_trade: bool = False
    settled_quantity: int | None = None


class DSEExecutionRules:
    def __init__(self, config_path: Path) -> None:
        self.config = json.loads(config_path.read_text(encoding="utf-8"))

    def tick_size(self, price: Decimal) -> Decimal:
        for row in self.config["tick_sizes"]:
            if price <= Decimal(str(row["up_to"])):
                return Decimal(str(row["tick"]))
        return Decimal(str(self.config["tick_sizes"][-1]["tick"]))

    def normalize_price(self, price: Decimal) -> Decimal:
        tick = self.tick_size(price)
        return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick

    def band(self, previous_close: Decimal) -> tuple[Decimal, Decimal]:
        percent = Decimal(str(self.config["price_limit_percent"])) / 100
        return self.normalize_price(previous_close * (1 - percent)), self.normalize_price(
            previous_close * (1 + percent)
        )

    def fillable_volume(self, context: DSEExecutionContext, mode: str) -> int:
        participation = Decimal(str(self.config["fill_modes"][mode]["participation_rate"]))
        queue_penalty = Decimal(str(self.config["fill_modes"][mode]["queue_penalty"]))
        visible = max(0, context.available_volume - context.queue_ahead)
        return max(0, int(Decimal(visible) * participation * (1 - queue_penalty)))
