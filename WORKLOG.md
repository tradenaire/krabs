# Krabs: исправления и проверка

Следующая интеграция upstream для MEXC описана отдельно: [INTEGRATION_REPORT.md](O:/krabs3-7sep26/INTEGRATION_REPORT.md). Ниже сохранён отчёт первоначального исправления и первого деплоя.

Исходный коммит: `9f29b59`. Проверки выполняются без доступа к бирже и Telegram.
PASS означает выполненную проверку с указанным тестом; не означает проверку реальной торговли.

| Фаза | Задачи исходного списка | Критерий PASS | Статус |
|---|---|---|---|
| 1. Принадлежность | 3, 4, 9 | Управление по ID позиции; ручные/старые неподтверждённые записи пропускаются; отмена только сохранённых ID ордеров | PASS (offline) |
| 2. Защита | 1, 2, 10 | Независимые TP/SL; проверка ответа и наличия; частичный отказ виден; подтверждённый SL не ослабляется после рестарта | PASS (offline) |
| 3. Маржа | 5, 6 | Ноль сохраняется; аварийная задача независима; размер/порог раздельны; активы отображаются без выдуманной конвертации обеспечения | PASS (offline) |
| 4. Закрытие | 7, 8, 9 | История по ID, цена исполнения, одна запись PnL, неизвестное не равно нулю/ликвидации; проверены ветки перезахода | PASS (offline) |
| 5. Регрессия | 1–10 | Офлайн-тесты, миграция копии БД, компиляция, проверка diff | PASS (offline) |
| 6. Поставка | Деплой | Локальный коммит; Railway t3-remote / krabs; загрузка CLI без GitHub; статус и логи проверены | PASS (standby) |

Решение по «−10%»: размер аварийного сокращения 10%; порог маржи — отдельная настройка 10%. Дополнительное доказательство: исходный UI в `9f29b59:bot/handlers/trading.py:716` уже обещал «закрыть 10% поз», тогда как `jobs/main.py` использовал `_TRIM_PCT = 0.05`. Исправлено расхождение UI и действия.

Контракт MEXC: https://mexcdevelop.github.io/apidocs/contract_v1_en/ — `positionId`, `orderId`, `dealAvgPrice`, `realised`, категории ордеров и поштучная отмена plan-order. Неизвестные/неподтверждённые данные не подменяются текущей ценой.

## Фаза 1. Принадлежность и отмена — пункты 3, 4, 9

- [x] `db.get_managed_position`: совпадают биржевой ID, символ, сторона и время открытия; запись должна иметь статус `open`. Старые строки без ID не дают права управлять.
- [x] `execute_open`: ID берётся из ответа истории конкретного отправленного ордера, затем сверяется с живой позицией. Регистрация происходит до попытки установить защиту.
- [x] `adopt_handler`: принятие ручной позиции требует `/adopt SYMBOL confirm POSITION_ID`. Не включает перезаход. Повторное использование конфликтующего ID отклоняется.
- [x] Из запуска, усреднения и enforce удалена авторегистрация. Открытие отказывается добавлять объём уже существующей позиции. Докупка/сокращение/закрытие перепроверяют ожидаемый ID.
- [x] `bot_orders` хранит ID и владельца каждого ордера. Отмена вызывается для этих ID; `CancelAll` удалён. Символы нормализуются одним методом.
- [x] `authorize_update` стоит перед обработчиками Telegram: разрешены только ID из `allowed_user_ids`; пустой список запрещает команды.

PASS: [test_manual_replacement_not_managed_in_any_background_path](O:/krabs3-7sep26/tests/test_safety.py:200), [test_legacy_symbol_record_is_not_ownership](O:/krabs3-7sep26/tests/test_safety.py:210), [test_stale_position_cannot_be_closed_or_averaged](O:/krabs3-7sep26/tests/test_safety.py:333), [test_adoption_requires_exact_confirmation_id](O:/krabs3-7sep26/tests/test_safety.py:343), [test_new_manual_position_drops_old_reentry](O:/krabs3-7sep26/tests/test_safety.py:317), [test_reused_id_with_different_open_time_is_not_ownership](O:/krabs3-7sep26/tests/test_safety.py:500), [test_cancel_only_owned_ids_and_normalize_symbols](O:/krabs3-7sep26/tests/test_safety.py:214), [test_orphan_sweep_keeps_live_protection_and_cancels_only_old_ids](O:/krabs3-7sep26/tests/test_safety.py:392), [test_unauthorized_update_is_stopped](O:/krabs3-7sep26/tests/test_safety.py:356).

## Фаза 2. TP/SL и profit-lock — пункты 1, 2, 10

- [x] `set_tp_sl` независимо проверяет каждую ногу: ID владельца, цену после округления биржи, направление, сторону закрытия, весь объём, режим маржи, рыночное исполнение и источник цены.
- [x] Отсутствующий SL восстанавливается при существующем TP. Сначала размещается SL; заменяемый старый ордер отменяется после подтверждения нового активного ордера.
- [x] `success:false`, исключение и отсутствие принятого ордера в списке активных дают ошибку. Проверка списка ордеров также отклоняет ошибочные ответы.
- [x] Частичный успех не откатывает подтверждённую ногу. `execute_open` сохраняет открытую позицию, возвращает `protection_status=не подтверждена` и не возвращает неподтверждённые цены как установленную защиту.
- [x] На неизвестный результат размещения сохраняется блокировка повторной отправки. Один lock сериализует мутации одного клиента.
- [x] Profit-lock сохраняет цену и ступень после подтверждения SL; восстановление и усреднение не ослабляют эту цену. Явное выключение через кнопку разрешает возврат обычного стопа.
- [x] Расчёт использует фактическое плечо позиции. Карточки отделяют расчётные уровни от фактического подтверждения ордеров.

PASS: [test_missing_sl_keeps_existing_tp_and_manual_order](O:/krabs3-7sep26/tests/test_safety.py:145), [test_wrong_side_volume_direction_price_replaced_individually](O:/krabs3-7sep26/tests/test_safety.py:182), [test_long_protection_direction_and_factual_leverage](O:/krabs3-7sep26/tests/test_safety.py:384), [test_rejection_partial_success_and_no_false_sl](O:/krabs3-7sep26/tests/test_safety.py:153), [test_tp_failure_retains_confirmed_profit_lock](O:/krabs3-7sep26/tests/test_safety.py:160), [test_accepted_but_invisible_is_not_success](O:/krabs3-7sep26/tests/test_safety.py:168), [test_timeout_has_no_automatic_duplicate_retry](O:/krabs3-7sep26/tests/test_safety.py:175), [test_concurrent_enforcement_does_not_duplicate_legs](O:/krabs3-7sep26/tests/test_safety.py:410), [test_open_registers_order_position_before_protection_failure](O:/krabs3-7sep26/tests/test_safety.py:370), [test_profit_lock_restart_restores_tight_stop](O:/krabs3-7sep26/tests/test_safety.py:191), [test_position_display_does_not_claim_calculated_protection_is_active](O:/krabs3-7sep26/tests/test_safety.py:488).

## Фаза 3. Маржа и активы — пункты 5, 6

- [x] `margin_emergency_job` запускается отдельно каждые 10 секунд. Не зависит от флага докупок и их паузы.
- [x] `available_margin` сохраняет нулевой `availableOpen`, использует `availableBalance` только при отсутствии первого поля, неизвестное обеспечение вызывает ошибку.
- [x] Условие: доступно ≤ 0 либо доступно меньше порога от свободного баланса. Порог 0 отключает проверку. Размер сокращения — отдельный процент контрактов; оба параметра показаны и проверяются на допустимый диапазон.
- [x] Сокращаются только принятые позиции. Минимум — один целый контракт; сообщение указывает фактическую долю, в том числе 100% для единственного контракта.
- [x] Флаг аварийного эпизода хранится в SQLite до отправки. При том же дефиците после рестарта повторного сокращения нет; восстановление доступной маржи снимает флаг.
- [x] Все активы фьючерсного и спотового счетов отображаются отдельно. Автоматический перевод со спота при открытии удалён. Обеспечение читается в валюте расчёта, без самостоятельной конвертации ETH в USDT.

PASS: [test_zero_margin_emergency_independent_of_averaging_and_restart](O:/krabs3-7sep26/tests/test_safety.py:222), [test_single_contract_trim_reports_actual_percentage](O:/krabs3-7sep26/tests/test_safety.py:463), [test_assets_preserved_zero_not_replaced_and_no_spot_collateral](O:/krabs3-7sep26/tests/test_safety.py:237), [test_margin_controls_validate_and_show_separate_values](O:/krabs3-7sep26/tests/test_safety.py:470), [test_non_usdt_assets_are_visible_but_unsupported_contract_not_adopted](O:/krabs3-7sep26/tests/test_safety.py:496).

Граница: код не реализует самостоятельный расчёт единого мультивалютного обеспечения с дисконтом ETH. Используются только суммы, возвращённые API фьючерсного счёта. Управление контрактами с расчётом не в USDT явно отклоняется; активы при этом отображаются. Доказательств режима счёта из майской переписки недостаточно, чтобы объявить его интеграцию проверенной.

## Фаза 4. Закрытия, PnL, перезаходы — пункты 7, 8, 9

- [x] `get_closed_position_result` находит закрытую позицию по ID, времени открытия, символу и стороне. Исчезновение из текущего списка не записывается как ликвидация.
- [x] Причина определяется по последнему исполненному ордеру: связь с сохранённым plan ID, ID ручного закрытия, категория системного ордера/ADL. При неоднозначных последних исполнениях причина `unknown`.
- [x] Цена берётся из `closeAvgPrice` закрытой позиции. PnL — поле `realised` MEXC без повторного прибавления/вычитания комиссий и funding. Текущая и триггерная цены не подставляются.
- [x] `record_closure` одной транзакцией обновляет позицию, журнал и историю. Уникальность по локальному ключу позиции не допускает вторую запись закрытия. Запоздавший PnL обновляет те же строки.
- [x] Неизвестный PnL хранится как NULL и показан отдельным счётчиком. Баланс читает `realized_pnl` и `closes`. Победы/поражения определяются знаком PnL, а не подписью TP. Даты новых записей и итогов — UTC.
- [x] Все ручные пути закрытия используют `_do_close`: сообщение означает отправку ордера; фиксация результата ожидает историю. После таймаута повторная отправка блокируется статусом `closing`.
- [x] Сначала учитывается закрытие, затем решается перезаход. Отключённые/исчерпанные циклы и неизвестная причина не открывают новую позицию. SL проверяет отдельное разрешение и паузу; profit-lock ждёт 60 секунд от сохранённого времени закрытия.
- [x] Перезаход не получает PnL предыдущего закрытия в свою строку журнала. Нормализация `sell/short` исключает старый переворот знака. Новая позиция того же символа отменяет старый перезаход.
- [x] Уведомление закрепляется за ключом закрытия в БД. Три последовательные ALLO — три записи и три уведомления, повторный опрос одной сделки — без дубля.

PASS: [test_close_reason_from_exact_filled_order_not_trigger_or_ticker](O:/krabs3-7sep26/tests/test_safety.py:254), [test_ambiguous_last_fill_is_unknown_not_arbitrary](O:/krabs3-7sep26/tests/test_safety.py:503), [test_other_position_history_cannot_close_current_position](O:/krabs3-7sep26/tests/test_safety.py:265), [test_closed_once_stats_history_and_message_share_pnl](O:/krabs3-7sep26/tests/test_safety.py:269), [test_delayed_pnl_updates_same_closure_without_second_notification](O:/krabs3-7sep26/tests/test_safety.py:431), [test_three_positions_same_symbol_recorded_separately](O:/krabs3-7sep26/tests/test_safety.py:442), [test_unknown_pnl_not_zero_and_no_reentry](O:/krabs3-7sep26/tests/test_safety.py:282), [test_reentry_disabled_exhausted_and_sl_disallowed](O:/krabs3-7sep26/tests/test_safety.py:291), [test_reentry_sl_allowed_has_loss_label_and_no_double_pnl](O:/krabs3-7sep26/tests/test_safety.py:301), [test_profit_lock_reentry_cooldown_survives_restart](O:/krabs3-7sep26/tests/test_safety.py:414), [test_close_submission_does_not_claim_execution](O:/krabs3-7sep26/tests/test_safety.py:324), [test_close_timeout_cannot_be_resubmitted](O:/krabs3-7sep26/tests/test_safety.py:455), [test_failed_position_snapshot_cannot_trigger_cleanup](O:/krabs3-7sep26/tests/test_safety.py:402), [test_unregistered_open_blocks_another_submission](O:/krabs3-7sep26/tests/test_safety.py:509).

## Фаза 5. Проверки и предел доказательств

- [x] В тестах подставлены ответы биржи/Telegram; DNS и socket.connect запрещены. Рабочая БД не используется.
- [x] Повторная миграция копии реальной локальной БД: `integrity_check=ok`, настройки и исходные количества строк сохранены; исходная БД не изменена. Результат — `migration-results.json`.
- [x] Локальный `/health` вернул `service=krabs`, `mode=standby`. Процесс тестового сервера остановлен.
- [x] Импорт `bot.main` проверен с `NUMBA_DISABLE_JIT=1`; это проверка импортов, не запуск бота.
- [x] Итоговый прогон: **41 тест, 80.186 секунд, OK**. Компиляция и `git diff --check`: PASS. Хэши проверенных файлов и команды: `verification-results.json`.
- [ ] Проверка реального исполнения MEXC не проводилась. Тесты подтверждают ветки кода и формат обработки подставных ответов, а не ликвидность, доступность endpoint или исполнение живого стопа.

Ограничения, оставленные явно:

1. При таймауте размещения без полученного ID нужен разбор истории перед снятием блокировки `plan_uncertain_*`/`order_uncertain_*`. Автоматический повтор не выполняется.
2. История зависит от доступности и глубины API. Позиция без подтверждённого закрытия остаётся ожидающей; неоднозначная причина не превращается в TP/SL/ликвидацию.
3. Неизвестная причина отменяет автоматический перезаход. Позднее уточнение учёта не включает его задним числом.
4. Чтобы исключить повтор при потерянном ответе Telegram, уведомление помечается до отправки. Доставка при таком сбое не гарантируется; учёт сделки остаётся в БД, повтор автоматически не отправляется.
5. Старые ордера без сохранённого ID владельца не отменяются и не принимаются автоматически. Их происхождение требует отдельной сверки.
6. Plan-order MEXC в используемом API задаётся по символу. Между ручным закрытием/переоткрытием и следующим опросом может существовать старый биржевой триггер; офлайн-тест не доказывает отсутствие этого интервала на бирже.
7. Поддерживается один процесс/реплика на один счёт. Файловая блокировка защищает каталог данных одного экземпляра; она не координирует разные серверы.

## Фаза 6. Локальный коммит и Railway

- [x] `Dockerfile`: Python 3.12, установлены зависимости, при сборке выполняются тесты и импорт приложения. CCXT и Telegram закреплены на версиях локальной проверки.
- [x] `start.py`: режим по умолчанию `standby`; только health endpoint и инициализация SQLite, без Telegram polling, scheduler и отправки ордеров.
- [x] `.gitignore` и `.dockerignore` исключают локальные данные, токены, логи и Git из контейнера. Деплой должен использовать отдельный volume `/data` и одну реплику.
- [x] Локальный коммит кода: `b7c27b7a37fdac24d740fedf7fe369a663bc292c`. GitHub/push не использовались.
- [x] Создан отдельный Railway service `krabs` (`cc6ea924-0964-4715-aa25-98ec73776706`) в `t3-remote`. Загрузка — `railway up`; `source.repo=null`. Отдельный volume `/data`.
- [x] Deployment `d55f1f1e-33f7-4636-9f5a-43bf98cb6f92`: **SUCCESS**. В контейнере **41 тест / 2.358s / OK** и успешный импорт приложения. `/health`: `standby`, ревизия `b7c27b7`. Логи подтверждают выключенные polling/торговлю. Хэш всего Python-кода совпадает с локальным проверенным кодом. Подробности: `deployment-results.json`.
- [x] Отдельная БД на `/data`: 0 строк конфигурации, 0 позиций; рабочие токены не переносились. ID деплоев существующих сервисов t3-remote и t3-local-relay не изменились.

Торговые ключи и локальная БД в этот деплой не переносятся. Включение режима `bot` является отдельным запуском действующего экземпляра; первичный деплой проверяется в `standby`.

## Проверяемые ссылки на реализацию

| Функция | Код |
|---|---|
| `get_managed_position` | [bot/db.py:253](O:/krabs3-7sep26/bot/db.py:253) |
| `record_closure` | [bot/db.py:310](O:/krabs3-7sep26/bot/db.py:310) |
| `available_margin` | [bot/exchange/client.py:30](O:/krabs3-7sep26/bot/exchange/client.py:30) |
| `place_futures_order` | [bot/exchange/client.py:316](O:/krabs3-7sep26/bot/exchange/client.py:316) |
| `cancel_tp_sl_orders` | [bot/exchange/client.py:537](O:/krabs3-7sep26/bot/exchange/client.py:537) |
| `set_tp_sl` | [bot/exchange/client.py:544](O:/krabs3-7sep26/bot/exchange/client.py:544) |
| `get_closed_position_result` | [bot/exchange/client.py:641](O:/krabs3-7sep26/bot/exchange/client.py:641) |
| `averaging_job` | [bot/jobs/main.py:269](O:/krabs3-7sep26/bot/jobs/main.py:269) |
| `reentry_job` | [bot/jobs/main.py:659](O:/krabs3-7sep26/bot/jobs/main.py:659) |
| `tpsl_enforce_job` | [bot/jobs/main.py:754](O:/krabs3-7sep26/bot/jobs/main.py:754) |
| `margin_emergency_job` | [bot/jobs/main.py:783](O:/krabs3-7sep26/bot/jobs/main.py:783) |
| `authorize_update` | [bot/lifecycle.py:9](O:/krabs3-7sep26/bot/lifecycle.py:9) |
| `register_position` | [bot/lifecycle.py:17](O:/krabs3-7sep26/bot/lifecycle.py:17) |
| `reconcile_closures` | [bot/lifecycle.py:59](O:/krabs3-7sep26/bot/lifecycle.py:59) |
| `adopt_handler` | [bot/lifecycle.py:93](O:/krabs3-7sep26/bot/lifecycle.py:93) |
| `execute_open` | [bot/handlers/trading.py:70](O:/krabs3-7sep26/bot/handlers/trading.py:70) |
| `_do_close` | [bot/handlers/trading.py:304](O:/krabs3-7sep26/bot/handlers/trading.py:304) |
| `serve_standby` | [start.py:26](O:/krabs3-7sep26/start.py:26) |
| `run` | [start.py:88](O:/krabs3-7sep26/start.py:88) |
