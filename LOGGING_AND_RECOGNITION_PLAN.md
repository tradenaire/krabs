# Logging and Recognition Plan

## Цель

Добавить полноценное логирование всех значимых событий бота в каждый момент времени, чтобы можно было восстановить полную картину: что пришло в Telegram, какое состояние было на бирже, какие raw-ответы пришли от API, какие решения принял бот и какие действия он выполнил.

В проекте уже есть базовое логирование, но его нужно расширить и сделать более системным.

## Что Нужно Логировать

### Входящие Telegram-события

Логировать каждый входящий update от пользователя/Telegram:

```text
timestamp
update_id
chat_id
user_id
username
message_id
message text / callback data / command
raw update payload
```

Важно: секреты, токены, API keys и приватные данные нельзя писать в открытом виде.

### Исходящие Telegram-сообщения

Логировать, что бот отправил пользователю:

```text
timestamp
chat_id
message type
message text
reply_to / edited message id
telegram response
```

### Снимок состояния биржи

Перед и после торговых действий сохранять snapshot:

```text
futures balance
open positions
open TP/SL / plan orders
recent orders if available
mark prices / tickers used for decision
raw exchange responses
```

Минимальные точки snapshot:

```text
before open
after open
before TP/SL set
after TP/SL set
before close
after close
before averaging
after averaging
before re-entry
after re-entry
on tpsl_enforce_job run
on balance/positions request
on auto_scan decision
```

### Raw API Ответы

Сохранять raw ответы MEXC/ccxt и Telegram API для спорных действий:

```text
place order
cancel order
set TP/SL
cancel TP/SL
get positions
get balance
get plan orders
close position
partial close
```

Raw-данные должны быть доступны для диагностики, но с маскированием секретов.

### Решения Бота

Логировать не только действие, но и почему оно было принято:

```text
signal source
calculated TP/SL
configured tp_pct/sl_pct
leverage
margin
averaging threshold
current PnL
reason for skip/open/close/re-entry
risk filters
auto scan filters
```

### Ошибки И Исключения

Для каждой ошибки сохранять:

```text
exception type
message
traceback
operation context
symbol
current position snapshot
last raw exchange response if available
```

## Формат Хранения

Предпочтительно хранить структурированные события в JSONL:

```text
data/logs/events-YYYY-MM-DD.jsonl
data/logs/exchange-YYYY-MM-DD.jsonl
data/logs/telegram-YYYY-MM-DD.jsonl
data/logs/decisions-YYYY-MM-DD.jsonl
```

Каждая строка должна быть отдельным JSON-событием:

```json
{"ts":"2026-05-31T12:00:00Z","event":"telegram_update","chat_id":123,"payload":{}}
```

## Требования К Безопасности

Не логировать в открытом виде:

```text
BOT_TOKEN
MEXC_API_KEY
MEXC_SECRET
OpenRouter/API keys
private keys
authorization headers
cookies
```

Нужна функция маскирования секретов перед записью raw payload.

Пример:

```text
abcd1234secret -> abcd...cret
```

## Требования К Диагностике

Должна быть возможность быстро ответить на вопросы:

```text
почему открылась позиция?
почему закрылась позиция?
какой SL/TP реально был поставлен?
что вернула биржа?
что бот видел в positions/balance в этот момент?
какое сообщение пришло от пользователя?
какое сообщение бот отправил обратно?
```

## Будущее Распознавание

Отдельным следующим этапом добавить распознавание входящих сообщений/сигналов.

Пока только зафиксировать место интеграции:

```text
Telegram update -> raw log -> recognizer -> parsed intent/signal -> decision log -> action
```

Распознавание пользователь пришлёт отдельно. До этого нужно подготовить логирование так, чтобы можно было сравнивать:

```text
raw input
recognized intent
parsed fields
bot decision
final exchange action
```

## TODO

- [x] Спроектировать единый `event_logger` для JSONL-событий.
- [x] Добавить masking/sanitizer для секретов.
- [x] Обернуть Telegram входящие update.
- [x] Обернуть исходящие Telegram send/edit calls.
- [x] Обернуть MEXC/ccxt raw calls или ключевые методы `ExchangeClient`.
- [x] Добавить snapshots до/после открытия позиции и TP/SL set.
- [x] Добавить snapshot и summary для `tpsl_enforce_job`.
- [x] Добавить correlation id для одной пользовательской команды или одного job-run.
- [ ] Добавить snapshots до/после close, averaging, re-entry, auto-scan.
- [ ] Добавить ротацию логов по размеру, сейчас ротация только по дате.
- [ ] Добавить команду/скрипт выгрузки диагностики по symbol/time range.
- [ ] Добавить распознавание входящих сообщений/сигналов, когда будет предоставлена спецификация.

## Implementation Tracking

### 2026-05-31 Step 1: Event Logger

Status: done.

Files:

```text
bot/event_logger.py
```

Implemented:

```text
JSONL writer to data/logs/*-YYYY-MM-DD.jsonl
secret masking by key name
correlation id via contextvars
exception logging with traceback
Telegram update payload extraction
Telegram outgoing method wrapper
exchange snapshot helper
```

Proof:

```text
python -m compileall bot
python -c "from bot.event_logger import log_event, sanitize; print(sanitize({'api_key':'abcdef1234567890','nested':{'telegram_token':'1234567890TOKEN'}})); log_event('events','smoke_test', api_key='abcdef1234567890', payload={'ok': True})"
output masks secrets as abcd...7890 / 1234...OKEN and writes JSONL under data/logs
```

### 2026-05-31 Step 2: Telegram Integration

Status: done.

Files:

```text
bot/main.py
```

Implemented:

```text
TypeHandler(Update, telegram_update_logger), group=-100
app.add_error_handler(telegram_error_logger)
patch_bot_logging(application.bot)
```

Expected logs:

```text
data/logs/telegram-YYYY-MM-DD.jsonl
data/logs/errors-YYYY-MM-DD.jsonl
```

Proof:

```text
grep: telegram_update_logger, patch_bot_logging, telegram_error_logger
python -m compileall bot
python -c "from telegram.ext import Application; from bot.event_logger import patch_bot_logging; app=Application.builder().token('123:ABC').build(); patch_bot_logging(app.bot); print(getattr(app.bot.send_message, '_krabs_logged', False)); print(getattr(app.bot.edit_message_text, '_krabs_logged', False))" -> True / True
```

### 2026-05-31 Step 3: Exchange Raw Logging

Status: done for key methods.

Files:

```text
bot/exchange/client.py
```

Implemented raw logging for:

```text
get_futures_balance
get_positions
place_futures_order
partial_close_futures_position
close_futures_position
cancel_plan_orders
set_tp_sl cancel/place
was_closed_by_tp
get_tp_sl_orders
cancel_tp_sl_orders
```

Expected logs:

```text
data/logs/exchange-YYYY-MM-DD.jsonl
data/logs/decisions-YYYY-MM-DD.jsonl
```

Proof:

```text
grep: mexc_raw_response, mexc_raw_error, set_tp_sl_start
python -m compileall bot
```

### 2026-05-31 Step 4: Trading Snapshots

Status: partial done.

Files:

```text
bot/handlers/trading.py
bot/jobs/main.py
```

Implemented snapshots for:

```text
before_open
after_open_order
before_tpsl_set
after_tpsl_set
```

Implemented decisions for:

```text
execute_open_start
execute_open_order_result
execute_open_persisted
tpsl_enforce_summary
tpsl_enforce_auto_registered_position
```

Proof:

```text
grep: snapshot_exchange_state, execute_open_start, tpsl_enforce_summary
python -m compileall bot
```

## Current Evidence Checklist

- [x] `python -m compileall bot` passes after current implementation.
- [x] Grep confirms Telegram hooks are wired.
- [x] Grep confirms exchange raw logging is wired.
- [x] Grep confirms restore-disabled TP/SL behavior remains unchanged.
- [ ] Runtime proof after deploy: Telegram update creates `telegram-YYYY-MM-DD.jsonl`.
- [x] Runtime proof after deploy: Telegram outgoing call creates `telegram-YYYY-MM-DD.jsonl`.
- [x] Runtime proof after deploy: exchange call creates `exchange-YYYY-MM-DD.jsonl`.
- [x] Runtime proof after deploy: job decision creates `decisions-YYYY-MM-DD.jsonl`.

Runtime proof from Mumbai after deploy:

```text
systemctl is-active krabs3 -> active
git rev-parse --short HEAD -> e1f7a76
git rev-parse --short origin/main -> e1f7a76
tpsl_enforce_job executed successfully after restart
data/logs/telegram-2026-05-31.jsonl contains telegram_outgoing_result
data/logs/exchange-2026-05-31.jsonl contains mexc_raw_response
data/logs/decisions-2026-05-31.jsonl contains tpsl_enforce_summary
```
