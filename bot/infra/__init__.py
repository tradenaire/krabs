"""Infrastructure layer: async DB, shared state, event bus, exchange helpers.

These modules are additive and do not change existing behavior. Engines and
services build on top of them; legacy handlers/jobs keep working unchanged.
"""
