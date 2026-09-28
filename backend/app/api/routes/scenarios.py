from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.engine import projection
from app.state import models
from app.state.schemas import (
    GoalRequest,
    ScenarioCreate,
    ScenarioParams,
    ScenarioRead,
    ScenarioRunRequest,
)

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


def _to_config(params: ScenarioParams) -> projection.ProjectionConfig:
    return projection.ProjectionConfig(
        start_value=params.start_value,
        monthly_contribution=params.monthly_contribution,
        annual_step_up_pct=params.annual_step_up_pct,
        expected_return=params.expected_return,
        volatility=params.volatility,
        years=params.years,
        inflation=params.inflation,
        assets=[a.model_dump() for a in params.assets],
    )


def _run(params: ScenarioParams, num_paths: int, seed: int | None) -> dict:
    cfg = _to_config(params)
    return {
        "projection": projection.project(cfg),
        "monte_carlo": projection.monte_carlo(cfg, num_paths=num_paths, seed=seed),
    }


@router.get("", response_model=list[ScenarioRead])
def list_scenarios(db: Session = Depends(get_db)) -> list[models.Scenario]:
    return db.query(models.Scenario).order_by(models.Scenario.name).all()


@router.post("", response_model=ScenarioRead, status_code=201)
def create_scenario(body: ScenarioCreate, db: Session = Depends(get_db)) -> models.Scenario:
    existing = db.query(models.Scenario).filter_by(name=body.name).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"scenario '{body.name}' already exists")
    scenario = models.Scenario(name=body.name, params=body.params.model_dump())
    db.add(scenario)
    db.commit()
    db.refresh(scenario)
    return scenario


@router.put("/{scenario_id}", response_model=ScenarioRead)
def update_scenario(
    scenario_id: int, body: ScenarioCreate, db: Session = Depends(get_db)
) -> models.Scenario:
    scenario = db.get(models.Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    conflict = (
        db.query(models.Scenario)
        .filter_by(name=body.name)
        .filter(models.Scenario.id != scenario_id)
        .first()
    )
    if conflict is not None:
        raise HTTPException(status_code=409, detail=f"scenario '{body.name}' already exists")
    scenario.name = body.name
    scenario.params = body.params.model_dump()
    db.commit()
    db.refresh(scenario)
    return scenario


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: int, db: Session = Depends(get_db)) -> None:
    scenario = db.get(models.Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    db.delete(scenario)
    db.commit()


@router.post("/preview")
def preview_scenario(req: ScenarioRunRequest) -> dict:
    return _run(req.params, req.num_paths, req.seed)


@router.post("/{scenario_id}/run")
def run_scenario(
    scenario_id: int, num_paths: int = 1000, seed: int | None = None, db: Session = Depends(get_db)
) -> dict:
    scenario = db.get(models.Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return _run(ScenarioParams(**scenario.params), num_paths, seed)


@router.post("/goal")
def goal_scenario(req: GoalRequest) -> dict:
    cfg = projection.ProjectionConfig(
        years=req.years,
        expected_return=req.expected_return,
        annual_step_up_pct=req.annual_step_up_pct,
        inflation=req.inflation,
    )
    required = projection.required_monthly_contribution(req.target, cfg)
    nominal = projection.project(replace(cfg, monthly_contribution=required or 0.0))[-1]["value"]
    return {
        "target": req.target,
        "years": req.years,
        "required_monthly_contribution": required,
        "projected_value": nominal,
    }
