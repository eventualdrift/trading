"""Configuration: YAML file for settings, environment variables (.env) for secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml

from .timeframes import TF_SECONDS


@dataclass
class ExchangeConfig:
    id: str = "binance"
    market_type: str = "spot"  # spot only for live trading in this version
    sandbox: bool = False  # use the exchange testnet (supported by binance, bybit, okx, ...)
    quote: str = "USDT"
    data_exchange_id: str | None = None  # optionally pull market data from a different exchange


@dataclass
class UniverseConfig:
    top_n: int = 30
    min_quote_volume: float = 5_000_000
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    refresh_hours: float = 6


@dataclass
class DataConfig:
    dir: str = "data"
    history_days: dict[str, int] = field(
        default_factory=lambda: {"15m": 180, "1h": 540, "4h": 1095, "1d": 1825}
    )


@dataclass
class CostConfig:
    fee_rate: float = 0.001  # taker fee per side (0.1%; 0.075% with Binance's BNB discount)
    slippage_rate: float = 0.0005  # adverse price move on market orders / stops
    maker_fee_rate: float | None = None  # resting limit orders (None = same as fee_rate)
    entry_order: str = "market"  # market | limit (limit: paper/backtest only in this version)


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0  # % of equity lost if the stop-loss is hit
    max_open_positions: int = 3
    max_position_pct: float = 30.0  # cap on a single position's notional, % of equity
    max_total_exposure_pct: float = 100.0  # cap on all open notional (100 = no leverage)
    daily_loss_limit_pct: float = 3.0  # pause new entries for the rest of the UTC day
    max_drawdown_pct: float = 15.0  # halt new entries until /resume
    min_reward_risk: float = 1.5
    breakeven_at_r: float = 1.0  # move stop to entry once price is +1R (0 = off)
    max_risk_multiplier: float = 1.5  # up to 1.5x risk on the ML's strongest setups (1 = off)
    max_chase_r: float = 0.3  # skip entries if price already ran this many R past the signal


@dataclass
class CoreConfig:
    fraction: float = 0.0  # share of the account in the core sleeve (0 = off, e.g. 0.65); paper only
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])  # equal slots
    sma_days: list[int] = field(default_factory=lambda: [50, 100, 150, 200])
    min_trade_usd: float = 10.0
    drift_tolerance: float = 0.2  # trade a coin when off target by 20% of its slot (a 25% step always is)
    rebalance_sleeves_days: float = 30  # reset the core/satellite split every N days (0 = never)


@dataclass
class ContextConfig:
    uptrend_days: int = 200  # BTC above its N-day average = "BTC uptrend" (for the btc_filter option)
    vol_short: int = 20  # BTC volatility ratio = sd(short) / sd(long) of hourly returns
    vol_long: int = 100


@dataclass
class GuardConfig:
    frozen_bars: int = 6  # skip a coin whose last N candles are identical with no volume (dead feed)
    extreme_move_pct: float = 10.0  # a price this far from the last 1m close must repeat on the next poll
    max_price_age_seconds: float = 300  # ignore a ticker older than this
    vol_breaker: bool = False  # pause new satellite entries during BTC volatility bursts
    vol_breaker_ratio: float = 2.5


@dataclass
class SelectionConfig:
    btc_filter: str = "off"  # off | auto: also test each strategy with the BTC-uptrend filter and keep
    # it only where it improves BOTH in-sample and out-of-sample expectancy
    in_sample_fraction: float = 0.7
    min_trades_in_sample: int = 30
    min_trades_out_of_sample: int = 15
    min_expectancy_r: float = 0.05
    min_profit_factor: float = 1.1
    min_symbol_win_fraction: float = 0.5


@dataclass
class MLConfig:
    enabled: bool = True
    min_candidates: int = 400
    min_test_trades: int = 30
    min_improvement_r: float = 0.02  # filtered expectancy must beat unfiltered by this much
    min_edge_t: float = 2.5  # ...and taken trades must beat rejected ones significantly (Welch t)
    threshold_grid: list[float] = field(
        default_factory=lambda: [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    )
    min_probability: float | None = None  # override the learned threshold


@dataclass
class LearningConfig:
    retrain_every_hours: float = 168  # weekly self-learning cycle
    learn_on_start: bool = True  # run a cycle at start-up if no model/selection exists


@dataclass
class PaperConfig:
    starting_balance: float = 1000.0


@dataclass
class LiveConfig:
    native_stop_loss: bool = True  # keep a stop-loss order on the exchange (protects if the bot is down)
    require_exchange_stop: bool = True  # sell at once if that stop can't be placed and verified
    fill_timeout_seconds: float = 30  # cancel any unfilled part of a market order after this


@dataclass
class TelegramConfig:
    enabled: bool = True
    daily_summary_hour_utc: int = 0


@dataclass
class Secrets:
    api_key: str | None = None
    api_secret: str | None = None
    api_password: str | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            api_key=os.getenv("EXCHANGE_API_KEY") or None,
            api_secret=os.getenv("EXCHANGE_API_SECRET") or None,
            api_password=os.getenv("EXCHANGE_API_PASSWORD") or None,
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        )


@dataclass
class BotConfig:
    mode: str = "paper"  # paper | live
    timeframes: list[str] = field(default_factory=lambda: ["15m", "1h", "4h", "1d"])
    strategies: dict[str, dict] = field(
        default_factory=lambda: {"trend": {}, "breakout": {}, "meanrev": {}, "momentum": {}}
    )
    allow_short: bool = False
    poll_seconds: float = 30
    candle_close_delay_seconds: float = 15
    state_dir: str = "state"
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    data: DataConfig = field(default_factory=DataConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    core: CoreConfig = field(default_factory=CoreConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    guards: GuardConfig = field(default_factory=GuardConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    secrets: Secrets = field(default_factory=Secrets, repr=False)

    @property
    def state_path(self) -> Path:
        p = Path(self.state_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def costs_model(self):
        from .backtest.engine import Costs

        c = self.costs
        return Costs(c.fee_rate, c.slippage_rate, c.maker_fee_rate, c.entry_order == "limit")

    def market_context(self, daily=None, hourly=None):
        from .context import MarketContext

        c = self.context
        return MarketContext(daily, hourly, c.uptrend_days, c.vol_short, c.vol_long)

    def validate(self) -> None:
        from .strategies import STRATEGIES

        errors = []
        if self.mode not in ("paper", "live"):
            errors.append(f"mode must be 'paper' or 'live', got {self.mode!r}")
        for tf in self.timeframes:
            if tf not in TF_SECONDS:
                errors.append(f"unknown timeframe {tf!r}")
        for name in self.strategies:
            if name not in STRATEGIES:
                errors.append(f"unknown strategy {name!r}; available: {sorted(STRATEGIES)}")
        r = self.risk
        if not 0 < r.risk_per_trade_pct <= 5:
            errors.append("risk.risk_per_trade_pct must be in (0, 5]")
        if not 1.0 <= r.max_risk_multiplier <= 3.0 or r.risk_per_trade_pct * r.max_risk_multiplier > 5:
            errors.append("risk.max_risk_multiplier must be in [1, 3] and keep any trade's risk <= 5%")
        if r.max_open_positions < 1:
            errors.append("risk.max_open_positions must be >= 1")
        if not 0 < r.max_position_pct <= 100:
            errors.append("risk.max_position_pct must be in (0, 100]")
        if r.min_reward_risk <= 0:
            errors.append("risk.min_reward_risk must be > 0")
        if self.mode == "live" and self.allow_short and self.exchange.market_type == "spot":
            errors.append("allow_short cannot be used for live spot trading")
        if self.live.require_exchange_stop and not self.live.native_stop_loss:
            errors.append("live.require_exchange_stop needs live.native_stop_loss: true")
        if self.mode == "live":
            from .execution.live import VENUES

            if self.exchange.id not in VENUES:
                errors.append(f"live trading supports {', '.join(sorted(VENUES))} only (got {self.exchange.id!r}); "
                              f"signals and paper trading work with any exchange")
        if not 0.0 <= self.core.fraction < 1.0:
            errors.append("core.fraction must be in [0, 1)")
        if self.core.fraction > 0 and self.mode == "live":
            errors.append("the core sleeve (core.fraction > 0) is paper-only in this version")
        if self.core.fraction > 0 and (not self.core.symbols or not self.core.sma_days):
            errors.append("core.symbols and core.sma_days must not be empty")
        if self.costs.entry_order not in ("market", "limit"):
            errors.append("costs.entry_order must be 'market' or 'limit'")
        if self.mode == "live" and self.costs.entry_order == "limit":
            errors.append("costs.entry_order: limit is paper-only in this version (live uses market entries)")
        if self.selection.btc_filter not in ("off", "auto"):
            errors.append("selection.btc_filter must be 'off' or 'auto'")
        if self.guards.vol_breaker_ratio <= 1.0:
            errors.append("guards.vol_breaker_ratio must be > 1")
        if not 0.3 <= self.selection.in_sample_fraction <= 0.9:
            errors.append("selection.in_sample_fraction must be in [0.3, 0.9]")
        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))


def _build(cls, data: dict[str, Any], path: str):
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    kwargs = {}
    for key, value in (data or {}).items():
        if key not in known or key == "secrets":
            raise ValueError(f"Unknown config key '{path}{key}'")
        ftype = hints[key]
        if is_dataclass(ftype):
            if not isinstance(value, dict):
                raise ValueError(f"Config key '{path}{key}' must be a mapping")
            kwargs[key] = _build(ftype, value, f"{path}{key}.")
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path | None = "config.yaml", env_file: str | None = ".env") -> BotConfig:
    """Load YAML settings (optional) and secrets from the environment / .env file."""
    if env_file and Path(env_file).exists():
        from dotenv import load_dotenv

        load_dotenv(env_file)
    data: dict[str, Any] = {}
    if path and Path(path).exists():
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
    cfg = _build(BotConfig, data, "")
    cfg.strategies = {k: (v or {}) for k, v in (cfg.strategies or {}).items()}
    cfg.secrets = Secrets.from_env()
    if os.getenv("TRADEBOT_MODE"):
        cfg.mode = os.environ["TRADEBOT_MODE"]
    cfg.validate()
    return cfg
