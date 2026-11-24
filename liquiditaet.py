"""Liquiditaet am offenen TradingView-Chart: Level, Status, Zeiten.

Liest den Chart der Desktop-App ueber CDP (Port 9222, Start mit tv_launch
oder der Verknuepfung "TradingView (Bridge)") und holt 15-Minuten-Kerzen ueber
tradingview_scraper. Beides liegt im venv des TradingView-MCP-Servers:

    C:\\MCP-Server\\tradingview\\.venv\\Scripts\\python.exe liquiditaet.py [--json] [--alle]

- Session-Level (Asia, London, NY AM, NY PM, Vortag, Vorwoche) werden aus den
  Kerzen gerechnet, mit den Zeiten von "ICT Killzones & Pivots [TFO]".
- Equal Highs/Lows kommen aus der Studie "Equal Highs and Lows". Sie loescht
  abgeholte Level selbst, was dasteht, ist offen.
- FVGs kommen aus der Studie "FVG/iFVG". Die Entstehungskerze wird in den
  Kerzen gesucht, der Status danach nachgerechnet.
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
TOL = 0.01  # Preise sind Vielfache von 0,25

# Killzones wie im TFO-Skript, New Yorker Zeit. Asia beginnt am Vorabend.
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


# ---------------------------------------------------------------- Daten holen

def chart_lesen():
    import websocket
    ziele = json.load(urllib.request.urlopen(CDP + "/json/list", timeout=5))
    ziel = next(z for z in ziele if z.get("type") == "page" and "tradingview.com/chart" in z.get("url", ""))
    ws = websocket.create_connection(ziel["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": CHART_JS, "returnByValue": True}}))
        while True:
            antwort = json.loads(ws.recv())
            if antwort.get("id") == 1:
                break
    finally:
        ws.close()
    res = antwort["result"]
    if "exceptionDetails" in res:
        raise RuntimeError(res["exceptionDetails"].get("text", "Fehler im Chart"))
    return res["result"]["value"]


def kerzen_15m(symbol, anzahl=2000):
    """15-Minuten-Kerzen als (start, dauer, hoch, tief, schluss), rund 30 Handelstage."""
    from tradingview_scraper.symbols.stream import Streamer
    logging.disable(logging.CRITICAL)
    boerse, name = symbol.split(":")
    alt = os.getcwd()
    os.chdir(tempfile.gettempdir())  # der Streamer legt immer eine Exportdatei an und meldet sie per print
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            ohlc = Streamer(export_result=True, export_type="json").stream(
                exchange=boerse, symbol=name, timeframe="15m", numb_price_candles=anzahl)["ohlc"]
    finally:
        os.chdir(alt)
    return [(int(k["timestamp"]), 900, k["high"], k["low"], k["close"]) for k in ohlc]


def zusammenfuehren(k15, chart_bars, dauer):
    """15m fuer die Vergangenheit, Chartkerzen ab der ersten vollen Viertelstunde, die sie abdecken."""
    if not chart_bars:
        return k15
    schnitt = -(-int(chart_bars[0][0]) // 900) * 900
    alt = [k for k in k15 if k[0] + 900 <= schnitt]
    neu = [(int(t), dauer, h, l, c) for t, h, l, c in chart_bars if t >= schnitt]
    return alt + neu


# ----------------------------------------------------------------- Rechnen

def handelstag(t):
    """Der Handelstag beginnt um 18:00 New York am Vortag."""
    return (datetime.fromtimestamp(t, NY) + timedelta(hours=6)).date()


def ny_zeit(tag, uhr, versatz_tage=0):
    return int(datetime.combine(tag + timedelta(days=versatz_tage), uhr, NY).timestamp())


def extrem(kerzen, von, bis):
    teil = [k for k in kerzen if von <= k[0] < bis]
    if not teil:
        return None
    return max(k[2] for k in teil), min(k[3] for k in teil)


def erster_treffer(kerzen, ab, bedingung):
    return next((k[0] for k in kerzen if k[0] >= ab and bedingung(k)), None)


def level_status(kerzen, preis, art, ab):
    """art: 'hoch', 'tief' oder 'mitte'. Liefert den ersten Treffer ab `ab` oder None."""
    if art == "hoch":
        t = erster_treffer(kerzen, ab, lambda k: k[2] > preis)
    elif art == "tief":
        t = erster_treffer(kerzen, ab, lambda k: k[3] < preis)
    else:
        t = erster_treffer(kerzen, ab, lambda k: k[3] <= preis <= k[2])
    return t


def session_level(kerzen, jetzt):
    heute = handelstag(jetzt)
    tage = sorted({handelstag(k[0]) for k in kerzen if handelstag(k[0]) < heute})
    vortag = tage[-1] if tage else None
    level = []

    def dazu(name, preis, art, fertig):
        level.append({"name": name, "preis": preis, "art": art, "ab": fertig, "quelle": "kerzen"})

    for tag, zusatz in ((heute, ""), (vortag, " (Vortag)")):
        if tag is None:
            continue
        for name, anfang, ende, versatz in SESSIONS:
            von, bis = ny_zeit(tag, anfang, versatz), ny_zeit(tag, ende)
            if von >= jetzt or (zusatz and name in ("Asia", "London")):
                continue
            e = extrem(kerzen, von, bis)
            if e:
                dazu(name + "-Hoch" + zusatz, e[0], "hoch", bis)
                dazu(name + "-Tief" + zusatz, e[1], "tief", bis)

    def block(namen, von, bis):
        e = extrem(kerzen, von, bis)
        if e:
            dazu(namen[0], e[0], "hoch", bis)
            dazu(namen[1], e[1], "tief", bis)
            dazu(namen[2], (e[0] + e[1]) / 2, "mitte", bis)

    if vortag:
        block(("PDH", "PDL", "PDM"), ny_zeit(vortag, time(18, 0), -1), ny_zeit(vortag, time(17, 0)))
    woche = heute.isocalendar()[:2]
    vorwoche = [t for t in tage if t.isocalendar()[:2] < woche]
    if vorwoche:
        kw = vorwoche[-1].isocalendar()[:2]
        wtage = [t for t in vorwoche if t.isocalendar()[:2] == kw]
        block(("PWH", "PWL", "PWM"), ny_zeit(wtage[0], time(18, 0), -1), ny_zeit(wtage[-1], time(17, 0)))
    return level


def buendeln(kerzen, rahmen):
    """15m-Kerzen zu 1h, 4h oder D zusammenfassen, Raster wie bei CME in TradingView."""
    gruppen = {}
    for k in kerzen:
        if k[1] != 900:
            continue
        if rahmen == "1h":
            schluessel = k[0] // 3600 * 3600
        elif rahmen == "4h":  # Raster ab Sessionbeginn 18:00 New York
            ab18 = datetime.fromtimestamp(k[0], NY) - timedelta(hours=18)
            start = datetime.combine(ab18.date(), time(0), NY) + timedelta(hours=18 + ab18.hour // 4 * 4)
            schluessel = int(start.timestamp())
        else:
            schluessel = int(datetime.combine(handelstag(k[0]), time(0), NY).timestamp())
        g = gruppen.setdefault(schluessel, [schluessel, 0, k[2], k[3], k[4], k[0] + k[1]])
        g[2], g[3], g[4], g[5] = max(g[2], k[2]), min(g[3], k[3]), k[4], k[0] + k[1]
    return [(g[0], g[5] - g[0], g[2], g[3], g[4]) for g in sorted(gruppen.values())]


def fvg_entstehung(kerzen, unten, oben):
    """Sucht das Dreier-Muster zur Zone. Liefert (richtung, ende der dritten Kerze) oder None."""
    for i in range(len(kerzen) - 1, 1, -1):
        a, c = kerzen[i - 2], kerzen[i]
        if abs(a[2] - unten) < TOL and abs(c[3] - oben) < TOL:
            return "bull", c[0] + c[1]
        if abs(c[2] - unten) < TOL and abs(a[3] - oben) < TOL:
            return "bear", c[0] + c[1]
    return None


def fvg_status(kerzen, rahmen_kerzen, richtung, unten, oben, ab, jetzt):
    """Fuellung an den feinsten Kerzen, Invertierung nur am Schluss einer fertigen Kerze des FVG-Rahmens."""
    nachher = [k for k in kerzen if k[0] >= ab]
    fertig = [k for k in rahmen_kerzen if k[0] >= ab and k[0] + k[1] <= jetzt]
    if richtung == "bull":
        inv = next((k[0] + k[1] for k in fertig if k[4] < unten), None)
        voll = next((k[0] for k in nachher if k[3] <= unten), None)
        tiefst = min((k[3] for k in nachher), default=None)
        angelaufen = tiefst is not None and tiefst < oben
    else:
        inv = next((k[0] + k[1] for k in fertig if k[4] > oben), None)
        voll = next((k[0] for k in nachher if k[2] >= oben), None)
        hoechst = max((k[2] for k in nachher), default=None)
        angelaufen = hoechst is not None and hoechst > unten
    if inv:
        return "invertiert", inv
    if voll:
        return "gefuellt", voll
    return ("angelaufen" if angelaufen else "unberuehrt"), None


def auswerten(chart, k15):
    res = chart["resolution"]
    dauer = int(res) * 60 if str(res).isdigit() else None
    kerzen = zusammenfuehren(k15, chart["bars"] if dauer else [], dauer or 900)
    letzte = kerzen[-1]
    jetzt, kurs = letzte[0] + letzte[1], letzte[4]
    zeilen = []

    for lv in session_level(kerzen, jetzt):
        if lv["ab"] > jetzt:
            lv["status"], lv["zeit"] = "laeuft", None
        else:
            t = level_status(kerzen, lv["preis"], lv["art"], lv["ab"])
            lv["status"] = ("beruehrt" if lv["art"] == "mitte" else "abgeholt") if t else (
                "unberuehrt" if lv["art"] == "mitte" else "offen")
            lv["zeit"] = t
        zeilen.append(lv)

    for name, items in chart["studies"].items():
        if "Equal Highs" in name:
            for p in sorted({round(i["y1"], 2) for i in items if i["k"] == "lines" and i["y1"] == i["y2"]}):
                zeilen.append({"name": "EQH" if p > kurs else "EQL", "preis": p, "art": "hoch" if p > kurs else "tief",
                               "status": "offen", "zeit": None, "quelle": "studie"})
        if "FVG" in name:
            gesehen = set()
            for i in items:
                if i["k"] != "boxes" or i["y1"] is None:
                    continue
                unten, oben = min(i["y1"], i["y2"]), max(i["y1"], i["y2"])
                rahmen = (i["t"].split() or ["?"])[0] if i["t"] != "FVG" else "Chart"
                if (rahmen, unten, oben) in gesehen:
                    continue
                gesehen.add((rahmen, unten, oben))
                if rahmen == "Chart":
                    basis = [k for k in kerzen if k[1] == dauer]
                elif rahmen == "15m":
                    basis = k15
                else:
                    basis = buendeln(k15, rahmen) if rahmen in ("1h", "4h", "D") else []
                treffer = fvg_entstehung(basis, unten, oben)
                z = {"name": f"FVG {rahmen}", "preis": oben, "unten": unten, "art": "fvg", "quelle": "studie"}
                if treffer:
                    z["richtung"] = treffer[0]
                    z["status"], z["zeit"] = fvg_status(kerzen, basis, treffer[0], unten, oben, treffer[1], jetzt)
                    z["entstanden"] = treffer[1]
                else:
                    z["status"], z["zeit"], z["richtung"] = "Entstehung nicht gefunden", None, None
                z["kurs_drin"] = unten <= kurs <= oben
                zeilen.append(z)

    zeilen.sort(key=lambda z: -z["preis"])
    return {"symbol": chart["symbol"], "kurs": kurs, "jetzt": jetzt, "level": zeilen}


# ---------------------------------------------------------------- Ausgabe

def uhr(t, jetzt):
    d = datetime.fromtimestamp(t, BERLIN)
    heute = datetime.fromtimestamp(jetzt, BERLIN).date()
    tag = "Mo Di Mi Do Fr Sa So".split()[d.weekday()]
    return d.strftime("%H:%M") if d.date() == heute else f"{tag} {d:%d.%m. %H:%M}"


def zahl(p):
    return f"{p:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def text(ergebnis, alle=False):
    jetzt, kurs = ergebnis["jetzt"], ergebnis["kurs"]
    out = [f"{ergebnis['symbol']}  Kurs {zahl(kurs)}  (Stand {uhr(jetzt, jetzt)} Berlin)", ""]
    markiert = False
    for z in ergebnis["level"]:
        if z["art"] == "fvg" and not alle and z["status"] in ("gefuellt", "invertiert", "Entstehung nicht gefunden"):
            continue
        if not markiert and z["preis"] < kurs:
            out.append(f"  ---- {zahl(kurs)}  Kurs ----")
            markiert = True
        status = z["status"]
        if z.get("zeit"):
            status += " " + uhr(z["zeit"], jetzt)
            if z["art"] in ("hoch", "tief"):
                status += ", Kurs zurueck" if (kurs < z["preis"]) == (z["art"] == "hoch") else ", Kurs dahinter"
        if z["art"] == "fvg":
            name = f"{z['name']} {'bull' if z['richtung'] == 'bull' else 'bear' if z['richtung'] else ''}".strip()
            preis = f"{zahl(z['preis'])}-{zahl(z['unten'])}"
            if z["kurs_drin"]:
                status += ", Kurs drin"
        else:
            name, preis = z["name"], zahl(z["preis"])
        out.append(f"  {preis:>21}  {name:<22} {status}")
    if not markiert:
        out.append(f"  ---- {zahl(kurs)}  Kurs ----")
    if not alle:
        out += ["", "FVGs gefuellt, invertiert oder ohne gefundene Entstehung ausgeblendet (--alle zeigt sie)."]
    return "\n".join(out)


def main():
    chart = chart_lesen()
    ergebnis = auswerten(chart, kerzen_15m(chart["symbol"]))
    if "--json" in sys.argv:
        print(json.dumps(ergebnis, ensure_ascii=False, indent=1))
    else:
        print(text(ergebnis, "--alle" in sys.argv))


if __name__ == "__main__":
    main()
