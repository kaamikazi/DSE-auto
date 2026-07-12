from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PaperSessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    approved_universe: list[str] = Field(min_length=1)
    strategies: list[str] = Field(min_length=1)
    risk_profile: dict[str, Any] = Field(default_factory=dict)
    fill_model: Literal["pessimistic", "balanced", "optimistic"] = "pessimistic"
