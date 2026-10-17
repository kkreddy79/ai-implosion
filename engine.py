"""AI Implosion engine: fetch market data and compute the stress score and alert levels.

Same rules as the Excel workbook (see update_dashboard.py); this version writes the
JSON (docs/data.json) that powers the GitHub Pages dashboard.

Rules
- DD (price) factors: x = dividend-adjusted close / highest close over the last `lookbackDays` (2 years) - 1.
- LVL (spread) factors, in bp: stress = max(level test, widening test)
    level   = clamp((spread - calm) / (trigger - calm))
    widening = clamp((spread - lowest spread over `widenDays` - wcalm) / (wtrig - wcalm))

- Blank days carry the last published value forward (prices and spreads).
- Score = round(weighted stress x 100). History before a full lookback window exists is warm-up.

Alert levels (L1-L4) are independent tripwires, not a ladder.
- Start: the level's conditions hold for 5 straight trading days (the start date is the first of those days).
- Exit (v1.4): an alert stays active while its conditions hold, unless the Nasdaq-100 has rebounded
  RECOVERY (20%) from its lowest close since that alert started. At the start the rebound is 0%, so this
  rule can never delay or move a start date (including the implosion date = first L4 start).
- Shock flag (v1.4, not a level): score >= SHOCK_SCORE (60) on a day when no level is active.
"""
import bisect, csv, datetime as dt, io, json, math, os, random, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FRED_CACHE = os.path.join(HERE, "data", "fred.json")
LEVEL_NAMES = ["Warning", "Financing Stress", "Bubble Unwind", "AI Implosion"]
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36", "Accept": "text/csv,application/json,text/plain,*/*"}
FETCH_NOTES = []
STALE_NOTES = []
RECOVERY = 0.20      # exit rule: Nasdaq-100 rebound off its low since the alert started
SHOCK_SCORE = 60     # shock flag: score at or above this with no level active
SENS_PCT = 0.20      # threshold sensitivity shown next to the live score


def load_config():
    with open(os.path.join(HERE, "config.json")) as f:
        return json.load(f)


def keep_days(cfg):
    """History kept: one full lookback window of warm-up plus a year of scored days."""
    return cfg["lookbackDays"] + 365


# ---------------------------------------------------------------- data
def _get(url, tries=4, timeout=60):
    last = None
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=BROWSER), timeout=timeout).read().decode()
        except Exception as e:  # rate limits / transient errors
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"Could not fetch {url.split('api_key=')[0]}: {last}")


def fetch_yahoo(symbol, rng="5y"):
    """Daily dividend-adjusted closes (falls back to raw close when no adjustment is published)."""
    sym = urllib.parse.quote(symbol)
    if rng == "max":  # range=max silently returns monthly bars; explicit dates keep them daily
        span = f"period1={int(dt.datetime(1985, 1, 1, tzinfo=dt.timezone.utc).timestamp())}&period2={int(time.time())}"
    else:
        span = f"range={rng}"
    txt = None
    for host in ("query1", "query2"):
        try:
            txt = _get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?{span}&interval=1d&events=div%2Csplit")
            break
        except RuntimeError:
            continue
    if txt is None:
        raise RuntimeError(f"Yahoo Finance unavailable for {symbol}")
    j = json.loads(txt)["chart"]["result"][0]
    ind = j["indicators"]
    close = ind["quote"][0]["close"]
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or close
    out, ratio = {}, 1.0
    for t, a, c in zip(j["timestamp"], adj, close):
        if a is not None and c:
            ratio = a / c
        elif a is None and c is not None:
            a = c * ratio  # newest bar often has no adjusted close yet: scale the raw close
        out[dt.datetime.fromtimestamp(t, dt.timezone.utc).date()] = round(a, 4) if a is not None else None
    return out


def fetch_fred(series_ids, start, cache_path=FRED_CACHE):
    """FRED series in bp (x100). Uses the FRED API when FRED_API_KEY is set (GitHub blocks the public CSV),
    merged into a cached history so an outage only means the newest days carry the last value."""
    try:
        with open(cache_path) as f:
            cache = {s: {dt.date.fromisoformat(k): v for k, v in d.items()} for s, d in json.load(f).items()}
    except (OSError, ValueError, TypeError):
        cache = {}
    key = os.environ.get("FRED_API_KEY", "").strip()
    for sid in series_ids:
        fresh, err = {}, None
        try:
            if key:
                j = json.loads(_get("https://api.stlouisfed.org/fred/series/observations?"
                                    f"series_id={sid}&file_type=json&observation_start={start}&api_key={key}", tries=3))
                rows = [(o["date"], o["value"]) for o in j["observations"]]
            else:
                rows = list(csv.reader(io.StringIO(_get(
                    f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}", tries=2, timeout=90))))
            for d_, v in rows:
                try:
                    fresh[dt.date.fromisoformat(d_)] = round(float(v) * 100)
                except (ValueError, TypeError):
                    pass  # header row or "." for missing days
        except Exception as e:
            err = e
        old = cache.get(sid, {})
        if not fresh and not old:
            FETCH_NOTES.append(f"{sid} unavailable from FRED (check the FRED_API_KEY secret); counted as 0 stress.")
            print(f"WARNING: {sid} unavailable ({err})")
        elif err or not fresh:
            FETCH_NOTES.append(f"FRED {sid} unavailable this run; carried from {max(old)}.")
        old.update(fresh)
        cache[sid] = old
    if not cache_path:
        return cache
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump({s: {k.isoformat(): d[k] for k in sorted(d) if k >= start} for s, d in cache.items()}, f, indent=0)
    return cache


def fetch_live(cfg):
    F = cfg["factors"]
    px = {f["col"]: fetch_yahoo(f["yahoo"]) for f in F if "yahoo" in f}
    master = F[0]["col"]
    cutoff = max(px[master]) - dt.timedelta(days=keep_days(cfg))
    fred = fetch_fred([f["fred"] for f in F if "fred" in f], cutoff - dt.timedelta(days=10))
    for f in F:
        if "fred" in f:
            px[f["col"]] = fred.get(f["fred"], {})
    dates = sorted(d for d, v in px[master].items() if d >= cutoff and v is not None)
    return dates, {f["col"]: [px[f["col"]].get(d) for d in dates] for f in F}


def sample_data(cfg, end=dt.date(2026, 10, 15)):
    """Illustrative random walks with a late selloff. NOT real market data."""
    rnd = random.Random(7)
    start = end - dt.timedelta(days=keep_days(cfg))
    dates, d = [], start
    while d <= end:
        if d.weekday() < 5:
            dates.append(d)
        d += dt.timedelta(days=1)
    n, SH = len(dates), 40
    price = {"NDX": (20200, .012, .0005, -.12), "SOX": (5100, .019, .0006, -.22), "AIQ": (37.5, .013, .0005, -.16),
             "DTCR": (17, .012, .0004, -.15), "BIZD": (16.9, .007, .0004, -.10), "BKLN": (21.1, .0018, .0003, -.03)}
    spread = {"HY": (310, 300, 125, 6), "BBB": (110, 105, 45, 2), "CCC": (800, 750, 500, 12)}
    series = {}
    for c, (v, vol, mu0, shock) in price.items():
        out = []
        for i in range(n):
            mu = math.log(1 + shock) / SH if i >= n - SH else mu0
            v *= math.exp(mu + rnd.gauss(0, vol))
            out.append(round(v, 2))
        series[c] = out
    for c, (v, base, jump, vol) in spread.items():
        out = []
        for i in range(n):
            tgt = base if i < n - SH else base + jump * (i - (n - SH)) / SH
            v += 0.15 * (tgt - v) + rnd.gauss(0, vol)
            out.append(None if rnd.random() < 0.03 else round(v))
        series[c] = out
    return dates, {f["col"]: series[f["col"]] for f in cfg["factors"]}


def get_data(cfg, sample=False):
    return sample_data(cfg) if sample else fetch_live(cfg)


# ---------------------------------------------------------------- rules
def _xround(v):  # Excel ROUND (half away from zero) for non-negative values
    return int(math.floor(v + 0.5))


def _clamp(x, calm, trig):
    return min(1.0, max(0.0, (x - calm) / (trig - calm)))


def carried(vals):
    out, last = [], None
    for v in vals:
        last = v if v is not None else last
        out.append(last)
    return out


class Ctx:
    """Shared look-ups for scoring: carried values and fast trailing-window min/max."""
    def __init__(self, dates, series, cfg):
        self.dates, self.series, self.cfg = dates, series, cfg
        self.eff = {f["col"]: carried(series[f["col"]]) for f in cfg["factors"]}  # blank days carry the last value

    def window(self, c, i, days, fn):
        j = bisect.bisect_left(self.dates, self.dates[i] - dt.timedelta(days=days))
        vals = [v for v in self.series[c][j:i + 1] if v is not None]
        return fn(vals) if vals else None

    def lvl_parts(self, f, i):
        x = self.eff[f["col"]][i]
        if x is None:
            return None, None, 0.0, 0.0
        low = self.window(f["col"], i, self.cfg["widenDays"], min)
        wid = x - low if low is not None else 0
        return x, low, _clamp(x, f["calm"], f["trigger"]), _clamp(wid, f["wcalm"], f["wtrig"])


def score_rows(dates, series, cfg, missing="zero"):
    """Daily stress, pillars, score and level flags. missing="zero": an indicator with no data counts as 0 stress
    (live dashboard). missing="exclude": it is left out and the weights re-normalised (backtests before an ETF existed)."""
    F = cfg["factors"]
    LB = cfg["lookbackDays"]
    ctx = Ctx(dates, series, cfg)
    n, d0 = len(dates), dates[0]
    rows = []
    for i, d in enumerate(dates):
        warm = (d - d0).days < LB - 1
        s = {}
        for f in F:
            c = f["col"]
            v = ctx.eff[c][i]
            if warm:
                s[c] = None
            elif v is None:
                s[c] = None if missing == "exclude" else 0.0
            elif f["type"] == "LVL":
                _, _, a, b = ctx.lvl_parts(f, i)
                s[c] = max(a, b)
            else:
                hi = ctx.window(c, i, LB, max)
                if hi is None:  # only a stale carried value, no price inside the lookback window
                    s[c] = None if missing == "exclude" else 0.0
                else:
                    s[c] = _clamp(v / hi - 1, f["calm"], f["trigger"])

        def wavg(fs):
            fs = [f for f in fs if s[f["col"]] is not None]
            return sum(f["weight"] * s[f["col"]] for f in fs) / sum(f["weight"] for f in fs) * 100 if fs else None

        if warm:
            eq = fin = cr = score = None
            L = [0, 0, 0, 0]
        else:
            eq, fin, cr = (wavg([f for f in F if f["pillar"] == p]) for p in ("Equity", "Financing", "Credit"))
            tot = wavg(F)
            score = _xround(tot) if tot is not None else None
            g = lambda c: s.get(c) or 0.0
            hy = ctx.eff["HY"][i] or 0
            sc = score or 0
            L = [int(sc >= 50),
                 int(sc >= 60 and ((g("BIZD") >= 0.8 and g("BKLN") >= 0.5) or g("CCC") >= 0.8)),
                 int(sc >= 70 and g("SOX") >= 0.83 and g("AIQ") >= 0.8),
                 int(sc >= 75 and g("NDX") == 1 and (hy >= 500 or g("BIZD") == 1))]
        rows.append({"date": d, "s": s, "eq": eq, "fin": fin, "cr": cr, "score": score, "L": L})
    for i in range(n):  # 5-straight-trading-day starts
        win = rows[i:i + 5]
        rows[i]["LS"] = [int(len(win) == 5 and all(r["L"][k] for r in win)) for k in range(4)]
    return rows, ctx


def confirm_levels(rows, ndx):
    """Confirmed streaks, active days and shock days, shared by the live page and the backtest.

    conf[k][i]   day i is inside a confirmed streak of level k+1 (5-day start, runs while conditions hold)
    active[k][i] conf, and the Nasdaq-100 has not rebounded RECOVERY from its low since the streak started
    starts[k]    first day of each streak (unchanged by the exit rule)
    shock[i]     score >= SHOCK_SCORE with no level active
    ndx          Nasdaq-100 closes with blank days carried forward
    """
    n = len(rows)
    conf = [[0] * n for _ in range(4)]
    active = [[0] * n for _ in range(4)]
    starts = [[] for _ in range(4)]
    for k in range(4):
        low = None
        for i in range(n):
            on = int(rows[i]["L"][k] and (rows[i]["LS"][k] or (i > 0 and conf[k][i - 1])))
            conf[k][i] = on
            if not on:
                low = None
                continue
            if i == 0 or not conf[k][i - 1]:
                starts[k].append(i)
                low = ndx[i]
            elif ndx[i] is not None:
                low = ndx[i] if low is None else min(low, ndx[i])
            rebound = ndx[i] / low - 1 if ndx[i] and low else 0.0
            active[k][i] = int(round(rebound, 9) < RECOVERY)  # rounding: exactly +20% pauses, as in Excel
    shock = [int((rows[i]["score"] or 0) >= SHOCK_SCORE and not any(active[k][i] for k in range(4))) for i in range(n)]
    return conf, active, starts, shock


def scaled_cfg(cfg, k):
    """Every calm/trigger threshold moved by factor k (k < 1 = tighter: alarms at smaller moves)."""
    c2 = json.loads(json.dumps(cfg))
    for f in c2["factors"]:
        if f["type"] == "DD":
            f["calm"] *= k; f["trigger"] *= k
        else:  # spread levels move half as much; widening steps move fully
            f["calm"] *= (1 + (k - 1) / 2); f["trigger"] *= (1 + (k - 1) / 2)
            f["wcalm"] *= k; f["wtrig"] *= k
    return c2


def weekdays_between(a, b):
    """Weekdays after date a up to and including date b."""
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        n += d.weekday() < 5
    return n


def compute(dates, series, cfg):
    STALE_NOTES.clear()
    F = cfg["factors"]
    LB, WD = cfg["lookbackDays"], cfg["widenDays"]
    n = len(dates)
    rows, ctx = score_rows(dates, series, cfg)
    window, lvl_parts = ctx.window, ctx.lvl_parts


    latest, maxd = rows[-1], dates[-1]
    conf, act, lv_starts, shock = confirm_levels(rows, ctx.eff["NDX"])
    levels = []
    for k in range(4):
        starts = [r["date"] for r in rows if r["LS"][k]]
        on = bool(n >= 5 and conf[k][-1])
        levels.append({"level": f"L{k+1}", "name": LEVEL_NAMES[k], "firstDate": starts[0].isoformat() if starts else None,
                       "active": bool(on and act[k][-1]),
                       # conditions still met, but the Nasdaq-100 has rebounded RECOVERY from its low since the start
                       "paused": bool(on and not act[k][-1]),
                       "streakStart": rows[lv_starts[k][-1]]["date"].isoformat() if on and lv_starts[k] else None})
    shock_days = [rows[i]["date"] for i in range(n) if shock[i] and rows[i]["date"] > maxd - dt.timedelta(days=364)]
    current = next((f"{lv['level']} {lv['name']}" for lv in reversed(levels) if lv["active"]), "None")
    implosion = levels[3]["firstDate"]

    def status(st):
        return "TRIGGERED" if st == 1 else "WARNING" if st >= 0.5 else "WATCH" if st > 0 else "CALM"

    def hy_band(bp):
        for lim, lab in [(300, "Very Healthy"), (400, "Normal"), (500, "Watch"), (600, "Warning"), (800, "Severe Stress")]:
            if bp < lim:
                return lab
        return "Crisis"

    factors = []
    for f in F:
        c = f["col"]
        base = {"name": f["name"], "col": c, "pillar": f["pillar"], "weight": f["weight"], "type": f["type"],
                "source": f["source"], "calm": f["calm"], "trigger": f["trigger"],
                "wcalm": f.get("wcalm"), "wtrig": f.get("wtrig")}
        have = [i for i in range(n) if series[c][i] is not None]
        if have:
            last = dates[max(have)]
            lag = weekdays_between(last, dates[-1])
            base["lastDate"], base["stale"] = last.isoformat(), lag > 3  # FRED normally lags 1 day
            if base["stale"]:
                STALE_NOTES.append(f"{f['name']} has not updated since {last.isoformat()} ({lag} trading days); its last value is carried forward.")
        if not have:
            factors.append({**base, "ref": None, "current": None, "move": None, "triggerPrice": None, "band": None,
                            "levelStress": 0.0, "widenStress": 0.0, "stress": 0.0, "status": "NO DATA"})
            continue
        if f["type"] == "DD":
            i = max(have)
            cur, ref = series[c][i], window(c, n - 1, LB, max)
            st = _clamp(cur / ref - 1, f["calm"], f["trigger"])
            factors.append({**base, "ref": ref, "current": cur, "move": cur / ref - 1, "triggerPrice": ref * (1 + f["trigger"]),
                            "band": None, "levelStress": st, "widenStress": None, "stress": st, "status": status(st)})
        else:
            cur, low, a, b = lvl_parts(f, n - 1)
            st = max(a, b)
            factors.append({**base, "ref": low, "current": cur, "move": cur - low, "triggerPrice": None,
                            "band": hy_band(cur) if c == "HY" else None, "levelStress": a, "widenStress": b,
                            "stress": st, "status": status(st)})
    trig_count = sum(1 for f in factors if f["status"] == "TRIGGERED")

    # contribution to the headline score, and what it would take to add 10 points
    wsum = sum(f["weight"] for f in F)
    for f, x in zip(F, factors):
        wn = f["weight"] / wsum
        x["rationale"] = f.get("rationale", "")
        x["descriptor"] = f.get("descriptor", "")
        x["points"] = wn * x["stress"] * 100
        x["maxPoints"] = wn * 100
        need = x["stress"] + 10 / (wn * 100)
        sens = None
        if x["current"] is not None and need <= 1:
            if f["type"] == "DD":
                dd = f["calm"] + need * (f["trigger"] - f["calm"])
                sens = {"kind": "price", "drawdown": dd, "price": x["ref"] * (1 + dd)}
            else:
                lvl = f["calm"] + need * (f["trigger"] - f["calm"])
                wid = (x["ref"] or 0) + f["wcalm"] + need * (f["wtrig"] - f["wcalm"])
                sens = {"kind": "spread", "bp": min(lvl, wid), "via": "widening" if wid < lvl else "level"}
        x["plus10"] = sens

    # previous session and per-gauge history, for "what changed" and the gauge drill-downs
    prev = rows[-2] if n >= 2 else None
    week_end = {}
    for i, r in enumerate(rows):
        if r["date"] > maxd - dt.timedelta(days=364):
            week_end[r["date"].isocalendar()[:2]] = i  # last trading day of each ISO week
    spark_idx = [week_end[k] for k in sorted(week_end)]
    for f, x in zip(F, factors):
        c = f["col"]
        x["prevStress"] = prev["s"].get(c) if prev else None
        x["spark"] = [{"date": rows[i]["date"].isoformat(), "stress": rows[i]["s"].get(c)} for i in spark_idx]

    level_on = any(lv["active"] for lv in levels)
    concentrated = [{"name": x["name"], "col": x["col"], "stress": x["stress"]} for x in factors
                    if x["stress"] >= 0.75 and not level_on]

    score = latest["score"]
    if implosion:
        band = "IMPLOSION CONFIRMED"
    elif score is None:
        band = "WARM-UP"
    else:
        band = "NORMAL" if score < 25 else "ELEVATED" if score < 50 else "SEVERE" if score < 75 else "CRITICAL"

    history = [{"date": r["date"].isoformat(), "equity": r["eq"], "financing": r["fin"], "credit": r["cr"], "score": r["score"]}
               for r in rows if r["date"] > maxd - dt.timedelta(days=364)]
    weekly = []
    fri = maxd - dt.timedelta(days=maxd.weekday()) + dt.timedelta(days=4)
    for w in range(26):
        end = fri - dt.timedelta(days=7 * w)
        sc = [r["score"] for r in rows if end - dt.timedelta(days=6) <= r["date"] <= end and r["score"] is not None]
        weekly.append({"weekEnding": end.isoformat(), "high": max(sc) if sc else None})
    weekly.reverse()

    return {"asOf": maxd.isoformat(), "score": score, "band": band, "triggers": f"{trig_count} of {len(F)}",
            "factorCount": len(F), "lookbackDays": LB, "widenDays": WD,
            "prevDate": prev["date"].isoformat() if prev else None, "prevScore": prev["score"] if prev else None,
            "pillars": {"Equity": latest["eq"], "Financing": latest["fin"], "Credit": latest["cr"]},
            "prevPillars": {"Equity": prev["eq"], "Financing": prev["fin"], "Credit": prev["cr"]} if prev else None,
            "currentLevel": current, "implosionDate": implosion, "levels": levels, "concentrated": concentrated,
            "shock": {"active": bool(shock[-1]), "threshold": SHOCK_SCORE,
                      "lastDate": shock_days[-1].isoformat() if shock_days else None, "daysLastYear": len(shock_days)},
            "exitRule": {"rebound": RECOVERY},
            "factors": factors, "history": history, "weekly": weekly}


def write_json(dates, series, cfg, path, sample=False):
    out = compute(dates, series, cfg)
    # how much today's score depends on the threshold choices (every threshold 20% tighter / looser)
    rng = {}
    for key, k in (("tight", 1 - SENS_PCT), ("loose", 1 + SENS_PCT)):
        r, _ = score_rows(dates, series, scaled_cfg(cfg, k))
        rng[key] = r[-1]["score"]
    out["scoreRange"] = {**rng, "pct": SENS_PCT}
    out["sample"] = sample
    out["editorNote"] = cfg.get("editorNote", "")
    out["model"] = cfg.get("model")
    out["notes"] = FETCH_NOTES + STALE_NOTES
    out["generatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    return out
