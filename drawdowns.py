"""AI drawdown monitor for KKReckons: writes docs/drawdowns.json and docs/drawdowns.html.

For each stock in drawdowns/basket.json: its highest-ever daily close, the latest close and the day's move, all
split-adjusted but not dividend-adjusted (a drawdown readers can check against any quote screen). Fills
drawdowns/template.html with the result, so GitHub Pages serves the finished page at /drawdowns.html.

Not part of the index: like markets.py, it runs after the index in the daily workflow and a failure never stops it.
A stock that can't be fetched keeps its row from the previous drawdowns.json, marked stale. If more than
MAX_MISSING stocks fail, or the checks below fail, nothing is written and the last good page stays up.

Usage: python drawdowns.py            # live data
       python drawdowns.py --check    # check the published docs/drawdowns.json only
"""
import datetime as dt, html, json, os, re, statistics, sys, time, urllib.parse
import engine

HERE = os.path.dirname(os.path.abspath(__file__))
BASKET = os.path.join(HERE, "drawdowns", "basket.json")
TEMPLATE = os.path.join(HERE, "drawdowns", "template.html")
OUT_JSON = os.path.join(HERE, "docs", "drawdowns.json")
OUT_HTML = os.path.join(HERE, "docs", "drawdowns.html")
MAX_MISSING = 5
PRE_AI_BOOM = "2025-01-01"  # peaks before this date are flagged: part of the fall predates the AI trade
CURRENCY = {"USD": "$", "KRW": "₩"}


def fetch_closes(symbol):
    """{date: close} of daily closes since listing, split-adjusted, not dividend-adjusted.
    Uses explicit dates (range=max returns monthly bars) and drops a bar still in session."""
    span = f"period1={int(dt.datetime(1985, 1, 1, tzinfo=dt.timezone.utc).timestamp())}&period2={int(time.time())}"
    sym = urllib.parse.quote(symbol)
    txt = None
    for host in ("query1", "query2"):
        try:
            txt = engine._get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?{span}&interval=1d&events=split")
            break
        except RuntimeError:
            continue
    if txt is None:
        raise RuntimeError(f"Yahoo Finance unavailable for {symbol}")
    j = json.loads(txt)["chart"]["result"][0]
    stamps, close = j["timestamp"], j["indicators"]["quote"][0]["close"]
    if stamps and engine._in_session(j.get("meta") or {}, stamps[-1]):
        stamps = stamps[:-1]
    off = (j.get("meta") or {}).get("gmtoffset", 0)  # date bars in the exchange's own time zone (Seoul, not UTC)
    return {dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date(): c for t, c in zip(stamps, close) if c}


def row(stock, closes):
    days = sorted(closes)
    if len(days) < 2:
        raise RuntimeError("fewer than two closes")
    peak_day = max(days, key=lambda d: closes[d])
    last, prev = closes[days[-1]], closes[days[-2]]
    peak = closes[peak_day]
    r = {"t": stock["ticker"], "symbol": stock["symbol"], "n": stock["name"], "g": stock["segment"],
         "ath": round(peak, 2), "d": peak_day.isoformat(), "c": round(last, 2), "asOf": days[-1].isoformat(),
         "dd": round(last / peak - 1, 4), "day": round(last / prev - 1, 4),
         "old": peak_day.isoformat() < PRE_AI_BOOM, "cur": CURRENCY[stock.get("currency", "USD")],
         "ipo": bool(stock.get("ipo"))}
    if stock.get("ipo"):
        r["ipoPrice"] = stock["ipo"]["price"]
    return r


def previous_rows(path=OUT_JSON):
    try:
        with open(path) as f:
            return {r["symbol"]: r for r in json.load(f)["stocks"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def build(basket, fetch=fetch_closes, previous=None):
    rows, notes, missing = [], [], []
    for s in basket["stocks"]:
        try:
            rows.append(row(s, fetch(s["symbol"])))
        except Exception as e:  # one feed down must not sink the others
            old = (previous or {}).get(s["symbol"])
            missing.append(s["symbol"])
            if old:
                rows.append({**old, "stale": True})
                notes.append(f"{s['ticker']}: kept the {old['asOf']} close ({str(e)[:80]})")
            else:
                notes.append(f"{s['ticker']} left out: {str(e)[:80]}")
    usd = [r["asOf"] for r in rows if r["cur"] == "$" and not r.get("stale")]
    as_of = max(usd) if usd else max(r["asOf"] for r in rows)
    behind = [r["t"] for r in rows if r["cur"] == "$" and r["asOf"] != as_of]
    if behind:
        notes.append("Close not yet current for " + ", ".join(behind))
    dd = [r["dd"] for r in rows]
    summary = {"count": len(rows), "medianDrawdown": round(statistics.median(dd), 4),
               "down50": sum(v <= -0.5 for v in dd), "down75": sum(v <= -0.75 for v in dd),
               "medianDay": round(statistics.median(r["day"] for r in rows), 4)}
    return {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "asOf": as_of,
            "source": "Yahoo Finance daily closes, split-adjusted, not dividend-adjusted",
            "summary": summary, "stocks": rows, "missing": missing, "notes": notes}


def check(data, basket):
    """Problems that must stop a publish. Empty list = good."""
    errs = []
    want = {s["symbol"] for s in basket["stocks"]}
    got = [r["symbol"] for r in data["stocks"]]
    if len(got) != len(set(got)):
        errs.append("duplicate stocks")
    if len(data["missing"]) > MAX_MISSING:
        errs.append(f"{len(data['missing'])} stocks failed to fetch (limit {MAX_MISSING})")
    if set(got) - want:
        errs.append("stocks not in the basket: " + ", ".join(sorted(set(got) - want)))
    segs = set(basket["segments"])
    for r in data["stocks"]:
        if not -1 < r["dd"] <= 0:
            errs.append(f"{r['t']}: drawdown {r['dd']} out of range")
        if r["g"] not in segs:
            errs.append(f"{r['t']}: segment {r['g']} not in basket.segments")
        if r["d"] > r["asOf"]:
            errs.append(f"{r['t']}: peak date after latest close")
        if abs(r["c"] / r["ath"] - 1 - r["dd"]) > 0.0002:
            errs.append(f"{r['t']}: drawdown does not match its prices")
        if abs(r["day"]) > 0.6:
            errs.append(f"{r['t']}: day move {r['day']:+.0%} looks like a bad print or an unadjusted split")
    s = data["summary"]
    dd = [r["dd"] for r in data["stocks"]]
    if s["count"] != len(dd) or s["down50"] != sum(v <= -0.5 for v in dd) or abs(s["medianDrawdown"] - statistics.median(dd)) > 1e-4:
        errs.append("summary does not match the rows")
    return errs


def render(data, basket, template_path=TEMPLATE):
    h = open(template_path, encoding="utf-8").read()
    title = html.escape(basket["title"])
    title = re.sub(r"\*(.+?)\*", r"<em>\1</em>", title)
    segs = [g for g in basket["segments"] if any(r["g"] == g for r in data["stocks"])]
    seglist = ", ".join(s.lower() if s != "Frontier AI" else "frontier AI" for s in segs[:-1]) + " and " + (segs[-1].lower() if segs[-1] != "Frontier AI" else "frontier AI")
    seglist = seglist.replace("cloud/neocloud", "cloud").replace("networking/optical", "networking")
    notes = []
    for r in data["stocks"]:
        if r.get("ipo"):
            notes.append(f"{html.escape(r['n'])} ({r['t']}) listed on {dt.date.fromisoformat(r['d']).strftime('%-d %b %Y')}"
                         + (f" after pricing its IPO at {r['cur']}{r['ipoPrice']:g}" if r.get("ipoPrice") else "")
                         + ", so its peak is recent.")
    for code, sign in CURRENCY.items():
        names = [r["n"] for r in data["stocks"] if r["cur"] == sign and code != "USD"]
        if names:
            notes.append(f"{', '.join(names)} {'is' if len(names) == 1 else 'are'} in {'Korean won' if code == 'KRW' else code}.")
    if any(r.get("stale") for r in data["stocks"]):
        notes.append("Stocks marked stale show their last available close.")
    foreign = [r["n"] for r in data["stocks"] if r["cur"] != "$"]
    basket_note = (f"The basket is KKReckons' selection of {len(data['stocks'])} large AI-linked stocks"
                   + (f", all US-listed except {', '.join(foreign)}." if foreign else ", all US-listed."))
    as_of = dt.date.fromisoformat(data["asOf"])
    fill = {"__DATA__": json.dumps(data["stocks"], separators=(",", ":"), ensure_ascii=False),
            "__SEGMENTS__": json.dumps(segs), "__TITLE__": title, "__COUNT__": str(len(data["stocks"])),
            "__SEGLIST__": seglist, "__NOTES__": " ".join(notes), "__BASKET__": basket_note,
            "__ASOF__": as_of.strftime("%-d %b %Y"), "__DAY__": as_of.strftime("%-d %b")}
    for k, v in fill.items():
        h = h.replace(k, v)
    left = set(re.findall(r"__[A-Z]+__", h)) - {"__PLACEHOLDER__"}
    if left:
        raise RuntimeError("template placeholders not filled: " + ", ".join(sorted(left)))
    return h


def main():
    basket = json.load(open(BASKET, encoding="utf-8"))
    if "--check" in sys.argv:
        errs = check(json.load(open(OUT_JSON, encoding="utf-8")), basket)
        print("\n".join(errs) if errs else "drawdowns.json: ok")
        sys.exit(1 if errs else 0)
    data = build(basket, previous=previous_rows())
    errs = check(data, basket)
    if errs:
        print("drawdowns: not published, keeping the previous page: " + "; ".join(errs))
        sys.exit(0)
    page = render(data, basket)
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(page)
    s = data["summary"]
    print(f"drawdowns: {s['count']} stocks as of {data['asOf']}, median {s['medianDrawdown']:+.1%}, "
          f"{s['down50']} down 50%+" + (f"; {'; '.join(data['notes'])}" if data["notes"] else ""))


if __name__ == "__main__":
    main()
