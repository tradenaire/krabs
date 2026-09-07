# Krabs: интеграция свежих изменений для MEXC

## Состав версии

Основа торгового ядра — локальный `b7c27b7`, с исправлениями исходных десяти пунктов. Проверены upstream `main@ea3304add9193d29ecb14d4eeff8797302488986` и `BinanceTest@fef24728d435a7d5993905401ce992204822e38b`. Сравнение исходников: [UPSTREAM_REVIEW.md](O:/krabs3-7sep26/UPSTREAM_REVIEW.md).

Это интеграция выбранных изменений для существующего MEXC-экземпляра. Она не является полным слиянием BinanceTest: Binance live/demo, исполнение многоцелевых сигналов, ladder engine, Telegram testing endpoint и отдельные scanner workers не включены. Их текущие пути обходят наши правила принадлежности позиций и учёта исполнений. Неактуальные авторегистрация, CancelAll, расчёт закрытия по ticker и сокращение 5% также не перенесены. Выбор сохранённого AI-провайдера/модели не изменяется.

## Фазы и критерии

| Фаза | Выполненный код | Критерий PASS и доказательство |
|---|---|---|
| 1. Защита | [audit_protection](O:/krabs3-7sep26/bot/exchange/client.py:586) проверяет ID владельца, направление закрытия, тип триггера, объём, margin mode, market order, trend и цену. Использует ту же функцию сравнения, что set_tp_sl. | `test_protection_audit_is_read_only_and_checks_owned_side_volume`: TP найден, неправильный SL отвергнут; изменений на бирже нет; новая ручная позиция получает UNMANAGED. |
| 2. Ручное восстановление | [/repair_tpsl](O:/krabs3-7sep26/bot/handlers/protection.py:8) показывает уровни; кнопка связана с user_data пользователя, одноразовым nonce и TTL 5 минут. Подтверждение сверяет ID/время, объём, сторону, плечо, вход, margin mode и настройки под общей блокировкой клиента. | `test_repair_rejects_changed_position_volume_or_settings_before_mutation`, `test_repair_preview_confirm_once_preserves_tp_and_manual_orders`, `test_repair_partial_error_never_reports_success`, `test_repair_expired_confirmation_never_mutates`. |
| 3. Диагностика | [event_logger](O:/krabs3-7sep26/bot/event_logger.py:38), адаптация идеи e1f7a76: JSONL с временем, correlation_id, началом/результатом/ошибкой мутации, аудитом защиты, сверкой закрытий и статусом уведомления. | Проверки маскирования вложенных ключей и известных секретов, фактической записи JSONL и ограничения размера: [тесты](O:/krabs3-7sep26/tests/test_upstream_integration.py:78). |
| 4. Исследование рынка | [research_snapshot](O:/krabs3-7sep26/bot/ai/research_snapshot.py:145) из BinanceTest: баланс, позиции, ордера, до трёх кандидатов с ценой, funding, стаканом и open interest. Работает в manual scan и auto scan. Ошибка данных обозначается unavailable. | [ошибки и нулевой баланс](O:/krabs3-7sep26/tests/test_upstream_integration.py:31), [передача snapshot в запрос модели](O:/krabs3-7sep26/tests/test_upstream_integration.py:50). Нет вызовов реального AI/API. |
| 5. Разбор ответа | Перенесены парсер COIN/таблиц и очистка ответа из BinanceTest. [parse_short_candidates](O:/krabs3-7sep26/bot/ai/analyst.py:289) исключает LONG и неизвестную сторону из текущей шорт-стратегии. | [парсер и очистка](O:/krabs3-7sep26/tests/test_upstream_integration.py:18), [реальный auto_scan_job с подставным LONG-ответом не отправляет ордер](O:/krabs3-7sep26/tests/test_upstream_integration.py:118). |
| 6. Завершение работы | Фикс закрытия сессий из BinanceTest применён к MEXC: close освобождает futures/spot HTTP-сессии; обращение к session после close не создаёт новую. Telegram post_shutdown вызывает client.close. AI-клиент закрывается async context manager. | [две проверки MEXC-сессий](O:/krabs3-7sep26/tests/test_upstream_integration.py:64), проверка async exit AI-клиента. |
| 7. Регрессия и поставка | Все прежние 41 acceptance-проверки сохранены; 17 новых проверяют интеграцию. Docker запускает тот же набор и импорт приложения. | PASS: 58 тестов локально (93.164 с) и в Linux-контейнере (8.800 с). [Хеши и локальный прогон](O:/krabs3-7sep26/integration-verification.json), [доказательства Railway](O:/krabs3-7sep26/integration-deployment.json). |

### Уточнения по реализации

- Восстановление использует существующий `set_tp_sl`: SL ставится первым; новая защита подтверждается до отмены старого собственного ордера. Ручные ордера не отменяются. При частичном отказе сообщение не объявляет успех. Автоматический enforce продолжает работать независимо от ручной команды.
- Логирование Telegram идёт после allowlist, в отдельной группе до обычных обработчиков. Это проверяется на фактически зарегистрированных handlers. Тела входящих команд не записываются; `/setkey` не повторяет секрет в ответе. Audit-файл — максимум 2 MB плюс четыре резервных файла. Это журнал операций, не полный архив биржевого трафика.
- Snapshot имеет таймаут 5 секунд на запрос и общий предел 20 секунд. В AI не передаются config, ключи или тела исключений. Доступность реальной биржи/модели этими офлайн-тестами не подтверждается.
- Перенесено отображение знака доходности SL: в бумажной карточке и расчётной цели реальной позиции. Реальная карточка сохраняет явную оговорку, что наличие ордеров в ней не проверяется; фактическая проверка доступна через `/repair_tpsl`.
- В конфиг добавлен явный `exchange_provider=mexc`. Иное значение останавливает запуск; настройки Binance не могут незаметно запустить MEXC-клиент.
- Схема и содержимое существующей БД не менялись этой интеграцией. Старые позиции без подтверждённого биржевого ID по-прежнему не принимаются автоматически.

## Проверка и границы

```powershell
$env:NUMBA_DISABLE_JIT='1'
python -m unittest discover -s tests -v
python -m compileall -q bot tests/test_safety.py tests/test_upstream_integration.py
git diff --check
```

PASS относится к названным офлайн-сценариям, компиляции и проверке развернутого standby-контейнера. Торговля, реальное исполнение стопов, Binance и платные AI-запросы не тестировались. Все ограничения предыдущего [WORKLOG.md](O:/krabs3-7sep26/WORKLOG.md) по неизвестным исполнениям, задержкам истории, владению старыми ордерами и единственной реплике сохраняются.

## Выполненный деплой

- [x] Локальный коммит кода: `3c643ad7ac0d28835fb548a26a7aac8df35d3038`.
- [x] Папка загружена Railway CLI напрямую, без GitHub; `source.repo=null`.
- [x] Существующий `t3-remote / production / krabs`: deployment `7da7c2ec-c6a7-4be1-9e2a-4cc1c2a8eb0f`, **SUCCESS**.
- [x] Сборка: 58 тестов PASS и импорт bot.main PASS.
- [x] `/health` внутри контейнера: service=krabs, mode=standby, revision=3c643ad7ac0d28835fb548a26a7aac8df35d3038.
- [x] Все 34 файла исходников/тестов/start.py/requirements.txt в контейнере совпадают по SHA-256 с проверенным кодом. Общий digest этой группы: `0adf930b479cc8f616abfc03a4bb733a20ccf39392f1dea99dcd7ccc1672e19e`.
- [x] БД контейнера: config=0 строк, positions=0 строк. Старые ключи/локальная торговая БД не перенесены.
- [x] Сервисы t3-remote и t3-local-relay сохранили прежние deployment ID и статус SUCCESS.

Telegram polling, планировщик торговли и сделки выключены режимом standby. Доказательства относятся к работающему standby-контейнеру и офлайн-тестам.
