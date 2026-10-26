"""Daily market snapshot for KKReckons' edition strip: writes docs/markets.json.

Six headline numbers (close, the previous close, the close a week earlier and the last 30 closes), read from
Yahoo Finance with the same fetcher as the index (engine.fetch_yahoo). KKReckons saves a copy into each edition
when it is published, so editions keep the numbers of their day. Not part of the index: a failed asset is left
out with a note and never stops the index update.

Usage: python markets.py [out.json]
"""
import datetime as dt, json, os, sys
import engine

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = next((a for a in sys.argv[1:] if a.endswith(".json")), os.path.join(HERE, "docs", "markets.json"))

# (id, name, Yahoo symbol, unit): "index" points, "yield" per cent, "usd" dollars, "fx" rupees per dollar.
ASSETS = [
    ("nifty", "Nifty 50", "^NSEI", "index"),
    ("spx", "S&P 500", "^GSPC", "index"),
    ("us10y", "US 10-year yield", "^TNX", "yield"),
    ("brent", "Brent crude", "BZ=F", "usd"),
    ("gold", "Gold", "GC=F", "usd"),
    ("usdinr", "USD/INR", "USDINR=X", "fx"),
]
SPARK = 30


def snapshot(closes):
    """Latest close, previous close, the last close at least 7 days before the latest, and the last 30 closes."""
    days = sorted(d for d, v in closes.items() if v is not None)
    if len(days) < 2:
        return None
    last = days[-1]
    week = [d for d in days if d <= last - dt.timedelta(days=7)]
    return {
        "close": closes[last], "asOf": last.isoformat(),
        "prevClose": closes[days[-2]], "prevDate": days[-2].isoformat(),
        **({"weekClose": closes[week[-1]], "weekDate": week[-1].isoformat()} if week else {}),
        "spark": [closes[d] for d in days[-SPARK:]],
    }


def build(fetch=engine.fetch_yahoo):
    assets, notes = [], []
    for aid, name, symbol, unit in ASSETS:
        try:
            snap = snapshot(fetch(symbol, "3mo"))
        except Exception as e:  # one feed down must not sink the others
            snap, why = None, str(e)[:160]
        else:
            why = "fewer than two closes"
        if snap:
            assets.append({"id": aid, "name": name, "symbol": symbol, "unit": unit, **snap})
        else:
            notes.append(f"{name} ({symbol}) left out: {why}")
    return {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source": "Yahoo Finance daily closes", "assets": assets, "notes": notes}


if __name__ == "__main__":
    data = build()
    if not data["assets"]:
        print("markets: no asset could be fetched; keeping the previous markets.json")
        sys.exit(0)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(data, f, indent=1)
    print(f"markets: {len(data['assets'])} of {len(ASSETS)} assets" + (f"; {'; '.join(data['notes'])}" if data["notes"] else ""))
