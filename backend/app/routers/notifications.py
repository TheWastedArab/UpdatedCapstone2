"""In-app alerts (FR-17) and AI service status."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..ai.engine import engine
from ..database import DbSession
from ..deps import FORBIDDEN, get_current_user
from ..models import Notification, User
from ..models import STATUSES
from ..schemas import AIStatusOut, Message, MetaOut, NotificationOut, UnreadOut
from ..services import settings_service

router = APIRouter(tags=["Notifications"])


@router.get("/notifications", response_model=list[NotificationOut], summary="Recent notifications, newest first")
def list_notifications(unread_only: bool = False, limit: int = Query(30, ge=1, le=100),
                       user: User = Depends(get_current_user), db: Session = DbSession):
    query = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        query = query.where(Notification.read.is_(False))
    return list(db.scalars(query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)))


@router.get("/notifications/unread-count", response_model=UnreadOut, summary="Number shown on the unread badge")
def unread_count(user: User = Depends(get_current_user), db: Session = DbSession):
    count = db.scalar(select(func.count(Notification.id)).where(Notification.user_id == user.id, Notification.read.is_(False)))
    return UnreadOut(unread=count or 0)


@router.post("/notifications/read-all", response_model=Message, summary="Mark every notification as read")
def read_all(user: User = Depends(get_current_user), db: Session = DbSession):
    db.execute(update(Notification).where(Notification.user_id == user.id, Notification.read.is_(False)).values(read=True))
    db.commit()
    return Message(message="All notifications marked as read")


@router.post("/notifications/{notification_id}/read", response_model=Message, summary="Mark one notification as read")
def read_one(notification_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    notification = db.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        raise FORBIDDEN
    notification.read = True
    db.commit()
    return Message(message="Marked as read")


@router.get("/ai/status", response_model=AIStatusOut, tags=["Bugs and triage"], summary="Which AI modules are loaded")
def ai_status(user: User = Depends(get_current_user)):
    return engine.status()


@router.get("/meta", response_model=MetaOut, tags=["Bugs and triage"], summary="Configured severity labels and the status vocabulary")
def meta(user: User = Depends(get_current_user), db: Session = DbSession):
    return MetaOut(severity_labels=settings_service.severity_labels(db), statuses=list(STATUSES))
