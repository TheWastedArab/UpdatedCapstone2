"""Registration, email verification, login, logout and own-profile management (FR-01, FR-02, FR-04)."""
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import DbSession, utcnow
from ..deps import CurrentUser, get_auth, get_current_user
from ..models import ROLE_ADMIN, ROLE_REPORTER, DeveloperProfile, User, UserSession
from ..schemas import (
    EmailIn, LoginIn, LoginOut, Message, PasswordChangeIn, ProfileOut, ProfileUpdateIn, RegisterIn, RegisterOut,
    UserOut, VerifyEmailIn,
)
from ..security import (
    create_token, hash_code, hash_password, new_session_id, new_verification_code, verify_password,
)
from ..services import profiles
from ..services.email import send_email

router = APIRouter(tags=["Authentication and account"])


def _issue_code(user: User, background: BackgroundTasks, to: str) -> str:
    settings = get_settings()
    code = new_verification_code()
    user.verification_code_hash = hash_code(code)
    user.verification_expires_at = utcnow() + timedelta(hours=settings.verification_code_hours)
    background.add_task(
        send_email, to, "Your Bug Tracker verification code",
        f"Your verification code is {code}\n\nEnter it in the Bug Tracker panel in VS Code. "
        f"It expires in {settings.verification_code_hours} hours.",
    )
    return code


def _code_valid(user: User, code: str) -> bool:
    return (
        user.verification_code_hash is not None
        and user.verification_expires_at is not None
        and user.verification_expires_at > utcnow()
        and user.verification_code_hash == hash_code(code)
    )


@router.post("/auth/register", response_model=RegisterOut, status_code=201, summary="Create an account")
def register(data: RegisterIn, background: BackgroundTasks, db: Session = DbSession):
    email = data.email.lower()
    if db.scalar(select(User.id).where(func.lower(User.email) == email)) is not None:
        raise HTTPException(409, "An account with this email already exists")
    # The first account on a fresh installation becomes the Administrator.
    first = db.scalar(select(func.count(User.id))) == 0
    user = User(
        email=email, display_name=data.display_name, password_hash=hash_password(data.password),
        role=ROLE_ADMIN if first else ROLE_REPORTER,
    )
    db.add(user)
    code = _issue_code(user, background, email)
    db.commit()
    return RegisterOut(
        message="Account created. Enter the verification code sent to your email to activate it.",
        verification_code=code if get_settings().expose_verification_code else None,
    )


@router.post("/auth/verify-email", response_model=Message, summary="Activate an account with the emailed code")
def verify_email(data: VerifyEmailIn, db: Session = DbSession):
    user = db.scalar(select(User).where(func.lower(User.email) == data.email.lower()))
    if user is None or user.email_verified or not _code_valid(user, data.code):
        raise HTTPException(400, "The verification code is invalid or has expired")
    user.email_verified = True
    user.verification_code_hash = user.verification_expires_at = None
    db.commit()
    return Message(message="Email verified. You can now sign in.")


@router.post("/auth/resend-verification", response_model=RegisterOut, summary="Send a new verification code")
def resend_verification(data: EmailIn, background: BackgroundTasks, db: Session = DbSession):
    user = db.scalar(select(User).where(func.lower(User.email) == data.email.lower()))
    code = None
    if user is not None and not user.email_verified:
        code = _issue_code(user, background, user.email)
        db.commit()
    # Same answer whether or not the account exists.
    return RegisterOut(
        message="If that account exists and is not yet verified, a new code has been sent.",
        verification_code=code if get_settings().expose_verification_code else None,
    )


@router.post("/auth/login", response_model=LoginOut, summary="Sign in and receive a session token")
def login(data: LoginIn, db: Session = DbSession):
    user = db.scalar(select(User).where(func.lower(User.email) == data.email.lower()))
    if user is None or not verify_password(data.password, user.password_hash) or not user.is_active:
        raise HTTPException(401, "Incorrect email or password")
    if not user.email_verified:
        raise HTTPException(403, "Email address not verified yet")
    session = UserSession(id=new_session_id(), user_id=user.id)
    db.add(session)
    db.commit()
    return LoginOut(access_token=create_token(user.id, session.id), user=UserOut.model_validate(user))


@router.post("/auth/logout", response_model=Message, summary="End the current session")
def logout(auth: CurrentUser = Depends(get_auth), db: Session = DbSession):
    auth.session.revoked = True
    db.commit()
    return Message(message="Signed out")


@router.get("/users/me", response_model=UserOut, summary="Current user")
def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/users/me", response_model=UserOut, summary="Update display name, email or notification preferences")
def update_me(data: ProfileUpdateIn, background: BackgroundTasks, user: User = Depends(get_current_user),
              db: Session = DbSession):
    if data.display_name is not None:
        user.display_name = data.display_name
    if data.notification_prefs is not None:
        user.notification_prefs = {**(user.notification_prefs or {}), **data.notification_prefs}
    if data.email is not None and data.email.lower() != user.email:
        new_email = data.email.lower()
        if db.scalar(select(User.id).where(func.lower(User.email) == new_email)) is not None:
            raise HTTPException(409, "An account with this email already exists")
        # The address only changes once the code sent to the new address is confirmed.
        user.pending_email = new_email
        _issue_code(user, background, new_email)
    db.commit()
    return user


@router.post("/users/me/confirm-email", response_model=UserOut, summary="Confirm a changed email address")
def confirm_email(data: VerifyEmailIn, user: User = Depends(get_current_user), db: Session = DbSession):
    if user.pending_email is None or data.email.lower() != user.pending_email or not _code_valid(user, data.code):
        raise HTTPException(400, "The verification code is invalid or has expired")
    user.email, user.pending_email = user.pending_email, None
    user.verification_code_hash = user.verification_expires_at = None
    db.commit()
    return user


@router.post("/users/me/password", response_model=Message, summary="Change password")
def change_password(data: PasswordChangeIn, auth: CurrentUser = Depends(get_auth), db: Session = DbSession):
    if not verify_password(data.current_password, auth.user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    auth.user.password_hash = hash_password(data.new_password)
    # Every other session is ended when the password changes.
    for session in db.scalars(select(UserSession).where(UserSession.user_id == auth.user.id, UserSession.id != auth.session.id)):
        session.revoked = True
    db.commit()
    return Message(message="Password changed")


@router.get("/users/me/developer-profile", response_model=ProfileOut | None, summary="Own developer profile, if any")
def my_developer_profile(user: User = Depends(get_current_user), db: Session = DbSession):
    profile = db.scalar(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
    return profiles.to_out(db, profile, with_history=True) if profile else None
