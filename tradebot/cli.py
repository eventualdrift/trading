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
    u = cfg.universe
    symbols = args.symbols or market.top_symbols(cfg.exchange.quote, u.top_n, u.min_quote_volume, u.whitelist, u.blacklist)
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
            is_t, oos_t, _ = run_combo(datasets[tf], name, cfg.strategies.get(name, {}), tf, cfg, context)
            trades = is_t + oos_t
            all_trades += trades
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
    u = cfg.universe
    symbols = market.top_symbols(cfg.exchange.quote, u.top_n, u.min_quote_volume, u.whitelist, u.blacklist)
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

    def learner(current):
        res = learning_cycle(cfg, learn_market, store=store, db=db, current_model=current, log_fn=log.info)
        return res.selection, res.model, res.summary

    follow = cfg.learning.follow_state_dir
    selection, model = load_brain(cfg)
    if follow:
        learner = None  # never learns: uses (and reloads) the leader's selection and model
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
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        bot.stop()
        print("stopped")


def cmd_report(args, cfg):
    from .db import Database
    from .learning import MODEL_REPORT_FILE, load_brain
    from .notify import formatting as fmt
    from .report import format_readiness, readiness, sleeve_summary

    db = Database(cfg.state_path / "tradebot.db")
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
    rep = cfg.brain_path / MODEL_REPORT_FILE
    print("ML filter:", "active" if model else "not active", f"(last training report: {rep})" if rep.exists() else "")
    print("\nGo-live readiness (paper track record):")
    print(format_readiness(readiness(db, cfg, int(time.time() * 1000))))


def cmd_project(args, cfg):
    from .learning import load_brain, load_context, load_datasets
    from .projection import format_projection, out_of_sample_trades, project

    selection, _ = load_brain(cfg)
    if not selection or not selection.selected:
        sys.exit("No validated strategies yet - run `tradebot learn` first (nothing to project).")
    market = _market(cfg, args.synthetic)
    u = cfg.universe
    symbols = market.top_symbols(cfg.exchange.quote, u.top_n, u.min_quote_volume, u.whitelist, u.blacklist)
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
    from .research import breaker_study, format_breaker_study

    selection, _ = load_brain(cfg)
    if not selection or not selection.selected:
        sys.exit("No validated strategies yet - run `tradebot learn` first.")
    market = _market(cfg, args.synthetic)
    u = cfg.universe
    symbols = market.top_symbols(cfg.exchange.quote, u.top_n, u.min_quote_volume, u.whitelist, u.blacklist)
    cfg.timeframes = selection.timeframes()
    store = _store(cfg, market)
    datasets = load_datasets(market, cfg, symbols, store, log_fn=lambda *_: None)
    context = load_context(market, cfg, store, log_fn=print)
    if context is None:
        sys.exit("BTC history unavailable - cannot evaluate the breaker.")
    print("Volatility circuit breaker: selected strategies with and without it "
          f"(ratio = BTC's last {cfg.context.vol_short}h volatility / the {cfg.context.vol_long}h before)\n")
    print(format_breaker_study(breaker_study(selection, datasets, cfg, context, tuple(args.ratios))))


def cmd_portfolio_backtest(args, cfg):
    from .learning import load_brain, load_context, load_datasets
    from .portfolio import combo_split_stats, format_portfolio_backtest, portfolio_backtest
    from .backtest.selection import run_combo

    selection, _ = load_brain(cfg)
    market = _market(cfg, args.synthetic)
    store = _store(cfg, market)
    now = market.now_ms()
    core_closes = {}
    for sym in cfg.core.symbols:
        df = (store.update(market, sym, "1d", args.days, now) if store is not None
              else market.history(sym, "1d", now - args.days * 86_400_000, now))
        core_closes[sym] = df["close"]
    trades, combos, sat_closes, universe = [], [], {}, None
    if selection and selection.selected:
        u = cfg.universe
        symbols = market.top_symbols(cfg.exchange.quote, u.top_n, u.min_quote_volume, u.whitelist, u.blacklist)
        cfg.timeframes = selection.timeframes()
        datasets = load_datasets(market, cfg, symbols, store, log_fn=lambda *_: None)
        context = load_context(market, cfg, store, log_fn=lambda *_: None)
        for c in selection.selected:
            is_t, oos_t, _ = run_combo(datasets.get(c.timeframe, {}), c.strategy, c.params, c.timeframe, cfg, context)
            trades += is_t + oos_t
            combos.append(combo_split_stats(c.key, is_t, oos_t))
        first = {}
        for tf, ds in sorted(datasets.items(), key=lambda kv: kv[0] != "1d"):  # daily data first
            for sym, df in ds.items():
                if sym not in sat_closes:  # daily closes to value open trades each day
                    sat_closes[sym] = df["close"] if tf == "1d" else df["close"].resample("1D").last().dropna()
                first[sym] = min(first.get(sym, df.index[0]), df.index[0])
        source = ("your universe.whitelist" if u.whitelist else
                  f"today's top {u.top_n} {cfg.exchange.quote} pairs by 24h volume ({time.strftime('%Y-%m-%d')})")
        universe = {"source": source, "symbols": symbols, "first": first}
    fraction = args.core_fraction if args.core_fraction is not None else (cfg.core.fraction or 0.65)
    res = portfolio_backtest(core_closes, trades, cfg, capital=args.capital, fraction=fraction,
                             since=args.since or None, sat_closes=sat_closes, combos=combos, universe=universe)
    print(format_portfolio_backtest(res, cfg.exchange.quote))


def cmd_dashboard(args, cfg):
    from .dashboard import serve_dashboard, write_dashboard
    from .db import Database

    db = Database(cfg.state_path / "tradebot.db")
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
    sp.add_argument("--synthetic", action="store_true")
    sp = sub.add_parser("research", help="evaluate optional rules on real data before enabling them")
    sp.add_argument("topic", choices=["breaker"], help="breaker: the BTC volatility circuit breaker")
    sp.add_argument("--ratios", type=float, nargs="+", default=[2.0, 2.5, 3.0])
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
        "compare-entries": cmd_compare_entries, "config-set": cmd_config_set,
        "telegram-test": cmd_telegram_test,
        "demo": cmd_demo,
    }[args.command]
    handler(args, cfg)


if __name__ == "__main__":
    main()
