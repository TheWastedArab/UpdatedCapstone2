"""Runtime-adjustable settings: database value first, environment default second (NFR-14)."""
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import SystemSetting


def duplicate_threshold(db: Session) -> float:
    row = db.get(SystemSetting, "duplicate_threshold")
    return float(row.value) if row is not None else get_settings().duplicate_threshold


def severity_labels(db: Session) -> list[str]:
    row = db.get(SystemSetting, "severity_labels")
    return list(row.value) if row is not None else get_settings().severity_label_list


def set_value(db: Session, key: str, value, actor_id: int) -> None:
    row = db.get(SystemSetting, key)
    if row is None:
        db.add(SystemSetting(key=key, value=value, updated_by=actor_id))
    else:
        row.value = value
        row.updated_by = actor_id
