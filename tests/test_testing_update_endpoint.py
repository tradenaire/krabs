from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class FakeRequest:
    def __init__(self, payload=None, headers=None, app=None):
        self._payload = payload if payload is not None else {"update_id": 123}
        self.headers = headers or {}
        self.app = app or {}

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeApplication:
    def __init__(self):
        self.bot = object()
        self.bot_data = {}
        self.processed = []

    async def process_update(self, update):
        self.processed.append(update)


class TestingUpdateEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_missing_secret_token(self):
        from bot.testing_update_endpoint import handle_test_update

        app = FakeApplication()
        request = FakeRequest(
            headers={},
            app={"krabs_application": app, "krabs_secret": "secret"},
        )

        response = await handle_test_update(request)

        self.assertEqual(response.status, 403)
        self.assertEqual(app.processed, [])

    async def test_accepts_telegram_update_json_and_processes_it(self):
        from bot.testing_update_endpoint import handle_test_update

        app = FakeApplication()
        request = FakeRequest(
            payload={"update_id": 777, "message": {"message_id": 1}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            app={"krabs_application": app, "krabs_secret": "secret"},
        )
        fake_update = SimpleNamespace(update_id=777)

        with patch("bot.testing_update_endpoint.Update.de_json", return_value=fake_update) as de_json:
            response = await handle_test_update(request)

        self.assertEqual(response.status, 200)
        de_json.assert_called_once_with({"update_id": 777, "message": {"message_id": 1}}, app.bot)
        self.assertEqual(app.processed, [fake_update])

    async def test_returns_bad_request_for_invalid_json(self):
        from bot.testing_update_endpoint import handle_test_update

        app = FakeApplication()
        request = FakeRequest(
            payload=ValueError("not json"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
            app={"krabs_application": app, "krabs_secret": "secret"},
        )

        response = await handle_test_update(request)

        self.assertEqual(response.status, 400)
        self.assertEqual(app.processed, [])

    async def test_maybe_start_skips_when_no_secret_configured(self):
        from bot.testing_update_endpoint import maybe_start_test_update_endpoint

        app = FakeApplication()
        config = SimpleNamespace(test_update_endpoint_token="", test_update_endpoint_host="127.0.0.1", test_update_endpoint_port=8787)

        runner = await maybe_start_test_update_endpoint(app, config)

        self.assertIsNone(runner)
        self.assertNotIn("test_update_endpoint_runner", app.bot_data)


if __name__ == "__main__":
    unittest.main()
