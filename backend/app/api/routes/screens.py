from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.state import models
from app.state.schemas import ScreenCreate, ScreenRead

router = APIRouter(prefix="/api/screens", tags=["screens"])


@router.get("", response_model=list[ScreenRead])
def list_screens(db: Session = Depends(get_db)) -> list[models.Screen]:
    return db.query(models.Screen).order_by(models.Screen.name).all()


@router.post("", response_model=ScreenRead, status_code=201)
def create_screen(body: ScreenCreate, db: Session = Depends(get_db)) -> models.Screen:
    existing = db.query(models.Screen).filter_by(name=body.name).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"screen '{body.name}' already exists")
    screen = models.Screen(
        name=body.name,
        filters=[f.model_dump() for f in body.filters],
        rank_by=body.rank_by,
        rank_desc=body.rank_desc,
    )
    db.add(screen)
    db.commit()
    db.refresh(screen)
    return screen


@router.delete("/{screen_id}", status_code=204)
def delete_screen(screen_id: int, db: Session = Depends(get_db)) -> None:
    screen = db.get(models.Screen, screen_id)
    if screen is None:
        raise HTTPException(status_code=404, detail="screen not found")
    db.delete(screen)
    db.commit()
