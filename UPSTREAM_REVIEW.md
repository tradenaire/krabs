# Актуальность tradenaire/krabs — 7 сентября 2026

## Полученные версии

| Версия | Коммит | Дата коммита | Что проверено |
|---|---|---|---|
| Исходный локальный архив | `9f29b59` | 28 мая | Сравнение содержимого файлов с main |
| Локальные исправления | `b7c27b7` | 7 сентября | Предыдущие доказательства: [WORKLOG.md](O:/krabs3-7sep26/WORKLOG.md) |
| GitHub main | `ea3304add9193d29ecb14d4eeff8797302488986` | 31 мая | Полный diff Python-файлов относительно архива; все 10 исходных пунктов |
| GitHub BinanceTest | `fef24728d435a7d5993905401ce992204822e38b` | 10 июня, 04:20 +05:00 | Выборочная проверка защиты, маржи, регистрации и учёта закрытий |

Репозиторий клонирован через сохранённую авторизацию murapolo. `main` находится в `tests/tmp-upstream-krabs`, снимок `BinanceTest` — в `tests/tmp-upstream-binance`; обе папки исключены из основного Git. В основной рабочий код изменения не вливались, Railway не обновлялся.

Предыдущее сообщение о последнем push 9 июня относилось к метаданным репозитория, а не к коммиту main. Самый свежий полученный код находится в BinanceTest. В обеих Git-историях shallow=false; общего коммита между локальной HEAD и upstream main не найдено. Поэтому числа ahead/behind не используются как мера актуальности: сравнивалось содержимое кода.

## Main против исходного архива: все 10 пунктов

Из Python-файлов отличаются восемь: семь изменены, `bot/event_logger.py` добавлен. `db.py`, баланс, AI-сканер и requirements.txt совпадают с исходным архивом при сравнении строк без различий LF/CRLF.

| № | Факт в main | Доказательство | Итог относительно задачи |
|---|---|---|---|
| 1. Отсутствующий SL | Коммит `9caca88` удалил из enforce весь блок чтения/восстановления TP/SL. Автоматического восстановления в этой задаче больше нет. | [tpsl_enforce_job](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:1091) | Требование восстановления не выполнено; поведение изменено намеренно отдельным коммитом. |
| 2. Ошибки установки | `_place` теперь отклоняет `success:false` и после попыток выбрасывает исключение. Однако `execute_open` ловит его, пишет warning и возвращает рассчитанные TP/SL; обработчик отображает их как установленные. TP размещается раньше SL, перед заменой вызывается CancelAll. | [отказ MEXC](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/exchange/client.py:569), [перехват](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/handlers/trading.py:168), [сообщение](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/handlers/trading.py:323) | Частично исправлено в клиенте; сквозное подтверждение не обеспечено. |
| 3. Ручные позиции | Авторегистрация осталась при запуске, в averaging и enforce. Profit-lock обрабатывается раньше блока авторегистрации averaging. | [запуск](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/main.py:108), [averaging](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:529), [enforce](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:1108) | Изоляция ручных позиций не реализована. |
| 4. Очистка защиты | Список plan-order возвращает сырой symbol; sweep вычитает из него унифицированные символы позиций. Отмена выполняется CancelAll по символу без владельца ордера. | [парсинг](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/exchange/client.py:709), [sweep](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:1193), [CancelAll](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/exchange/client.py:736) | Дефект остался. |
| 5. Аварийное сокращение | `availableOpen=0` подменяется `_free`; при `_free=0` условие ложно. Размер 5%. Проверка внутри averaging после его ранних выходов. | [отключение averaging](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:309), [маржа](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:368) | Дефекты остались. Нулевая маржа воспроизведена на извлечённом выражении. |
| 6. ETH и другие активы | Из ответа account assets выбирается только USDT; итоговые словари содержат только USDT. | [get_futures_balance](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/exchange/client.py:121) | Отбрасывание ETH воспроизведено с подставным ответом API. |
| 7. Причина закрытия | При неизвестной причине используется текущий ticker. Исчезнувшая позиция без reentry записывается как liquidated. | [fallback](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:799), [синхронизация](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:1183) | Дефекты остались. Неизвестное закрытие воспроизводимо становится TP. |
| 8. PnL | Первое уведомление profit-lock без PnL; close пишется с нулём, последующий результат — в reentry; числовой расчёт проверяет `side == short`, хотя сторона ордера может быть sell. Баланс читает pnl вместо realized_pnl. | [close и уведомление](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:939), [reentry](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:1038), [баланс](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/handlers/balance.py:63) | Исходные дефекты остались. Цена берётся из триггера/текущего ticker, а не подтверждённого исполнения. |
| 9. Перезаход | `_close_pnl` вызывается на строках 887 и 906, определяется на 939. Подпись после SL с перезаходом остаётся «в прибыль». Учёт по символу. | [ранний вызов](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:887), [подпись](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:1043) | Дефекты остались. |
| 10. Profit-lock | Добавлена `_set_tp_sl_verified`; флаг успешной защиты выставляется после проверки, при ошибке ступень откатывается, флаг снимается. Проверяются только тип триггера и близость цены, без объёма/стороны закрытия/владельца. Ступень остаётся в bot_data. | [проверка SL](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:58), [откат](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/jobs/main.py:511) | Частично исправлено; восстановление состояния после рестарта не реализовано. Неправильная сторона с нулевым объёмом проходит проверку — воспроизведено. |

Дополнение main: [event_logger.py](O:/krabs3-7sep26/tests/tmp-upstream-krabs/bot/event_logger.py:76) пишет JSONL с временем, correlation_id, событиями Telegram и ответами биржи. Маскирует значения по именам ключей. Это полезная диагностика, отсутствующая в нашей версии. `sanitize` сохраняет произвольные строки без маскирования содержимого; сообщения и traceback требуют отдельной проверки перед переносом. Галочки runtime proof в upstream-документе — заявления автора, повторной проверкой сервера в этой задаче не подтверждались.

## BinanceTest: что существенно новее

Это отдельное развитие приложения: diff с main затрагивает 95 файлов. Есть Binance live/testnet-клиент, сервисы торговли, три TP, обработка сигналов и тесты. Поддержка MEXC также остаётся в [factory](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/exchange/factory.py:18).

Подтверждённые улучшения:

- [validate_exit_prices / verify_exit_orders](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/services/tpsl.py:44) проверяют положительные цены, сторону относительно reference и наличие ожидаемых уровней TP/SL после установки.
- [classify_protection](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/services/protection.py:25) различает отсутствие/дубли БД, частичную защиту, 1 TP + SL и 3 TP + SL. Проверка по типам/ценам; объём, владелец и сторона конкретного закрывающего ордера не проверяются.
- [/repair_tpsl](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/handlers/trading.py:244) показывает preview; после кнопки подтверждения отменяет старые ордера символа, ставит SL и TP, перечитывает уровни. Это ручное восстановление, не независимый автоматический контроль отсутствующего SL.
- [EmergencyEngine](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/engines/emergency.py:90) вынесен из averaging и [зарегистрирован отдельно](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/jobs/main.py:1403).
- [_close_pnl](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/jobs/main.py:662) определяется до ранних веток; для расчёта используется pos_side_str. Первое уведомление profit-lock теперь формируется через `_close_message` с PnL, когда есть цена. Неизвестный PnL ручного закрытия передаётся в сообщение как None.

Подтверждённые оставшиеся проблемы:

- Авторегистрация при [запуске](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/main.py:135) и [enforce](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/jobs/main.py:967) осталась.
- В [аварийном движке](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/engines/emergency.py:50) сохранились `or free`, условие free > 0, размер 5% и перебор всех позиций. Вынос задачи не исправил эти условия.
- [_resolve_close_reason](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/jobs/main.py:552) всё ещё подменяет неизвестную причину текущей ценой; [синхронизация](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/jobs/main.py:1013) пишет liquidated без доказательства исполнения.
- [Основная ветка reentry](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/jobs/main.py:773) по-прежнему сохраняет close с нулевым PnL. [Ручное закрытие](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/services/trading.py:280) сохраняет неизвестный PnL в БД как 0. [Баланс](O:/krabs3-7sep26/tests/tmp-upstream-binance/bot/handlers/balance.py:66) читает старый ключ pnl.
- MEXC-клиент сохранил фильтр USDT и CancelAll. Изменения Binance не доказывают исправления MEXC.

## Выполненные проверки

Результаты: [upstream-review-results.json](O:/krabs3-7sep26/upstream-review-results.json).

- PASS: синтаксическая компиляция 27 Python-файлов bot/tests main и 94 файлов BinanceTest, без импорта приложения.
- PASS: 8 существующих тестов BinanceTest из `test_protection_audit` и `test_tpsl_validation`. Проверяют классификацию защиты, допустимость цен, наличие ожидаемых уровней. Полный набор тестов ветки не запускался.
- REPRODUCED, не PASS безопасности: шесть проверок воспроизводят неверную проверку SL, потерю ETH, подмену нулевой маржи и определение TP по ticker. Выполняются исходные функции/выражения, извлечённые AST, с подставными клиентами.
- Биржа, Telegram и чужой сервер не вызывались. Документация upstream не использовалась как доказательство успешной торговли.

Повторить проверки из O:/krabs3-7sep26:

```powershell
python tests/tmp-upstream-krabs/review_checks.py
```

## Вывод для продолжения

Main новее архива, но не заменяет наши исправления: подтверждены частичные исправления ошибок MEXC/profit-lock и новое логирование, остальные исходные проблемы сохранились либо восстановление SL отключено. BinanceTest содержит существенно более новую функциональность и отдельные исправления, но проверенные финансовые дефекты остаются и там.

Для текущего MEXC-экземпляра оснований заменять `b7c27b7` на main целиком нет. Имеет смысл отдельно перенести проверенную диагностику и нужные изменения интерфейса. Переход на BinanceTest требует отдельного переноса наших правил принадлежности позиций, ордеров и учёта закрытий в её сервисы; её тесты не подтверждают эти правила. В этой задаче выполнено получение и сравнение, без слияния, нового коммита или деплоя.
