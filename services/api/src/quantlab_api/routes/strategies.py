from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError
from sqlalchemy import select

from quantlab.strategies.library import REGISTRY, catalog, create_strategy
from quantlab_api.errors import ApiError
from quantlab_api.models import Role, StrategyDefinition
from quantlab_api.schemas import StrategyIn, StrategyOut
from quantlab_api.security import DB, CurrentUser, audit, require_workspace

router = APIRouter(tags=["strategies"])


def validate_strategy(key: str, params: dict[str, Any]) -> dict[str, Any]:
    if key not in REGISTRY:
        raise ApiError(
            422, "unknown_strategy", f"Unknown strategy {key!r}", {"available": sorted(REGISTRY)}
        )
    try:
        return create_strategy(key, params).params.model_dump(mode="json")
    except ValidationError as exc:
        raise ApiError(
            422,
            "invalid_strategy_params",
            "Invalid strategy parameters",
            [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc


@router.get("/strategies/catalog")
def strategy_catalog() -> list[dict[str, Any]]:
    return catalog()


@router.get("/workspaces/{workspace_id}/strategies", response_model=list[StrategyOut])
def list_strategies(workspace_id: uuid.UUID, user: CurrentUser, db: DB) -> list[StrategyDefinition]:
    require_workspace(db, user, workspace_id)
    return list(
        db.scalars(
            select(StrategyDefinition)
            .where(StrategyDefinition.workspace_id == workspace_id)
            .order_by(StrategyDefinition.created_at.desc())
        )
    )


@router.post("/workspaces/{workspace_id}/strategies", response_model=StrategyOut, status_code=201)
def create_strategy_def(
    workspace_id: uuid.UUID, body: StrategyIn, user: CurrentUser, db: DB, request: Request
) -> StrategyDefinition:
    require_workspace(db, user, workspace_id, Role.editor)
    params = validate_strategy(body.strategy_key, body.params)
    s = StrategyDefinition(
        workspace_id=workspace_id,
        name=body.name,
        strategy_key=body.strategy_key,
        params=params,
        description=body.description,
        created_by=user.id,
    )
    db.add(s)
    db.flush()
    audit(
        db,
        request,
        "strategy.create",
        user=user,
        workspace_id=workspace_id,
        target_type="strategy",
        target_id=s.id,
    )
    db.commit()
    return s


@router.delete("/workspaces/{workspace_id}/strategies/{strategy_id}", status_code=204)
def delete_strategy_def(
    workspace_id: uuid.UUID, strategy_id: uuid.UUID, user: CurrentUser, db: DB, request: Request
) -> None:
    require_workspace(db, user, workspace_id, Role.editor)
    s = db.get(StrategyDefinition, strategy_id)
    if s is None or s.workspace_id != workspace_id:
        raise ApiError(404, "not_found", "Strategy not found")
    db.delete(s)
    audit(
        db,
        request,
        "strategy.delete",
        user=user,
        workspace_id=workspace_id,
        target_type="strategy",
        target_id=strategy_id,
    )
    db.commit()
