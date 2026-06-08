# Чек-лист: торговая диагностика закрытий и ошибок открытия

## Закрыто

- [x] **Авто-закрытие по TP/SL переведено на полный понятный отчёт**
  В `bot/jobs/main.py` re-entry/job-ветки используют единый формат `format_close_message`: причина, Entry/Exit, плечо, маржа, PnL в процентах и USDT, перезаход да/нет и почему.

- [x] **В авто-закрытии явно пишется: стоп это был или тейк-профит**
  Причина строится через `_close_reason_parts(...)`: `тейк-профит`, `стоп-лосс`, `профит-локк SL`.

- [x] **В авто-закрытии показывается PnL**
  PnL считается через `_close_pnl_amount(...)` и выводится в сообщении `format_close_message`.

- [x] **В авто-закрытии понятно, будет ли перезаход**
  Все re-entry ветки передают `ReentryFacts`: `Перезаход: да/нет`, цикл, пауза и причина.

- [x] **Ликвидация/внешнее закрытие больше не короткое**
  `tpsl_enforce_job` пишет: позиция исчезла с биржи, Entry, плечо, маржа, `PnL: неизвестно`, `Перезаход: нет`, и почему PnL нельзя честно определить.

- [x] **Ошибки открытия позиции не показывают сырой Binance JSON в основных пользовательских путях**
  `format_open_error(...)` скрывает `binanceusdm`, `{"code":...}`, `Order would immediately trigger`, `closePosition in the direction is existing` и показывает понятную причину.

- [x] **`-2021 Order would immediately trigger` переводится в понятную причину**
  Сообщение объясняет, что TP/SL сработал бы сразу, и какую сторону цены надо поправить для LONG/SHORT.

- [x] **`-4130 closePosition existing` переводится в понятную причину**
  Сообщение объясняет, что на Binance уже есть защитный `closePosition` стоп/тейк, и что старые TP/SL должны быть отменены перед установкой новых.

- [x] **Открытие сигнала по картинке/тексту не создаёт дефолтные TP/SL перед multi-TP**
  `execute_signal(...)` вызывает `execute_open(..., setup_exits=False)`, чтобы не создавать конфликтующий `closePosition` до установки signal TP/SL.

## Доказательства тестами

- [x] `python -m unittest -v tests.test_trade_job_diagnostics`
  Результат: `Ran 7 tests ... OK`.
  Покрывает авто-закрытие по TP, SL, profit-lock SL, re-entry wait, re-entry disabled, cycles exhausted, successful re-entry и external close.

- [x] `tests.test_trade_diagnostics_contract` через timeout-wrapper
  Результат: `Ran 11 tests ... OK`.
  Покрывает пользовательские ошибки открытия сигнала, ручного `/short`, min-open, scan-open, NLP-open, ручные закрытия, monitor-close, NLP-close и formatter contracts.

- [x] Поиск старых пользовательских фраз в `bot/`
  Команда `rg -n "закрыта по стопу|закрыта по TP|закрыта в прибыль|перезаход пропущен|закрыта принудительно|Ошибка открытия сигнала: \\{e\\}" bot -S` не находит совпадений.

## Не делалось

- [ ] **Live smoke на реальной бирже/сервере**
  Не выполнялся: не открывали и не закрывали реальную/testnet позицию через Telegram. Текущее доказательство — код + автоматические unit/contract tests.
