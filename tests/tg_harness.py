from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace


class FakeTelegramMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.chat = SimpleNamespace(type="private")
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return FakeTelegramStatus(text)


class FakeTelegramStatus:
    def __init__(self, text: str):
        self.text = text
        self.edits: list[tuple[str, dict]] = []
        self.deleted = False

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))
        self.text = text

    async def delete(self):
        self.deleted = True


class FakeCallbackQuery:
    def __init__(self, data: str, message: FakeTelegramMessage | None = None):
        self.data = data
        self.message = message or FakeTelegramMessage()
        self.answers: list[tuple[tuple, dict]] = []
        self.edits: list[tuple[str, dict]] = []
        self.deleted = False

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))

    async def delete_message(self):
        self.deleted = True


@dataclass
class FakeTelegramUpdate:
    message: FakeTelegramMessage | None = None
    callback_query: FakeCallbackQuery | None = None


@dataclass
class FakeTelegramApplication:
    bot_data: dict = field(default_factory=dict)


@dataclass
class FakeTelegramContext:
    args: list[str] = field(default_factory=list)
    bot_data: dict = field(default_factory=dict)
    user_data: dict = field(default_factory=dict)
    application: FakeTelegramApplication | None = None

    def __post_init__(self):
        if self.application is None:
            self.application = FakeTelegramApplication(self.bot_data)
        else:
            self.application.bot_data = self.bot_data


class FakeConfig(SimpleNamespace):
    default_trade_usdt: float = 1.0
    default_leverage: int = 5
    tp_pct: float = 500.0
    sl_pct: float = 500.0
    averaging_amount: float = 0.10
    max_averaging_count: int = 3
    averaging_threshold: float = -100.0
    averaging_interval: int = 30
    max_reentry_cycles: int = 3
    auto_scan_capital_pct: float = 0.0


class FakeFuturesClient:
    def futures_symbol(self, symbol: str) -> str:
        if "/" in symbol:
            return symbol
        return f"{symbol}/USDT:USDT"

    async def get_funding_rate(self, symbol: str):
        return {"rate": 0.0}

    async def get_free_futures_balance(self):
        return 1000.0

    async def get_min_order_usdt(self, symbol: str, leverage: int, min_notional=None):
        return 0.01

    async def get_max_leverage(self, symbol: str):
        return 5
