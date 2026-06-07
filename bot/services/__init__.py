"""Service layer: pure trading logic, decoupled from Telegram handlers and jobs.

- ``tpsl``: TP/SL price math + set/verify on the exchange.
- ``sizing``: leverage caps, minimum order notional, capital/budget math.
- ``trading``: unified open/close/averaging (single source of truth).
"""
