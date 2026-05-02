"""Tests для apply-if-changed поведения wizard EXIT/Готово.

Жалоба пользователя 02.05.2026 23:18: «если я в визарде хоть какое-то значение
меняю а потом жму выход автоматически все что я в визарде делал должно
применяться. Я не хочу всех 12 вопросов отвечать.»

До фикса: ✖️ EXIT / 'отмена' тихо отбрасывали wizard state.
После: если wizard["changed"] непуст → spec.finish (apply), иначе обычная отмена.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

import bot.handlers.wizard as wizard_mod
from bot.handlers.wizard import _exit_wizard, register, Step, _wizard_key


@pytest.fixture
def wizard_registered():
    """Регистрирует одноразовый wizard 'test_exit' с моком finish для тестов."""
    finish_mock = AsyncMock()
    steps = [
        Step(key="bet", attr="default_trade_usdt", prompt="Маржа", kind="float", unit="$"),
        Step(key="lev", attr="default_leverage", prompt="Плечо", kind="int:0:200", unit="#"),
    ]
    register("test_exit", steps, finish_mock)
    yield finish_mock
    # Cleanup: removing test wizard from registry to keep tests isolated.
    wizard_mod._REGISTRY.pop("test_exit", None)


def _ctx_with_wizard(state: dict):
    """Build mock context with active wizard state."""
    ctx = MagicMock()
    ctx.user_data = {_wizard_key("test_exit"): state}
    ctx.bot_data = {"config": MagicMock()}
    return ctx


# ── Apply path: changed непуст ───────────────────────────────────


@pytest.mark.asyncio
async def test_exit_with_changes_calls_finish(wizard_registered):
    """Если в changed что-то есть — вызвать spec.finish, не молча отменить."""
    finish = wizard_registered
    state = {
        "step": 1,
        "values": {"lev": 100},
        "changed": {"lev": 100},
        "initial": {},
    }
    ctx = _ctx_with_wizard(state)
    reply = AsyncMock()

    await _exit_wizard(ctx, "test_exit", chat_id=42, reply=reply)

    # finish был вызван с теми же changes
    finish.assert_called_once()
    args = finish.call_args.args
    assert args[1] == 42  # chat_id
    assert args[2]["changed"] == {"lev": 100}
    # reply НЕ вызван — apply-ветка не шлёт «Отменено»
    reply.assert_not_called()
    # wizard state очищен
    assert ctx.user_data.get(_wizard_key("test_exit")) is None


@pytest.mark.asyncio
async def test_exit_with_partial_changes_applies_them(wizard_registered):
    """Пользователь ввёл значения только на части шагов — применяем именно их."""
    finish = wizard_registered
    state = {
        "step": 0,
        "values": {"bet": 2.5},  # ввёл только маржу, остальное не дошёл
        "changed": {"bet": 2.5},
        "initial": {},
    }
    ctx = _ctx_with_wizard(state)
    await _exit_wizard(ctx, "test_exit", chat_id=42, reply=AsyncMock())

    finish.assert_called_once()
    assert finish.call_args.args[2]["changed"] == {"bet": 2.5}


# ── Cancel path: changed пуст ────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_without_changes_cancels(wizard_registered):
    """Если ничего не изменено — обычная отмена (finish НЕ вызывается)."""
    finish = wizard_registered
    state = {
        "step": 0,
        "values": {},
        "changed": {},
        "initial": {},
    }
    ctx = _ctx_with_wizard(state)
    reply = AsyncMock()

    await _exit_wizard(ctx, "test_exit", chat_id=42, reply=reply)

    finish.assert_not_called()
    reply.assert_called_once()
    # Сообщение начинается с ✖️
    assert "Отменено" in reply.call_args.args[0]
    assert ctx.user_data.get(_wizard_key("test_exit")) is None


@pytest.mark.asyncio
async def test_exit_when_wizard_already_gone(wizard_registered):
    """Защита от race: wizard уже pop'нут — _exit_wizard не падает."""
    finish = wizard_registered
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = {}
    reply = AsyncMock()

    # Не должно быть exception
    await _exit_wizard(ctx, "test_exit", chat_id=42, reply=reply)
    finish.assert_not_called()
    reply.assert_not_called()
