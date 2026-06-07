"""Engines: each long-running concern runs as an independent async task.

Replaces the single APScheduler with one self-driven loop per module
(averaging, emergency, re-entry, tp/sl enforce, scout, monitor, reporting,
paper). They run concurrently on the event loop; order safety is provided by
per-symbol locks in the service layer / AppState.
"""
