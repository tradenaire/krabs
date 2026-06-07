from __future__ import annotations

import secrets
from typing import MutableMapping

from bot.signals.model import ParsedSignal


_STORE_KEY = "pending_signals"


def save_signal(user_data: MutableMapping, signal: ParsedSignal) -> str:
    signals = user_data.setdefault(_STORE_KEY, {})
    signal_id = secrets.token_hex(4)
    signals[signal_id] = signal
    return signal_id


def get_signal(user_data: MutableMapping, signal_id: str) -> ParsedSignal | None:
    signals = user_data.get(_STORE_KEY) or {}
    return signals.get(signal_id)


def clear_signal(user_data: MutableMapping, signal_id: str) -> None:
    signals = user_data.get(_STORE_KEY)
    if not signals:
        return
    signals.pop(signal_id, None)
