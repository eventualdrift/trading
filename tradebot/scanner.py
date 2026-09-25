"""Market scanner: runs the selected strategies on the latest closed candle of
every symbol, scores each setup with the ML filter and ranks the opportunities."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .backtest.engine import reward_risk
from .backtest.selection import Selection
from .config import BotConfig
from .ml.features import candidate_features, market_features
from .ml.model import SignalModel
from .models import Position, Signal
from .strategies import Strategy, make_strategy
from .timeframes import drop_unclosed, index_ms, tf_ms

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    accepted: list[Signal] = field(default_factory=list)
    filtered: list[Signal] = field(default_factory=list)
    exits: dict[int, str] = field(default_factory=dict)  # position id -> reason
    errors: list[str] = field(default_factory=list)


class Scanner:
    def __init__(self, market, cfg: BotConfig, selection: Selection | None, model: SignalModel | None):
        self.market = market
        self.cfg = cfg
        self.model = model if cfg.ml.enabled else None
        self.combos: dict[str, list[Strategy]] = {}
        for c in (selection.selected if selection else []):
            self.combos.setdefault(c.timeframe, []).append(make_strategy(c.strategy, c.params))

    @property
    def threshold(self) -> float | None:
        if self.model is None:
            return None
        return self.cfg.ml.min_probability if self.cfg.ml.min_probability is not None else self.model.threshold

    def _strategy_for(self, pos: Position) -> Strategy:
        for s in self.combos.get(pos.timeframe, []):
            if s.name == pos.strategy:
                return s
        return make_strategy(pos.strategy, self.cfg.strategies.get(pos.strategy, {}))

    def scan(self, tf: str, symbols: list[str], now_ms: int, candle_open_ms: int,
             open_positions: list[Position] = ()) -> ScanResult:
        res = ScanResult()
        strategies = self.combos.get(tf, [])
        by_symbol: dict[str, list[Position]] = {}
        for p in open_positions:
            if p.timeframe == tf:
                by_symbol.setdefault(p.symbol, []).append(p)
        # EMAs need several multiples of their length to converge to the values the
        # backtest saw on full history; 1000 is the max most exchanges return at once.
        need = min(1000, 4 * max([s.warmup for s in strategies] + [250]))
        for symbol in dict.fromkeys([*symbols, *by_symbol]):
            try:
                df = drop_unclosed(self.market.fetch_ohlcv_df(symbol, tf, limit=need), tf, now_ms)
            except Exception as exc:
                res.errors.append(f"{symbol} {tf}: {exc}")
                continue
            if df.empty or int(index_ms(df.index)[-1]) != candle_open_ms:
                res.errors.append(f"{symbol} {tf}: latest candle missing/stale")
                continue
            for pos in by_symbol.get(symbol, []):
                pop = self._strategy_for(pos).populate(df)
                if bool(pop[f"exit_{pos.side}"].iloc[-1]):
                    res.exits[pos.id] = "exit_signal"
            mkt = None
            for strat in strategies:
                if len(df) < strat.warmup:
                    continue
                pop = strat.populate(df)
                row = pop.iloc[-1]
                for side in ("long", "short"):
                    if side == "short" and not self.cfg.allow_short:
                        continue
                    if not bool(row[f"enter_{side}"]):
                        continue
                    close = float(row["close"])
                    sl, tp = float(row[f"{side}_sl"]), float(row[f"{side}_tp"])
                    rr = reward_risk(side, close, sl, tp)
                    if rr < self.cfg.risk.min_reward_risk:
                        continue
                    if mkt is None:
                        mkt = market_features(df)
                    X = candidate_features(mkt.iloc[[-1]], np.array([side]), np.array([strat.name]),
                                           tf, np.array([close]), np.array([sl]), np.array([tp]))
                    prob = float(self.model.predict_proba(X)[0]) if self.model is not None else None
                    close_ms = candle_open_ms + tf_ms(tf)
                    sig = Signal(
                        symbol=symbol, timeframe=tf, strategy=strat.name, side=side,
                        entry=close, stop_loss=sl, take_profit=tp, candle_time=candle_open_ms,
                        created_at=now_ms, valid_until=close_ms + tf_ms(tf),
                        max_hold_until=close_ms + strat.max_hold_bars * tf_ms(tf),
                        reason=strat.explain(row, side), confidence=prob,
                        expected_r=(prob * rr - (1 - prob)) if prob is not None else None,
                        features={k: (None if v != v else float(v)) for k, v in X.iloc[0].items()},
                    )
                    if prob is not None and prob < self.threshold:
                        sig.status, sig.note = "filtered", f"ML probability {prob:.0%} < {self.threshold:.0%}"
                        res.filtered.append(sig)
                    else:
                        res.accepted.append(sig)
        res.accepted.sort(key=lambda s: (s.expected_r if s.expected_r is not None else s.reward_risk - 1), reverse=True)
        return res
