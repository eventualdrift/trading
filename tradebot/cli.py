"""Command line interface: `tradebot <command>`."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .config import BotConfig, load_config
from .universe import describe_universe, select_universe

log = logging.getLogger("tradebot")


# --------------------------------------------------------------------- helpers
def _market(cfg: BotConfig, synthetic: bool = False, authenticated: bool = False):
    if synthetic:
        from .data import SyntheticMarket

        days = max(cfg.data.history_days.get(tf, 365) for tf in cfg.timeframes) + 30
        return SyntheticMarket(min(cfg.universe.top_n, 8), days=days, base_tf="15m")
    from .data import ExchangeClient

    s = cfg.secrets
    if authenticated or not cfg.exchange.data_exchange_id:
        return ExchangeClient(
            cfg.exchange.id,
            api_key=s.api_key if authenticated else None,
            secret=s.api_secret if authenticated else None,
            password=s.api_password if authenticated else None,
            sandbox=cfg.exchange.sandbox,
            market_type=cfg.exchange.market_type,
        )
    return ExchangeClient(cfg.exchange.data_exchange_id, market_type=cfg.exchange.market_type)


def _bot_db(cfg: BotConfig):
    """The database to report on: an observed bot's (read-only), else this config's own."""
    from .db import Database

    return Database(cfg.bot_db_path, readonly=bool(cfg.observe_state_dir))


def _store(cfg: BotConfig, market):
    from .data import OHLCVStore

    return None if market.id == "synthetic" else OHLCVStore(cfg.data.dir, market.id)


def _notifier(cfg: BotConfig):
    from .notify import ConsoleNotifier, MultiNotifier, TelegramNotifier

    s = cfg.secrets
    if cfg.telegram.enabled and s.telegram_token and s.telegram_chat_id:
        return MultiNotifier(ConsoleNotifier(log.info), TelegramNotifier(s.telegram_token, s.telegram_chat_id))
    if cfg.telegram.enabled:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) - printing to console")
    return ConsoleNotifier()


# -------------------------------------------------------------------- commands
def cmd_init(args, cfg):
    for src, dst in (("config.example.yaml", "config.yaml"), (".env.example", ".env")):
        if Path(dst).exists():
            print(f"{dst} already exists - leaving it alone")
        elif Path(src).exists():
            shutil.copy(src, dst)
            print(f"created {dst} from {src}")
    print("Next: edit config.yaml and .env, then run `tradebot learn`.")


def cmd_learn(args, cfg):
    from .db import Database
    from .learning import learning_cycle, load_brain

    if cfg.learning.follow_state_dir:
        sys.exit(f"This instance uses the strategies in {cfg.learning.follow_state_dir} "
                 f"(learning.follow_state_dir) - run `tradebot learn` with that instance's config.")
    market = _market(cfg, args.synthetic)
    _, current = load_brain(cfg)
    db = Database(cfg.state_path / "tradebot.db")
    res = learning_cycle(cfg, market, store=_store(cfg, market), db=db, current_model=current)
    print("\n" + res.summary)


def cmd_backtest(args, cfg):
    import pandas as pd

    from .backtest import format_metrics, portfolio_simulation, summarize
    from .backtest.selection import run_combo
    from .learning import load_context, load_datasets

    market = _market(cfg, args.synthetic)
    symbols = args.symbols or select_universe(market, cfg, log_fn=print)
    tfs = [args.timeframe] if args.timeframe else cfg.timeframes
    strategies = [args.strategy] if args.strategy else list(cfg.strategies)
    cfg.timeframes = tfs
    datasets = load_datasets(market, cfg, symbols, _store(cfg, market))
    context = load_context(market, cfg, _store(cfg, market))
    r = cfg.risk
    all_trades = []
    print(cfg.costs_description())
    for tf in tfs:
        for name in strategies:
            cand: dict = {}
            is_t, oos_t, _ = run_combo(datasets[tf], name, cfg.strategies.get(name, {}), tf, cfg, context,
                                       candidates=cand)
            all_trades += is_t + oos_t
            trades = cand.get("is", []) + cand.get("oos", [])  # the account replay picks from every signal
            curve, taken = portfolio_simulation(trades, risk_per_trade_pct=r.risk_per_trade_pct,
                                                max_position_pct=r.max_position_pct,
                                                max_open_positions=r.max_open_positions)
            print(f"{name}@{tf:<4} {format_metrics(summarize(taken, curve))}")
    if args.trades_csv and all_trades:
        pd.DataFrame([t.to_dict() for t in all_trades]).to_csv(args.trades_csv, index=False)
        print(f"trades written to {args.trades_csv}")


def cmd_scan(args, cfg):
    from .learning import load_brain
    from .notify import formatting as fmt
    from .scanner import Scanner
    from .timeframes import last_closed_open_ms

    selection, model = load_brain(cfg)
    if not selection or not selection.selected:
        sys.exit("No validated strategies yet - run `tradebot learn` first.")
    market = _market(cfg, args.synthetic)
    symbols = select_universe(market, cfg)
    scanner = Scanner(market, cfg, selection, model)
    now = market.now_ms()
    found = 0
    for tf in selection.timeframes():
        res = scanner.scan(tf, symbols, now, last_closed_open_ms(now, tf))
        for s in res.accepted:
            found += 1
            conf = f"{s.confidence:.0%}" if s.confidence is not None else "n/a"
            print(f"{s.side.upper():<5} {s.symbol:<12} {tf:<4} {s.strategy:<9} entry {fmt.fmt_price(s.entry)} "
                  f"SL {fmt.fmt_price(s.stop_loss)} TP {fmt.fmt_price(s.take_profit)} "
                  f"R:R 1:{s.reward_risk:.1f} p(win) {conf}\n      {s.reason}")
        for s in res.filtered:
            print(f"  (filtered) {s.symbol} {tf} {s.strategy}: {s.note}")
    if not found:
        print("No setups on the latest closed candles. (That is normal - good setups are rare.)")


def cmd_run(args, cfg):
    from .bot import TradingBot
    from .db import Database
    from .execution import LiveBroker, PaperBroker
    from .learning import learning_cycle, load_brain
    from .report import format_readiness, readiness

    if cfg.observe_state_dir:
        sys.exit("This config observes another bot (observe_state_dir): it reports, it never runs a bot.")
    db = Database(cfg.state_path / "tradebot.db")
    live = cfg.mode == "live"
    if live:
        s = cfg.secrets
        if not (s.api_key and s.api_secret):
            sys.exit("Live mode needs EXCHANGE_API_KEY and EXCHANGE_API_SECRET in .env")
        checks = readiness(db, cfg, int(time.time() * 1000))
        if not all(c.passed for c in checks) and not args.force_live:
            print("Paper-trading track record:\n" + format_readiness(checks))
            sys.exit("\nRefusing to trade real money. Keep paper trading, or pass --force-live if you accept the risk.")
    market = _market(cfg, authenticated=live)
    learn_market = _market(cfg)  # separate connection: learning runs in a background thread
    store = _store(cfg, learn_market)
    try:
        broker = (LiveBroker(market, cfg.exchange.quote, cfg.live.native_stop_loss, cfg.live.fill_timeout_seconds)
                  if live else
                  PaperBroker(db, cfg.costs_model(), cfg.paper.starting_balance, market))
    except ValueError as exc:
        sys.exit(str(exc))

    def learn_now(current):
        res = learning_cycle(cfg, learn_market, store=store, db=db, current_model=current, log_fn=log.info)
        return res.selection, res.model, res.summary

    follow = cfg.learning.follow_state_dir
    learner = None if follow else learn_now  # a follower never learns: it reloads the leader's brain
    selection, model = load_brain(cfg)
    if follow:
        print(f"Using the strategies in {follow} (learning.follow_state_dir)"
              + ("" if selection else " - none there yet; they'll be picked up once that instance learns"))
    elif selection is None and cfg.learning.learn_on_start:
        print("No strategy selection found - running the first learning cycle (this can take a while)...")
        selection, model_new, summary = learner(model)
        model = model_new or model
        print(summary)
        db.kv_set("last_learn_ms", learn_market.now_ms())
    bot = TradingBot(cfg, market, broker, db, _notifier(cfg), selection=selection, model=model, learner=learner)
    if not bot.active_timeframes():
        print("WARNING: no validated strategy - the bot will only manage existing positions and "
              "re-learn on schedule (or on /learn).")
    print(f"tradebot running in {cfg.mode.upper()} mode on {market.id}. Ctrl+C to stop.")
    import signal

    # launchd / docker stop and restart with SIGTERM: finish the tick and record a clean stop
    signal.signal(signal.SIGTERM, lambda *_: bot.stop("SIGTERM: service stopped or restarted"))
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        bot.stop()
        print("stopped")


def ml_status(model, report_path, switched_off: bool = False) -> str:
    """Which ML model filters paper's signals now, and what the last learn's training decided (a
    learn that doesn't promote a new model leaves the previous one in force). ``switched_off``: the
    config asks for it, but it stays off until the account replay models it (config.ML_IN_REPLAY)."""
    import json

    import pandas as pd

    def when(t):
        return pd.Timestamp(t, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M") if t else "?"

    if switched_off:
        lines = ["ML filter: OFF - switched off until the account replay models it (paper runs only what the "
                 "backtest models; BACKLOG a2, then a registered test decides). Paper takes every signal at 1x "
                 "size, like the backtest"]
    elif model is None:
        lines = ["ML filter: not active (no promoted model) - paper takes every signal, like the backtest"]
    else:
        r = model.report
        lines = [f"ML filter: ACTIVE - model trained {when(r.trained_at)} UTC on data to {str(r.trained_until)[:10]}, "
                 f"threshold {model.threshold:.0%}, confidence sizing {'on' if r.confidence_scaling else 'off'}. "
                 f"Paper filters and sizes signals with it; the backtest does not."]
    if report_path.exists():
        last = json.loads(report_path.read_text())
        lines.append(f"  Last learn's ML training ({when(last.get('trained_at'))} UTC): "
                     + ("PROMOTED" if last.get("promoted") else f"not deployed - {last.get('reason', '')}")
                     + (" - from before the switch-off; no model is used and learn trains none now" if switched_off
                        else ""))
    return "\n".join(lines)


def cmd_report(args, cfg):
    from .learning import MODEL_REPORT_FILE, load_brain
    from .notify import formatting as fmt
    from .report import format_readiness, readiness, sleeve_summary

    db = _bot_db(cfg)
    summary = sleeve_summary(db, cfg, cfg.mode)
    if summary:
        print(summary + "\n")
    for mode in ("paper", "live"):
        closed = db.closed_positions(mode)
        if closed or mode == "paper":
            print(fmt.format_performance(closed, cfg.exchange.quote, f"{mode.title()} track record").replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))
            print()
    selection, model = load_brain(cfg)
    if selection:
        print("Selected strategies:", ", ".join(c.key for c in selection.selected) or "none")
    print(ml_status(model, cfg.brain_path / MODEL_REPORT_FILE, switched_off=cfg.ml.enabled and not cfg.ml.active))
    print("\nGo-live readiness (paper track record):")
    print(format_readiness(readiness(db, cfg, int(time.time() * 1000))))


def cmd_project(args, cfg):
    from .learning import load_brain, load_context, load_datasets
    from .projection import format_projection, out_of_sample_trades, project

    selection, _ = load_brain(cfg)
    if not selection or not selection.selected:
        sys.exit("No validated strategies yet - run `tradebot learn` first (nothing to project).")
    market = _market(cfg, args.synthetic)
    symbols = select_universe(market, cfg)
    cfg.timeframes = selection.timeframes()
    bench_symbol = f"BTC/{cfg.exchange.quote}"
    datasets = load_datasets(market, cfg, sorted(set(symbols) | {bench_symbol}), _store(cfg, market),
                             log_fn=lambda *_: None)
    context = load_context(market, cfg, _store(cfg, market), log_fn=lambda *_: None)
    trades = out_of_sample_trades(selection, {tf: {s: d for s, d in ds.items() if s in symbols}
                                              for tf, ds in datasets.items()}, cfg, context)
    bench = next((ds[bench_symbol] for ds in datasets.values() if bench_symbol in ds), None)
    p = project(trades, cfg, capital=args.capital, runs=args.runs, benchmark=bench, benchmark_symbol=bench_symbol)
    print(format_projection(p, cfg.exchange.quote))


def cmd_research(args, cfg):
    from .learning import load_brain, load_context, load_datasets
    from .research import add_note, breaker_study, format_breaker_study

    if args.topic.startswith("oos-"):
        return _research_oos(args, cfg)
    if args.topic in ("register", "protocols", "run", "record"):
        return _research_protocols(args, cfg)
    if args.topic == "note":
        if not args.id or not args.text:
            sys.exit("usage: tradebot research note --id <test id> --text \"...\"")
        add_note(cfg.state_path, args.id, args.text)
        print(f"Note added to {args.id} (research/ledger.jsonl); the recorded result is unchanged.")
        return
    selection, _ = load_brain(cfg)
    if not selection or not selection.selected:
        sys.exit("No validated strategies yet - run `tradebot learn` first.")
    if args.topic == "sizing":
        return _research_sizing(args, cfg, selection)
    if args.topic == "core":
        return _research_core(args, cfg, selection)
    market = _market(cfg, args.synthetic)
    frozen = None
    if args.universe_file:
        from .universe import load_universe

        frozen = load_universe(args.universe_file)
    symbols = list(frozen["symbols"]) if frozen else select_universe(market, cfg)
    end = frozen["data_end_ms"] if frozen else None
    cfg.timeframes = selection.timeframes()
    store = _store(cfg, market)
    datasets = load_datasets(market, cfg, symbols, store, log_fn=lambda *_: None, end_ms=end)
    context = load_context(market, cfg, store, log_fn=print, end_ms=end)
    if context is None:
        sys.exit("BTC history unavailable - cannot evaluate the breaker.")
    print("Volatility circuit breaker: selected strategies with and without it "
          f"(ratio = BTC's last {cfg.context.vol_short}h volatility / the {cfg.context.vol_long}h before)\n")
    print(format_breaker_study(breaker_study(selection, datasets, cfg, context, tuple(args.ratios))))


def _portfolio_inputs(cfg, selection, synthetic: bool, days: int, universe_file: str | None = None,
                      save_dir: str | None = "reports", current_selection: bool = False,
                      ml_replay: bool | None = None) -> dict:
    """Everything the whole-account backtests need: core closes, satellite candidate trades,
    daily closes to value them, per-strategy split stats and the universe used.

    ``universe_file``: rerun on a saved coin list, data end date AND strategy selection
    (reproducible); ``current_selection`` reruns it with today's selection instead. Without it the
    universe is today's, and it is saved to ``save_dir`` so the run can be reproduced later.
    ``ml_replay``: model the ML filter walk-forward (BACKLOG a2); None: only when config.ML_IN_REPLAY
    says the bot uses it."""
    from .backtest.engine import refine_flat_times
    from .backtest.selection import run_combo
    from .learning import load_context, load_datasets, load_frame
    from .portfolio import combo_split_stats
    from .provenance import code_version, config_differences, config_hash, settings_snapshot, without_timeframes
    from .timeframes import tf_ms
    from .universe import candle_pins, compare_candles, load_universe, save_universe, saved_selection

    market = _market(cfg, synthetic)
    store = _store(cfg, market)
    frozen = load_universe(universe_file) if universe_file else None
    today = selection
    selection_source = "today's (learn's latest)"
    if frozen and not current_selection:
        saved = saved_selection(frozen, cfg)
        if saved is not None:
            selection, selection_source = saved
        elif frozen.get("selection"):
            selection_source = ("today's - the saved run's selection could not be recovered (the file saved only "
                                "the strategy names)")
    end = frozen["data_end_ms"] if frozen else None
    data_end = end or market.now_ms()
    now = None if frozen else data_end  # one 'now' for every frame: the pins then match a frozen rerun's window
    core_closes = {}
    for sym in cfg.core.symbols:
        core_closes[sym] = load_frame(market, store, sym, "1d", days, now_ms=now, end_ms=end)["close"]
    trades, combos, sat_closes, sat_marks, universe, ml_rep = [], [], {}, {}, None, None
    if selection and selection.selected:
        keys = [c.key for c in selection.selected]
        if frozen:
            symbols = list(frozen["symbols"])
            if frozen.get("selection") and sorted(frozen["selection"]) != sorted(keys):
                print(f"WARNING: the strategies used ({', '.join(keys)}) differ from the saved run's "
                      f"({', '.join(frozen['selection'])}) - results will differ for that reason.")
        else:
            symbols = select_universe(market, cfg)
        configured_tfs = list(cfg.timeframes)  # live's learn trains the ML filter on every configured timeframe
        cfg.timeframes = selection.timeframes()
        datasets = load_datasets(market, cfg, symbols, store, now_ms=now, log_fn=lambda *_: None, end_ms=end)
        context = load_context(market, cfg, store, now_ms=now, log_fn=lambda *_: None, end_ms=end)
        for c in selection.selected:
            cand: dict = {}
            is_t, oos_t, _ = run_combo(datasets.get(c.timeframe, {}), c.strategy, c.params, c.timeframe, cfg, context,
                                       candidates=cand)
            trades += cand.get("is", []) + cand.get("oos", [])  # every signal: the account replay picks
            combos.append(combo_split_stats(c.key, is_t, oos_t))  # the strategy on its own
        refine_flat_times(trades, datasets, cfg.costs.slippage_rate)
        first = {}
        for tf, ds in sorted(datasets.items(), key=lambda kv: kv[0] != "1d"):  # daily data first
            for sym, df in ds.items():
                if sym not in sat_closes:  # daily closes to value open trades each day
                    sat_closes[sym] = df["close"] if tf == "1d" else df["close"].resample("1D").last().dropna()
                first[sym] = min(first.get(sym, df.index[0]), df.index[0])
        for tf in sorted(datasets, key=tf_ms):  # finest first: the satellite's value at each signal (the breakers)
            for sym, df in datasets[tf].items():
                sat_marks.setdefault(sym, []).append(df["close"])
        pins = candle_pins(datasets, core_closes)  # the candles this run loaded, pinned for frozen reruns
        if ml_replay is None:  # a frozen rerun follows its file; otherwise only when the bot uses the filter
            ml_replay = bool(frozen["ml_replayed"]) if frozen and frozen.get("ml_replayed") is not None else cfg.ml.active
        if ml_replay:  # the ML filter, as a weekly learn would have had it
            from .ml.dataset import build_candidates
            from .ml.replay import apply, learn_times, walk_forward

            chosen = {(c.strategy, c.timeframe): c.params for c in selection.combos}
            extra = [tf for tf in configured_tfs if tf not in datasets]  # the learn's other timeframes
            datasets_ml = dict(datasets)
            if extra:
                cfg.timeframes, kept = extra, cfg.timeframes
                datasets_ml.update(load_datasets(market, cfg, symbols, store, now_ms=now, log_fn=lambda *_: None,
                                                 end_ms=end))
                cfg.timeframes = kept
                pins = candle_pins(datasets_ml, core_closes)
            cands = build_candidates(datasets_ml, cfg, context, params_for=chosen)
            focus = {(c.strategy, c.timeframe) for c in selection.selected}
            start = min(int(v.value // 1_000_000) for v in first.values()) if first else data_end
            ml_rep = apply(trades, cands, walk_forward(cands, cfg, learn_times(start, data_end), focus), cfg)
        source = frozen["source"] if frozen else describe_universe(cfg, time.strftime("%Y-%m-%d"))
        code, snap = code_version(), settings_snapshot(cfg)  # the config file's settings, as loaded
        run = {"timeframes_used": list(cfg.timeframes), "core_fraction_used": cfg.core.fraction,
               "ml_replayed": ml_rep is not None}
        path = universe_file
        if not frozen and save_dir and not synthetic:
            stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(data_end / 1000))
            path = save_universe(Path(save_dir) / f"universe-{stamp}.json", symbols, data_end, source, keys, cfg,
                                 code=code, config=snap, selection=selection, run=run, candles=pins)
        universe = {"source": source, "symbols": symbols, "first": first, "data_end_ms": data_end,
                    "file": path, "frozen": bool(frozen), "code": code, "config": snap,
                    "config_hash": config_hash(snap), **run, "selection_now": keys,
                    "selection_created": selection.created_at,
                    "selection_saved": (frozen or {}).get("selection"),
                    "selection_source": selection_source,
                    "selection_today": [c.key for c in today.selected] if today else [],
                    "candles_pinned": sum(len(v) for v in pins.values())}
        if frozen:
            universe["saved_code"] = frozen.get("code")
            saved_cfg = frozen.get("config")
            universe["saved_config_hash"] = config_hash(saved_cfg) if saved_cfg else None
            legacy = saved_cfg is not None and "timeframes_used" not in frozen  # its settings hold the run's timeframes
            universe["legacy_settings"] = legacy
            universe["config_diffs"] = (config_differences(*(without_timeframes(x) for x in (saved_cfg, snap))) if legacy
                                        else config_differences(saved_cfg, snap)) if saved_cfg else None
            universe["candle_diffs"] = compare_candles(frozen.get("candles"), pins)  # None: nothing pinned then
            outdated = ((frozen.get("code"), universe["saved_config_hash"]) != (code, universe["config_hash"])
                        or not frozen.get("selection_full") or not frozen.get("candles"))
            if outdated and not current_selection:
                # same coins, data end and selection, stamped with THIS run's code, settings and candles: a
                # complete reference
                base = Path(universe_file).stem.split("@")[0]  # the original run's name, without an older stamp
                stamped = Path(universe_file).with_name(f"{base}@{code.split('+')[0]}-{universe['config_hash']}.json")
                if not stamped.exists() or not all(load_universe(stamped).get(k) for k in ("selection_full", "candles")):
                    save_universe(stamped, symbols, data_end, source, keys, cfg, code=code, config=snap,
                                  selection=selection, run=run, candles=pins)
                else:  # kept as it is: say if it was written from other candles than this run's
                    universe["stamped_candle_diffs"] = compare_candles(load_universe(stamped).get("candles"), pins)
                universe["stamped_file"] = str(stamped)
    return {"core_closes": core_closes, "trades": trades, "combos": combos, "sat_closes": sat_closes,
            "sat_marks": sat_marks, "universe": universe, "ml_replay": ml_rep}


def _research_oos(args, cfg):
    """The pre-2017 BTC out-of-sample test: fetch -> register -> run (once) -> show."""
    from . import oos
    from .provenance import code_version

    step = args.topic
    if step == "oos-fetch":
        if args.synthetic:
            sys.exit("the out-of-sample test uses real Bitstamp data only")
        from .data import ExchangeClient

        path = oos.fetch(ExchangeClient("bitstamp", market_type="spot"), cfg.data.dir)
        print(oos.quality_summary(path))
        print("\nNext: check the summary, then register the protocol: tradebot research oos-register")
    elif step == "oos-register":
        print(oos.format_protocol())
        path = oos.data_path(cfg.data.dir)
        if not path.exists():
            sys.exit("\nNo data yet - run: tradebot research oos-fetch")
        try:
            reg = oos.register(cfg.state_path, cfg.data.dir, code_version())
        except RuntimeError as exc:
            sys.exit(f"\nNOT registered: {exc}")
        print(f"\nRegistered {reg['registered_at']} with data SHA-256 {reg['data_sha256']} (code {reg['code']}). "
              f"The run will refuse any other data. Next: tradebot research oos-run")
    elif step == "oos-run":
        try:
            res = oos.run(cfg.state_path, cfg.data.dir, code_version())
        except RuntimeError as exc:
            sys.exit(str(exc))
        print(oos.format_result(res))
        print("\nRecorded in the research ledger. This test does not run again.")
    elif step == "oos-show":
        res = oos.result(cfg.state_path)
        reg = oos.registration(cfg.state_path)
        if res:
            print(oos.format_result(res))
        elif reg:
            print(f"Registered {reg['registered_at']} (data SHA-256 {reg['data_sha256']}); not run yet.")
        else:
            print("Not registered yet.")


def _research_core(args, cfg, selection):
    from .core_research import format_core_robustness, reset_phase_study, sma_scale_study
    from .portfolio import _universe_lines, portfolio_backtest

    fraction = cfg.core.fraction or 0.65
    cfg.core.fraction = fraction
    x = _portfolio_inputs(cfg, selection, args.synthetic, args.days, args.universe_file)
    res = portfolio_backtest(x["core_closes"], x["trades"], cfg, capital=args.capital, fraction=fraction,
                             since="2022-01-01", sat_closes=x["sat_closes"], combos=x["combos"],
                             universe=x["universe"], sat_marks=x["sat_marks"], ml_replay=x["ml_replay"])
    print(format_core_robustness(res, reset_phase_study(res), sma_scale_study(x["core_closes"], res, cfg), cfg))
    print("\n".join([""] + _universe_lines(res)))
    if not args.synthetic:
        import json

        folder = cfg.state_path / "research"
        folder.mkdir(parents=True, exist_ok=True)
        u = x["universe"] or {}
        with open(folder / "ledger.jsonl", "a") as fh:  # what was looked at, for the variant count
            fh.write(json.dumps({"id": "core-robustness", "kind": "report", "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "variants_looked_at": {"reset_phases": int(res.reset_days),
                                                        "sma_scales": [0.8, 1.0, 1.2]},
                                 "selected": None, "universe_file": u.get("file"),
                                 "data_end_ms": u.get("data_end_ms"), "code": u.get("code")}) + "\n")


def _research_sizing(args, cfg, selection):
    from .research import SIZING_TEST, format_sizing_study, previous_result, record_study, sizing_study

    prev = previous_result(cfg.state_path)
    if prev and not args.rerun:
        print(f"This test already ran on {prev.get('ran_at')}: {'PASS' if prev['verdict']['pass'] else 'FAIL'}. "
              f"It is a one-time test - its first result stands. (--rerun shows it again, marked as a re-run.)")
        for n in prev.get("notes", []):
            print(f"  note ({n['at']}): {n['note']}")
        return
    if cfg.risk.max_open_risk_pct is not None:
        sys.exit("risk.max_open_risk_pct is already set in the config - the test compares against today's rule.")
    fraction = cfg.core.fraction or 0.65
    cfg.core.fraction = fraction
    # a test registered with a frozen universe runs on it (coins + data end date fixed in advance)
    x = _portfolio_inputs(cfg, selection, args.synthetic, args.days,
                          args.universe_file or SIZING_TEST.get("universe_file"))
    study = sizing_study(x["core_closes"], x["trades"], cfg, capital=args.capital, fraction=fraction,
                         sat_closes=x["sat_closes"], combos=x["combos"], universe=x["universe"],
                         sat_marks=x["sat_marks"])
    if prev:
        print(f"RE-RUN - not a new test. The first result ({prev.get('ran_at')}: "
              f"{'PASS' if prev['verdict']['pass'] else 'FAIL'}) stands.\n")
    print(format_sizing_study(study))
    if not args.synthetic:
        path = record_study(cfg.state_path, study, rerun=bool(prev))
        print(f"\nRecorded: {path} (and research/ledger.jsonl)")
    else:
        print(f"\n(synthetic data: not recorded - {SIZING_TEST['id']} can still be run once on real data)")


def cmd_portfolio_backtest(args, cfg):
    from .learning import load_brain
    from .portfolio import format_portfolio_backtest, portfolio_backtest

    selection, _ = load_brain(cfg)
    fraction = args.core_fraction if args.core_fraction is not None else (cfg.core.fraction or 0.65)
    cfg.core.fraction = fraction  # recorded with the run: the split it actually used
    x = _portfolio_inputs(cfg, selection, args.synthetic, args.days, args.universe_file,
                          current_selection=args.current_selection, ml_replay=True if args.ml_replay else None)
    res = portfolio_backtest(x["core_closes"], x["trades"], cfg, capital=args.capital, fraction=fraction,
                             since=args.since or None, sat_closes=x["sat_closes"], combos=x["combos"],
                             sat_marks=x["sat_marks"], ml_replay=x["ml_replay"],
                             universe=x["universe"])
    print(format_portfolio_backtest(res, cfg.exchange.quote))


def cmd_dashboard(args, cfg):
    from .dashboard import serve_dashboard, write_dashboard

    db = _bot_db(cfg)
    if not args.serve:
        path = write_dashboard(db, cfg, args.out)
        print(f"dashboard written to {path.resolve()} - open it in a browser")
        return
    port = args.port or cfg.dashboard.port
    try:
        server = serve_dashboard(db, cfg, port)
    except OSError as exc:
        sys.exit(f"cannot use port {port}: {exc} (another instance? pass --port or set dashboard.port)")
    print(f"dashboard at http://127.0.0.1:{port} (this computer only). Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


def cmd_compare_entries(args, cfg):
    import pandas as pd

    from .compare import compare_entries, config_differences, format_comparison, write_csv
    from .db import Database

    other = load_config(args.other, args.other_env)
    if Path(other.state_dir).resolve() == Path(cfg.state_dir).resolve():
        sys.exit("--other must be the limit-entry instance's config (a different state_dir)")
    db_b_path = Path(other.state_dir) / "tradebot.db"
    if not db_b_path.exists():
        sys.exit(f"no database at {db_b_path} - has the second instance run yet?")
    since = int(pd.Timestamp(args.since, tz="UTC").value // 1_000_000) if args.since else None
    rows = compare_entries(Database(cfg.state_path / "tradebot.db"), Database(db_b_path), cfg.mode, since)
    print(format_comparison(rows, cfg, other, config_differences(cfg, other)))
    if args.csv:
        write_csv(rows, args.csv)
        print(f"\nper-signal rows written to {args.csv}")


def cmd_config_set(args, cfg):
    from .config_edit import config_set

    try:
        report = config_set(args.config, args.assignments)
    except (ValueError, OSError) as exc:
        sys.exit(f"{args.config} NOT changed: {exc}")
    print(f"{args.config} updated:")
    for line in report:
        print(f"  {line}")


def _reconcile_run(cfg, since_ms: int | None, end_ms: int | None, synthetic: bool = False):
    """Paper vs backtest over [since, end] on frozen candles -> (Reconciliation, report text).
    ``since_ms`` None: from the first paper record; ``end_ms`` None: the last full hour.
    Raises ValueError when there is nothing to reconcile."""
    import pandas as pd

    from .learning import load_brain, load_context, load_datasets, load_frame
    from .provenance import code_version, settings_hash
    from .reconcile import format_reconciliation, reconcile
    from .timeframes import last_closed_open_ms, tf_ms

    db = _bot_db(cfg)
    mode = cfg.mode
    market = _market(cfg, synthetic)
    store = _store(cfg, market)
    # frozen candles: nothing in the cache changes and --end reproduces the run
    end = end_ms if end_ms is not None else last_closed_open_ms(market.now_ms(), "1h") + 3_600_000
    since = since_ms
    if since is None:  # the paper period: from the first signal or equity snapshot
        firsts = [s.created_at for s in db.signals_since(0)[:1]]
        snaps = db.snapshots(mode)
        if len(snaps):
            firsts.append(int(snaps.index[0].value // 1_000_000))
        if not firsts:
            raise ValueError("no paper history yet (no signals or equity snapshots)")
        since = min(firsts)
    selection, _ = load_brain(cfg)
    logged = db.botlog(mode, "universe")
    if logged and logged[0][0] <= since:
        fallback = list(logged[0][1])
    else:  # coin lists not logged for (part of) the window: coins paper signalled or held + today's list
        fallback = sorted({s.symbol for s in db.signals_since(since - 86_400_000)}
                          | {p.symbol for p in db.positions_since(mode, since)}
                          | set(logged[0][1] if logged else []) | set(select_universe(market, cfg)))
    symbols = sorted(set(fallback).union(*(set(d) for _, d in logged)))
    tfs = {c.timeframe for c in (selection.selected if selection else [])}
    for _, d in db.botlog(mode, "selection"):
        tfs |= {c["timeframe"] for c in d.get("combos", [])}
    if not tfs:
        raise ValueError("no strategies selected - nothing to reconcile")
    cfg.timeframes = sorted(tfs, key=tf_ms)
    print(f"Loading candles for {len(symbols)} coins ({', '.join(cfg.timeframes)}) to "
          f"{pd.Timestamp(end, unit='ms', tz='UTC'):%Y-%m-%d %H:%M} UTC ...", file=sys.stderr)
    datasets = load_datasets(market, cfg, symbols, store, log_fn=lambda *_: None, end_ms=end)
    context = load_context(market, cfg, store, log_fn=lambda *_: None, end_ms=end)
    days = max(cfg.core.sma_days) + 60 + (end - since) // 86_400_000
    core = ({s: load_frame(market, store, s, "1d", days, end_ms=end)["close"] for s in cfg.core.symbols}
            if cfg.core.fraction > 0 else {})
    rec = reconcile(db, cfg, datasets, context, core, since, end, selection, fallback)
    fmt_t = "%Y-%m-%d %H:%M"
    rec.reproduce = (f"tradebot reconcile --since \"{pd.Timestamp(since, unit='ms', tz='UTC'):{fmt_t}}\" "
                     f"--end \"{pd.Timestamp(end, unit='ms', tz='UTC'):{fmt_t}}\"")
    rec.header = [f"  Candles frozen at {pd.Timestamp(end, unit='ms', tz='UTC'):{fmt_t}} UTC · code {code_version()} "
                  f"· settings {settings_hash(cfg)} · timeframes replayed {', '.join(cfg.timeframes)}",
                  f"  Reproduce: {rec.reproduce}"]
    return rec, format_reconciliation(rec, cfg)


def cmd_reconcile(args, cfg):
    import pandas as pd

    from .reconcile import write_csv

    def ms(text):
        return int(pd.Timestamp(text, tz="UTC").value // 1_000_000) if text else None

    try:
        rec, text = _reconcile_run(cfg, ms(args.since), ms(args.end), args.synthetic)
    except ValueError as exc:
        sys.exit(str(exc))
    print(text)
    if args.csv:
        write_csv(rec, args.csv)
        print(f"\nevery signal row written to {args.csv}")


def _reference_backtest(cfg, reference: str, synthetic: bool = False):
    """The frozen reference backtest: coins, data end and strategy selection from its universe file."""
    import copy

    from .learning import load_brain
    from .portfolio import portfolio_backtest

    pcfg = copy.deepcopy(cfg)
    selection, _ = load_brain(pcfg)
    fraction = pcfg.core.fraction or 0.65
    pcfg.core.fraction = fraction
    x = _portfolio_inputs(pcfg, selection, synthetic, 3200, reference)
    res = portfolio_backtest(x["core_closes"], x["trades"], pcfg, capital=1000.0, fraction=fraction,
                             since="2022-01-01", sat_closes=x["sat_closes"], combos=x["combos"],
                             universe=x["universe"], sat_marks=x["sat_marks"], ml_replay=x["ml_replay"])
    return res, x, pcfg, fraction


def _telegram_send(cfg, text: str) -> str | None:
    """Send a plain-text Telegram message; returns the error, or None when it was sent."""
    from .notify.telegram import TelegramNotifier, _chunks

    s = cfg.secrets
    if not (s.telegram_token and s.telegram_chat_id):
        return "Telegram is not configured (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the --env file)"
    tg = TelegramNotifier(s.telegram_token, s.telegram_chat_id)
    prefix = f"[{cfg.name}] " if cfg.name else ""
    try:
        for chunk in _chunks(prefix + text, 4000):
            tg._call("sendMessage", chat_id=s.telegram_chat_id, text=chunk, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return f"Telegram send failed: {exc}"
    return None


def cmd_notify(args, cfg):
    """Gate 4: a Telegram message to the owner (the weekly summary, or a gate that can't resolve)."""
    text = args.text if args.text is not None else Path(args.file).read_text() if args.file else ""
    if not text.strip():
        sys.exit("usage: tradebot notify --text \"...\" (or --file path)")
    err = _telegram_send(cfg, text)
    if err:
        sys.exit(err)
    print("sent")


def _gate_drawdown(args, cfg):
    """Gate 3: limits set in advance; on a hit, reconcile the period and check the range."""
    import copy
    import time as _time

    import pandas as pd

    from . import weekly as wk
    from .gates import drawdown as dd
    from .provenance import code_version, settings_hash

    state_dir = cfg.state_path
    if args.set_limits:
        ref = args.reference or wk.load_state(state_dir).get("reference")
        if not ref:
            sys.exit("--set-limits needs --reference <universe file> (or a weekly reference)")
        res, _, _, _ = _reference_backtest(cfg, ref, args.synthetic)
        dist = dd.backtest_distribution(res)
        since = (int(pd.Timestamp(args.paper_from, tz="UTC").value // 1_000_000) if args.paper_from
                 else int(_time.time() * 1000))
        try:
            data = dd.set_limits(state_dir, dist, reference=ref, code=code_version(), settings=settings_hash(cfg),
                                 paper_from_ms=since)
        except ValueError as exc:
            sys.exit(str(exc))
        lim = data["limits"]
        print(f"Drawdown limits set ({data['rule']}), watching paper from "
              f"{pd.Timestamp(since, unit='ms', tz='UTC'):%Y-%m-%d %H:%M} UTC:\n"
              f"  account drawdown {lim['account_dd_pct']:.1f}% (backtest worst {dist['combined_dip_pct']:.1f}%)\n"
              f"  satellite drawdown {lim['satellite_dd_pct']:.1f}% (backtest worst {dist['satellite_dip_pct']:.1f}%)\n"
              f"  losing run {lim['losing_run']} trades (the backtest's longest, over {dist['trades']} trades)")
        return
    limits = dd.load_limits(state_dir)
    if not limits:
        sys.exit("No drawdown limits yet: tradebot gate drawdown --set-limits --reference <universe file>")
    db = _bot_db(cfg)
    st = dd.paper_state(db, cfg.mode, limits["paper_from_ms"])
    print(dd.status_line(limits, st))
    which = dd.hits(limits, st)
    if not which:
        return
    key = {"which": which, "account_peak_ms": st["account_peak_ms"], "satellite_peak_ms": st["satellite_peak_ms"],
           "run_start_ms": st["run_start_ms"]}
    for ev in reversed(dd.events(state_dir)):  # a hit is handled once: the same dip is not re-reported daily
        if ev.get("key") == key:
            print(f"Already handled at {ev['at']}: {ev['outcome']} (see drawdown-events.jsonl)")
            if args.notify and ev.get("notified") is False and ev.get("message"):  # it didn't go out then
                err = _telegram_send(cfg, ev["message"])
                dd.record_event(state_dir, {**ev, "at": _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime()),
                                            "notified": err is None, "resend": True})
                print(err or "sent")
            sys.exit({"in range": 10, "outside range": 11}.get(ev["outcome"], 12))
    starts = [t for t in (st["account_peak_ms"] if "account" in which else None,
                          st["satellite_peak_ms"] if "satellite" in which else None,
                          st["run_start_ms"] if "losing_run" in which else None) if t]
    since = max(min(starts) if starts else limits["paper_from_ms"], limits["paper_from_ms"])
    try:
        rec, text = _reconcile_run(copy.deepcopy(cfg), since, None, args.synthetic)
        _, up = wk.uptime_summary(rec)
        _, rc = wk.reconcile_summary(rec)
        mechanics_ok = (not rc.get("first_differences") and not rc.get("core_rule_diffs")
                        and not rc.get("core_target_mismatch") and up.get("uptime", 0.0) >= 0.95)
        reproduce = rec.reproduce
    except ValueError as exc:
        text, up, rc, mechanics_ok, reproduce = str(exc), {}, {}, False, ""
    ranges = dd.range_check(limits, st, which)
    result = dd.outcome(ranges, mechanics_ok)
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    report = out_dir / f"drawdown-{_time.strftime('%Y-%m-%d-%H%M', _time.gmtime())}.txt"
    report.write_text(text + "\n")
    next_step = {"in range": "recorded as in range; trading continues",
                 "outside range": "outside the backtest's range with correct mechanics: register a test (gate 2)",
                 "mechanics differ": "paper's mechanics differ from the backtest's: fix them (see the report)"}[result]
    msg = (f"Drawdown gate hit ({', '.join(which)}): {dd.status_line(limits, st)}.\n"
           + "".join(f"Range check {w}: the backtest does this badly {r['p']:.0%} of the time "
                     f"({'in' if r['in_range'] else 'outside'} range).\n" for w, r in ranges.items())
           + f"Outcome: {result} - {next_step}. Reconcile: {report}")
    print(msg)
    notified = None
    if args.notify and result != "in range":  # in range is recorded and shows in the weekly summary
        err = _telegram_send(cfg, msg)
        notified = err is None
        if err:
            print(err)
    dd.record_event(state_dir, {"at": _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime()), "key": key, "state": st,
                                "ranges": ranges, "mechanics_ok": mechanics_ok, "reconcile": {**rc, **up},
                                "outcome": result, "report": str(report), "reproduce": reproduce,
                                "message": msg, "notified": notified})
    sys.exit({"in range": 10, "outside range": 11, "mechanics differ": 12}[result])


def cmd_weekly(args, cfg):
    """Weekly check-in: reconcile the last week, rerun the reference backtest on frozen data,
    summarise uptime, append a STATUS.md entry. Reporting only."""
    import copy

    import pandas as pd

    from . import weekly as wk
    from .learning import load_brain
    from .portfolio import format_portfolio_backtest
    from .provenance import code_version, config_differences, config_hash, settings_snapshot
    from .reconcile import write_csv
    from .timeframes import last_closed_open_ms

    state = wk.load_state(cfg.state_path)
    if args.set_reference:  # switch the reference without running: Monday's entry reports the switch
        if not Path(args.set_reference).exists():
            sys.exit(f"reference universe file not found: {args.set_reference}")
        old = state.get("reference")
        if old and old != args.set_reference:
            state["previous_reference"] = old
        state["reference"] = args.set_reference
        wk.save_state(cfg.state_path, state)
        print(f"Weekly reference: {old or '(none)'} -> {args.set_reference} (reported in the next check-in)")
        return
    reference = args.reference or state.get("reference")
    if not reference:
        sys.exit("Pass --reference reports/universe-<run>.json once: the frozen backtest to rerun every week "
                 "(it is remembered in state/research/weekly.json).")
    if not Path(reference).exists():
        sys.exit(f"reference universe file not found: {reference}")
    market = _market(cfg, args.synthetic)
    end = (int(pd.Timestamp(args.end, tz="UTC").value // 1_000_000) if args.end
           else last_closed_open_ms(market.now_ms(), "1h") + 3_600_000)
    since = end - args.days * 86_400_000
    out_dir = Path(args.out_dir) / pd.Timestamp(end, unit="ms", tz="UTC").strftime("%Y-%m-%d-%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    code, snap = code_version(), settings_snapshot(cfg)
    numbers, reproduce, nums = [], [], {"reference": reference}

    rec = None
    try:  # 1) paper vs backtest over the week, with uptime
        rec, text = _reconcile_run(copy.deepcopy(cfg), since, end, args.synthetic)
        (out_dir / "reconcile.txt").write_text(text + "\n")
        write_csv(rec, out_dir / "reconcile.csv")
        up_lines, nums["uptime"] = wk.uptime_summary(rec)
        rc_lines, nums["reconcile"] = wk.reconcile_summary(rec)
        numbers += up_lines + rc_lines
        reproduce.append(rec.reproduce)
    except ValueError as exc:
        numbers.append(f"- Reconcile: {exc}")

    try:  # 2) the reference backtest, frozen: coins, data end and strategy selection as saved
        res, x, pcfg, fraction = _reference_backtest(cfg, reference, args.synthetic)
        (out_dir / "portfolio-backtest.txt").write_text(format_portfolio_backtest(res, pcfg.exchange.quote) + "\n")
        pf_lines, nums["portfolio"] = wk.portfolio_summary(res)
        u = x["universe"] or {}
        if u.get("selection_source"):
            pf_lines.append(f"  Strategy selection: {u['selection_source']}")
        numbers += pf_lines
        reproduce.append(f"tradebot portfolio-backtest --core-fraction {fraction:g} --universe-file {reference}")
    except Exception as exc:  # noqa: BLE001 - the check-in still gets written
        numbers.append(f"- Frozen reference rerun failed: {type(exc).__name__}: {exc}")
    try:  # 3) the drawdown gate's state (the gate itself runs daily: scripts/daily.sh)
        from .gates import drawdown as dd

        limits = dd.load_limits(cfg.state_path)
        dd_state = dd.paper_state(_bot_db(cfg), cfg.mode, limits["paper_from_ms"]) if limits else None
        numbers.append("- " + dd.status_line(limits, dd_state))
        week = [e for e in dd.events(cfg.state_path) if pd.Timestamp(e["at"], tz="UTC").value // 1_000_000 >= since]
        if week:
            numbers.append("  Hits this week: " + "; ".join(f"{e['at'][:10]} {', '.join(e['key']['which'])}: "
                                                            f"{e['outcome']}" for e in week))
        nums["drawdown"] = {"hits": dd.hits(limits, dd_state) if limits else [],
                            "events": [e["outcome"] for e in week]}
    except Exception as exc:  # noqa: BLE001 - the check-in still gets written
        numbers.append(f"- Drawdown gate: could not read paper's state ({exc})")
    numbers.append(f"- Full reports: {out_dir}")

    # what changed since last week
    today, _ = load_brain(cfg)
    sel_now = {"keys": sorted(c.key for c in today.selected) if today else [],
               "created": today.created_at if today else None}
    changed = []
    logged = wk.selection_changes(rec) if rec is not None else []
    changed += logged  # learn's changes during the week, from the bot's activity log: first, they change what trades
    prev_sel = state.get("selection")
    selection_changed = bool(logged) or (bool(prev_sel) and prev_sel.get("created") != sel_now["created"])
    if selection_changed and not logged:
        changed.append(f"learn re-selected strategies: {', '.join(prev_sel.get('keys') or []) or 'none'} -> "
                       f"{', '.join(sel_now['keys']) or 'none'}")
    commits = wk.git_log(state.get("code"))
    if state.get("code") and state["code"] != code:
        changed.append(f"code {state['code']} -> {code}" + (": " + "; ".join(commits[:8]) if commits else ""))
    diffs = config_differences(state["settings"], snap) if state.get("settings") else []
    changed += [f"setting {d}" for d in diffs]
    previous_ref = state.get("previous_reference") or state.get("reference")
    if previous_ref and previous_ref != reference:
        changed.append(f"reference backtest {previous_ref} -> {reference}")

    # a switched reference: last week's numbers are another backtest's, not a move of this one
    prev_state = {**state, "reference": previous_ref} if previous_ref and previous_ref != reference else state
    needs = wk.needs_owner(nums, prev_state, diffs, selection_changed)
    entry = wk.status_entry(date=wk.today(), code=code, settings=config_hash(snap), changed=changed,
                            numbers=numbers, reproduce=reproduce, needs=needs,
                            next_items=wk.backlog_next(wk.REPO / "BACKLOG.md"))
    status = Path(args.status_file) if args.status_file else wk.REPO / "STATUS.md"
    wk.append_status(status, entry)
    wk.save_state(cfg.state_path, {"reference": reference, "code": code, "settings": snap, "selection": sel_now,
                                   "numbers": nums, "ran_at": wk.today()})
    print(entry)
    print(f"Appended to {status}; full reports in {out_dir}")
    if args.notify:
        err = _telegram_send(cfg, wk.telegram_summary(wk.today(), code, numbers, needs, changed))
        print(err or "Telegram summary sent")
        if err:
            sys.exit(3)  # written, not sent: scripts/weekly.sh still commits and pushes


def _research_protocols(args, cfg):
    """Gate 2: register a protocol, run it once, record its verdict once; list them all."""
    import json

    import yaml

    from . import protocols

    state = cfg.state_path
    if args.topic == "protocols":
        print(protocols.format_status(state))
        return
    try:
        if args.topic == "register":
            if not args.file:
                sys.exit("usage: tradebot research register --file protocol.yaml")
            entry = protocols.register(state, yaml.safe_load(Path(args.file).read_text()))
            print(f"Registered {entry['id']} (trial {entry['trial']} overall, {entry['family_trial']} in its family; "
                  f"sha256 {entry['sha256'][:12]}). Run it once: tradebot research run --id {entry['id']}")
        elif args.topic == "run":
            code, out = protocols.run(state, args.id or "", cwd=str(Path.cwd()))
            print(out)
            print(f"\nRan {args.id} (exit {code}); output kept in {protocols.folder(state) / (args.id + '.out')}. "
                  f"Apply the registered pass rule, then: tradebot research record --id {args.id} --verdict ...")
        else:
            numbers = json.loads(args.numbers) if args.numbers else {}
            if not isinstance(numbers, dict):
                sys.exit("--numbers must be a JSON object, e.g. '{\"overall\": 0.12, \"first_half\": 0.1, "
                         "\"second_half\": 0.15}'")
            entry = protocols.record(state, args.id or "", args.verdict or "", numbers, args.text or "")
            print(f"Recorded {args.id}: {entry['verdict'].upper()} ({entry['trials_total']} trials so far, "
                  f"{entry['trials_in_family']} in this family).")
    except ValueError as exc:
        sys.exit(str(exc))


def cmd_gate(args, cfg):
    if args.gate == "same-trades":
        return _gate_same_trades(args, cfg)
    if args.gate == "deploy":
        return _gate_deploy(args, cfg)
    return _gate_drawdown(args, cfg)


def _gate_same_trades(args, cfg):
    """Gate 1: replay the live path on the fixed dataset and compare its orders with the golden file."""
    from .gates import same_trades as st
    from .protocols import _append, ledger, passed
    from .provenance import code_version

    golden = st.load_golden()
    if golden and golden["dataset_sha256"] != st.dataset_sha256():
        sys.exit("The stored dataset no longer matches the golden file: it must never change.")
    days = golden["replay"]["days"] if golden else st.REPLAY_DAYS
    unknown = [n for n in (args.record_scenarios or []) if n not in st.SCENARIOS]
    if unknown:
        sys.exit(f"unknown scenario(s) {', '.join(unknown)}: the scenarios are {', '.join(st.SCENARIOS)}")
    orders = st.replay(days=days)
    recorded = dict((golden or {}).get("scenarios") or {})
    for name, sc in recorded.items():
        if sc["sha256"] != st.scenario_sha256(name):
            sys.exit(f"The stored scenario {name} no longer matches the golden file: it must never change.")
    if args.accept:
        if not passed(cfg.state_path, args.accept):
            sys.exit(f"{args.accept} has no passing verdict in the ledger: the golden file stays as it is.")
        used = [e for e in ledger(cfg.state_path) if e.get("kind") == "golden" and e.get("id") == args.accept]
        if used:
            sys.exit(f"{args.accept} already accepted a golden file ({used[0]['at']}): a registered test accepts one "
                     f"change. Another change is another protocol.")
        import hashlib
        import json
        import time as _time

        scenarios = {name: {"sha256": st.scenario_sha256(name), "recorded_by_code": code_version(),
                            "orders": st.replay(days=days, scenario=name)} for name in recorded}
        st.write_golden(orders, args.accept, code_version(), scenarios=scenarios)
        _append(cfg.state_path, {"id": args.accept, "kind": "golden", "at": _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime()),
                                 "code": code_version(), "orders_sha256": hashlib.sha256(
                                     json.dumps(orders, sort_keys=True).encode()).hexdigest()})
        print(f"Golden file rewritten, accepted by {args.accept}: {st.summary(orders)}"
              + (f"; scenarios re-recorded: {', '.join(scenarios)}" if scenarios else ""))
        return
    if golden is None:
        if not args.init:
            sys.exit("No golden file yet: --init writes the first one from this code.")
        first = {}
        if args.record_scenarios is not None:  # --init --record-scenarios: the scenarios from the start
            first = {name: {"sha256": st.scenario_sha256(name), "recorded_by_code": code_version(),
                            "orders": st.replay(days=days, scenario=name)} for name in st.SCENARIOS}
        st.write_golden(orders, None, code_version(), scenarios=first or None)
        print(f"First golden file written: {st.summary(orders)}"
              + (f"; scenarios recorded: {', '.join(first)}" if first else ""))
        return
    diffs = st.differences(golden["orders"], orders)
    if diffs:
        print("ORDERS DIFFER from the golden file - this change affects what the bot trades:")
        print("\n".join(diffs))
        sys.exit(1)
    print(f"identical to the golden file: {st.summary(orders)}")
    # the scenarios: what the plain orders can't see (a lagging feed, outages, a model in use). A missing
    # one is recorded once (--record-scenarios); a changed one is a failure-path change, re-recorded the
    # same way after review - the plain orders above decide whether a registered test is needed
    to_record = None if args.record_scenarios is None else (set(args.record_scenarios) or set(st.SCENARIOS))
    failed = []
    for name in st.SCENARIOS:
        got = st.replay(days=days, scenario=name)
        if name in recorded:
            sdiffs = st.differences(recorded[name]["orders"], got)
            if not sdiffs:
                print(f"scenario {name}: identical to the golden file: {st.summary(got)}")
                continue
            if to_record is None or name not in to_record:
                print(f"scenario {name}: ORDERS DIFFER from the golden file - a change to the failure path "
                      f"(--record-scenarios {name} re-records it once reviewed):")
                print("\n".join(sdiffs))
                failed.append(name)
                continue
        elif to_record is None or name not in to_record:
            print(f"scenario {name}: no golden orders yet (--record-scenarios {name} records them, first time only)")
            failed.append(name)
            continue
        recorded[name] = {"sha256": st.scenario_sha256(name), "recorded_by_code": code_version(), "orders": got}
        print(f"scenario {name}: recorded: {st.summary(got)}")
    if to_record is not None and not failed:
        st.write_golden(golden["orders"], golden.get("accepted_by"), golden.get("generated_by_code"),
                        days=days, scenarios=recorded)
    if failed:
        sys.exit(1)


def _gate_deploy(args, cfg):
    """Gate 1: lint, suite, fresh-clone suite, same-trades; then deploy to the bot's checkout."""
    from .gates.deploy import GateFailed, deploy

    agent_dir = Path(__file__).resolve().parent.parent
    print(f"Deploy gate: {agent_dir} -> {args.bot_dir}" + (" (dry run)" if args.dry_run else ""))
    try:
        rec = deploy(agent_dir, Path(args.bot_dir).expanduser(), cfg.state_path, dry_run=args.dry_run)
    except GateFailed as exc:
        print(f"GATE FAILED at {exc.step}:\n{exc.detail}")
        sys.exit(2)
    print(f"{'Deployed' if rec.get('deployed') else 'All checks passed (dry run)'}: {rec['commit']}")


def cmd_telegram_test(args, cfg):
    import requests

    s = cfg.secrets
    if not s.telegram_token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
    if not s.telegram_chat_id:
        r = requests.get(f"https://api.telegram.org/bot{s.telegram_token}/getUpdates", timeout=15).json()
        chats = {str(u["message"]["chat"]["id"]): u["message"]["chat"].get("first_name", "") for u in r.get("result", []) if "message" in u}
        if not chats:
            sys.exit("Send any message to your bot in Telegram, then run this again to see your chat id.")
        for cid, name in chats.items():
            print(f"chat id {cid} ({name}) -> put TELEGRAM_CHAT_ID={cid} in .env")
        return
    from .notify import TelegramNotifier

    TelegramNotifier(s.telegram_token, s.telegram_chat_id).send("✅ tradebot can reach you on Telegram.")
    print("Test message sent.")


def cmd_demo(args, cfg):
    from .demo import run_demo

    run_demo(days=args.days, sim_days=args.sim_days, symbols=args.symbols_n, seed=args.seed)


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tradebot", description="Crypto scanner, signal bot and auto-trader")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env", default=".env")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create config.yaml and .env from the examples")
    sp = sub.add_parser("learn", help="download data, select strategies, train the ML filter")
    sp.add_argument("--synthetic", action="store_true", help="use synthetic data (offline)")
    sp = sub.add_parser("backtest", help="backtest strategies over history")
    sp.add_argument("--strategy")
    sp.add_argument("--timeframe")
    sp.add_argument("--symbols", nargs="*")
    sp.add_argument("--trades-csv")
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("scan", help="show current opportunities (no trading)")
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("run", help="run the bot 24/7 (paper or live per config)")
    sp.add_argument("--force-live", action="store_true", help="go live even if the paper record is not ready")
    sub.add_parser("report", help="track record and go-live readiness")
    sp = sub.add_parser("portfolio-backtest", help="core + satellite account vs holding BTC")
    sp.add_argument("--capital", type=float, default=1000.0)
    sp.add_argument("--core-fraction", type=float, default=None, help="default: core.fraction, or 0.65 if unset")
    sp.add_argument("--days", type=int, default=3200, help="daily history for the core (default ~8.8 years)")
    sp.add_argument("--since", default="2022-01-01", help="also report from this date (the test period)")
    sp.add_argument("--universe-file", default=None,
                    help="rerun on a saved coin list, data end date and strategy selection (reports/universe-*.json)")
    sp.add_argument("--ml-replay", action="store_true",
                    help="model the ML filter walk-forward: the model a weekly learn would have had at each signal "
                         "(default: only when config.ML_IN_REPLAY says the bot uses it)")
    sp.add_argument("--current-selection", action="store_true",
                    help="with --universe-file: use today's strategy selection instead of the saved run's")
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("research", help="evaluate optional rules on real data before enabling them")
    sp.add_argument("topic", choices=["breaker", "sizing", "core", "note", "oos-fetch", "oos-register", "oos-run",
                                      "oos-show", "register", "protocols", "run", "record"],
                    help="breaker: the BTC volatility circuit breaker; sizing: the pre-registered open-risk budget "
                         "test; core: robustness of the core (reset timing, trend lengths) - reporting only; "
                         "note: annotate a recorded test (--id, --text); oos-fetch / oos-register / oos-run / "
                         "oos-show: the pre-2017 BTC out-of-sample test of the core, in that order; "
                         "register (--file) / run (--id) / record (--id --verdict --numbers): a protocol from the "
                         "CLAUDE.md template, once each; protocols: every test in the ledger with trial counts")
    sp.add_argument("--id", default=None, help="the test id, e.g. sizing-open-risk-budget-v1")
    sp.add_argument("--file", default=None, help="register: the protocol (YAML or JSON, the CLAUDE.md template)")
    sp.add_argument("--verdict", choices=["pass", "fail", "inconclusive"], default=None,
                    help="record: the verdict, from the registered pass rule")
    sp.add_argument("--numbers", default=None, help="record: the metric and supporting numbers, as JSON")
    sp.add_argument("--text", default=None, help="note: the text to add")
    sp.add_argument("--ratios", type=float, nargs="+", default=[2.0, 2.5, 3.0])
    sp.add_argument("--capital", type=float, default=1000.0, help="sizing: account size")
    sp.add_argument("--days", type=int, default=3200, help="sizing: daily history for the core")
    sp.add_argument("--rerun", action="store_true", help="sizing: show the test again (marked as a re-run)")
    sp.add_argument("--universe-file", default=None, help="run on a saved coin list and data end date")
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("project", help="what could the account become? (Monte Carlo from out-of-sample trades)")
    sp.add_argument("--capital", type=float, default=1000.0)
    sp.add_argument("--runs", type=int, default=5000)
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("dashboard", help="equity vs holding BTC, open trades, signals, alerts (HTML)")
    sp.add_argument("--serve", action="store_true", help="serve a live page on 127.0.0.1 instead of writing a file")
    sp.add_argument("--port", type=int, default=None, help="default: dashboard.port (8765)")
    sp.add_argument("--out", default=None, help="file to write (default: <state_dir>/dashboard.html)")
    sp = sub.add_parser("compare-entries", help="market (this config) vs limit entries (--other), per signal")
    sp.add_argument("--other", required=True, help="config of the limit-entry instance, e.g. config-b.yaml")
    sp.add_argument("--other-env", default=None, help="its .env file (not needed for the comparison)")
    sp.add_argument("--since", default=None, help="only signals from this date (default: B's first signal)")
    sp.add_argument("--csv", default=None, help="also write every signal's row to this CSV file")
    sp = sub.add_parser("config-set", help="change settings in the config file (keeps comments, makes a backup)")
    sp.add_argument("assignments", nargs="+", metavar="key=value", help="e.g. costs.fee_rate=0.00075 core.fraction=0.65")
    sp = sub.add_parser("reconcile", help="paper vs backtest, trade by trade (reporting only)")
    sp.add_argument("--since", default=None, help="start of the paper period (default: its first record)")
    sp.add_argument("--end", default=None, help="freeze the candles at this UTC time (default: the last full hour)")
    sp.add_argument("--csv", default=None, help="also write every signal row to this CSV")
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("weekly", help="weekly check-in: reconcile the last week, rerun the reference backtest "
                                       "frozen, uptime, and a STATUS.md entry (reporting only)")
    sp.add_argument("--reference", default=None,
                    help="universe file of the backtest to rerun each week (needed once; remembered)")
    sp.add_argument("--set-reference", default=None, metavar="FILE",
                    help="only switch the remembered reference to FILE (no check-in now; the next one reports it)")
    sp.add_argument("--days", type=int, default=7, help="reconcile this many days up to the end (default 7)")
    sp.add_argument("--end", default=None, help="UTC end of the week (default: the last full hour)")
    sp.add_argument("--status-file", default=None, help="default: STATUS.md in the repository")
    sp.add_argument("--out-dir", default="reports/weekly", help="where the full reports go")
    sp.add_argument("--notify", action="store_true", help="also send a Telegram summary")
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("gate", help="the gates that replace approvals (CLAUDE.md): same-trades, deploy, drawdown")
    sp.add_argument("gate", choices=["same-trades", "deploy", "drawdown"])
    sp.add_argument("--init", action="store_true", help="same-trades: write the first golden file")
    sp.add_argument("--record-scenarios", nargs="*", default=None, metavar="NAME",
                    help="same-trades: record the scenarios' orders in the golden file (names, or all)")
    sp.add_argument("--accept", default=None,
                    help="same-trades: rewrite the golden file for a change that passed this registered test")
    sp.add_argument("--bot-dir", default="~/tradebot", help="deploy: the running bot's checkout")
    sp.add_argument("--dry-run", action="store_true", help="deploy: every check, but don't touch the bot")
    sp.add_argument("--set-limits", action="store_true",
                    help="drawdown: set the limits, once, from the frozen reference backtest")
    sp.add_argument("--reference", default=None, help="drawdown --set-limits: the reference universe file")
    sp.add_argument("--paper-from", default=None, help="drawdown --set-limits: watch paper from (UTC; default now)")
    sp.add_argument("--notify", action="store_true",
                    help="drawdown: Telegram the owner when a hit needs work (outside range, mechanics differ)")
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("notify", help="send the owner a Telegram message (weekly summary, unresolved gate)")
    sp.add_argument("--text", default=None)
    sp.add_argument("--file", default=None, help="send this file's text")
    sub.add_parser("telegram-test", help="check Telegram setup / find your chat id")
    sp = sub.add_parser("demo", help="offline end-to-end demo on synthetic data")
    sp.add_argument("--days", type=int, default=540, help="history for learning")
    sp.add_argument("--sim-days", type=int, default=45, help="days of simulated live paper trading")
    sp.add_argument("--symbols-n", type=int, default=6)
    sp.add_argument("--seed", type=int, default=7)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("ccxt", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(args.config, args.env) if args.command != "demo" else BotConfig()
    handler = {
        "init": cmd_init, "learn": cmd_learn, "backtest": cmd_backtest, "scan": cmd_scan,
        "run": cmd_run, "report": cmd_report, "project": cmd_project, "research": cmd_research,
        "portfolio-backtest": cmd_portfolio_backtest, "dashboard": cmd_dashboard,
        "compare-entries": cmd_compare_entries, "config-set": cmd_config_set, "reconcile": cmd_reconcile,
        "weekly": cmd_weekly, "gate": cmd_gate, "notify": cmd_notify,
        "telegram-test": cmd_telegram_test,
        "demo": cmd_demo,
    }[args.command]
    handler(args, cfg)


if __name__ == "__main__":
    main()
