"""Research execution contracts; never orders or verification of a real account."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExecutionCosts(ExecutionContract):
    account_equity: float = Field(default=1_000_000, gt=0)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.01)
    minimum_commission: float = Field(default=5, ge=0)
    sell_tax_rate: float = Field(default=0.0005, ge=0, le=0.01)
    transfer_rate: float = Field(default=0.00001, ge=0, le=0.01)
    slippage: float = Field(default=0.001, ge=0, le=0.05)
    max_volume_participation: float = Field(default=0.01, gt=0, le=0.1)


class ExecutionPolicy(ExecutionContract):
    version: Literal["1.0.0"] = "1.0.0"
    market_gate: Literal["frozen_universe_breadth_v1"] = "frozen_universe_breadth_v1"
    minimum_market_breadth: Literal[0.4] = 0.4
    minimum_market_median: Literal[-0.005] = -0.005
    minimum_winner_premium: Literal[-0.01] = -0.01
    scheduled_exit_time: Literal["14:55"] = "14:55"
    vwap_basis: Literal["archived_continuous_minutes"] = "archived_continuous_minutes"
    costs: ExecutionCosts = Field(default_factory=ExecutionCosts)


class ExecutionLabel(ExecutionContract):
    horizon: int = Field(ge=1, le=3)
    trade_date: str | None
    state: Literal["pending", "unavailable", "no_entry", "open", "exited", "not_executable"]
    mature: bool = False
    exit_time: str | None = None
    exit_price: float | None = None
    net_return: float | None = None
    outcome_available_at: str | None = None
    reason: str


class ExecutionRow(ExecutionContract):
    symbol: str
    name: str
    mode: str
    sector: str
    regime: str
    signal_time: str | None = None
    entry_time: str | None = None
    entry_price: float | None = None
    quantity: int = Field(default=0, ge=0)
    reason: str
    labels: list[ExecutionLabel]


class ExecutionEvaluation(ExecutionContract):
    version: str = "1.0.0"
    report_id: str
    evaluated_at: str
    report_created_at: str | None = None
    input_version: str
    rule_version: str
    kind: Literal["simulated_execution"] = "simulated_execution"
    status: Literal["pending", "partial", "complete", "unavailable"]
    entry_authorized: Literal[False] = False
    rows: list[ExecutionRow]
    costs: ExecutionCosts
    evidence_summary: list[str] = Field(default_factory=list)
    limitations: list[str]


class WalkForwardComparison(ExecutionContract):
    state: Literal["insufficient", "shadow_only"]
    training_min_dates: int = 30
    test_dates: int
    accepted_dates: int
    withheld_dates: int
    fixed_mean_net_return: float | None
    filtered_mean_net_return: float | None
    difference: float | None
    reason: str


class AdaptationGroup(ExecutionContract):
    id: str
    rule_version: str
    mode: str
    regime: str
    horizon: int
    sample_size: int
    independent_dates: int
    excluded_count: int
    state: Literal["insufficient", "observing", "weakening"]
    mean_net_return: float | None
    reason: str
    walk_forward: WalkForwardComparison


class AdaptationResult(ExecutionContract):
    version: str = "1.0.0"
    evaluated_at: str
    status: Literal["shadow"] = "shadow"
    automatic_adjustment: Literal[False] = False
    validation_status: Literal["not_validated"] = "not_validated"
    groups: list[AdaptationGroup]
    limitations: list[str]
