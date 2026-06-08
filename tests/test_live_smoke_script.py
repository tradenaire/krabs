from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch


class LiveSmokeScriptTests(unittest.TestCase):
    def test_build_smoke_text_uses_real_formatters_without_mojibake(self):
        from tools.live_smoke_telegram import MOJIBAKE_MARKERS, build_smoke_text, assert_clean_text

        text = build_smoke_text("788796e")
        assert_clean_text(text)

        self.assertIn("LIVE SMOKE 788796e", text)
        self.assertIn("Сделок не открывал", text)
        self.assertIn("Причина: ордер сработал бы сразу", text)
        self.assertIn("Причина: ручное закрытие", text)
        self.assertIn("Перезаход: нет", text)
        for marker in MOJIBAKE_MARKERS:
            self.assertNotIn(marker, text)

    def test_json_request_is_utf8_markdown_payload(self):
        from tools.live_smoke_telegram import build_send_message_request

        req = build_send_message_request(
            "TOKEN",
            chat_id="252422856",
            text="✅ *HYPE* Причина: ручное закрытие",
        )

        self.assertEqual(req.full_url, "https://api.telegram.org/botTOKEN/sendMessage")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.headers["Content-type"], "application/json; charset=utf-8")
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], "252422856")
        self.assertEqual(body["parse_mode"], "Markdown")
        self.assertEqual(body["text"], "✅ *HYPE* Причина: ручное закрытие")

    def test_run_smoke_posts_update_and_sends_clean_summary(self):
        from tools.live_smoke_telegram import SmokeConfig, run_smoke

        responses = [
            {"ok": True, "update_id": 900001},
            {"ok": True, "result": {"message_id": 707}},
        ]
        captured = []

        def fake_json_request(req, timeout=15):
            captured.append(req)
            return responses.pop(0)

        with patch("tools.live_smoke_telegram.read_telegram_token", return_value="TOKEN"), \
             patch("tools.live_smoke_telegram.json_request", fake_json_request), \
             patch("tools.live_smoke_telegram.current_head", return_value="788796e"):
            result = run_smoke(SmokeConfig(
                chat_id="252422856",
                endpoint_url="http://127.0.0.1:8787/telegram/update",
                test_update_token="secret",
            ))

        self.assertTrue(result["endpoint_ok"])
        self.assertTrue(result["send_message_ok"])
        self.assertEqual(result["message_id"], 707)
        self.assertEqual(captured[0].headers["X-telegram-bot-api-secret-token"], "secret")
        self.assertEqual(captured[1].headers["Content-type"], "application/json; charset=utf-8")
        send_body = json.loads(captured[1].data.decode("utf-8"))
        self.assertEqual(send_body["parse_mode"], "Markdown")
        self.assertNotIn("????", send_body["text"])
        self.assertNotIn("Рџ", send_body["text"])


if __name__ == "__main__":
    unittest.main()
