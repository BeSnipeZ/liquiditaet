"""Selbsttest der Rechenlogik, ohne Chart und ohne Netz.

    C:\\MCP-Server\\tradingview\\.venv\\Scripts\\python.exe test_liquiditaet.py
"""
from datetime import date, datetime, time

import liquiditaet as L


def k(ny_tag, uhr, hoch, tief, schluss=None, dauer=900):
    t = int(datetime.combine(ny_tag, uhr, L.NY).timestamp())
    return (t, dauer, hoch, tief, hoch if schluss is None else schluss)


def test_handelstag():
    assert L.handelstag(k(date(2026, 9, 23), time(18, 0), 1, 1)[0]) == date(2026, 9, 24)
    assert L.handelstag(k(date(2026, 9, 23), time(17, 45), 1, 1)[0]) == date(2026, 9, 23)


def test_session_und_sweep():
    tag = date(2026, 9, 24)
    kerzen = [
        k(tag, time(2, 0), 110, 100),   # London
        k(tag, time(4, 45), 105, 95),   # London-Tief 95
        k(tag, time(5, 30), 104, 96),   # nach London, noch nichts abgeholt
        k(tag, time(6, 0), 100, 94),    # holt 95 ab
    ]
    jetzt = kerzen[-1][0] + 900
    lv = {z["name"]: z for z in L.session_level(kerzen, jetzt)}
    assert lv["London-Tief"]["preis"] == 95 and lv["London-Hoch"]["preis"] == 110
    assert L.level_status(kerzen, 95, "tief", lv["London-Tief"]["ab"]) == kerzen[3][0]
    assert L.level_status(kerzen, 110, "hoch", lv["London-Hoch"]["ab"]) is None


def test_fvg():
    tag = date(2026, 9, 24)
    kerzen = [k(tag, time(10, 0), 100, 90), k(tag, time(10, 15), 120, 99), k(tag, time(10, 30), 125, 110)]
    assert L.fvg_entstehung(kerzen, 100, 110) == ("bull", kerzen[2][0] + 900)
    bear = [k(tag, time(10, 0), 130, 120), k(tag, time(10, 15), 121, 100), k(tag, time(10, 30), 110, 100)]
    assert L.fvg_entstehung(bear, 110, 120)[0] == "bear"

    ab = kerzen[2][0] + 900
    docht = [k(tag, time(10, 45), 115, 99, 112, 60)]           # Docht unter die Zone, Schluss drueber
    assert L.fvg_status(docht, kerzen, "bull", 100, 110, ab, ab + 3600)[0] == "gefuellt"
    rahmen = kerzen + [k(tag, time(10, 45), 112, 95, 97)]     # 15m-Schluss unter der Zone
    assert L.fvg_status(docht, rahmen, "bull", 100, 110, ab, ab + 3600)[0] == "invertiert"
    assert L.fvg_status([], [], "bull", 100, 110, ab, ab)[0] == "unberuehrt"


def test_buendeln_4h_raster():
    tag = date(2026, 9, 24)
    vier = L.buendeln([k(tag, time(6, 0), 1, 1), k(tag, time(9, 45), 2, 0)], "4h")
    assert len(vier) == 1 and vier[0][0] == int(datetime.combine(tag, time(6, 0), L.NY).timestamp())


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
    print("ok")
