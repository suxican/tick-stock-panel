"""Versioned HTTP contracts for the research-stage market game plan."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.market_time import CN_TZ
from app.services.market_game_ecology_models import FeedbackHypothesis, MarketEcology
from app.services.market_game_execution_models import ExecutionPolicy
from app.services.market_game_overseas_models import OverseasContext

Ratio = Annotated[float, Field(ge=0, le=1)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RiskConfig(Contract):
    total_cap: float = Field(default=0.5, ge=0, le=1, strict=True)
    single_cap: float = Field(default=0.15, ge=0, le=1, strict=True)
    sector_cap: float = Field(default=0.25, ge=0, le=1, strict=True)
    risk_per_trade: float = Field(default=0.005, ge=0.0001, le=0.02, strict=True)
    portfolio_risk_budget: float = Field(default=0.015, ge=0, le=0.1, strict=True)
    min_amount: float = Field(default=100_000_000, ge=10_000_000, le=100_000_000_000, strict=True)
    max_candidates: int = Field(default=5, ge=1, le=5, strict=True)


class AnalyzeRequest(Contract):
    as_of: date | None = None
    risk: RiskConfig = Field(default_factory=RiskConfig)


class MarketState(Contract):
    trend: str
    phase: str
    emotion: str
    crowding: str
    change: str


class Allocation(Contract):
    min: Ratio
    max: Ratio
    total_cap: Ratio
    single_cap: Ratio
    sector_cap: Ratio
    risk_per_trade: Ratio
    portfolio_risk_budget: Ratio | None = None
    estimated_stress_loss: Ratio | None = None
    reason: str


class Hypothesis(Contract):
    id: str
    title: str
    status: Literal["matched", "watch", "inactive"]
    facts: list[str]
    interpretation: str
    alternative: str
    confirm: list[str]
    invalidation: list[str]


class Scenario(Contract):
    horizon: int = Field(ge=1, le=3)
    label: str
    scenario: str
    condition: str
    action: str
    max_position: Ratio


class Sector(Contract):
    name: str
    avg_return: float | None
    breadth: Ratio | None
    status: str
    reason: str


class PlanCondition(Contract):
    """Frozen requirements; definitions never imply live verification or a fill."""

    id: str
    phase: Literal["entry", "cancel"]
    scope: Literal["stock", "sector", "market", "execution"]
    metric: str
    operator: Literal[">", ">=", "<=", "<", "==", "between"]
    value: float
    upper: float | None = None
    window: Literal["quote", "1m", "session", "execution"]
    minimum_samples: int = Field(default=1, ge=1)
    description: str

    @model_validator(mode="after")
    def check_bounds(self):
        if self.operator == "between":
            if self.upper is None or self.upper < self.value:
                raise ValueError("区间条件上下界无效")
        elif self.upper is not None:
            raise ValueError("非区间条件不能设置上界")
        return self


class Candidate(Contract):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    name: str
    sector: str
    mode: str
    role: str
    reference_price: float = Field(gt=0)
    trigger_low: float = Field(gt=0)
    trigger_high: float = Field(gt=0)
    invalidation_price: float = Field(gt=0)
    max_position: Ratio
    stress_loss_pct: float = Field(gt=0)
    holding_days: int = Field(ge=1, le=3)
    evidence: list[str]
    trigger: list[str]
    invalidation: list[str]
    exit_rules: list[str]
    score: float | None = Field(default=None, ge=0, le=100)
    pressure_price: float | None = Field(default=None, gt=0)
    reward_risk_ratio: float | None = Field(default=None, ge=0)
    conditions: list[PlanCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_prices(self):
        if self.invalidation_price >= self.trigger_low or self.trigger_low > self.trigger_high:
            raise ValueError("候选触发与失效价格次序无效")
        return self


class Evidence(Contract):
    id: str
    label: str
    value: float | None
    unit: str
    source: str
    observed_at: str
    available_at: str | None


class PsychologyMetric(Contract):
    id: str
    label: str
    value: float | None
    unit: str
    percentile: float | None = Field(ge=0, le=100)


class PsychologyDimension(Contract):
    id: Literal["risk_appetite", "panic_pressure", "profit_pressure", "repair_support"]
    label: str
    score: float | None = Field(ge=0, le=100)
    delta: float | None = Field(ge=-100, le=100)
    level: str
    sample_size: int = Field(ge=0)
    coverage: Ratio
    metrics: list[PsychologyMetric]
    facts: list[str]
    interpretation: str
    alternative: str
    confirm: list[str]
    invalidation: list[str]
    limitations: list[str]


class PsychologyPoint(Contract):
    date: str
    risk_appetite: float | None = Field(ge=0, le=100)
    panic_pressure: float | None = Field(ge=0, le=100)
    profit_pressure: float | None = Field(ge=0, le=100)
    repair_support: float | None = Field(ge=0, le=100)


class Psychology(Contract):
    version: str
    scope: Literal["market_proxy"]
    summary: str
    dimensions: list[PsychologyDimension]
    history: list[PsychologyPoint]
    limitations: list[str]


class PlanValidity(Contract):
    calendar_verified: bool
    entry_session: str | None = None
    observation_sessions: list[str] = Field(default_factory=list, max_length=3)
    expires_at: str | None = None
    status: Literal["scheduled", "unverified", "research_only"]
    reason: str

    @model_validator(mode="after")
    def check_sessions(self):
        sessions = self.observation_sessions
        if sessions != sorted(set(sessions)):
            raise ValueError("观察交易日必须唯一且递增")
        for day in sessions:
            date.fromisoformat(day)
        if self.status == "scheduled" and (
            not self.calendar_verified or len(sessions) != 3
            or self.entry_session != sessions[0] or not self.expires_at
        ):
            raise ValueError("有效计划须具有完整的独立交易日历证据")
        if self.expires_at is not None:
            expiry = datetime.fromisoformat(self.expires_at)
            if expiry.tzinfo is None or not sessions or expiry.astimezone(CN_TZ) != datetime.combine(
                date.fromisoformat(sessions[-1]), time(15), tzinfo=CN_TZ,
            ):
                raise ValueError("计划到期须为最后一个观察交易日北京时间15:00")
        return self


class GameReport(Contract):
    id: str = ""
    created_at: str = ""
    as_of: str
    cutoff: str
    input_version: str
    rule_version: str
    quality: Literal["ready", "limited", "unavailable"]
    research_only: bool
    summary: str
    market_state: MarketState
    allocation: Allocation
    hypotheses: list[Hypothesis]
    scenarios: list[Scenario]
    sectors: list[Sector] = Field(max_length=3)
    candidates: list[Candidate] = Field(max_length=5)
    evidence: list[Evidence]
    limitations: list[str]
    requested_risk: RiskConfig | None = None
    validity: PlanValidity | None = None
    psychology: Psychology | None = None
    overseas: OverseasContext | None = None
    ecology: MarketEcology | None = None
    feedback_hypothesis: FeedbackHypothesis | None = None
    execution_policy: ExecutionPolicy | None = None

    @model_validator(mode="after")
    def check_allocation(self):
        allocation = self.allocation
        if self.validity is not None and self.validity.status == "scheduled":
            cutoff = datetime.fromisoformat(self.cutoff)
            entry = datetime.combine(date.fromisoformat(self.validity.entry_session), time(9, 30), tzinfo=CN_TZ)
            if cutoff.tzinfo is None or entry <= cutoff or self.validity.entry_session <= self.as_of:
                raise ValueError("入场观察日必须在本次信息截止及行情日期之后")
        if not 0 <= allocation.min <= allocation.max <= allocation.total_cap:
            raise ValueError("报告总仓位超出约束")
        symbols = [item.symbol for item in self.candidates]
        if len(symbols) != len(set(symbols)):
            raise ValueError("候选股票重复")
        if self.quality == "unavailable" and (self.candidates or allocation.max > 0):
            raise ValueError("不可用数据不能产生新增仓位")
        if sum(item.max_position for item in self.candidates) > allocation.max + 1e-6:
            raise ValueError("候选仓位合计超出总上限")
        sectors: dict[str, float] = {}
        for item in self.candidates:
            if item.max_position > allocation.single_cap + 1e-6:
                raise ValueError("个股仓位超出上限")
            sectors[item.sector] = sectors.get(item.sector, 0) + item.max_position
        if any(value > allocation.sector_cap + 1e-6 for value in sectors.values()):
            raise ValueError("板块仓位超出上限")
        stress = sum(item.max_position * item.stress_loss_pct for item in self.candidates)
        if allocation.portfolio_risk_budget is not None and stress > allocation.portfolio_risk_budget + 1e-6:
            raise ValueError("候选合计压力损失超出组合预算")
        if allocation.estimated_stress_loss is not None and abs(stress - allocation.estimated_stress_loss) > 1e-6:
            raise ValueError("组合压力损失与候选明细不一致")
        return self
