"""Email delivery (FR-16). Backend is chosen by BT_EMAIL_BACKEND: console, smtp or memory."""
import logging
import smtplib
from email.message import EmailMessage

from ..config import get_settings

log = logging.getLogger("bugtracker.email")
# Captured messages for the "memory" backend (tests).
sent_messages: list[dict] = []


def send_email(to: str, subject: str, body: str) -> bool:
    settings = get_settings()
    backend = settings.email_backend
    if backend == "memory":
        sent_messages.append({"to": to, "subject": subject, "body": body})
        return True
    if backend == "console":
        log.info("EMAIL to=%s subject=%s\n%s", to, subject, body)
        return True
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("Email to %s failed: %s", to, exc)
        return False
