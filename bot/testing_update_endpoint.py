from __future__ import annotations

import hmac
import logging
import os

from aiohttp import web
from telegram import Update

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _configured_token(config) -> str:
    return (
        os.environ.get("KRABS_TEST_UPDATE_TOKEN")
        or str(getattr(config, "test_update_endpoint_token", "") or "")
    ).strip()


def _configured_host(config) -> str:
    return (
        os.environ.get("KRABS_TEST_UPDATE_HOST")
        or str(getattr(config, "test_update_endpoint_host", "127.0.0.1") or "127.0.0.1")
    ).strip()


def _configured_port(config) -> int:
    raw = os.environ.get("KRABS_TEST_UPDATE_PORT") or getattr(config, "test_update_endpoint_port", 8787)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 8787


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def handle_test_update(request: web.Request) -> web.Response:
    secret = request.app["krabs_secret"]
    provided = request.headers.get(SECRET_HEADER, "")
    if not provided or not hmac.compare_digest(provided, secret):
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    application = request.app["krabs_application"]
    try:
        update = Update.de_json(payload, application.bot)
        await application.process_update(update)
    except Exception as exc:
        logger.exception("test update endpoint failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    return web.json_response({"ok": True, "update_id": getattr(update, "update_id", None)})


def build_test_update_web_app(application, secret: str) -> web.Application:
    app = web.Application()
    app["krabs_application"] = application
    app["krabs_secret"] = secret
    app.router.add_get("/health", handle_health)
    app.router.add_post("/telegram/update", handle_test_update)
    return app


async def maybe_start_test_update_endpoint(application, config):
    token = _configured_token(config)
    if not token:
        return None

    host = _configured_host(config)
    port = _configured_port(config)
    web_app = build_test_update_web_app(application, token)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    application.bot_data["test_update_endpoint_runner"] = runner
    application.bot_data["test_update_endpoint_url"] = f"http://{host}:{port}/telegram/update"
    logger.warning("Test update endpoint listening on http://%s:%s/telegram/update", host, port)
    return runner


async def stop_test_update_endpoint(application) -> None:
    runner = application.bot_data.pop("test_update_endpoint_runner", None)
    application.bot_data.pop("test_update_endpoint_url", None)
    if runner is not None:
        await runner.cleanup()
