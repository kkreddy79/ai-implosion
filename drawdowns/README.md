# AI drawdown monitor

How far 50 AI-linked stocks have fallen from their highest-ever close. It's a companion to the AI Market Stress
Index, not part of it: the index scores funds, indexes and credit spreads; this page shows the damage stock by stock.

**Live page:** https://kkreddy79.github.io/ai-implosion/drawdowns.html · **Data:** `docs/drawdowns.json`

| File | What it is | Edit it to… |
|---|---|---|
| `drawdowns/basket.json` | The 50 stocks, their segments, the page title and a log of basket changes | add, drop or re-segment a stock |
| `drawdowns/template.html` | The page: layout, colors, controls. `drawdowns.py` fills its `__PLACEHOLDERS__` | change the look |
| `drawdowns.py` | Fetches prices, computes drawdowns, checks them and writes the page | change the method |

## How it updates

The daily workflow (`.github/workflows/update.yml`) runs `python drawdowns.py` after the index and `markets.py`,
and commits `docs/drawdowns.json` and `docs/drawdowns.html`. Like `markets.py`, a failure never stops the index.

- **Run by hand:** Actions → *Update dashboard data* → **Run workflow**, or locally `python drawdowns.py`.
- **Check the published file:** `python drawdowns.py --check`.
- **One stock fails to fetch:** its row keeps the previous close and shows a STALE tag.
- **More than 5 fail, or a check fails:** nothing is written, so the last good page stays up. The run log says why.

## Method

- **Prices:** daily closes from Yahoo Finance, **split-adjusted, not dividend-adjusted**, so readers can match them
  to any quote screen. (The index itself uses dividend-adjusted closes over 2 years; the two are meant to differ.)
- **Drawdown** = latest close ÷ highest close since listing − 1.
- **Today** = latest close ÷ previous close − 1.
- **Severity bands** match the Stress Index: Normal 0–24% off peak (green), Elevated 25–49% (amber),
  Severe 50–74% (orange), Critical 75–100% (red).
- **Pre-2025 peak:** a stock whose record close came before 1 Jan 2025 is tagged and its bar striped, because part of
  its fall predates the AI trade. The page's toggle hides these.
- **Rank (#):** each stock's rank by drawdown among the whole basket. It never changes with sorting or filters.
- **As-of date:** the latest US close. SK hynix trades in Seoul, so its close is from earlier the same day.

### Checks before publishing (`check()` in `drawdowns.py`)
Every stock is in the basket and appears once · every drawdown is between −100% and 0 and matches its two prices ·
no day move over ±60% (a bad print or an unadjusted split) · every segment is listed in `basket.json` ·
the headline numbers match the rows · no more than 5 stocks missing.

## Basket rules

1. **AI-linked first.** A stock is in because its value depends on AI demand: chips and memory, cloud and neoclouds,
   data centers, AI networking and optics, power for data centers, AI servers, AI software, frontier model makers.
2. **Cover the gauges.** The main holdings of the index's AI-specific gauges (^SOX, AIQ, DTCR) should be in the
   basket, so the monitor explains what moves those gauges.
3. **About 50, large and liquid.** Swap rather than grow: a new name replaces the weakest AI link.
4. **Pre-2025 peakers stay only if they are clearly AI plays** (C3.ai, SoundHound, Super Micro, Ciena).
   Software and data companies whose record came in the 2021 boom and that aren't AI plays are out
   (Adobe, Salesforce, Snowflake, MongoDB, UiPath were dropped on 20 Nov 2026).
5. **Conglomerates stay out** when AI is a small part of the business (Samsung was left out for that reason).
6. **New listings join on their first day.** Mark them with `"ipo": {"date": …, "price": …}` so the page says the
   peak is recent (Anthropic, ANT, listed 19 Nov 2026 at $83).
7. **Non-US listings** need `"currency"` (only `"KRW"` is set up; add others to `CURRENCY` in `drawdowns.py`).
8. **Log every change** in `basket.json` → `changes`: date, added, dropped, and why.

To change a stock: edit `basket.json` (`symbol` is the Yahoo symbol, `ticker` the label shown, `segment` one of
`segments`), then run the workflow. A new segment needs adding to `segments`, which also sets the order of the
segment cards.

## Page rules

- **Brand:** dark theme `#14111b`, the Stress Index band colors, "KK**Reckons**" wordmark and **"Analysis by KK Reddy"**
  in the footer, with the data source and date. Keep the "not investment advice" line.
- **Controls:** a sort dropdown (biggest/smallest drop, today worst/best, most recent/oldest peak, ticker A–Z);
  tap-to-filter on the severity bar and the segment cards (tap again to clear); the "Only peaks set in 2025–26"
  toggle. Filters stack, and the four headline cards recompute for what's showing.
  No separate "group by" control: the cards already do that job (decided 20 Nov 2026).
- **Mobile first:** most readers are on phones. Each row collapses to ticker, drop and today's move, with the bar
  underneath. Check any change at 390 px wide as well as desktop.
- **Self-contained:** inline CSS and JS, no external libraries or fonts, all styles scoped under `.kkdd`, so the
  page can be iframed or pasted into kkreckons.com without clashing.

## On kkreckons.com

Recommended: give the monitor **its own page** (e.g. `/ai-stress-index/drawdowns`) and put a small **summary card**
on `/ai-stress-index` linking to it. The card shows the four headline numbers and the severity bar. The index page is
already long, and a separate page can be shared and found in search on its own.

- **Quickest:** iframe `https://kkreddy79.github.io/ai-implosion/drawdowns.html` on the new page.
- **Native:** read `https://kkreddy79.github.io/ai-implosion/drawdowns.json` the way the site reads `data.json`
  (same hourly cache, same revalidate call), and render it with the same layout.
  `summary` has the card's numbers: `count`, `medianDrawdown`, `down50`, `down75`, `medianDay`.
