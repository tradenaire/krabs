"""Separate worker processes for CPU/IO-heavy work (hybrid parallelism).

The trading core (orders, averaging, emergency) stays in the main asyncio
process for account safety. Heavy technical scanning (pandas / pandas_ta over
many symbols) is offloaded here to a dedicated process so it never blocks the
order loop.
"""
