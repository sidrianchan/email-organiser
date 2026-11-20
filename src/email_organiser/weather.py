"""Today's Hong Kong weather and any warning signals in force, for the morning brief (21 Nov).

Source: the Hong Kong Observatory's open data API (free, no key, no account):
    dataType=fnd      9-day forecast (we use today's entry)
    dataType=warnsum  warnings in force, e.g. rainstorm or typhoon signals
Documentation: https://www.hko.gov.hk/en/weatherAPI/doc/files/HKO_Open_Data_API_Documentation.pdf

Why bother: a black rainstorm or No. 8 signal cancels interviews and exams, and you'd want to
know at 08:00. Only used while this Mac's time zone is Hong Kong; when travelling, the
Hong Kong forecast would just be noise. No Claude call. Never raises: no weather line is
better than no summary.
"""
import json
import os
import urllib.request
from datetime import date

URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType={kind}&lang=en"
TIMEOUT = 10   # seconds per request
RAIN_COLOURS = {"WRAINA": "Amber", "WRAINR": "Red", "WRAINB": "Black"}
# Only warnings that can change the day's plans. Left out: cold / very hot weather, fire danger,
# frost and the strong monsoon signal. They can stay in force for days, the temperature line
# already says it's cold, and listing them every winter morning would be noise that trains you
# to skip the line where a black rainstorm would one day appear.
PLAN_CHANGING = {"WRAIN", "WTCSGNL", "WTS", "WL", "WFNTSA", "WTMW"}
TYPHOON = {"TC1": "No. 1 Standby Signal", "TC3": "No. 3 Strong Wind Signal",
           "TC8": "No. 8 Gale or Storm Signal", "TC9": "No. 9 Increasing Gale or Storm Signal",
           "TC10": "No. 10 Hurricane Signal"}


def in_hong_kong() -> bool:
    """True if this Mac's time zone is Hong Kong (System Settings -> General -> Date & Time)."""
    return os.path.realpath("/etc/localtime").endswith("Asia/Hong_Kong")


def fetch(kind: str) -> dict:
    with urllib.request.urlopen(URL.format(kind=kind), timeout=TIMEOUT) as resp:
        return json.load(resp)


def forecast_for(fnd: dict, day: date) -> dict | None:
    """Today's entry of the 9-day forecast, or None if the forecast doesn't cover today
    (never show another day's weather as today's)."""
    for f in fnd.get("weatherForecast", []):
        if f.get("forecastDate") == f"{day:%Y%m%d}":
            return dict(summary=(f.get("forecastWeather") or "").strip(),
                        low=(f.get("forecastMintemp") or {}).get("value"),
                        high=(f.get("forecastMaxtemp") or {}).get("value"),
                        rain=(f.get("PSR") or "").strip())
    return None


def warnings(warnsum: dict) -> list[str]:
    """Plan-changing warnings in force: 'Amber Rainstorm Warning Signal', 'No. 8 Gale or Storm Signal'."""
    out = []
    for code, w in (warnsum or {}).items():
        if not isinstance(w, dict) or w.get("actionCode") == "CANCEL" or code not in PLAN_CHANGING:
            continue
        name, kind = w.get("name") or code, w.get("type") or ""
        if code == "WRAIN" and kind in RAIN_COLOURS:
            name = f"{RAIN_COLOURS[kind]} {name}"
        elif code == "WTCSGNL":
            name = TYPHOON.get(kind.rstrip("NESW"), name)   # 'TC8NE' -> 'TC8'; 'TC10' stays
        out.append(name)
    return out


def get(day: date) -> dict | None:
    """{'forecast': {...} or None, 'warnings': [...]}; None when not in Hong Kong or the
    Observatory couldn't be reached. Never raises."""
    if not in_hong_kong():
        return None
    forecast, warns, ok = None, [], False
    try:
        forecast, ok = forecast_for(fetch("fnd"), day), True
    except Exception as e:
        print(f"Weather forecast FAILED (summary unaffected): {e}")
    try:
        warns, ok = warnings(fetch("warnsum")), True
    except Exception as e:
        print(f"Weather warnings FAILED (summary unaffected): {e}")
    return dict(forecast=forecast, warnings=warns) if ok else None


def line(w: dict | None, brief: bool = False) -> str:
    """'13–21°C, rain chance low. Mainly cloudy. One or two rain patches at first.' or ''.
    brief=True (Telegram): '13–21°C · Mainly cloudy · rain chance low', first sentence only."""
    f = (w or {}).get("forecast")
    if not f:
        return ""
    temps = f"{f['low']}–{f['high']}°C" if f["low"] is not None and f["high"] is not None else ""
    rain = f"rain chance {f['rain'].lower()}" if f["rain"] else ""
    if brief:
        first = f["summary"].split(". ")[0].rstrip(".")
        return " · ".join(b for b in (temps, first, rain) if b)
    head = ", ".join(b for b in (temps, rain) if b)
    return ". ".join(b for b in (head, f["summary"]) if b)
