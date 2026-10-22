"""Daily snapshot and consistency checks for the published index.

check(out) reconciles everything docs/data.json will publish: the headline equals the weighted sum
of the gauge contributions, each component equals the weighted average of its gauges, the chart's
last point is the headline, the previous close matches the chart, the band matches the score, and
no gauge is dated after the index. Any mismatch stops the run, so an inconsistent reading is never
published (the site keeps showing the last good one).

record(out) saves one immutable snapshot per trading day, docs/snapshots/<asOf>.json: the score,
components, alert state, every gauge's raw value, transformation result, weight, contribution and
data date, the methodology version and the calculation time. A later run for the same day never
overwrites it: if the numbers changed (for example a late FRED release), the new calculation is kept
beside it as docs/snapshots/<asOf>.rev<N>.json and flagged in the log. docs/signal-log.csv gets one
row per published calculation, so the record of what the index said, and when, can't be rewritten.
"""
import csv
import datetime as dt
import json
import math
import os

REPO = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(REPO, "docs", "snapshots")
LOG_PATH = os.path.join(REPO, "docs", "signal-log.csv")
LOG_FIELDS = ["asOf", "calculatedAt", "modelVersion", "score", "band", "prevScore", "equity", "financing", "credit",
              "alert", "shock", "rangeLoose", "rangeTight", "staleGauges", "revision", "snapshot"]
EPS = 1e-6


def _xround(v):  # same rounding as engine.score_rows (half away from zero)
    return int(math.floor(v + 0.5))


def _weekdays_between(a, b):
    d, n = a, 0
    while d < b:
        d += dt.timedelta(days=1)
        n += d.weekday() < 5
    return n


def check(out):
    """Return a list of problems; empty when every published figure reconciles."""
    p = []
    score, F = out.get("score"), out.get("factors") or []
    if score is None:
        return p  # warm-up: nothing to reconcile
    if not 0 <= score <= 100:
        p.append(f"score {score} is outside 0-100")
    wsum = sum(f["weight"] for f in F)
    if wsum <= 0:
        return p + ["gauge weights sum to zero"]
    total = sum(f["weight"] / wsum * f["stress"] * 100 for f in F)
    if _xround(total) != score:
        p.append(f"headline {score} does not equal the weighted gauges ({total:.3f} rounds to {_xround(total)})")
    if abs(sum(f["points"] for f in F) - total) > 1e-6:
        p.append("gauge contributions do not add up to the weighted total")
    for pil, val in (out.get("pillars") or {}).items():
        fs = [f for f in F if f["pillar"] == pil]
        if not fs or val is None:
            continue
        exp = sum(f["weight"] * f["stress"] for f in fs) / sum(f["weight"] for f in fs) * 100
        if abs(exp - val) > EPS:
            p.append(f"{pil} component {val:.3f} does not equal its gauges' weighted average {exp:.3f}")
    for f in F:
        if not 0 <= f["stress"] <= 1:
            p.append(f"{f['col']} stress {f['stress']} is outside 0-1")
    H = out.get("history") or []
    if not H or H[-1]["date"] != out["asOf"] or H[-1]["score"] != score:
        last = H[-1] if H else None
        p.append(f"chart's last point {last and (last['date'], last['score'])} is not the headline ({out['asOf']}, {score})")
    if len(H) >= 2 and (out.get("prevDate"), out.get("prevScore")) != (H[-2]["date"], H[-2]["score"]):
        p.append(f"previous close ({out.get('prevDate')}, {out.get('prevScore')}) does not match the chart ({H[-2]['date']}, {H[-2]['score']})")
    if not out.get("implosionDate"):
        band = "NORMAL" if score < 25 else "ELEVATED" if score < 50 else "SEVERE" if score < 75 else "CRITICAL"
        if out.get("band") != band:
            p.append(f"band {out.get('band')} does not match score {score} ({band})")
    asof = dt.date.fromisoformat(out["asOf"])
    for f in F:
        if not f.get("lastDate"):
            continue
        last = dt.date.fromisoformat(f["lastDate"])
        if last > asof:
            p.append(f"{f['col']} data dated {f['lastDate']}, after the index date {out['asOf']}")
        if bool(f.get("stale")) != (_weekdays_between(last, asof) > 3):
            p.append(f"{f['col']} stale flag does not match its data date {f['lastDate']}")
    r = out.get("scoreRange") or {}
    if r.get("loose") is not None and r.get("tight") is not None and not r["loose"] <= score <= r["tight"]:
        p.append(f"threshold range {r['loose']}-{r['tight']} does not contain the score {score}")
    return p


def _snapshot(out):
    active = next((lv for lv in reversed(out.get("levels") or []) if lv.get("active")), None)
    return {
        "asOf": out["asOf"],
        "calculatedAt": out.get("generatedAt"),
        "modelVersion": (out.get("model") or {}).get("version"),
        "score": out["score"], "band": out.get("band"),
        "prevDate": out.get("prevDate"), "prevScore": out.get("prevScore"),
        "components": out.get("pillars"),
        "scoreRange": out.get("scoreRange"),
        "alert": {"current": out.get("currentLevel"), "active": active["level"] if active else None,
                  "levels": [{k: lv.get(k) for k in ("level", "name", "active", "paused", "streakStart")} for lv in out.get("levels") or []],
                  "shock": (out.get("shock") or {}).get("active"),
                  "recoveryMode": (out.get("exitRule") or {}).get("recoveryMode")},
        "gauges": [{k: f.get(k) for k in ("col", "name", "pillar", "source", "type", "weight", "lastDate", "stale",
                                         "current", "ref", "move", "calm", "trigger", "wcalm", "wtrig",
                                         "levelStress", "widenStress", "stress", "points")} for f in out.get("factors") or []],
        "notes": out.get("notes") or [],
    }


def _same(a, b):  # same published numbers, ignoring when they were calculated
    strip = lambda s: {k: v for k, v in s.items() if k not in ("calculatedAt", "notes")}
    return json.dumps(strip(a), sort_keys=True) == json.dumps(strip(b), sort_keys=True)


def record(out):
    """Save today's snapshot (never overwriting) and log the calculation. Returns a one-line summary."""
    if out.get("score") is None or out.get("sample"):
        return "no snapshot (warm-up or sample data)"
    os.makedirs(SNAP_DIR, exist_ok=True)
    snap = _snapshot(out)
    day = snap["asOf"]
    path = os.path.join(SNAP_DIR, f"{day}.json")
    revision = 0
    if os.path.exists(path):
        versions = [path] + sorted((os.path.join(SNAP_DIR, n) for n in os.listdir(SNAP_DIR) if n.startswith(f"{day}.rev")),
                                   key=lambda x: int(x.rsplit(".rev", 1)[1].split(".")[0]))
        latest = json.load(open(versions[-1]))
        if _same(latest, snap):
            return f"snapshot {day} unchanged"
        revision = len(versions)
        path = os.path.join(SNAP_DIR, f"{day}.rev{revision}.json")
    with open(path, "w") as fh:
        json.dump(snap, fh, indent=1)
    new_log = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new_log:
            w.writeheader()
        c, r = snap["components"] or {}, snap["scoreRange"] or {}
        w.writerow({"asOf": day, "calculatedAt": snap["calculatedAt"], "modelVersion": snap["modelVersion"],
                    "score": snap["score"], "band": snap["band"], "prevScore": snap["prevScore"],
                    "equity": round(c.get("Equity") or 0, 2), "financing": round(c.get("Financing") or 0, 2),
                    "credit": round(c.get("Credit") or 0, 2), "alert": snap["alert"]["active"] or "None",
                    "shock": int(bool(snap["alert"]["shock"])), "rangeLoose": r.get("loose"), "rangeTight": r.get("tight"),
                    "staleGauges": ";".join(g["col"] for g in snap["gauges"] if g.get("stale")),
                    "revision": revision, "snapshot": f"snapshots/{os.path.basename(path)}"})
    return f"snapshot {os.path.basename(path)} saved" + (f" (revision {revision}: the numbers for {day} changed)" if revision else "")


if __name__ == "__main__":  # check the published file: python snapshot.py [docs/data.json]
    import sys
    data = json.load(open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "docs", "data.json")))
    problems = check(data)
    print("\n".join(problems) if problems else f"{data['asOf']}: all published figures reconcile (score {data['score']})")
    sys.exit(1 if problems else 0)
