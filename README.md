# Krabs3 — MEXC/Binance futures Telegram bot

Telegram-бот для шорт-трейдинга крипто-фьючерсов. Стратегия: AI-скан перегретых
монет → шорт → усреднение → TP/SL → перезаход. Управление полностью через Telegram.

## Запуск

```powershell
# 1. окружение и зависимости
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. ввод ключей (интерактивно): токен, user ids, биржа, API-ключи, OpenRouter
.\.venv\Scripts\python.exe start.py --setup     # или setup.bat

# 3. запуск
.\.venv\Scripts\python.exe start.py             # или start.bat (с автоперезапуском)
```

## Выбор биржи (провайдеры)

Конфиг `exchange_provider` (таблица `config` в `data/bot.db`):

| Значение | Что это |
|----------|---------|
| `mexc` (по умолчанию) | Боевая торговля на MEXC (реальные деньги). |
| `binance_testnet` | Binance USDⓂ Futures **testnet** — настоящие тестовые ордера на тестовый сервер биржи, без риска. |
| `binance` | Боевая торговля на Binance USDⓂ Futures (реальные деньги). |

Сменить провайдера на лету можно через Telegram:

```
/setkey exchange_provider binance_testnet
/setkey binance_api_key <key>
/setkey binance_secret <secret>
```

Бот пересоздаёт биржевой клиент сразу после смены провайдера/ключей (рестарт не нужен).

### Тестовые сделки на Binance Futures testnet

У MEXC нет API-доступа к testnet, поэтому для проверки реального исполнения ордеров
используется Binance Futures testnet. Получите ключи на `testnet.binancefuture.com`
(API Key management), выберите провайдер `binance_testnet`, и бот будет слать
настоящие ордера на тестовый сервер.

Ручной smoke-тест адаптера (открытие/TP-SL/частичное+полное закрытие):

```powershell
$env:BINANCE_API_KEY="..."; $env:BINANCE_SECRET="..."
.\.venv\Scripts\python.exe tools\binance_smoke.py            # BTC, ~$60, x5
```

## Бумажный режим

Помимо реальных бирж есть встроенная **симуляция** (`/paper`): виртуальный портфель
$500 на реальных ценах, ордера исполняются внутри бота (не на бирже). Это не testnet
биржи, а локальный симулятор.

## Архитектура

Подробности — в коде. Кратко: вход `start.py` → `bot/main.py`; биржевые клиенты за
единым интерфейсом (`bot/exchange/base.py`, фабрика `bot/exchange/factory.py`); чистая
торговая логика в `bot/services/`; независимые async-движки в `bot/engines/`
(докупка, аварийка, перезаход, TP/SL, скан-скаут, мониторинг, отчёты); тяжёлый скан —
в отдельном процессе `bot/workers/scanner_worker.py`. Хранилище — SQLite (`bot/db.py`),
async-обёртка `bot/infra/db.py`.
