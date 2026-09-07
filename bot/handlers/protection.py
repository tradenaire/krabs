"""/repair_tpsl: audit first; repair only the explicitly confirmed managed position."""
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


async def repair_tpsl_handler(update, context):
    if len(context.args or []) != 1:
        await update.message.reply_text("/repair_tpsl SYMBOL — проверить TP/SL и показать восстановление")
        return
    context.user_data.pop("protection_repair", None)
    try:
        audit = await context.bot_data["exchange"].audit_protection(context.args[0])
        if audit["status"] == "UNMANAGED":
            await update.message.reply_text("Ручная или неподтверждённая позиция. Сначала /adopt SYMBOL.")
            return
        lines = [f"{audit['symbol']} · позиция {audit['position_id']}"]
        for kind in ("TP", "SL"):
            lines.append(f"{kind}: {audit['prices'][kind]:.8g} — " +
                         ("подтверждён" if audit["legs"][kind] else "корректный ордер не найден"))
        if audit["status"] == "CONFIRMED":
            await update.message.reply_text("\n".join(lines))
            return
        nonce = secrets.token_hex(8)
        context.user_data["protection_repair"] = {"nonce": nonce, "expires": time.time() + 300, "audit": audit}
        lines.append("Подтверждение восстановит защиту этой позиции по указанным уровням.")
        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Восстановить TP/SL", callback_data=f"repair_tpsl_{nonce}")]]))
    except Exception as error:
        await update.message.reply_text(f"Проверка защиты не завершена: {error}")


async def repair_tpsl_callback(update, context):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("protection_repair")
    if not pending or query.data != f"repair_tpsl_{pending['nonce']}" or time.time() > pending["expires"]:
        await query.edit_message_text("Подтверждение устарело. Повторите /repair_tpsl SYMBOL.")
        return
    context.user_data.pop("protection_repair")  # consume before awaiting any mutation
    audit = pending["audit"]
    try:
        result = await context.bot_data["exchange"].set_tp_sl(audit["symbol"],
            tp_price=audit["prices"]["TP"], sl_price=audit["prices"]["SL"],
            expected_snapshot=audit["snapshot"])
        await query.edit_message_text("Защита подтверждена: " + ", ".join(
            f"{r['type']} {r['price']:.8g} (ордер {r['id']})" for r in result))
    except Exception as error:
        await query.edit_message_text(f"Восстановление защиты не подтверждено: {error}")
