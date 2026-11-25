"""Liquidity on the open TradingView chart: levels, status, times.

Reads the chart of the desktop app over CDP (port 9222, start it with tv_launch
or the "TradingView (Bridge)" shortcut) and fetches 15-minute candles via
tradingview_scraper. Both live in the venv of the TradingView MCP server:

    C:\\MCP-Server\\tradingview\\.venv\\Scripts\\python.exe liquidity.py [--json] [--all]

- Session levels (Asia, London, NY AM, NY PM, previous day, previous week) are
  computed from the candles, with the times of "ICT Killzones & Pivots [TFO]".
- Equal highs/lows come from the "Equal Highs and Lows" study. It deletes
  swept levels itself, so whatever is drawn is still open.
- FVGs come from the "FVG/iFVG" study. The candle that created each gap is
  searched in the candles and its status is recomputed from there.
"""
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")
CDP = "http://127.0.0.1:9222"
TOL = 0.01  # prices are multiples of 0.25

# Killzones as in the TFO script, New York time. Asia starts the evening before.
SESSIONS = [
    ("Asia", time(20, 0), time(0, 0), -1),
    ("London", time(2, 0), time(5, 0), 0),
    ("NY AM", time(9, 30), time(11, 0), 0),
    ("NY PM", time(13, 30), time(16, 0), 0),
]

CHART_JS = r"""(() => {
  const api = window.TradingViewApi, m = api._activeChartWidgetWV.value()._chartWidget.model();
  const b = m.mainSeries().bars(), bars = [];
  for (let i = b.firstIndex(); i <= b.lastIndex(); i++) { const v = b.valueAt(i); if (v) bars.push([v[0], v[2], v[3], v[4]]); }
  const out = {symbol: api.activeChart().symbol(), resolution: api.activeChart().resolution(), bars, studies: {}};
  for (const s of m.model().dataSources()) {
    if (!s.metaInfo || !s._graphics) continue;
    const pc = s._graphics._primitivesCollection, items = [];
    for (const [col, key] of [['dwglines', 'lines'], ['dwgboxes', 'boxes']]) {
      try { pc[col].get(key).get(false)._primitivesDataById.forEach(v => items.push({k: key, y1: v.y1, y2: v.y2, t: v.t || ''})); } catch (e) {}
    }
    out.studies[s.metaInfo().description] = items;
  }
  return out;
})()"""


# ---------------------------------------------------------------- Fetching

def read_chart():
    import websocket
    targets = json.load(urllib.request.urlopen(CDP + "/json/list", timeout=5))
    target = next(t for t in targets if t.get("type") == "page" and "tradingview.com/chart" in t.get("url", ""))
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": CHART_JS, "returnByValue": True}}))
        while True:
            reply = json.loads(ws.recv())
            if reply.get("id") == 1:
                break
    finally:
        ws.close()
    res = reply["result"]
    if "exceptionDetails" in res:
        raise RuntimeError(res["exceptionDetails"].get("text", "error in chart"))
    return res["result"]["value"]


def candles_15m(symbol, count=2000):
    """15-minute candles as (start, duration, high, low, close), about 30 trading days."""
    from tradingview_scraper.symbols.stream import Streamer
    logging.disable(logging.CRITICAL)
    exchange, name = symbol.split(":")
    old = os.getcwd()
    os.chdir(tempfile.gettempdir())  # the streamer always writes an export file and announces it via print
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ohlc = Streamer(export_result=True, export_type="json").stream(
                exchange=exchange, symbol=name, timeframe="15m", numb_price_candles=count)["ohlc"]
    finally:
        os.chdir(old)
    return [(int(c["timestamp"]), 900, c["high"], c["low"], c["close"]) for c in ohlc]


def merge(c15, chart_bars, duration):
    """15m for the past, chart bars from the first full quarter hour they cover."""
    if not chart_bars:
        return c15
    cut = -(-int(chart_bars[0][0]) // 900) * 900
    old = [c for c in c15 if c[0] + 900 <= cut]
    new = [(int(t), duration, h, l, c) for t, h, l, c in chart_bars if t >= cut]
    return old + new


# ---------------------------------------------------------------- Computing

def trading_day(t):
    """The trading day starts at 18:00 New York on the previous calendar day."""
    return (datetime.fromtimestamp(t, NY) + timedelta(hours=6)).date()


def ny_time(day, clock, offset_days=0):
    return int(datetime.combine(day + timedelta(days=offset_days), clock, NY).timestamp())


def extreme(candles, start, end):
    part = [c for c in candles if start <= c[0] < end]
    if not part:
        return None
    return max(c[2] for c in part), min(c[3] for c in part)


def first_hit(candles, since, condition):
    return next((c[0] for c in candles if c[0] >= since and condition(c)), None)


def level_status(candles, price, kind, since):
    """kind: 'high', 'low' or 'mid'. Returns the first hit from `since` on, or None."""
    if kind == "high":
        return first_hit(candles, since, lambda c: c[2] > price)
    if kind == "low":
        return first_hit(candles, since, lambda c: c[3] < price)
    return first_hit(candles, since, lambda c: c[3] <= price <= c[2])


def session_levels(candles, now):
    today = trading_day(now)
    days = sorted({trading_day(c[0]) for c in candles if trading_day(c[0]) < today})
    prev_day = days[-1] if days else None
    levels = []

    def add(name, price, kind, done):
        levels.append({"name": name, "price": price, "kind": kind, "since": done, "source": "candles"})

    for day, suffix in ((today, ""), (prev_day, " (prev day)")):
        if day is None:
            continue
        for name, start, end, offset in SESSIONS:
            t0, t1 = ny_time(day, start, offset), ny_time(day, end)
            if t0 >= now or (suffix and name in ("Asia", "London")):
                continue
            e = extreme(candles, t0, t1)
            if e:
                add(name + " high" + suffix, e[0], "high", t1)
                add(name + " low" + suffix, e[1], "low", t1)

    def block(names, t0, t1):
        e = extreme(candles, t0, t1)
        if e:
            add(names[0], e[0], "high", t1)
            add(names[1], e[1], "low", t1)
            add(names[2], (e[0] + e[1]) / 2, "mid", t1)

    if prev_day:
        block(("PDH", "PDL", "PDM"), ny_time(prev_day, time(18, 0), -1), ny_time(prev_day, time(17, 0)))
    week = today.isocalendar()[:2]
    prev_week = [d for d in days if d.isocalendar()[:2] < week]
    if prev_week:
        wk = prev_week[-1].isocalendar()[:2]
        wdays = [d for d in prev_week if d.isocalendar()[:2] == wk]
        block(("PWH", "PWL", "PWM"), ny_time(wdays[0], time(18, 0), -1), ny_time(wdays[-1], time(17, 0)))
    return levels


def resample(candles, frame):
    """Combine 15m candles into 1h, 4h or D, on the same grid as CME in TradingView."""
    groups = {}
    for c in candles:
        if c[1] != 900:
            continue
        if frame == "1h":
            key = c[0] // 3600 * 3600
        elif frame == "4h":  # grid from session start 18:00 New York
            from18 = datetime.fromtimestamp(c[0], NY) - timedelta(hours=18)
            start = datetime.combine(from18.date(), time(0), NY) + timedelta(hours=18 + from18.hour // 4 * 4)
            key = int(start.timestamp())
        else:
            key = int(datetime.combine(trading_day(c[0]), time(0), NY).timestamp())
        g = groups.setdefault(key, [key, 0, c[2], c[3], c[4], c[0] + c[1]])
        g[2], g[3], g[4], g[5] = max(g[2], c[2]), min(g[3], c[3]), c[4], c[0] + c[1]
    return [(g[0], g[5] - g[0], g[2], g[3], g[4]) for g in sorted(groups.values())]


def fvg_origin(candles, bottom, top):
    """Finds the three-candle pattern for the zone. Returns (direction, end of third candle) or None."""
    for i in range(len(candles) - 1, 1, -1):
        a, c = candles[i - 2], candles[i]
        if abs(a[2] - bottom) < TOL and abs(c[3] - top) < TOL:
            return "bull", c[0] + c[1]
        if abs(c[2] - bottom) < TOL and abs(a[3] - top) < TOL:
            return "bear", c[0] + c[1]
    return None


def fvg_status(candles, frame_candles, direction, bottom, top, since, now):
    """Fill on the finest candles, inversion only on the close of a finished candle of the FVG's timeframe."""
    after = [c for c in candles if c[0] >= since]
    done = [c for c in frame_candles if c[0] >= since and c[0] + c[1] <= now]
    if direction == "bull":
        inv = next((c[0] + c[1] for c in done if c[4] < bottom), None)
        full = next((c[0] for c in after if c[3] <= bottom), None)
        lowest = min((c[3] for c in after), default=None)
        tapped = lowest is not None and lowest < top
    else:
        inv = next((c[0] + c[1] for c in done if c[4] > top), None)
        full = next((c[0] for c in after if c[2] >= top), None)
        highest = max((c[2] for c in after), default=None)
        tapped = highest is not None and highest > bottom
    if inv:
        return "inverted", inv
    if full:
        return "filled", full
    return ("tapped" if tapped else "untouched"), None


def evaluate(chart, c15):
    res = chart["resolution"]
    duration = int(res) * 60 if str(res).isdigit() else None
    candles = merge(c15, chart["bars"] if duration else [], duration or 900)
    last = candles[-1]
    now, price = last[0] + last[1], last[4]
    rows = []

    for lv in session_levels(candles, now):
        if lv["since"] > now:
            lv["status"], lv["time"] = "forming", None
        else:
            t = level_status(candles, lv["price"], lv["kind"], lv["since"])
            lv["status"] = ("touched" if lv["kind"] == "mid" else "swept") if t else (
                "untouched" if lv["kind"] == "mid" else "open")
            lv["time"] = t
        rows.append(lv)

    for name, items in chart["studies"].items():
        if "Equal Highs" in name:
            for p in sorted({round(i["y1"], 2) for i in items if i["k"] == "lines" and i["y1"] == i["y2"]}):
                rows.append({"name": "EQH" if p > price else "EQL", "price": p, "kind": "high" if p > price else "low",
                             "status": "open", "time": None, "source": "study"})
        if "FVG" in name:
            seen = set()
            for i in items:
                if i["k"] != "boxes" or i["y1"] is None:
                    continue
                bottom, top = min(i["y1"], i["y2"]), max(i["y1"], i["y2"])
                frame = (i["t"].split() or ["?"])[0] if i["t"] != "FVG" else "Chart"
                if (frame, bottom, top) in seen:
                    continue
                seen.add((frame, bottom, top))
                if frame == "Chart":
                    base = [c for c in candles if c[1] == duration]
                elif frame == "15m":
                    base = c15
                else:
                    base = resample(c15, frame) if frame in ("1h", "4h", "D") else []
                hit = fvg_origin(base, bottom, top)
                z = {"name": f"FVG {frame}", "price": top, "bottom": bottom, "kind": "fvg", "source": "study"}
                if hit:
                    z["direction"] = hit[0]
                    z["status"], z["time"] = fvg_status(candles, base, hit[0], bottom, top, hit[1], now)
                    z["formed"] = hit[1]
                else:
                    z["status"], z["time"], z["direction"] = "origin not found", None, None
                z["price_inside"] = bottom <= price <= top
                rows.append(z)

    rows.sort(key=lambda z: -z["price"])
    return {"symbol": chart["symbol"], "price": price, "now": now, "levels": rows}


# ---------------------------------------------------------------- Output

def clock(t, now):
    d = datetime.fromtimestamp(t, BERLIN)
    today = datetime.fromtimestamp(now, BERLIN).date()
    return d.strftime("%H:%M") if d.date() == today else d.strftime("%a %d.%m. %H:%M")


def num(p):
    return f"{p:,.2f}"


def text(result, show_all=False):
    now, price = result["now"], result["price"]
    out = [f"{result['symbol']}  price {num(price)}  (as of {clock(now, now)} Berlin)", ""]
    marked = False
    for z in result["levels"]:
        if z["kind"] == "fvg" and not show_all and z["status"] in ("filled", "inverted", "origin not found"):
            continue
        if not marked and z["price"] < price:
            out.append(f"  ---- {num(price)}  price ----")
            marked = True
        status = z["status"]
        if z.get("time"):
            status += " " + clock(z["time"], now)
            if z["kind"] in ("high", "low"):
                status += ", price back" if (price < z["price"]) == (z["kind"] == "high") else ", price beyond"
        if z["kind"] == "fvg":
            name = f"{z['name']} {z['direction'] or ''}".strip()
            level = f"{num(z['price'])}-{num(z['bottom'])}"
            if z["price_inside"]:
                status += ", price inside"
        else:
            name, level = z["name"], num(z["price"])
        out.append(f"  {level:>21}  {name:<22} {status}")
    if not marked:
        out.append(f"  ---- {num(price)}  price ----")
    if not show_all:
        out += ["", "Filled, inverted and unmatched FVGs hidden (--all shows them)."]
    return "\n".join(out)


def main():
    chart = read_chart()
    result = evaluate(chart, candles_15m(chart["symbol"]))
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(text(result, "--all" in sys.argv))


if __name__ == "__main__":
    main()
