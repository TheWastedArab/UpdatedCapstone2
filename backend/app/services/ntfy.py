"""Push notifications through a self-hosted ntfy server (Section 3.3.2)."""
import logging

import httpx

from ..config import get_settings

log = logging.getLogger("bugtracker.ntfy")


def topic_for_project(project_id: int) -> str:
    return f"{get_settings().ntfy_topic_prefix}-project-{project_id}"


def publish(topic: str, title: str, message: str, priority: str = "urgent", tags: str = "rotating_light") -> bool:
    settings = get_settings()
    if not settings.ntfy_url:
        return False
    # HTTP headers are latin-1; keep the title safe and put the full text in the body.
    headers = {"Title": title.encode("ascii", "replace").decode("ascii"), "Priority": priority, "Tags": tags}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        resp = httpx.post(
            f"{settings.ntfy_url.rstrip('/')}/{topic}", content=message.encode("utf-8"), headers=headers, timeout=5
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("ntfy publish failed: %s", exc)
        return False
