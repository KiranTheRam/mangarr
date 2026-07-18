"""Outbound webhook notifications.

Fires a small JSON POST when chapters are imported so a request manager
(e.g. NextPanel) can track availability without waiting for its next poll.
Delivery is fire-and-forget: a dead webhook endpoint must never block or
fail a download.
"""

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

APP_NAME = "mangarr"
TIMEOUT = 10.0


async def send_webhook(url: str, secret: str, payload: dict) -> bool:
    headers = {"X-Webhook-Secret": secret} if secret else {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("webhook to %s failed: %s", url, exc)
        return False
    if resp.status_code >= 400:
        log.warning("webhook to %s returned %d", url, resp.status_code)
        return False
    return True


def notify_import(values: dict[str, str], series_id: int, detail: str = "") -> None:
    """Schedule an import notification if webhooks are configured."""
    if values.get("webhook_enabled") != "true":
        return
    url = values.get("webhook_url", "").strip()
    if not url:
        return
    payload = {
        "app": APP_NAME,
        "event": "import",
        "series_id": series_id,
        "detail": detail,
    }
    asyncio.get_running_loop().create_task(
        send_webhook(url, values.get("webhook_secret", ""), payload)
    )
