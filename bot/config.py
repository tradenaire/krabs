from dataclasses import dataclass, field


@dataclass
class Config:
    telegram_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)

    mexc_api_key: str = ""
    mexc_secret: str = ""

    openrouter_api_key: str = ""
    openrouter_model: str = "x-ai/grok-4-fast:online"
    anthropic_api_key: str = ""

    # Trade defaults
    default_trade_usdt: float = 1.0       # initial margin per trade
    default_leverage: int = 0             # 0 = max allowed by symbol
    tp_pct: float = 500.0
    sl_pct: float = 500.0

    # Averaging
    averaging_enabled: bool = True               # master on/off toggle
    averaging_threshold: float = -100.0          # trigger at PnL ≤ this %
    averaging_amount: float = 0.50               # add per step
    averaging_interval: int = 10                 # seconds
    max_averaging_count: int = 100               # hard cap
    averaging_profit_lock_trigger: float = 0.0   # move SL to profit when PnL ≥ this % (0=off)
    averaging_profit_lock_sl_pct: float = 0.0    # lock SL at this PnL % (e.g. 100 = lock at +100%)
    margin_emergency_threshold_pct: float = 0.0  # close 10% positions when avail < X% of free (0=off)

    # Re-entry
    max_reentry_cycles: int = 3
    reentry_on_sl: bool = False          # re-enter after SL (tight-stop strategy, not Martingale)
    reentry_sl_cooldown_min: int = 10    # minutes to wait after SL before re-entering

    # Auto scan
    auto_scan_enabled: bool = False
    auto_scan_interval_min: int = 30
    auto_scan_max_positions: int = 3
    auto_scan_max_risk: int = 7
    auto_scan_capital_pct: float = 0.0  # 0=require full budget, 100=always open

    # Paper trading
    paper_enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        obj = cls()
        for k, v in d.items():
            if not hasattr(obj, k):
                continue
            try:
                attr = getattr(obj, k)
                if isinstance(attr, bool):
                    setattr(obj, k, str(v).lower() in ("1", "true", "yes"))
                elif isinstance(attr, float):
                    setattr(obj, k, float(v))
                elif isinstance(attr, int):
                    setattr(obj, k, int(v))
                elif isinstance(attr, list):
                    if isinstance(v, str):
                        setattr(obj, k, [int(x.strip()) for x in v.split(",") if x.strip()])
                    else:
                        setattr(obj, k, v)
                else:
                    setattr(obj, k, str(v))
            except Exception:
                pass
        return obj
