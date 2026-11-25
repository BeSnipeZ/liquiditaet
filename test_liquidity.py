"""Self-test of the computing logic, no chart and no network needed.

    C:\\MCP-Server\\tradingview\\.venv\\Scripts\\python.exe test_liquidity.py
"""
from datetime import date, datetime, time

import liquidity as L


def c(ny_day, clock, high, low, close=None, duration=900):
    t = int(datetime.combine(ny_day, clock, L.NY).timestamp())
    return (t, duration, high, low, high if close is None else close)


def test_trading_day():
    assert L.trading_day(c(date(2026, 9, 23), time(18, 0), 1, 1)[0]) == date(2026, 9, 24)
    assert L.trading_day(c(date(2026, 9, 23), time(17, 45), 1, 1)[0]) == date(2026, 9, 23)


def test_session_and_sweep():
    day = date(2026, 9, 24)
    candles = [
        c(day, time(2, 0), 110, 100),   # London
        c(day, time(4, 45), 105, 95),   # London low 95
        c(day, time(5, 30), 104, 96),   # after London, nothing swept yet
        c(day, time(6, 0), 100, 94),    # sweeps 95
    ]
    now = candles[-1][0] + 900
    lv = {z["name"]: z for z in L.session_levels(candles, now)}
    assert lv["London low"]["price"] == 95 and lv["London high"]["price"] == 110
    assert L.level_status(candles, 95, "low", lv["London low"]["since"]) == candles[3][0]
    assert L.level_status(candles, 110, "high", lv["London high"]["since"]) is None


def test_fvg():
    day = date(2026, 9, 24)
    candles = [c(day, time(10, 0), 100, 90), c(day, time(10, 15), 120, 99), c(day, time(10, 30), 125, 110)]
    assert L.fvg_origin(candles, 100, 110) == ("bull", candles[2][0] + 900)
    bear = [c(day, time(10, 0), 130, 120), c(day, time(10, 15), 121, 100), c(day, time(10, 30), 110, 100)]
    assert L.fvg_origin(bear, 110, 120)[0] == "bear"

    since = candles[2][0] + 900
    wick = [c(day, time(10, 45), 115, 99, 112, 60)]           # wick below the zone, close above
    assert L.fvg_status(wick, candles, "bull", 100, 110, since, since + 3600)[0] == "filled"
    frame = candles + [c(day, time(10, 45), 112, 95, 97)]     # 15m close below the zone
    assert L.fvg_status(wick, frame, "bull", 100, 110, since, since + 3600)[0] == "inverted"
    assert L.fvg_status([], [], "bull", 100, 110, since, since)[0] == "untouched"


def test_resample_4h_grid():
    day = date(2026, 9, 24)
    four = L.resample([c(day, time(6, 0), 1, 1), c(day, time(9, 45), 2, 0)], "4h")
    assert len(four) == 1 and four[0][0] == int(datetime.combine(day, time(6, 0), L.NY).timestamp())


def test_text_output():
    r = {"symbol": "CME_MINI:NQ1!", "price": 100.0, "now": 0, "levels": [
        {"name": "PDH", "price": 110.0, "kind": "high", "status": "open", "time": None},
        {"name": "FVG 1h", "price": 99.0, "bottom": 98.0, "kind": "fvg", "status": "tapped", "time": None,
         "direction": "bull", "price_inside": False},
        {"name": "FVG 4h", "price": 90.0, "bottom": 89.0, "kind": "fvg", "status": "filled", "time": None,
         "direction": None, "price_inside": False}]}
    out = L.text(r)
    assert "PDH" in out and "FVG 1h bull" in out and "FVG 4h" not in out and "--all" in out
    assert "FVG 4h" in L.text(r, show_all=True)


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
    print("ok")
