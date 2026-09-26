"""A self-contained HTML dashboard: equity by sleeve vs holding BTC, open trades,
signals and alerts. Built from the database only (no exchange access needed):
the bot rewrites <state_dir>/dashboard.html every few minutes, and
`tradebot dashboard --serve` (or ``dashboard.serve: true``) serves a live copy on
127.0.0.1 only.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

from .config import BotConfig
from .db import Database

log = logging.getLogger(__name__)

MAX_POINTS = 1500


def _downsample(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) <= MAX_POINTS:
        return df
    step = -(-len(df) // MAX_POINTS)
    return pd.concat([df.iloc[:-1:step], df.iloc[[-1]]])  # keep the latest point


def build_data(db: Database, cfg: BotConfig, mode: str | None = None, prices: dict | None = None,
               now_ms: int | None = None) -> dict:
    mode = mode or cfg.mode
    q = cfg.exchange.quote
    snaps = _downsample(db.snapshots(mode))
    start = float(db.kv_get("paper_starting_balance", cfg.paper.starting_balance) if mode == "paper"
                  else (snaps["total"].iloc[0] if len(snaps) else 0.0))
    series = {"t": [], "total": [], "core": None, "satellite": [], "btc": None}
    if len(snaps):
        series["t"] = [int(t.value // 1_000_000) for t in snaps.index]
        series["total"] = [round(float(v), 2) for v in snaps["total"]]
        series["satellite"] = [round(float(v), 2) for v in snaps["satellite"]]
        if (snaps["core"] > 0).any():  # gap (None) before the core sleeve started
            started = (snaps["core"] > 0).cummax()
            series["core"] = [round(float(v), 2) if on else None for v, on in zip(snaps["core"], started)]
        btc = snaps["btc_price"]
        first = btc.first_valid_index()
        if first is not None:
            base_total, base_btc = float(snaps.loc[first, "total"]), float(btc.loc[first])
            series["btc"] = [round(base_total * float(b) / base_btc, 2) if b == b and b is not None else None
                             for b in btc]

    halted, paused = db.kv_get(f"{mode}:halted"), db.kv_get(f"{mode}:paused")
    state = ({"label": "Halted", "kind": "critical", "detail": str(halted)} if halted else
             {"label": "Paused", "kind": "warning", "detail": "no new entries"} if paused else
             {"label": "Running", "kind": "good", "detail": ""})

    prices = prices or db.kv_get(f"{mode}:last_prices", {}) or {}
    positions = []
    for p in db.open_positions(mode) + db.positions_with_status(mode, ("working",)):
        px = prices.get(p.symbol)
        live = bool(px) and p.status == "open"
        positions.append({
            "id": p.id, "symbol": p.symbol, "side": p.side, "tf": p.timeframe, "strategy": p.strategy,
            "status": p.status, "entry": p.entry_price, "stop": p.stop_loss, "target": p.take_profit,
            "amount": p.open_amount, "opened": p.opened_at,
            "upnl": round(p.unrealized(px), 2) if live else None,
            "r": round(p.r_at(px), 2) if live else None,
        })
    closed = db.closed_positions(mode)
    rs = [p.r_multiple or 0.0 for p in closed]
    perf = {"trades": len(rs), "win_rate": (sum(r > 0 for r in rs) / len(rs)) if rs else None,
            "avg_r": (sum(rs) / len(rs)) if rs else None, "total_r": sum(rs),
            "pnl": round(sum(p.pnl or 0.0 for p in closed), 2)}
    core_kv = f"{mode}:core:"
    core = {"enabled": bool(db.kv_get(core_kv + "initialized", False)),
            "weights": db.kv_get(core_kv + "weights", {}) or {},
            "holdings": db.kv_get(core_kv + "holdings", {}) or {},
            "contributed": float(db.kv_get(core_kv + "contributed", 0.0) or 0.0),
            "fraction": cfg.core.fraction}
    signals = [{"time": s.created_at, "symbol": s.symbol, "side": s.side, "tf": s.timeframe,
                "strategy": s.strategy, "status": s.status, "note": s.note} for s in db.recent_signals(15)]
    events = [{"time": e["ts"], "text": e["text"]} for e in db.events(mode, 25)]
    return {"title": "tradebot" + (f" · {cfg.name}" if cfg.name else ""), "mode": mode, "quote": q,
            "generated": int(now_ms if now_ms is not None else time.time() * 1000), "start": start,
            "state": state, "series": series, "positions": positions, "performance": perf, "core": core,
            "signals": signals, "events": events}


def render(data: dict) -> str:
    # "<" as \u003c: nothing inside the data can end the <script> block or open a comment
    payload = json.dumps(data, separators=(",", ":"), default=str).replace("<", "\\u003c")
    head, tail = TEMPLATE.split("__DATA__")
    return head.replace("__TITLE__", html.escape(data["title"])) + payload + tail


def write_dashboard(db: Database, cfg: BotConfig, path: Path | None = None, prices: dict | None = None,
                    now_ms: int | None = None) -> Path:
    path = Path(path or cfg.state_path / "dashboard.html")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(render(build_data(db, cfg, prices=prices, now_ms=now_ms)), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a browser never sees a half-written page
    return path


def serve_dashboard(db: Database, cfg: BotConfig, port: int, host: str = "127.0.0.1",
                    background: bool = False) -> ThreadingHTTPServer:
    """Serve a freshly built dashboard on every request (localhost only by default)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self.send_error(404)
                return
            try:
                body = render(build_data(db, cfg)).encode("utf-8")
            except Exception as exc:
                log.warning("dashboard build failed: %s", exc)
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug("dashboard: " + fmt, *args)

    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    if background:
        threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    return server


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
.viz-root{color-scheme:light;
 --surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;--text-secondary:#52514e;--muted:#898781;
 --grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--up:#006300;--down:#d03b3b;
 --series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--deemph:#898781;
 --good:#0ca30c;--warning:#fab219;--critical:#d03b3b;--wash:rgba(11,11,11,.04)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
 --surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--up:#0ca30c;--down:#e66767;
 --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--wash:rgba(255,255,255,.05)}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;
 --surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;
 --grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--up:#0ca30c;--down:#e66767;
 --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--wash:rgba(255,255,255,.05)}
html,body{margin:0}
body{background:#f9f9f7}
@media (prefers-color-scheme:dark){body{background:#0d0d0d}}
.viz-root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--page);color:var(--text-primary);
 min-height:100vh;padding:24px 16px 48px;box-sizing:border-box}
.wrap{max-width:1180px;margin:0 auto}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 16px;margin-bottom:16px}
h1{font-size:20px;font-weight:600;margin:0}
.sub{color:var(--text-secondary);font-size:13px}
.filters{display:flex;gap:4px;margin:0 0 16px;flex-wrap:wrap}
.filters button{font:inherit;font-size:13px;padding:6px 12px;border-radius:8px;border:1px solid var(--border);
 background:var(--surface-1);color:var(--text-secondary);cursor:pointer}
.filters button:hover{background:var(--wash)}
.filters button[aria-pressed="true"]{color:var(--text-primary);font-weight:600;border-color:var(--axis)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.tile,.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tile .label{font-size:13px;color:var(--text-secondary)}
.tile .value{font-size:22px;font-weight:600;margin-top:4px}
.tile.hero{grid-column:span 2}
@media (max-width:600px){.tile.hero{grid-column:1/-1} .tile.hero .value{font-size:40px}}
.tile.hero .value{font-size:48px;line-height:1.1}
.delta{font-size:13px;margin-top:4px;color:var(--text-secondary)}
.delta.up{color:var(--up)} .delta.down{color:var(--down)}
.status{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:15px;margin-top:6px}
.status .dot{width:10px;height:10px;border-radius:50%}
.card{margin-bottom:16px}
.card h2{font-size:15px;font-weight:600;margin:0 0 2px}
.card .note{font-size:12px;color:var(--text-secondary);margin:0 0 10px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-secondary);margin:6px 0}
.legend .key{display:inline-block;width:14px;height:2px;border-radius:1px;vertical-align:middle;margin-right:6px}
.chart{position:relative}
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.chart svg:focus{outline:2px solid var(--axis);outline-offset:2px}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
 padding:8px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;white-space:nowrap;z-index:2}
.tip .when{color:var(--text-secondary);margin-bottom:4px}
.tip .row{display:flex;align-items:center;gap:6px}
.tip .row b{font-weight:600;color:var(--text-primary)} .tip .row span{color:var(--text-secondary)}
.tip .key{display:inline-block;width:12px;height:2px;border-radius:1px}
details{margin-top:8px;font-size:13px} summary{cursor:pointer;color:var(--text-secondary)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:600;color:var(--text-secondary);border-bottom:1px solid var(--grid);padding:6px 8px}
td{border-bottom:1px solid var(--grid);padding:6px 8px;font-variant-numeric:tabular-nums}
td.num,th.num{text-align:right}
td.time{white-space:nowrap;color:var(--text-secondary)}
.scroll{overflow-x:auto}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.pos{color:var(--up)} .neg{color:var(--down)}
td details{margin:0} td summary{color:var(--text-primary)} td .more{white-space:pre-line;color:var(--text-secondary);margin-top:4px}
</style></head>
<body><div class="viz-root"><div class="wrap">
<header><h1 id="title"></h1><span class="sub" id="subtitle"></span></header>
<div class="filters" role="group" aria-label="Date range" id="filters"></div>
<section class="tiles" id="tiles"></section>
<section class="card"><h2>Account vs holding BTC</h2>
 <p class="note">Total account value, and what the same money would be worth held in BTC from the first snapshot.</p>
 <div class="legend" id="legend-a"></div><div class="chart" id="chart-a"></div><details><summary>Data table</summary><div class="scroll" id="table-a"></div></details></section>
<section class="card" id="sleeve-card"><h2>Equity by sleeve</h2>
 <p class="note">Core = BTC/ETH trend allocation. Satellite = the signal strategies. Sleeves are reset to their target split periodically.</p>
 <div class="legend" id="legend-b"></div><div class="chart" id="chart-b"></div><details><summary>Data table</summary><div class="scroll" id="table-b"></div></details></section>
<section class="card"><h2>Open trades</h2><div class="scroll" id="positions"></div></section>
<div class="grid2">
 <section class="card"><h2>Recent signals</h2><div class="scroll" id="signals"></div></section>
 <section class="card"><h2>Core allocation</h2><div id="core"></div></section>
</div>
<section class="card"><h2>Alerts &amp; events</h2><div class="scroll" id="events"></div></section>
</div></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(function(){
"use strict";
let DATA = JSON.parse(document.getElementById("data").textContent);
const RANGES = [["7d",7],["30d",30],["90d",90],["All",0]];
let rangeDays = 0;
try { rangeDays = Number(localStorage.getItem("tb-range") || 0); } catch (e) {}
const $ = id => document.getElementById(id);
const el = (tag, attrs, text) => { const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  if (text !== undefined && text !== null) n.textContent = String(text); return n; };
const svgEl = (tag, attrs) => { const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v); return n; };
const money = v => v === null || v === undefined ? "–" :
  v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const compact = v => (v = Math.abs(v) < 1e-9 ? 0 : v, Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Math.abs(v) >= 1e4 ? (v / 1e3).toFixed(1) + "K" :
  v.toLocaleString(undefined, {maximumFractionDigits: 0}));
const pct = v => (v >= 0 ? "+" : "") + v.toFixed(1) + "%";
const price = v => v === null || v === undefined ? "–" : Math.abs(v) >= 1000 ? v.toLocaleString(undefined, {maximumFractionDigits: 2})
  : Math.abs(v) >= 1 ? v.toFixed(4) : v.toPrecision(6);
const when = ms => new Date(ms).toISOString().replace("T", " ").slice(0, 16) + " UTC";
const day = ms => new Date(ms).toISOString().slice(0, 10);
const short = ms => new Date(ms).toISOString().slice(5, 16).replace("T", " ");  // MM-DD HH:MM (UTC)

function slice() {
  const s = DATA.series, t = s.t || [];
  if (!t.length) return null;
  const cut = rangeDays ? t[t.length - 1] - rangeDays * 864e5 : -Infinity;
  const idx = t.map((v, i) => v >= cut ? i : -1).filter(i => i >= 0);
  const pick = arr => arr ? idx.map(i => arr[i]) : null;
  return {t: pick(t), total: pick(s.total), core: pick(s.core), satellite: pick(s.satellite), btc: pick(s.btc)};
}

function tile(label, value, deltaText, deltaDir, hero) {
  const n = el("div", {class: "tile" + (hero ? " hero" : "")});
  n.append(el("div", {class: "label"}, label), el("div", {class: "value"}, value));
  if (deltaText) n.append(el("div", {class: "delta" + (deltaDir ? " " + deltaDir : "")}, deltaText));
  return n;
}

function renderTiles(sl) {
  const box = $("tiles"); box.replaceChildren();
  const q = DATA.quote;
  if (!sl) { box.append(tile("Account", "No data yet", "The bot records equity every 15 minutes.")); }
  else {
    const last = sl.total[sl.total.length - 1], first = sl.total[0];
    const ch = (last / first - 1) * 100;
    const allTime = DATA.start ? (last / DATA.start - 1) * 100 : null;
    box.append(tile("Total equity (" + q + ")", money(last),
      (allTime !== null ? pct(allTime) + " since start" : ""), allTime > 0 ? "up" : allTime < 0 ? "down" : "", true));
    box.append(tile(rangeDays ? "Change, last " + rangeDays + " days" : "Change, all data", pct(ch), "", ch > 0 ? "up" : ch < 0 ? "down" : ""));
    if (sl.btc) {
      const b = sl.btc.filter(v => v !== null);
      if (b.length > 1) { const bch = (b[b.length - 1] / b[0] - 1) * 100, diff = ch - bch;
        box.append(tile("vs holding BTC", (diff >= 0 ? "+" : "") + diff.toFixed(1) + " pts", "BTC " + pct(bch) + " over the same days", diff > 0 ? "up" : diff < 0 ? "down" : "")); }
    }
    if (sl.core) {
      const c = sl.core[sl.core.length - 1];
      box.append(tile("Core sleeve", money(c), "P&L " + money(c - DATA.core.contributed), c - DATA.core.contributed > 0 ? "up" : "down"));
      const sv = sl.satellite[sl.satellite.length - 1], sp = DATA.start ? sv - (DATA.start - DATA.core.contributed) : null;
      box.append(tile("Satellite sleeve", money(sv), sp === null ? "" : "P&L " + money(sp), sp > 0 ? "up" : sp < 0 ? "down" : ""));
    }
  }
  const p = DATA.performance;
  box.append(tile("Closed trades", String(p.trades), p.trades ? "win " + Math.round(p.win_rate * 100) + "% · avg " + p.avg_r.toFixed(2) + "R" : "none yet"));
  const st = el("div", {class: "tile"});
  st.append(el("div", {class: "label"}, "Bot state · " + DATA.mode));
  const s = el("div", {class: "status"}), dot = el("span", {class: "dot"});
  dot.style.background = "var(--" + DATA.state.kind + ")";
  s.append(dot, el("span", {}, (DATA.state.kind === "critical" ? "⛔ " : DATA.state.kind === "warning" ? "⏸ " : "") + DATA.state.label));
  st.append(s); if (DATA.state.detail) st.append(el("div", {class: "delta"}, DATA.state.detail));
  box.append(st);
}

function niceTicks(lo, hi, n) {
  const span = hi - lo || Math.abs(hi) || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v); return out;
}

function lineChart(boxId, legendId, t, series) {
  const box = $(boxId), legend = $(legendId); box.replaceChildren(); legend.replaceChildren();
  series = series.filter(s => s.values && s.values.some(v => v !== null));
  if (!t || t.length < 2 || !series.length) { box.append(el("div", {class: "empty"}, "Not enough data yet.")); return; }
  if (series.length > 1) for (const s of series) {
    const item = el("span"), key = el("span", {class: "key"}); key.style.background = s.color;
    item.append(key, document.createTextNode(s.label)); legend.append(item); }
  const W = Math.max(280, Math.round(box.clientWidth || 1100)), narrow = W < 640;
  const H = narrow ? 220 : 300, m = {l: 56, r: narrow ? 12 : 150, t: 12, b: 28};
  const all = series.flatMap(s => s.values.filter(v => v !== null));
  let lo = Math.min(...all), hi = Math.max(...all); const pad = (hi - lo) * 0.08 || hi * 0.02 || 1; lo -= pad; hi += pad;
  const x = i => m.l + (t[i] - t[0]) / (t[t.length - 1] - t[0] || 1) * (W - m.l - m.r);
  const y = v => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const svg = svgEl("svg", {viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", tabindex: "0",
    "aria-label": series.map(s => s.label).join(", ") + " over time; use left and right arrows to read values"});
  for (const v of niceTicks(lo, hi, 4)) {
    svg.append(svgEl("line", {x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1}));
    const lab = svgEl("text", {x: m.l - 8, y: y(v) + 4, "text-anchor": "end", "font-size": 12, fill: "var(--muted)",
      style: "font-variant-numeric:tabular-nums"}); lab.textContent = compact(v); svg.append(lab); }
  svg.append(svgEl("line", {x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1}));
  const nX = narrow ? 3 : 5; for (let k = 0; k < nX; k++) { const i = Math.round(k * (t.length - 1) / (nX - 1));
    const lab = svgEl("text", {x: x(i), y: H - 8, "text-anchor": k === 0 ? "start" : k === nX - 1 ? "end" : "middle",
      "font-size": 12, fill: "var(--muted)"}); lab.textContent = day(t[i]); svg.append(lab); }
  const ends = [];
  for (const s of series) {
    let d = "", pen = false;
    s.values.forEach((v, i) => { if (v === null) { pen = false; return; } d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1); pen = true; });
    svg.append(svgEl("path", {d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"}));
    let li = s.values.length - 1; while (li > 0 && s.values[li] === null) li--;
    ends.push({s, i: li, y: y(s.values[li])});
  }
  const collide = narrow || ends.some((a, k) => ends.some((b, j) => j > k && Math.abs(a.y - b.y) < 16));
  for (const e of ends) {
    svg.append(svgEl("circle", {cx: x(e.i), cy: e.y, r: 4, fill: e.s.color, stroke: "var(--surface-1)", "stroke-width": 2}));
    if (!collide) { const lab = svgEl("text", {x: x(e.i) + 10, y: e.y + 4, "font-size": 12, fill: "var(--text-primary)"});
      lab.textContent = money(e.s.values[e.i]) + " " + e.s.label; svg.append(lab); }
  }
  const cross = svgEl("line", {y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden"});
  svg.append(cross);
  const tip = el("div", {class: "tip"}); box.append(svg, tip);
  let cur = t.length - 1;
  const show = i => { cur = Math.max(0, Math.min(t.length - 1, i));
    cross.setAttribute("x1", x(cur)); cross.setAttribute("x2", x(cur)); cross.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", {class: "when"}, when(t[cur])));
    for (const s of series) { const row = el("div", {class: "row"}), key = el("span", {class: "key"}); key.style.background = s.color;
      row.append(key, el("b", {}, money(s.values[cur])), el("span", {}, s.label)); tip.append(row); }
    const r = svg.getBoundingClientRect(), px = x(cur) / W * r.width;
    tip.style.display = "block"; tip.style.top = "8px";
    tip.style.left = (px + 12 + tip.offsetWidth > r.width ? px - tip.offsetWidth - 12 : px + 12) + "px"; };
  const hide = () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; };
  svg.addEventListener("pointermove", ev => { const r = svg.getBoundingClientRect(), vx = (ev.clientX - r.left) / r.width * W;
    let best = 0, bd = Infinity; for (let i = 0; i < t.length; i++) { const dd = Math.abs(x(i) - vx); if (dd < bd) { bd = dd; best = i; } } show(best); });
  svg.addEventListener("pointerleave", hide); svg.addEventListener("blur", hide);
  svg.addEventListener("focus", () => show(cur));
  svg.addEventListener("keydown", ev => { if (ev.key === "ArrowLeft") { show(cur - 1); ev.preventDefault(); }
    if (ev.key === "ArrowRight") { show(cur + 1); ev.preventDefault(); } });
}

function dataTable(boxId, t, series) {
  const box = $(boxId); box.replaceChildren();
  if (!t || !t.length) { box.append(el("div", {class: "empty"}, "No data yet.")); return; }
  const byDay = new Map(); t.forEach((ms, i) => byDay.set(day(ms), i));
  const tbl = el("table"), head = el("tr"); head.append(el("th", {}, "Day (last value)"));
  series.filter(s => s.values).forEach(s => head.append(el("th", {class: "num"}, s.label))); tbl.append(head);
  [...byDay.entries()].reverse().forEach(([d, i]) => { const tr = el("tr"); tr.append(el("td", {}, d));
    series.filter(s => s.values).forEach(s => tr.append(el("td", {class: "num"}, money(s.values[i])))); tbl.append(tr); });
  box.append(tbl);
}

function table(boxId, cols, rows, emptyText) {
  const box = $(boxId); box.replaceChildren();
  if (!rows.length) { box.append(el("div", {class: "empty"}, emptyText)); return; }
  const tbl = el("table"), head = el("tr");
  cols.forEach(c => head.append(el("th", {class: c.num ? "num" : ""}, c.label))); tbl.append(head);
  rows.forEach(r => { const tr = el("tr"); cols.forEach(c => { const v = c.get(r);
    const td = el("td", {class: (c.num ? "num " : "") + (c.time ? "time " : "") + (c.tone ? c.tone(r) : "")});
    if (v instanceof Node) td.append(v); else td.textContent = v === undefined || v === null ? "" : String(v);
    tr.append(td); }); tbl.append(tr); });
  box.append(tbl);
}

function eventCell(text) {
  const lines = String(text || "").split("\n").filter(l => l.trim());
  if (lines.length < 2) return lines[0] || "";
  const d = el("details"); d.append(el("summary", {}, lines[0]), el("div", {class: "more"}, lines.slice(1).join("\n")));
  return d;
}

function render() {
  $("title").textContent = DATA.title;
  $("subtitle").textContent = DATA.mode + " mode · updated " + when(DATA.generated);
  const f = $("filters"); f.replaceChildren();
  RANGES.forEach(([label, d]) => { const b = el("button", {type: "button", "aria-pressed": String(d === rangeDays)}, label);
    b.addEventListener("click", () => { rangeDays = d; try { localStorage.setItem("tb-range", String(d)); } catch (e) {} render(); }); f.append(b); });
  const sl = slice();
  renderTiles(sl);
  const A = sl ? [{label: "Account", values: sl.total, color: "var(--series-1)"},
                  {label: "Holding BTC", values: sl.btc, color: "var(--deemph)"}] : [];
  lineChart("chart-a", "legend-a", sl && sl.t, A); dataTable("table-a", sl && sl.t, A);
  const hasCore = sl && sl.core;
  $("sleeve-card").style.display = hasCore ? "" : "none";
  if (hasCore) { const B = [{label: "Core", values: sl.core, color: "var(--series-2)"},
                            {label: "Satellite", values: sl.satellite, color: "var(--series-3)"}];
    lineChart("chart-b", "legend-b", sl.t, B); dataTable("table-b", sl.t, B); }
  table("positions", [
    {label: "#", get: r => r.id}, {label: "Opened (UTC)", time: true, get: r => short(r.opened)}, {label: "Coin", get: r => r.symbol + (r.status === "working" ? " (limit)" : "")},
    {label: "TF", get: r => r.tf}, {label: "Strategy", get: r => r.strategy},
    {label: "Entry", num: true, get: r => price(r.entry)}, {label: "Stop", num: true, get: r => price(r.stop)},
    {label: "Target", num: true, get: r => price(r.target)},
    {label: "P&L", num: true, get: r => r.upnl === null ? "–" : money(r.upnl), tone: r => r.upnl > 0 ? "pos" : r.upnl < 0 ? "neg" : ""},
    {label: "R", num: true, get: r => r.r === null ? "–" : r.r.toFixed(2)}], DATA.positions, "No open trades.");
  const c = DATA.core, coreBox = $("core"); coreBox.replaceChildren();
  if (!c.enabled) coreBox.append(el("div", {class: "empty"}, c.fraction > 0 ? "Starts at the next daily close." : "Core sleeve is off (core.fraction: 0)."));
  else table("core", [{label: "Coin", get: r => r[0]}, {label: "Target weight", num: true, get: r => r[1] === undefined ? "–" : Math.round(r[1] * 100) + "%"},
                      {label: "Holding", num: true, get: r => r[2] ? r[2].toPrecision(6) : "0"}],
             [...new Set([...Object.keys(c.weights), ...Object.keys(c.holdings)])].map(k => [k, c.weights[k], c.holdings[k]]), "No holdings.");
  table("signals", [{label: "Time (UTC)", time: true, get: r => short(r.time)}, {label: "Coin", get: r => r.symbol},
    {label: "Setup", get: r => r.side + " " + r.tf + " " + r.strategy},
    {label: "Outcome", get: r => r.status + (r.note ? " – " + r.note : "")}], DATA.signals, "No signals yet.");
  table("events", [{label: "Time (UTC)", time: true, get: r => short(r.time)}, {label: "Event", get: r => eventCell(r.text)}], DATA.events, "No events yet.");
}

render();
let resizeTimer = null, lastW = window.innerWidth;
window.addEventListener("resize", () => { if (window.innerWidth === lastW) return; lastW = window.innerWidth;
  clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });
setInterval(async () => {  // refresh in place: keep the frame, no flash
  if (location.protocol === "file:") { location.reload(); return; }  // opened as a file: no fetch
  try { const res = await fetch(location.href, {cache: "no-store"}); if (!res.ok) return;
    const doc = new DOMParser().parseFromString(await res.text(), "text/html");
    const next = doc.getElementById("data"); if (!next) return;
    const fresh = JSON.parse(next.textContent); if (fresh.generated !== DATA.generated) { DATA = fresh; render(); }
  } catch (e) {}
}, 60000);
})();
</script></body></html>
"""
