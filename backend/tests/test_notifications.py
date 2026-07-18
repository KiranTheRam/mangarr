import asyncio

import pytest
import respx
from httpx import Response

from mangarr import notifications


@respx.mock
async def test_send_webhook_posts_secret_header():
    route = respx.post("http://panel.test/api/v1/webhooks/mangarr").mock(
        return_value=Response(204)
    )
    ok = await notifications.send_webhook(
        "http://panel.test/api/v1/webhooks/mangarr", "s3cret",
        {"app": "mangarr", "event": "import", "series_id": 7},
    )
    assert ok is True
    request = route.calls[0].request
    assert request.headers["x-webhook-secret"] == "s3cret"
    assert b'"series_id": 7' in request.content or b'"series_id":7' in request.content


@respx.mock
async def test_send_webhook_swallows_errors():
    respx.post("http://panel.test/hook").mock(return_value=Response(401))
    ok = await notifications.send_webhook("http://panel.test/hook", "", {"event": "import"})
    assert ok is False


@respx.mock
async def test_notify_import_fire_and_forget():
    route = respx.post("http://panel.test/hook").mock(return_value=Response(204))
    values = {
        "webhook_enabled": "true",
        "webhook_url": "http://panel.test/hook",
        "webhook_secret": "s",
    }
    notifications.notify_import(values, 42, "Chapter 12")
    await asyncio.sleep(0.05)
    assert route.called


async def test_notify_import_disabled_schedules_nothing():
    # would raise if it tried to reach the (unmocked) network
    notifications.notify_import({"webhook_enabled": "false", "webhook_url": "http://x"}, 1)
    notifications.notify_import({"webhook_enabled": "true", "webhook_url": ""}, 1)
    await asyncio.sleep(0.01)
