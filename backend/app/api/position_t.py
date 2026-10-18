"""持仓做 T HTTP 契约。候选与成交分别登记，不提供券商下单接口。"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.services.position_t_store import LedgerError

router = APIRouter(prefix="/api/position-t", tags=["position-t"])
Qty = Annotated[int, Field(strict=True, ge=0, le=100_000_000)]
Money = Annotated[float, Field(ge=0, le=1e12, allow_inf_nan=False)]
Key = Annotated[str, Field(min_length=1, max_length=160)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountBody(Body):
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["manual", "simulation"] = "manual"
    available_cash: Money | None = None
    trade_date: date
    broker_frozen_cash: Money = 0
    cash_net_of_frozen: bool = True
    snapshot_at: AwareDatetime | None = None
    reconciled: bool = False


class SnapshotBody(Body):
    available_cash: Money | None
    broker_frozen_cash: Money = 0
    cash_net_of_frozen: bool = True
    trade_date: date
    snapshot_at: AwareDatetime
    reconciled: bool
    included_event_seq: Qty
    expected_version: Qty


class PositionBody(Body):
    symbol: str | None = None
    name: str = Field(default="", max_length=100)
    exchange: Literal["SH", "SZ", "BJ"]
    board: Literal["main", "star", "chinext", "bse"]
    security_type: Literal["stock"] = "stock"
    qty: Qty
    sellable_qty: Qty | None = None
    today_bought: Qty = 0
    today_sold: Qty = 0
    broker_frozen_qty: Qty = 0
    sellable_net_of_frozen: bool = True
    external_sell_qty: Qty = 0
    core_qty: Qty
    target_qty: Qty
    max_t_qty: Qty
    max_extra_qty: Qty
    cost_price: Money | None = None
    cost_method: Literal["manual_reference", "weighted_average", "broker_reported"] = "manual_reference"
    enabled: bool = True
    trade_date: date
    snapshot_at: AwareDatetime
    reconciled: bool
    included_event_seq: Qty
    expected_version: Qty | None = None
    benchmark_symbol: str = Field(default="000001.SH", pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    sector_id: str | None = Field(default=None, max_length=160)


class PlanBody(Body):
    signal_id: Key
    quantity: Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
    expected_account_version: Qty
    expected_position_version: Qty
    idempotency_key: Key


class StopBody(Body):
    expected_version: Qty


class OrderBody(Body):
    plan_id: Key
    leg: Literal[1, 2]
    order_id: Key
    event_id: Key
    status: Literal["submitted", "partially_filled", "filled", "cancel_pending", "cancelled", "rejected"]
    quantity: Qty
    cumulative_filled: Qty = 0
    cancelled_quantity: Qty = 0
    price: Money
    occurred_at: AwareDatetime


class ExecutionBody(Body):
    plan_id: Key
    leg: Literal[1, 2]
    order_id: Key
    quantity: Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
    price: Annotated[float, Field(gt=0, le=1e6, allow_inf_nan=False)]
    executed_at: AwareDatetime
    fee: Money | None = None
    idempotency_key: Key
    proceeds_available: bool = False


class FeeBody(Body):
    fee: Money
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: Key


class ReplayBody(Body):
    account_id: Key
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    start_date: date
    end_date: date
    direction: Literal["buy_sell", "sell_buy"] | None = None


def _service(request):
    value = getattr(request.app.state, "position_t_service", None)
    if value is None:
        raise HTTPException(503, "持仓做 T 服务未启动")
    return value


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LedgerError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/overview")
def overview(request: Request, account_id: str = ""):
    return _call(_service(request).overview, account_id or None)


@router.get("/capabilities")
def capabilities(request: Request):
    return _call(_service(request).market.capabilities)


@router.post("/accounts")
def create_account(request: Request, body: AccountBody):
    return {"account": _call(_service(request).store.create_account, body.model_dump(mode="json", exclude_none=True))}


@router.put("/accounts/{account_id}/snapshot")
def snapshot(request: Request, account_id: str, body: SnapshotBody):
    return {"account": _call(_service(request).store.update_snapshot, account_id, body.model_dump(mode="json"))}


@router.post("/accounts/{account_id}/sync-lots")
def sync_lots(request: Request, account_id: str, body: StopBody):
    return _call(_service(request).sync_lots, account_id, body.expected_version)


@router.put("/accounts/{account_id}/positions/{symbol}")
def position(request: Request, account_id: str, symbol: str, body: PositionBody):
    if body.symbol and body.symbol != symbol:
        raise HTTPException(422, "路径与持仓证券代码不一致")
    return {"position": _call(_service(request).store.upsert_position, account_id, symbol, body.model_dump(mode="json", exclude={"symbol"}))}


@router.put("/accounts/{account_id}/config")
def config(request: Request, account_id: str, body: dict):
    if not isinstance(body.get("expected_version"), int):
        raise HTTPException(422, "缺少账户版本")
    account = _call(_service(request).store.update_config, account_id, body)
    return {"account": account, "config": account["config"]}


@router.post("/plans")
def plan(request: Request, body: PlanBody):
    return {"plan": _call(_service(request).accept_plan, body.model_dump())}


@router.post("/plans/{plan_id}/stop-first-leg")
def stop_first(request: Request, plan_id: str, body: StopBody):
    return {"plan": _call(_service(request).store.stop_first_leg, plan_id, body.expected_version)}


@router.post("/order-events")
def order_event(request: Request, body: OrderBody):
    return {"plan": _call(_service(request).store.record_order_event, body.model_dump(mode="json"))}


@router.post("/executions")
def execution(request: Request, body: ExecutionBody):
    return _call(_service(request).store.record_execution, body.model_dump(mode="json"))


@router.post("/executions/{execution_id}/fee")
def correct_fee(request: Request, execution_id: str, body: FeeBody):
    return _call(_service(request).store.correct_execution, execution_id, body.model_dump())


@router.get("/plans/{plan_id}")
def plan_detail(request: Request, plan_id: str):
    result = _call(_service(request).store.plan_detail, plan_id)
    return {**result, "ledger_events": result["events"], "events": [e["payload"] for e in result["events"] if e["kind"] == "order_event"]}


@router.get("/reviews")
def review(request: Request, account_id: str, date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None):
    return {"review": _call(_service(request).review, account_id, date)}


@router.post("/replays")
def replay(request: Request, body: ReplayBody):
    return {"replay": _call(_service(request).replay, body.model_dump(mode="json"))}
