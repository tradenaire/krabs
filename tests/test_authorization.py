import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from telegram.ext import ApplicationHandlerStop

from bot.config import Config
from bot.lifecycle import authorize_update


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(bot_data={
            "config": Config(allowed_user_ids=[1]),
        })
        self.env = patch.dict(os.environ, {"KRABS_DIAGNOSTIC_BOT_ID": "5647955535"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_auth(self, *, user_id, is_bot=False, text=None, chat_type="private",
                 update_kind="message", entities=None):
        user = SimpleNamespace(id=user_id, is_bot=is_bot)
        if entities is None and text in {"/positions", "/balance"}:
            entities = [SimpleNamespace(type="bot_command", offset=0, length=len(text))]
        message = SimpleNamespace(text=text, entities=entities,
                                  chat=SimpleNamespace(type=chat_type))
        update = SimpleNamespace(effective_user=user, message=message)
        if update_kind != "message":
            update.message = None
            setattr(update, update_kind, message)
        return asyncio.run(authorize_update(update, self.context))

    def test_diagnostic_allowlist_is_read_only_and_exact(self):
        for command in ("/positions", "/balance"):
            self.run_auth(user_id=5647955535, is_bot=True, text=command)

        denied = (
            {"text": "/positions now"},
            {"text": "positions"},
        ) + tuple({"text": command} for command in (
                "/start", "/close", "/short", "/repair_tpsl", "/adopt", "/ask", "/pin"
            ))
        denied += (
            {"text": "/balance", "chat_type": "group"},
            {"text": "/balance", "update_kind": "edited_message"},
            {"text": "/balance", "update_kind": "channel_post"},
            {"text": "/balance", "update_kind": "business_message"},
            {"text": "/balance", "update_kind": "callback_query"},
            {"text": "/balance", "entities": []},
            {"text": "/balance", "entities": [SimpleNamespace(type="bot_command", offset=1, length=8)]},
            {"text": "/balance", "is_bot": False},
            {"text": "/balance", "user_id": 2},
        )
        for case in denied:
            with self.assertRaises(ApplicationHandlerStop):
                self.run_auth(user_id=case.get("user_id", 5647955535),
                              is_bot=case.get("is_bot", True),
                              text=case["text"], chat_type=case.get("chat_type", "private"),
                              update_kind=case.get("update_kind", "message"),
                              entities=case.get("entities"))

    def test_normal_user_path_is_preserved_but_diagnostic_id_is_restricted(self):
        self.run_auth(user_id=1, text=None)
        self.context.bot_data["config"].allowed_user_ids.append(5647955535)
        with self.assertRaises(ApplicationHandlerStop):
            self.run_auth(user_id=5647955535, is_bot=True, text="/start")


if __name__ == "__main__":
    unittest.main()
