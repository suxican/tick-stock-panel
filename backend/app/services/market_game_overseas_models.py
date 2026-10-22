"""Optional overseas evidence, separate from domestic psychology scores."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class OverseasQuote(Contract):
    symbol: str
    name: str
    market: Literal["US", "KR"]
    currency: str
    state: Literal["ready", "stale", "unavailable", "unverified"]
    price: float | None = Field(default=None, gt=0)
    previous_close: float | None = Field(default=None, gt=0)
    change_pct: float | None = None
    session_date: str | None = None
    observed_at: str | None = None
    available_at: str | None = None
    phase: Literal["intraday", "closed", "unknown"] = "unknown"
    source_url: str | None = None
    reason: str


class OverseasFactor(Contract):
    id: Literal["us_tech", "korea_market", "memory_chain"]
    label: str
    state: Literal["support", "pressure", "mixed", "neutral", "unknown"]
    change_pct: float | None
    symbols: list[str]
    explanation: str


class OverseasContext(Contract):
    version: Literal["1.0.0"] = "1.0.0"
    source: str
    fetched_at: str | None
    cutoff: str
    state: Literal["ready", "limited", "unavailable", "historical_unverified"]
    quotes: list[OverseasQuote] = Field(max_length=4)
    factors: list[OverseasFactor] = Field(max_length=3)
    summary: str
    psychology_link: str
    risk_state: Literal["caution", "support", "mixed", "neutral", "unknown"]
    affected_sectors: list[str]
    limitations: list[str]
