# liquiditaet

Reads the chart that is open in the TradingView desktop app and lists where the
liquidity sits: session highs and lows, equal highs/lows and fair value gaps,
each with its status (open, taken, filled, inverted) and the time it formed.

Built for intraday Nasdaq futures, but it works on whatever symbol is on the chart.

## How it works

1. **Chart state** comes from the running desktop app over the Chrome DevTools
   Protocol (`127.0.0.1:9222`): symbol, timeframe and the drawings of two
   indicators (*Equal Highs and Lows*, *FVG/iFVG*).
2. **Candles** (15 minutes, ~2000 bars) come from `tradingview_scraper` and are
   merged with the bars the chart already holds.
3. **Session levels** (Asia, London, NY AM, NY PM, previous day, previous week)
   are computed from the candles in New York time, so daylight-saving shifts
   between Europe and the US are handled.
4. **Status** is recomputed from the candles instead of trusting the indicator:
   the first candle that trades through a level marks it as taken, and an FVG is
   traced back to the candle that created it.

## Usage

```
python liquiditaet.py            # readable table
python liquiditaet.py --json     # machine-readable, used by other tools
python liquiditaet.py --alle     # also show filled / inverted gaps
```

Requires TradingView Desktop started with `--remote-debugging-port=9222` and
Python 3.9+ with `tradingview_scraper`.

## Tests

```
python test_liquiditaet.py
```

The self-test covers the pure logic (trading-day boundaries, session levels,
level status, FVG status) with synthetic candles, no chart and no network needed.

Code comments and output are in German.
