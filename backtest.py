"""Historical backtest of the AI Implosion rules (by KK Reddy).

Replays the exact live rules (engine.score_rows, config.json) on daily data since 1997 and writes
docs/backtest.json, which docs/backtest.html renders.

Indicators without full history (ETFs that did not exist yet, and ICE spreads, which FRED only
publishes for ~3 years) are back-filled with a labelled stand-in, scaled to join the real series on its first day. Before any stand-in exists, that indicator is left out and the weights are
re-normalised over the indicators that do have data.

Usage:  FRED_API_KEY=... python backtest.py        (real data; runs in GitHub Actions)
        python backtest.py --sample                 (synthetic data, for testing the code)
"""
import datetime as dt, json, math, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine

START = dt.date(1997, 1, 1)
OUT = os.path.join(HERE, "docs", "backtest.json")

# indicator -> stand-ins used (in order) before the real series begins
PROXIES = {
    "AIQ": [("QQQ", "Nasdaq-100 ETF (QQQ)"), ("^NDX", "Nasdaq-100 index")],
    "DTCR": [("IYR", "US real-estate ETF (IYR)")],
    "BIZD": [("ARCC", "Ares Capital, the largest BDC (ARCC)")],
    "BKLN": [("VVR", "Invesco Senior Income Trust, a loan fund (VVR)")],
}

# ICE BofA spreads on FRED only go back ~3 years (licensing), so older history uses Moody's Baa
# corporate yield minus the 10-year Treasury, scaled to join each ICE series where it begins.
SPREAD_PROXY = ("BAA10Y", "Moody's Baa corporate spread over 10-year Treasury (BAA10Y)")
STABILITY_DAYS = 30   # re-run as if FRED's 3-year window began this many days later; report alert dates that move
# Fixed stand-in scales (ICE spread / Moody's Baa spread): median ratio over the ICE history FRED served on
# 2026-10-17 (2023-10-17 to 2026-10-16, 748 days). Frozen so the replay doesn't drift as FRED's window rolls.
# That overlap was a calm credit period; in crises high-yield and CCC spreads widen proportionally more than Baa,
# so the stand-in likely UNDERSTATES spread stress before late 2023.
SPREAD_SCALE = {"BBB": 0.689, "CCC": 5.383, "HY": 1.861}
SPREAD_CALIBRATION = "median ratio, 17 Oct 2023 – 16 Oct 2026"

# Candidate lower-redundancy weights, tested side by side with the live ones. AIQ tracks the Nasdaq (0.97) and
# BBB tracks HY (0.86), so they give weight to the indicators that add independent information.
ALT_WEIGHTS = {"NDX": 0.16, "SOX": 0.16, "AIQ": 0.04, "DTCR": 0.08, "BBB": 0.05, "CCC": 0.09,
               "BIZD": 0.17, "BKLN": 0.13, "HY": 0.12}

# Market episodes to score. Windows run from before the Nasdaq peak to after the trough.
EPISODES = [
    ("Dot-com bust", "2000-01-01", "2002-12-31", "Tech bubble — the closest historical analogue to an AI implosion"),
    ("Global financial crisis", "2007-07-01", "2009-06-30", "Credit and banking crisis"),
    ("2011 debt-ceiling & euro scare", "2011-07-01", "2011-12-31", "Sovereign-debt shock"),
    ("2015–16 oil & credit scare", "2015-07-01", "2016-03-31", "Energy-led high-yield sell-off"),
    ("Q4 2018 sell-off", "2018-09-01", "2019-01-31", "Rate-hike and trade-war correction"),
    ("COVID crash", "2020-02-01", "2020-06-30", "Pandemic shock, not AI-related"),
    ("2022 rate-shock bear market", "2021-11-01", "2022-12-31", "Inflation and rate hikes; tech and semis hit hardest"),
    ("2025 tariff shock", "2025-02-01", "2025-06-30", "Tariff announcement sell-off"),
]


# ---------------------------------------------------------------- data
def splice(real, chain):
    """Back-fill `real` (date->value) with stand-ins, each scaled to join the series after it."""
    out = {d: v for d, v in real.items() if v is not None}
    used = []
    for sym, label, prox in chain:
        if not out:
            out = {d: v for d, v in prox.items() if v is not None}
            used.append({"symbol": sym, "label": label, "from": min(out).isoformat() if out else None,
                         "until": None})
            continue
        first = min(out)
        before = [d for d in prox if d <= first and prox[d]]
        if not before:
            continue
        anchor = max(before)
        k = out[first] / prox[anchor]
        added = {d: v * k for d, v in prox.items() if d < first and v is not None}
        if added:
            used.append({"symbol": sym, "label": label, "from": min(added).isoformat(), "until": first.isoformat()})
            out.update(added)
    return out, used


def splice_median(real, prox, sym, label, k=None):
    """Back-fill a spread with a stand-in scaled by the MEDIAN ratio over every day both exist.
    FRED serves only the last ~3 years of the ICE spreads, so the first real day moves forward daily; joining on
    that single day (as `splice` does) rescaled the whole pre-2023 history each run. The median over the overlap
    barely moves from one day to the next."""
    out = {d: v for d, v in real.items() if v is not None}
    if not out:
        return {d: v for d, v in prox.items() if v is not None}, [{"symbol": sym, "label": label, "from": min(prox).isoformat() if prox else None, "until": None}]
    first = min(out)
    ratios = sorted(out[d] / prox[d] for d in out if prox.get(d))
    if not ratios:
        return out, []
    live_k = ratios[len(ratios) // 2]
    k = k or live_k
    added = {d: v * k for d, v in prox.items() if d < first and v is not None}
    out.update(added)
    return out, ([{"symbol": sym, "label": label, "from": min(added).isoformat(), "until": first.isoformat(),
                   "scale": round(k, 3), "medianNow": round(live_k, 3), "overlapDays": len(ratios)}] if added else [])


def fetch_history(cfg):
    F = cfg["factors"]
    px, proxies = {}, []
    for f in F:
        if "yahoo" not in f:
            continue
        c = f["col"]
        real = engine.fetch_yahoo(f["yahoo"], rng="max")
        chain = [(s, lab, engine.fetch_yahoo(s, rng="max")) for s, lab in PROXIES.get(c, [])]
        series, used = splice(real, chain)
        px[c] = series
        for u in used:
            proxies.append({"col": c, "name": f["name"], **u})
    ids = [f["fred"] for f in F if "fred" in f] + [SPREAD_PROXY[0]]
    fred = engine.fetch_fred(ids, START - dt.timedelta(days=10), cache_path=None)
    baa = fred.get(SPREAD_PROXY[0], {})
    if not baa:
        raise RuntimeError("Moody's Baa spread (BAA10Y) unavailable; cannot back-fill credit spreads")
    shifted = {}
    for f in F:
        if "fred" in f:
            real = fred.get(f["fred"], {})
            series, used = splice_median(real, baa, SPREAD_PROXY[0], SPREAD_PROXY[1], SPREAD_SCALE.get(f["col"]))
            px[f["col"]] = series
            # stability check: the same splice if FRED's window started STABILITY_DAYS later
            cut = min(real) + dt.timedelta(days=STABILITY_DAYS) if real else None
            shifted[f["col"]], _ = splice_median({d: v for d, v in real.items() if cut and d >= cut}, baa, SPREAD_PROXY[0], SPREAD_PROXY[1], SPREAD_SCALE.get(f["col"]))
            for u in used:
                proxies.append({"col": f["col"], "name": f["name"], **u})
    master = F[0]["col"]
    dates = sorted(d for d, v in px[master].items() if d >= START and v is not None)
    per_year = len(dates) / max(1, (dates[-1] - dates[0]).days / 365.25)
    if per_year < 200:
        raise RuntimeError(f"History is not daily ({per_year:.0f} rows a year); refusing to publish a misleading backtest")
    alt = {f["col"]: [(shifted if f["col"] in shifted else px)[f["col"]].get(d) for d in dates] for f in F}
    return dates, {f["col"]: [px[f["col"]].get(d) for d in dates] for f in F}, proxies, alt


def synthetic(cfg):
    """Random data with crashes in the episode windows, only to exercise the code."""
    rnd = random.Random(3)
    dates, d = [], START
    end = dt.date(2026, 10, 15)
    while d <= end:
        if d.weekday() < 5:
            dates.append(d)
        d += dt.timedelta(days=1)
    crash = lambda day: any(dt.date.fromisoformat(a) <= day <= dt.date.fromisoformat(b) for _, a, b, _ in EPISODES[:2] + EPISODES[6:7])
    series = {}
    for f in cfg["factors"]:
        v, out = (100.0 if f["type"] == "DD" else f["calm"] * 0.8), []
        for day in dates:
            if f["type"] == "DD":
                v *= math.exp((-0.0025 if crash(day) else 0.0004) + rnd.gauss(0, 0.012))
            else:
                tgt = f["trigger"] * 1.1 if crash(day) else f["calm"] * 0.8
                v += 0.03 * (tgt - v) + rnd.gauss(0, f["calm"] * 0.01)
            out.append(round(v, 2))
        series[f["col"]] = out
    series["DTCR"] = [None if day < dt.date(2000, 6, 1) else v for day, v in zip(dates, series["DTCR"])]
    return dates, series, [{"col": "DTCR", "name": "synthetic", "symbol": "TEST", "label": "synthetic stand-in", "from": "2000-06-01", "until": "2020-01-01"}]


# ---------------------------------------------------------------- analysis
def analyse(dates, series, cfg, proxies, sample):
    rows, _ = engine.score_rows(dates, series, cfg, missing="exclude")
    n = len(rows)
    ndx = engine.carried(series["NDX"])
    # conf: inside a 5-day-confirmed streak; act: conf and not in recovery mode (exit rule)
    conf, act, starts, shock = engine.confirm_levels(rows, ndx, cfg["lookbackDays"])
    first_scored = next((r["date"] for r in rows if r["score"] is not None), None)
    first_i = next((i for i, r in enumerate(rows) if r["score"] is not None), n)

    # Nasdaq-100 2-year high (and the day it was set), for drawdowns and "days after the peak"
    hi2, hi2_i, j = [], [], 0
    for i in range(n):
        lo_d = rows[i]["date"] - dt.timedelta(days=730)
        while rows[j]["date"] < lo_d:
            j += 1
        cand = [x for x in range(j, i + 1) if ndx[x]]
        b = max(cand, key=lambda x: ndx[x]) if cand else None
        hi2.append(ndx[b] if b is not None else None); hi2_i.append(b)

    def dd_at(i):
        return ndx[i] / hi2[i] - 1 if ndx[i] and hi2[i] else 0.0

    def in_window(i, a, b):
        return a <= rows[i]["date"] <= b

    episodes = []
    for name, a, b, desc in EPISODES:
        a, b = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
        idx = [i for i in range(n) if a <= rows[i]["date"] <= b]
        if not idx or first_scored is None or rows[idx[-1]]["date"] < first_scored:
            continue
        pk = max(idx, key=lambda i: ndx[i] or 0)
        after = [i for i in idx if i >= pk]
        tr = min(after, key=lambda i: ndx[i] or float("inf"))
        # deepest Nasdaq decline inside the window, measured from its prior peak
        best, run_pk = (0, pk, tr), idx[0]
        for i in idx:
            if (ndx[i] or 0) > (ndx[run_pk] or 0):
                run_pk = i
            ddn = (ndx[i] or 0) / (ndx[run_pk] or 1) - 1
            if ddn < best[0]:
                best = (ddn, run_pk, i)
        dd, pk, tr = best
        scored = [i for i in idx if rows[i]["score"] is not None]
        ms = max(scored, key=lambda i: rows[i]["score"]) if scored else None
        lv = []
        for k in range(4):
            hit = [i for i in starts[k] if in_window(i, a, b)]
            if not hit:
                # a streak may have started before the window and still be running
                running = [i for i in idx if conf[k][i]]
                hit = running[:1]
            i0 = hit[0] if hit else None
            lv.append({"level": f"L{k+1}", "date": rows[i0]["date"].isoformat() if i0 is not None else None,
                       "daysFromPeak": (rows[i0]["date"] - rows[pk]["date"]).days if i0 is not None else None,
                       "daysActive": sum(act[k][i] for i in idx), "daysConfirmed": sum(conf[k][i] for i in idx)})
        sh = [i for i in idx if shock[i]]
        episodes.append({"name": name, "description": desc, "from": a.isoformat(), "to": b.isoformat(),
                         "ndxPeak": rows[pk]["date"].isoformat(), "ndxTrough": rows[tr]["date"].isoformat(),
                         "ndxDrawdown": dd, "maxScore": rows[ms]["score"] if ms is not None else None,
                         "maxScoreDate": rows[ms]["date"].isoformat() if ms is not None else None,
                         "shockDate": rows[sh[0]]["date"].isoformat() if sh else None, "shockDays": len(sh),
                         "levels": lv, "proxyUsed": sorted({p["col"] for p in proxies
                                                     if p.get("until") and p["from"] <= b.isoformat() and p["until"] > a.isoformat()})})
        wt = {f["col"]: f["weight"] for f in cfg["factors"]}
        share = sum(wt[c] for c in episodes[-1]["proxyUsed"]) / sum(wt.values())
        episodes[-1]["standInShare"] = share
        episodes[-1]["dataQuality"] = "Real data" if share == 0 else "Partly stand-in" if share <= 0.35 else "Mostly stand-in"

    windows = [(dt.date.fromisoformat(a), dt.date.fromisoformat(b)) for _, a, b, _ in EPISODES]
    outside = []
    for k in range(4):
        for i in starts[k]:
            d = rows[i]["date"]
            if not any(a <= d <= b for a, b in windows):
                j = i
                while j + 1 < n and conf[k][j + 1]:
                    j += 1
                live = sum(act[k][i:j + 1])
                if live:  # a streak paused by recovery mode for its whole run never showed as an alert
                    outside.append({"level": f"L{k+1}", "date": d.isoformat(), "days": j - i + 1, "activeDays": live, "score": rows[i]["score"]})
    outside.sort(key=lambda x: x["date"])

    # weekly timeline (Friday week-ending)
    weekly = {}
    for i, r in enumerate(rows):
        if r["score"] is None:
            continue
        wk = r["date"] + dt.timedelta(days=4 - r["date"].weekday())
        lvl = max([k + 1 for k in range(4) if act[k][i]], default=0)
        w = weekly.setdefault(wk, {"date": wk.isoformat(), "score": 0, "level": 0})
        w["score"] = max(w["score"], r["score"]); w["level"] = max(w["level"], lvl)
    timeline = [weekly[k] for k in sorted(weekly)]

    # how much the indicators move together (daily stress, scored days)
    cols = [f["col"] for f in cfg["factors"]]

    # only compare two indicators over days when both are real data (stand-ins would inflate correlation)
    real_from = {}
    for p in proxies:
        if p.get("until"):
            real_from[p["col"]] = max(real_from.get(p["col"], ""), p["until"])

    def corr(a, b):
        since = max(real_from.get(a, ""), real_from.get(b, ""))
        pairs = [(r["s"][a], r["s"][b]) for r in rows if r["s"].get(a) is not None and r["s"].get(b) is not None
                 and r["date"].isoformat() >= since]
        if len(pairs) < 30:
            return None
        xa, xb = [p[0] for p in pairs], [p[1] for p in pairs]
        ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
        va = sum((x - ma) ** 2 for x in xa); vb = sum((x - mb) ** 2 for x in xb)
        if va == 0 or vb == 0:
            return None
        return sum((x - ma) * (y - mb) for x, y in pairs) / math.sqrt(va * vb)

    correlation = {"cols": cols, "realFrom": real_from, "matrix": [[None if a == b else (round(corr(a, b), 2) if corr(a, b) is not None else None)
                                            for b in cols] for a in cols]}
    pillars = ["eq", "fin", "cr"]
    pc = {}
    for x in range(3):
        for y in range(x + 1, 3):
            pairs = [(r[pillars[x]], r[pillars[y]]) for r in rows if r[pillars[x]] is not None and r[pillars[y]] is not None]
            if len(pairs) > 30:
                xa, xb = [p[0] for p in pairs], [p[1] for p in pairs]
                ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
                va = sum((v - ma) ** 2 for v in xa); vb = sum((v - mb) ** 2 for v in xb)
                pc[f"{pillars[x]}-{pillars[y]}"] = round(sum((p - ma) * (q - mb) for p, q in pairs) / math.sqrt(va * vb), 2) if va and vb else None

    # ---- outcome check: was each alert followed by a real Nasdaq-100 fall?
    HORIZON, FALL = 126, -0.10   # about 6 months of trading days; a 10% fall
    windows_d = [(dt.date.fromisoformat(a), dt.date.fromisoformat(b)) for _, a, b, _ in EPISODES]
    outcomes = []
    for k in range(4):
        for i in starts[k]:
            d0, p0 = rows[i]["date"], ndx[i]
            fwd = [ndx[j] for j in range(i, min(n, i + HORIZON + 1)) if ndx[j]]
            worst = min(fwd) / p0 - 1 if fwd and p0 else 0.0
            in_ep = any(a <= d0 <= b for a, b in windows_d)
            kind = "during" if in_ep else ("followed" if worst <= FALL else "false")
            outcomes.append({"level": f"L{k+1}", "date": d0.isoformat(), "fwdWorst": worst, "kind": kind,
                             "complete": i + HORIZON < n})
    # ---- missed declines: Nasdaq-100 bear markets (20%+ below its 2-year high) with no alert
    spans, cur = [], None
    for i in range(first_i, n):
        dd = ndx[i] / hi2[i] - 1 if ndx[i] and hi2[i] else 0
        if dd <= -0.20:
            if cur and (rows[i]["date"] - rows[cur[1]]["date"]).days <= 60:
                cur[1] = i
            else:
                cur = [i, i]; spans.append(cur)
    declines = []
    for a, b in spans:
        tr = min(range(a, b + 1), key=lambda i: ndx[i] / hi2[i])
        look = [i for i in range(max(first_i, a - 60), b + 1) if conf[0][i]]
        sh = [i for i in range(max(first_i, a - 60), b + 1) if shock[i]]
        declines.append({"from": rows[a]["date"].isoformat(), "to": rows[b]["date"].isoformat(),
                         "worst": ndx[tr] / hi2[tr] - 1, "alerted": bool(look),
                         "firstAlert": rows[look[0]]["date"].isoformat() if look else None,
                         "firstShock": rows[sh[0]]["date"].isoformat() if sh else None})

    # ---- alert episodes: one uniform test for every alert, wherever it falls in time.
    # Days with any level active are merged into one alert episode when gaps are <= MERGE trading days,
    # so a cluster of back-to-back streaks counts once. Each episode is then judged on the Nasdaq-100:
    #   early warning  - a further fall of 10%+ within ~6 months of the alert's first day
    #   confirmation   - no further 10% fall, but it began with the Nasdaq-100 already 20%+ below its 2-year high
    #   false alarm    - neither
    MERGE, BEAR = 20, -0.20

    def spans_of(flag):
        out, cur = [], None
        for i in range(first_i, n):
            if not flag(i):
                continue
            if cur and i - cur[1] <= MERGE + 1:
                cur[1] = i
            else:
                cur = [i, i]; out.append(cur)
        return out

    def judge(a, b, extra):
        p0 = ndx[a]
        fwd = [ndx[j] for j in range(a, min(n, a + HORIZON + 1)) if ndx[j]]
        worst = min(fwd) / p0 - 1 if fwd and p0 else 0.0
        dd0 = dd_at(a)
        verdict = "early" if worst <= FALL else "confirm" if dd0 <= BEAR else "false"
        pk = hi2_i[a]
        return {"start": rows[a]["date"].isoformat(), "end": rows[b]["date"].isoformat(),
                "tradingDays": b - a + 1, "ndxFromHighAtStart": dd0,
                "ndxPeak": rows[pk]["date"].isoformat() if pk is not None else None,
                "daysFromPeak": (rows[a]["date"] - rows[pk]["date"]).days if pk is not None else None,
                "fwdWorst": worst, "verdict": verdict, "complete": a + HORIZON < n,
                "inWindow": next((nm for nm, x, y, _ in EPISODES if x <= rows[a]["date"].isoformat() <= y), None), **extra}

    alert_eps = []
    for a, b in spans_of(lambda i: any(act[k][i] for k in range(4))):
        hit = [k for k in range(4) if any(act[k][i] for i in range(a, b + 1))]
        firsts = {f"L{k+1}": rows[next(i for i in range(a, b + 1) if act[k][i])]["date"].isoformat() for k in hit}
        alert_eps.append(judge(a, b, {"maxLevel": max(hit) + 1, "levels": firsts}))
    shock_eps = []
    for a, b in spans_of(lambda i: shock[i]):
        shock_eps.append(judge(a, b, {"maxScore": max(rows[i]["score"] or 0 for i in range(a, b + 1))}))

    # ---- levels are independent tripwires: where did they fire out of order or skip a level?
    ladder = []
    for e in episodes:
        ds = [l["date"] for l in e["levels"]]
        for hi_k in range(1, 4):
            if not ds[hi_k]:
                continue
            for lo_k in range(hi_k):
                if not ds[lo_k]:
                    ladder.append({"episode": e["name"], "kind": "skipped", "fired": f"L{hi_k+1}", "missing": f"L{lo_k+1}"})
                elif ds[lo_k] > ds[hi_k]:
                    ladder.append({"episode": e["name"], "kind": "order", "fired": f"L{hi_k+1}", "before": f"L{lo_k+1}",
                                   "firedDate": ds[hi_k], "laterDate": ds[lo_k]})

    # ---- in-sample vs out-of-sample: rules were shaped on this history up to the freeze date
    frozen = (cfg.get("model") or {}).get("outOfSampleFrom")
    oos = {"from": frozen,
           "alerts": [x for x in alert_eps if frozen and x["start"] >= frozen],
           "shocks": [x for x in shock_eps if frozen and x["start"] >= frozen]}

    # ---- spreads: before the ICE series begin, all spread gauges share one stand-in (Moody's Baa)
    sp = [p for p in proxies if p["symbol"] == SPREAD_PROXY[0] and p.get("until")]
    spread_standin = {"cols": sorted({p["col"] for p in sp}), "label": SPREAD_PROXY[1], "calibration": SPREAD_CALIBRATION,
                      "scales": {p["col"]: p.get("scale") for p in sp}, "mediansNow": {p["col"]: p.get("medianNow") for p in sp},
                      "until": min(p["until"] for p in sp) if sp else None,
                      "weight": sum(f["weight"] for f in cfg["factors"] if f["col"] in {p["col"] for p in sp}) / sum(f["weight"] for f in cfg["factors"])}

    scored = [r for r in rows if r["score"] is not None]
    days_by_level = [sum(act[k]) for k in range(4)]
    return {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "sample": sample, "dataStart": dates[0].isoformat(), "dataEnd": dates[-1].isoformat(),
        "firstScored": first_scored.isoformat() if first_scored else None,
        "scoredDays": len(scored), "daysByLevel": days_by_level,
        "streaksByLevel": [len(s) for s in starts],
        "proxies": proxies, "episodes": episodes, "outsideEpisodes": outside,
        "timeline": timeline, "correlation": correlation, "pillarCorrelation": pc,
        "rules": {"lookbackDays": cfg["lookbackDays"], "widenDays": cfg["widenDays"]},
        "outcomeRule": {"horizonDays": HORIZON, "fall": FALL},
        "alertEpisodes": alert_eps, "shockEpisodes": shock_eps,
        "episodeRule": {"mergeDays": MERGE, "horizonDays": HORIZON, "fall": FALL, "bear": BEAR,
                        "exitRebound": engine.RECOVERY, "shockScore": engine.SHOCK_SCORE},
        "ladderBreaks": ladder, "outOfSample": oos, "spreadStandIn": spread_standin,
        "inSample": True,
        "alertOutcomes": outcomes, "nasdaqDeclines": declines,
        "latestScore": rows[-1]["score"],
    }


def main():
    cfg = engine.load_config()
    sample = "--sample" in sys.argv
    alt_series = None
    if sample:
        dates, series, proxies = synthetic(cfg)
    else:
        dates, series, proxies, alt_series = fetch_history(cfg)
    out = analyse(dates, series, cfg, proxies, sample)
    if alt_series:
        r2 = analyse(dates, alt_series, cfg, proxies, sample)
        moved = [{"episode": e["name"], "level": l["level"], "date": l["date"], "shifted": l2["date"]}
                 for e, e2 in zip(out["episodes"], r2["episodes"]) for l, l2 in zip(e["levels"], e2["levels"]) if l["date"] != l2["date"]]
        out["spliceStability"] = {"days": STABILITY_DAYS, "moved": moved, "latestScore": r2["latestScore"]}
        print(f"splice stability ({STABILITY_DAYS}d): {len(moved)} level dates move", moved)
    alt_cfg = json.loads(json.dumps(cfg))
    for f in alt_cfg["factors"]:
        f["weight"] = ALT_WEIGHTS[f["col"]]
    alt = analyse(dates, series, alt_cfg, proxies, sample)

    def summary(name, c, r):
        return {"name": name, "weights": {f["col"]: f["weight"] for f in c["factors"]},
                "streaksByLevel": r["streaksByLevel"], "daysByLevel": r["daysByLevel"],
                "outsideByLevel": [sum(1 for x in r["outsideEpisodes"] if x["level"] == f"L{k+1}") for k in range(4)],
                "episodes": [{"name": e["name"], "maxScore": e["maxScore"],
                              "levels": [{"level": l["level"], "date": l["date"], "daysFromPeak": l["daysFromPeak"]} for l in e["levels"]]}
                             for e in r["episodes"]]}
    out["comparison"] = [summary("Current weights", cfg, out), summary("Lower-redundancy weights", alt_cfg, alt)]

    sens = []
    for label, k in (("Thresholds 20% tighter", 0.8), ("Current thresholds", 1.0), ("Thresholds 20% looser", 1.2)):
        r = out if k == 1.0 else analyse(dates, series, engine.scaled_cfg(cfg, k), proxies, sample)
        sens.append({"name": label, "latestScore": r["latestScore"],
                     "l4Episodes": [e["name"] for e in r["episodes"] if e["levels"][3]["date"]],
                     "outsideByLevel": [sum(1 for x in r["outsideEpisodes"] if x["level"] == f"L{j+1}") for j in range(4)],
                     "streaksByLevel": r["streaksByLevel"],
                     "alertVerdicts": {v: sum(1 for x in r["alertEpisodes"] if x["verdict"] == v) for v in ("early", "confirm", "false")}})
    out["sensitivity"] = sens
    out["notes"] = engine.FETCH_NOTES
    out["model"] = cfg.get("model")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=0)
    print(f"Backtest {out['dataStart']} → {out['dataEnd']}: {out['scoredDays']} scored days, "
          f"streaks by level {out['streaksByLevel']}, {len(out['outsideEpisodes'])} alarms outside episodes")
    for v in out["sensitivity"]:
        print(f"[{v['name']}] today {v['latestScore']} L4 in {v['l4Episodes']} outside {v['outsideByLevel']}")
    oc = out["alertOutcomes"]
    print("outcomes:", {k: sum(1 for o in oc if o["kind"] == k) for k in ("during", "followed", "false")},
          "declines:", [(x["from"], round(x["worst"], 2), x["alerted"]) for x in out["nasdaqDeclines"]])
    ae = out["alertEpisodes"]
    print("alert episodes:", {v: sum(1 for x in ae if x["verdict"] == v) for v in ("early", "confirm", "false")})
    for x in ae:
        print(f"  {x['start']} -> {x['end']} max L{x['maxLevel']} dd {x['ndxFromHighAtStart']:.0%} fwd {x['fwdWorst']:.0%} {x['verdict']} {x['inWindow'] or ''}")
    print("shock episodes:", [(x["start"], x["maxScore"], x["verdict"]) for x in out["shockEpisodes"]])
    print("ladder breaks:", out["ladderBreaks"])
    print("spread stand-in:", out["spreadStandIn"])
    for v in out["comparison"]:
        print(f"[{v['name']}] streaks {v['streaksByLevel']} outside {v['outsideByLevel']}")
        for e in v["episodes"]:
            print(f"  {e['name']}: max {e['maxScore']}, " + ", ".join(f"{l['level']}={l['date'] or '-'}" for l in e["levels"]))


if __name__ == "__main__":
    main()
