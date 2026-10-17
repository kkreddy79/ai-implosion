# AI Implosion Dashboard

Daily AI-bubble stress score (0–100) with rule-based alert levels. Built by KK Reddy.

**Live page:** https://kkreddy79.github.io/ai-implosion/ (GitHub Pages, served from `docs/`)

## Indicators (9, in 3 pillars)
| Pillar | Indicator | Source | Weight | Calm → Trigger |
|---|---|---|---|---|
| Equity | Nasdaq-100 | Yahoo ^NDX | 18% | −5% → −25% |
| Equity | Semiconductors | Yahoo ^SOX | 18% | −10% → −40% |
| Equity | AI & tech ETF | Yahoo AIQ | 9% | −5% → −30% |
| AI financing | Data-center infrastructure ETF | Yahoo DTCR | 6% | −10% → −35% |
| AI financing | BBB corporate spread | FRED BAMLC0A4CBBB | 6% | 140 → 250 bp; widen +30 → +100 |
| AI financing | CCC high-yield spread | FRED BAMLH0A3HYC | 6% | 900 → 1,500 bp; widen +150 → +500 |
| Credit | BDC (private credit) ETF | Yahoo BIZD | 14% | −5% → −25% |
| Credit | Leveraged loan ETF | Yahoo BKLN | 13% | −2% → −8% |
| Credit | High-yield spread | FRED BAMLH0A0HYM2 | 10% | 400 → 600 bp; widen +75 → +250 |

## How it updates
`.github/workflows/update.yml` runs every weekday at 22:17 UTC (after the US close). It runs
`update_dashboard.py`, which pulls ~3 years of dividend-adjusted daily closes from Yahoo Finance and the
three ICE BofA spreads from FRED (×100 for bp), then writes:

- `docs/data.json` — read by the page (`docs/index.html`)
- `docs/AI_Implosion_Dashboard.xlsx` — the full Excel 365 workbook (live formulas, 4 charts), downloadable from the page
- `data/fred.json` — cached spread history, so a FRED outage only carries the last value forward

The spreads need a free FRED API key saved as the repository secret `FRED_API_KEY`
(Settings → Secrets and variables → Actions). To refresh now: Actions → *Update dashboard data* → **Run workflow**.

## Files
| File | What it is |
|---|---|
| `config.json` | Indicators, weights, thresholds and lookbacks. Edit here; the next run applies them to the page and the workbook. |
| `engine.py` | Data fetch + the scoring rules |
| `update_dashboard.py` | Runs the engine and builds the Excel workbook |
| `docs/index.html` | The dashboard page |
| `backtest.py`, `docs/backtest.html` | Replays the rules on daily data since 1997 (stand-ins before an ETF existed); results at [/backtest.html](https://kkreddy79.github.io/ai-implosion/backtest.html). Runs on rule changes and monthly. |

## Rules
- Prices: drawdown x = dividend-adjusted close ÷ highest close of the last 2 years − 1. Stress = clamp((x − Calm) ÷ (Trigger − Calm), 0, 1).
- Spreads (bp): stress = max(level test, widening test). Level = clamp((spread − Calm) ÷ (Trigger − Calm)); widening = clamp((spread − 52-week low − widen calm) ÷ (widen trigger − widen calm)). Blank days carry the last value.
- Status: 1 TRIGGERED, ≥0.5 WARNING, >0 WATCH, else CALM. Score = round(weighted stress × 100). The first 2 years of history are warm-up.
- Levels must hold for 5 straight trading days:
  L1 Score ≥ 50 · L2 Score ≥ 60 and (BIZD ≥ 0.8 with BKLN ≥ 0.5, or CCC ≥ 0.8) · L3 Score ≥ 70, SOX ≥ 0.83, AIQ ≥ 0.8 ·
  L4 Score ≥ 75, NDX = 1, (HY ≥ 500bp or BIZD = 1).
- Levels are independent tripwires, not a ladder (L4 can fire before L3; L2 can be skipped). L1–L2 are the early warnings, L3–L4 late confirmations.
- Exit rule (v1.4), "recovery mode": while the Nasdaq-100 is 20%+ above its lowest close since its 2-year high, levels whose
  conditions still hold are shown as PAUSED instead of active. Start dates, including the Implosion date, are computed as before and never move.
- Shock flag (v1.4, not a level): score ≥ 60 on a day when no level has confirmed yet (a streak counts from its 5th day, so no hindsight).
  Catches fast sell-offs (April 2025) that never hold 5 days.
- Score range: `data.json` → `scoreRange` is today's score with every threshold 20% tighter / looser.
- An indicator that hasn't updated for more than 3 trading days is flagged as stale on the page.
- Concentrated stress: the page calls out any indicator at ≥ 75% stress while no level is active.
- Backtest honesty: thresholds were shaped on the replayed history, so results are in-sample. `config.json` → `model.outOfSampleFrom`
  starts the out-of-sample record; alerts after it are the real test. Every alert episode (streaks merged when < 20 trading days apart)
  is judged by one rule: early warning (Nasdaq-100 falls 10%+ more within ~6 months), late confirmation (began 20%+ below the 2-year high)
  or false alarm. Before 2023-10-16 the three spread gauges share one stand-in (Moody's Baa), so they act as one signal.
- Weights were checked against a lower-redundancy alternative in the backtest (same implosions caught, but more false alarms), so the current weights stay.
- Implosion date = first day of the first L4 streak.

## Run locally
```
pip install openpyxl
FRED_API_KEY=... python update_dashboard.py   # live data
python update_dashboard.py --sample           # offline demo data
```
