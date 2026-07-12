from __future__ import annotations

from typing import Any


class BDStockClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> BDStockClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    def ticker(self, symbol: str) -> Ticker:
        return Ticker(symbol)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass


class Ticker:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    async def quote(self) -> Any:
        class MockQuote:
            symbol = self.symbol
            ltp = 100.0
            high = 105.0
            low = 95.0
            ycp = 100.0
            change = 0.0
            volume = 10000
            trade = 100
            value = 1000000.0

        return MockQuote()

    async def history(self, start: str, end: str) -> Any:
        import pandas as pd  # type: ignore[import-untyped]

        # Return a DataFrame with open, high, low, close, volume, trade, value
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "trade", "value"]
        )

    async def info(self, summary: bool = True) -> Any:
        class MockInfo:
            def model_dump(self, mode: str = "json") -> dict[str, Any]:
                return {
                    "basic_information": {
                        "company_name": "Mock Company",
                        "sector": "Mock Sector",
                    }
                }

        return MockInfo()

    async def depth(self) -> Any:
        class MockDepth:
            def model_dump(self, mode: str = "json") -> dict[str, Any]:
                return {"bids": [], "asks": []}

        return MockDepth()

    async def news(self) -> list[Any]:
        return []
