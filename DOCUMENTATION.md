# NSE Swing Screener — End-to-End Documentation

> **Version:** v3 (Adaptive Market Regime Engine)
> **Stack:** Python/Flask · Supabase · yfinance · Groww API · Vanilla JS SPA

---

## Table of Contents

1. [Application Overview](#1-application-overview)
2. [Tech Stack & Dependencies](#2-tech-stack--dependencies)
3. [Project Structure](#3-project-structure)
4. [Database Schema](#4-database-schema)
5. [Backend — Code Flow (`groww_proxy.py`)](#5-backend--code-flow)
   - 5a. [Startup & Connections](#5a-startup--connections)
   - 5b. [Market Regime Detection](#5b-market-regime-detection)
   - 5c. [Technical Indicators](#5c-technical-indicators)
   - 5d. [10-Step Screening Funnel](#5d-10-step-screening-funnel)
   - 5e. [Target & Stop Loss Logic](#5e-target--stop-loss-logic)
   - 5f. [Async Scan Job](#5f-async-scan-job)
   - 5g. [Trade Outcome Reconciliation](#5g-trade-outcome-reconciliation-2-pass)
   - 5h. [Live Price Fetching](#5h-live-price-fetching)
6. [API Routes Reference](#6-api-routes-reference)
7. [Frontend Architecture](#7-frontend-architecture)
8. [Caching & Global State](#8-caching--global-state)
9. [NSE Universe Fallback Chain](#9-nse-universe-fallback-chain)
10. [Deployment Guide](#10-deployment-guide)

---

## 1. Application Overview

The NSE Swing Screener is a **full-stack swing trade screening tool** for Indian equities (NSE). It screens Nifty 500 stocks using a rigorous 10-step technical methodology and adapts its filter thresholds dynamically to the prevailing market regime (BULL / NEUTRAL / BEAR / CRISIS).

**Key characteristics:**
- Capital per trade: ₹20,000
- Holding period: ~1 week
- Target: ~5% gain; Stop Loss: ~3.5% below entry
- Signals: `STRONG BUY`, `BUY`, `WATCH`
- All trade outcomes (target hit / SL hit) are automatically tracked via historical OHLC reconciliation

**Architecture:**
```
Browser (nse-swing-screener.html)
        │
        ▼
Flask Backend (groww_proxy.py — port 5001)
        │                    │
        ▼                    ▼
  Supabase DB          External APIs
  (trades, recs,       · yfinance (OHLC)
   positions,          · Groww API (live LTP)
   events)             · NSE archives (universe)
```

---

## 2. Tech Stack & Dependencies

### Backend (`requirements.txt`)

| Package | Purpose |
|---------|---------|
| `flask` | REST API framework |
| `flask-cors` | Cross-origin request headers for browser SPA |
| `requests` | HTTP client for NSE API, Groww API, CSV fetches |
| `yfinance` | Free stock OHLC data (Yahoo Finance) |
| `pandas` | Time-series manipulation, EWM calculations, timestamps |
| `numpy` | Vectorized math (max/min/mean, nan handling) |
| `gunicorn` | Production WSGI server |

### Frontend
- Pure vanilla JavaScript — no framework dependencies
- CSS Grid layout with CSS custom properties for theming

### Database
- **Supabase** (managed PostgreSQL + auto-generated REST API)
- Accessed via plain `requests` HTTP calls (avoids `httpx` HTTP/2 issues on Render.com)

### External Services
| Service | Used For |
|---------|---------|
| Yahoo Finance (yfinance) | 1-year daily OHLC for all screened stocks |
| Groww API | Live intraday LTP, day_high, day_low |
| NSE archives CSV | Stock universe (Nifty 50/100/200/500) |
| NSE direct API | Fallback universe, market breadth, event calendar |
| India VIX (`^INDIAVIX`) | Regime classification |

---

## 3. Project Structure

```
fnc/
├── groww_proxy.py              # Main Flask backend (2295 lines)
├── nse-swing-screener.html     # Single-page frontend application (~106 KB)
├── requirements.txt            # Python dependencies
├── Procfile                    # Gunicorn startup command (Heroku/Render)
├── netlify.toml                # Netlify SPA routing + security headers
├── .gitignore                  # Python/IDE ignores
│
├── db/
│   └── migrations/
│       └── 20260326_001_portfolio_module.sql  # Supabase table DDL
│
└── frontend/
    └── portfolio/
        ├── index.js                           # Portfolio module entry point
        ├── services/
        │   └── portfolioApi.js               # HTTP client for portfolio routes
        ├── state/
        │   └── portfolioStore.js             # Reactive observer-pattern store
        └── components/
            ├── RecommendationsTable.js        # Buy/Strong Buy recommendations UI
            ├── ActivePositionsTable.js        # Holdings + live P&L table
            ├── PortfolioAnalyticsCards.js     # KPI summary cards
            └── HistoryTimeline.js             # Portfolio event audit log
```

---

## 4. Database Schema

All tables live in the `public` schema of Supabase PostgreSQL.

### 4.1 `trades` (core table — inferred from Flask code)

This is the central table populated by the screener. It is **not** in the migration file (assumed pre-existing).

| Column | Type | Description |
|--------|------|-------------|
| `id` | uuid PK | Auto-generated unique ID |
| `symbol` | text | NSE ticker (e.g. `RELIANCE`) |
| `name` | text | Company name |
| `sector` | text | NSE sector string |
| `signal` | text | `BUY`, `STRONG BUY`, or `WATCH` |
| `score` | numeric | 0–100 composite screening score |
| `regime` | text | Market regime at scan time (`BULL`, etc.) |
| `entry_price` | numeric(12,2) | Price at time of signal |
| `buy_range_low` | numeric(12,2) | Lower bound of suggested buy range |
| `buy_range_high` | numeric(12,2) | Upper bound of suggested buy range |
| `target1` | numeric(12,2) | T1 target price (~5% above entry) |
| `target2` | numeric(12,2) | T2 stretch target (~6.5% above entry) |
| `stop_loss` | numeric(12,2) | SL price (~3.5% below entry) |
| `tp_pct` | numeric(5,2) | Target % gain |
| `sl_pct` | numeric(5,2) | SL % loss |
| `rr` | numeric(5,2) | Risk-Reward ratio |
| `shares_20k` | integer | Shares purchasable with ₹20,000 |
| `exp_profit_20k` | numeric(12,2) | Expected profit at T1 with ₹20,000 |
| `max_loss_20k` | numeric(12,2) | Max loss at SL with ₹20,000 |
| `status` | text | `open`, `target_hit`, `sl_hit` |
| `scanned_at` | timestamptz | When signal was generated (EOD = next-day entry) |
| `entry_date` | date | Explicit entry date (overrides scanned_at inference) |
| `entered_at` | timestamptz | Explicit entry timestamp (overrides scanned_at inference) |
| `outcome_date` | date | Date T1/SL was hit |
| `outcome_price` | numeric(12,2) | Actual outcome fill price |
| `days_to_outcome` | integer | Trading days from entry to outcome |
| `created_at` | timestamptz | Row creation time |
| `updated_at` | timestamptz | Last modification time |

**Key Index:** `(symbol, status)` — used by outcome reconciliation and portfolio summary.

---

### 4.2 `recommendations`

Manual or automated analyst recommendations (separate from screener trades).

```sql
CREATE TABLE public.recommendations (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol       text NOT NULL,
  rating       text NOT NULL CHECK (rating IN ('buy', 'strong_buy')),
  target_price numeric(12,2) NOT NULL DEFAULT 0,
  stop_loss    numeric(12,2) NOT NULL DEFAULT 0,
  status       text NOT NULL DEFAULT 'active',
  rationale    text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
```

**Indexes:** `symbol`, `status`, `created_at DESC`

---

### 4.3 `portfolio_positions`

Manual position tracking (actual holdings, not screener signals).

```sql
CREATE TABLE public.portfolio_positions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol        text NOT NULL,
  qty           numeric(14,4) NOT NULL DEFAULT 0,
  entry_price   numeric(12,2) NOT NULL DEFAULT 0,
  current_price numeric(12,2) NOT NULL DEFAULT 0,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
  opened_at     timestamptz NOT NULL DEFAULT now(),
  closed_at     timestamptz,           -- populated when status → 'closed'
  notes         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
```

**Indexes:** `symbol`, `status`, `opened_at DESC`

---

### 4.4 `portfolio_events`

Append-only audit log for all position changes.

```sql
CREATE TABLE public.portfolio_events (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  position_id uuid REFERENCES portfolio_positions(id) ON DELETE SET NULL,
  symbol      text,
  event_type  text NOT NULL CHECK (event_type IN ('create', 'update', 'delete', 'close')),
  payload_json jsonb NOT NULL DEFAULT '{}',
  message     text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
```

**Indexes:** `position_id`, `symbol`, `created_at DESC`

---

## 5. Backend — Code Flow

### 5a. Startup & Connections

**File:** `groww_proxy.py`, lines 1–92

#### Supabase Client (`_SBClient`, `_SBTable`)

A lightweight custom Supabase client is used instead of the official `supabase-py` SDK. The official SDK uses `httpx` with HTTP/2, which causes connection issues on Render.com (the deployment host). The custom client uses plain `requests` and maps Supabase's REST API conventions:

```
_SBClient.table("trades")
    .select("*")
    .eq("status", "open")
    .order("scanned_at", desc=True)
    .execute()
```

Method chaining builds URL params, then `.execute()` dispatches the HTTP call.

**Connection verification:** On startup, a probe `GET /rest/v1/trades?select=id&limit=1` is issued. If it fails (missing env vars or network), `SB_OK = False` and all database routes return 503.

#### Groww API Authentication

Groww uses a **SHA-256 checksum** authentication scheme:

```
checksum = SHA256(api_secret + unix_timestamp)
POST /v1/token/api/access  →  Bearer access_token (valid ~4 hours)
```

Three request body formats are attempted in sequence (the API is inconsistent across SDK versions). The token is cached globally for 4 hours with thread-safe access.

**Credentials priority:**
1. Session-level override (`POST /set-credentials`) — stored in `_session_api_key/secret`
2. Environment variables `GROWW_API_KEY` / `GROWW_API_SECRET`

---

### 5b. Market Regime Detection

**Function:** `detect_market_regime()` · **Cache TTL:** 15 minutes

The regime determines which filter thresholds apply to the entire screening run.

#### Inputs
| Signal | Source | Threshold |
|--------|--------|-----------|
| Nifty 50 price | yfinance `^NSEI` (1 year, daily) | vs EMA50, EMA200 |
| India VIX | yfinance `^INDIAVIX` (5 days) | 15 / 20 / 28 |
| Advance/Decline ratio | NSE API `NIFTY 500` breadth | 1.2 / 1.5 |

#### Classification Logic (evaluated in order)

```
VIX > 28  AND  price < EMA200  →  CRISIS
price < EMA50  AND  VIX > 20   →  BEAR
price > EMA50  AND  VIX < 15  AND  A/D ratio > 1.5  →  BULL
Otherwise                       →  NEUTRAL
```

#### Regime Config (`get_regime_config()`)

Each regime returns a configuration dict that modifies all downstream filters:

| Parameter | BULL | NEUTRAL | BEAR | CRISIS |
|-----------|------|---------|------|--------|
| `rsi_min` | 45 | 45 | 46 | 48 |
| `rsi_max` | 68 | 68 | 62 | 60 |
| `vol_min` | 1.2x | 1.2x | 1.2x | 2.0x |
| `rs_required` | No | No | Yes | Yes |
| `higher_lows_req` | No | No | No | Yes |
| `no_gap_req` | No | No | Yes | Yes |
| `bb_filter_req` | No | No | No | Yes |
| `atr_max_pct` | 7.0% | 6.0% | 5.0% | 4.5% |
| `min_score` | 50 | 52 | 60 | 68 |
| `strong_buy_score` | 75 | 75 | 78 | 999 (disabled) |
| `buy_score` | 50 | 52 | 62 | 68 |
| `sl_atr_mult` | 1.5x | 1.5x | 1.2x | 1.0x |
| `min_rr` | 1.5 | 1.5 | 1.8 | 1.8 |

---

### 5c. Technical Indicators

All indicator functions operate on raw NumPy arrays or pandas Series of daily closing prices.

#### RSI — `calc_rsi(closes, period=14)`
```
gain = clip(delta, lower=0).ewm(alpha=1/14)
loss = clip(-delta, upper=0).ewm(alpha=1/14)
RSI  = 100 - 100 / (1 + gain/loss)
```
EWM smoothing (not simple average) — matches industry-standard Wilder's RSI.

#### EMA — `calc_ema(closes, period)`
```
EMA = closes.ewm(span=period, adjust=False).mean()
```

#### MACD — `calc_macd_full(closes, fast=12, slow=26, sig=9)`
Returns: `(signal_state, histogram_value, crossover_in_last_5d)`
- `signal_state`: `"bullish"` | `"bearish"` | `"neutral"`
- **Crossover detection:** Scans last 5 candles for MACD crossing above signal line
- A fresh crossover (even if histogram is small) is treated as a valid entry signal

#### ATR — `calc_atr(highs, lows, closes, period=14)`
```
TrueRange = max(H-L, |H-PrevClose|, |L-PrevClose|)
ATR       = TrueRange.ewm(alpha=1/14)
ATR%      = ATR / CurrentPrice * 100
```
Returns: `(atr_absolute, atr_percentage)`

#### ADX — `calc_adx(highs, lows, closes, period=14)`
Measures trend strength (0–100). Used as optional filter (`adx_min` param).
- +DM / -DM directional movement
- ADX < 20 = sideways; ADX > 25 = trending

#### Bollinger Bands — `calc_bollinger_position(closes, period=20, std=2)`
Returns position: `"above_upper"` | `"sweet_spot"` | `"below_middle"`
- CRISIS regime requires `"sweet_spot"` (price between mid and upper band)

#### Higher Lows — `check_higher_lows(lows, n=3)`
Checks that the last `n` candle lows are sequentially increasing (accumulation pattern).

#### Gap-Down Check — `check_no_gap_down(opens, closes, threshold=1.5%, n=5)`
Returns `False` if any candle in last 5 days opened >1.5% below the prior close.

#### Relative Strength vs Nifty — `calc_relative_strength(stock_closes, nifty_closes)`
**The most important filter (Step 2).**
```
rs_5d_ok  = stock_5d_return > nifty_5d_return + 1.5%   (outperform by 1.5%)
rs_10d_ok = stock_10d_return > nifty_10d_return + 2.0%  (outperform by 2%)
rs_ok     = rs_5d_ok OR rs_10d_ok                       (either timeframe works)
```
In BEAR/CRISIS: `rs_ok = rs_ok AND (stock outperforms its sector)`

#### RS vs Sector — `calc_rs_vs_sector(stock_closes, sector_closes)`
10-day return comparison. Only applied as additional gate in BEAR/CRISIS regime.

#### 52-Week Metrics
- `calc_52wk_proximity(closes, highs)` → `% below 52-week high`
- `calc_52wk_position_pctile(closes, highs, lows)` → `0–100th percentile` in 52-week range

#### MA Structure — `check_ma_structure(closes, ema20, ema50, ema200, rsi)`
**4 OR-conditions** — at least one must be true:
```
a) price > EMA20  AND  EMA20 > EMA50           (short-term bullish alignment)
b) price crossed above EMA20 within last 3 days (early breakout)
c) price > EMA200                               (long-term uptrend intact)
d) price between EMA20 and EMA50 AND RSI rising (base-building)
```

#### Volume Quality — `calc_volume_quality(volumes, n_recent=3, n_avg=20)`
```
avg_vol   = mean(volumes[-23:-3])   # 20-day baseline (excluding last 3 days)
vol_ratio = mean(volumes[-3:]) / avg_vol
```
Returns: `(vol_ratio, all_recent_above_avg)`

#### Consolidation Breakout — `detect_consolidation_breakout(closes, highs, lows, lookback=15)`
```
range_pct  = (max_high - min_low) / min_low * 100  (over 15 days)
breakout   = range_pct < 8%  AND  today's close > 15d_high * 1.005
```

---

### 5d. 10-Step Screening Funnel

**Function:** `analyze_stock(sym_info, df, params, context)` — called per stock.

#### Step 1 — Universe Hard Filters (instant rejection)

| Condition | Rejection reason |
|-----------|-----------------|
| Sector in `{"information technology", "it", "software", "technology"}` | IT sector excluded |
| `pct_below_52wk < 2%` | Within 2% of 52-week high — overstretched |
| `pct_below_52wk > 35%` AND no recovery signal (MACD not bullish, price below EMA20, MA structure failed) | Falling knife |

#### Step 3 — RSI Filter

```python
rsi_min <= rsi_14 <= rsi_max   # regime-adaptive range, e.g. 45–68 in NEUTRAL
```
RSI below range = no momentum. RSI above range = overbought. Sweet spot = 45–68.

#### Step 4 — MA Structure

```python
ema_filter = params.get("ema_filter", "at_or_above")
# "above"       → price must be strictly above EMA20
# "at_or_above" → price below EMA20 allowed only if another MA condition is met
```
The fallback MA conditions (crossover, price > EMA200, base-building) prevent over-filtering in bear markets.

#### Step 5 — MACD Filter

```python
macd_ok = (macd_histogram > 0) OR (macd_crossover_in_last_5d)

macd_filter = params.get("macd_filter", "bullish_neutral")
# "bullish"        → only accept macd_sig == "bullish"
# "bullish_neutral"→ reject only if bearish AND no crossover
```

#### Step 6 — Volume Confirmation

```python
vol_ratio >= vol_min   # last-3d avg / 20d avg; vol_min = regime config (1.2x to 2.0x)
```

#### Step 2 — Relative Strength (applied after other soft filters)

```python
if rs_required (BEAR/CRISIS regime, or user param):
    reject if not rs_ok
```

#### Other Optional Gates

| Gate | Param | When Applied |
|------|-------|-------------|
| Higher lows (3 consecutive) | `higher_lows` / regime | CRISIS: always |
| No gap-down (5 days) | `no_gap_down` / regime | BEAR/CRISIS: always |
| Bollinger sweet_spot | `bb_filter` / regime | CRISIS: always |
| ATR% ceiling | `atr_max_pct` / regime | All regimes |
| ATR% floor | `atr_min_pct` | User param only |
| ADX minimum | `adx_min` | User param only |
| 52-week distance limit | `high52_max_pct` | User param only |
| Sector above EMA20 | `sector_momentum` | User param only |
| Earnings in 5 days | `earnings_filter` | User param only |
| Recent FII/DII deal | `fii_dii_filter` | User param only |
| Delivery % minimum | `delivery_min` | User param only |

#### Step 8 — Fundamental Quick Check (optional, slow)

Fetches `yfinance.Ticker.info` for shortlisted stocks:
- `trailingPE` > 60 → expensive flag
- `debtToEquity` > 2.0 → high leverage flag
- `returnOnEquity` > 12% → healthy ROE flag
- `marketCap` > ₹5,000 crore → liquid stock flag

Only runs if `params["fetch_fundamentals"] == True`.

#### Step 9 — Scoring (100-point scale)

| Component | Max Points | Scoring Logic |
|-----------|-----------|---------------|
| RS vs Nifty 5d | 25 | +5%=25pts, +2%=15pts, +1%=10pts, ≥0%=5pts |
| RSI sweet spot | 15 | 55–65=15pts, 50–55=10pts, 45–50 or 65–68=5pts |
| MA alignment | 15 | 5pts per condition met (max 3 conditions) |
| MACD | 10 | Bullish=10, Crossover=7, Histogram>0=4 |
| Volume | 10 | ≥2.0x=10, ≥1.5x=7, ≥1.2x=5 |
| Sector bonus | 10 | Mapped: +3→10pts, +2→7pts, +1→3pts, -1→-3pts, -2→-7pts |
| Fundamentals | 10 | PE/ROE/DE composite; 5pts if not fetched |
| 52-week range | 5 | 40–80th percentile=5, 30–40 or 80–90=3, else=1 |

**Sector Bonus Rules (Step 7):**
| Sector | Bonus | Rationale |
|--------|-------|-----------|
| Energy / Coal / Oil & Gas / Power | +3 | Geopolitical hedge |
| Pharma / Healthcare | +3 | Defensive outperformer |
| Metals / Steel / Mining | +2 | Infrastructure capex |
| PSU / Defence / Railways | +2 | Government spending theme |
| FMCG / Consumer Staples | +1 | Defensive |
| IT / Software | -1 | FII selling headwind |
| Real Estate | -2 | Rate sensitive, FII exit |

#### Step 10 — Signal Classification

```python
STRONG BUY:  allow_strong_buy AND score >= strong_buy_score
             AND macd == "bullish"
             AND price above EMA20
             AND last_day_vol >= strong_buy_vol_threshold
             AND rs_ok
             AND (not BEAR/CRISIS OR higher_lows)

BUY:         score >= buy_score
             AND macd in ("bullish", "neutral") OR crossover
             AND price at or above EMA20

WATCH:       otherwise

# Downgrade: STRONG BUY → BUY if market breadth poor (A/D < 1.2) in BEAR/CRISIS
```

---

### 5e. Target & Stop Loss Logic

Calculated only for `BUY` and `STRONG BUY` signals:

```python
# Buy Range
buy_low   = max(price * 0.98, EMA20)   # don't buy more than 2% below market
buy_high  = price * 1.01               # don't chase more than 1% above

entry = (buy_low + buy_high) / 2       # midpoint used for T1/SL math

# Target 1 (5% gain in ~1 week)
t1 = entry * 1.05
# Cap at nearest EMA resistance (EMA50 or EMA200) above price
resistances = [ema for ema in [EMA50, EMA200] if ema > price * 1.01]
if resistances and min(resistances) < t1:
    t1 = min(resistances) * 0.998      # just below resistance

# Target 2 (stretch: 6.5%)
t2 = entry * 1.065

# Stop Loss (3.5% below entry, floored at EMA20-support)
sl = max(entry * 0.965, EMA20 * 0.995)

# Position sizing
shares = int(20000 // price)           # shares for ₹20,000 capital
```

---

### 5f. Async Scan Job

**Route:** `POST /screen`
**Worker:** `scan_worker(job_id, stocks, params)`

#### Job Lifecycle

```
POST /screen
    → Creates job_id (8-char UUID prefix)
    → Stores in _jobs dict with status="queued"
    → Spawns daemon thread: scan_worker()

scan_worker():
    1. status = "prefetch"
    2. detect_market_regime()
    3. _prefetch_nifty_closes()       # 3-month Nifty OHLC
    4. _prefetch_sector_ema()         # sector index EMA20 position
    5. _prefetch_sector_closes()      # sector index OHLC (for RS vs sector)
    6. _prefetch_earnings_symbols()   # if earnings_filter enabled
    7. _prefetch_institutional_symbols() # if fii_dii_filter enabled
    8. _prefetch_market_breadth()     # Nifty 500 A/D ratio
    9. status = "running"
    10. ThreadPoolExecutor (max 15 workers):
        for each stock:
            fetch_ohlcv(symbol)       # 1-year daily OHLC from yfinance
            analyze_stock(...)        # 10-step funnel
    11. status = "done"

Frontend polls GET /scan-progress/<job_id> every 500ms
    → Returns: status, progress, total, results[], rejected[]
```

#### Cancellation
`POST /cancel-scan/<job_id>` sets `job["cancelled"] = True`. Each worker thread checks this flag before processing the next stock.

---

### 5g. Trade Outcome Reconciliation (2-Pass)

**Function:** `run_trade_outcome_check()` — called on every `GET /trades` request.

Determines if open trades (with `target1` and `stop_loss` set) have hit their targets or stop losses.

#### Entry Date Inference

```python
# Explicit entry date: use directly
entry_day = trade["entry_date"]  or  trade["entered_at"]

# EOD scan signal only: actual trade entry is next calendar day
# (Signal generated end-of-day → trade placed next morning)
entry_day = scanned_at + 1 day
```

#### Pass 1: Historical OHLC

```
For each open trade:
    Fetch yfinance OHLC: entry_day → yesterday (today exclusive)
    Scan each candle row-by-row:
        if candle_high >= target1  →  status = "target_hit"   (T1 has priority)
        elif candle_low <= stop_loss  →  status = "sl_hit"

    If yfinance returns empty (rebranded/illiquid ticker):
        Retry with auto_adjust=False
```

**Priority rule:** If both T1 and SL are touched on the same candle, T1 wins — a limit-sell at target is assumed to fill before a stop-loss trigger.

#### Pass 2: Live Intraday LTP

For trades not resolved by Pass 1 (e.g., today's hit not yet in historical data):

```
Fetch _fetch_ltp_batch(remaining_symbols)
    → Groww API: day_high, day_low (intraday extremes)
    → fallback: yfinance

For each unresolved trade:
    if day_high >= target1  →  target_hit
    elif day_low <= stop_loss  →  sl_hit
    elif still unresolved:
        Scan last 10 trading days (catches yesterday's miss)
```

#### Supabase Update
```python
_sb.table("trades").update({
    "status": "target_hit",       # or "sl_hit"
    "outcome_price": t1_or_sl,
    "outcome_date": "YYYY-MM-DD",
    "days_to_outcome": N,
}).eq("id", trade_id).execute()
```

---

### 5h. Live Price Fetching

**Function:** `_fetch_ltp_batch(symbols)`

For each symbol, tries sources in order:

```
1. Groww API  GET /v1/live-data/quote?exchange=NSE&segment=CASH&trading_symbol=SYM
   → Returns: last_price, high_price, low_price, day_change, day_change_perc
   → 429 response: circuit-breaks remaining symbols to yfinance

2. yfinance fallback
   → 5-day daily history, last candle
   → Returns: Close, High, Low (close as LTP, day's H/L as intraday range)

Returns: (prices_dict, failed_list)
prices[sym] = {ltp, day_high, day_low, change, change_pct, source}
```

---

## 6. API Routes Reference

| Method | Route | Purpose | Key Response Fields |
|--------|-------|---------|---------------------|
| GET | `/health` | Status check | `yf_available`, `supabase_ok`, `session_creds_active` |
| GET | `/regime` | Current market regime | `regime`, `data.vix`, `data.ad_ratio`, `config` |
| GET | `/ltp?symbols=A,B,C` | Batch live prices | `prices{sym: {ltp, day_high, day_low}}`, `failed[]` |
| GET | `/quote/<symbol>` | Single stock quote | Groww payload or yfinance equivalent |
| POST | `/refresh-token` | Force Groww token refresh | `ok` |
| GET | `/universe?index=NIFTY 500` | Stock list | `count`, `stocks[]`, `sectors[]` |
| POST | `/screen` | Start async screening | `job_id`, `total`, `scope` |
| GET | `/scan-progress/<job_id>` | Poll scan status | `status`, `progress`, `results[]`, `rejected[]`, `regime` |
| POST | `/cancel-scan/<job_id>` | Stop running scan | `ok` |
| GET | `/market-data` | Nifty/BankNifty/VIX + breadth + regime | `nifty50`, `banknifty`, `indiavix`, `breadth`, `regime` |
| POST | `/set-credentials` | Set Groww API key/secret | `ok` |
| POST | `/save-trades` | Save BUY/STRONG BUY signals to Supabase | `saved`, `inserted`, `updated` |
| GET | `/trades` | All trades + auto outcome reconciliation | `trades[]`, `outcome_reconciled` |
| GET | `/portfolio-summary` | Holdings, PnL, sector allocation | `summary`, `holdings[]`, `allocation[]` |
| GET | `/portfolio/recommendations` | List recommendations | `recommendations[]` |
| POST | `/portfolio/recommendations` | Create recommendation | `recommendation` |
| PUT | `/portfolio/recommendations/<id>` | Update recommendation | `recommendation` |
| DELETE | `/portfolio/recommendations/<id>` | Delete recommendation | `deleted` |
| GET | `/portfolio/positions` | List positions (with live LTP) | `positions[]` |
| POST | `/portfolio/positions` | Create position | `position` |
| PUT | `/portfolio/positions/<id>` | Update position | `position` |
| DELETE | `/portfolio/positions/<id>` | Delete position | `deleted` |
| GET | `/portfolio/history` | Event audit log | `history[]` |
| GET | `/portfolio/analytics` | KPIs: P&L, win rate | `analytics` |
| POST | `/check-outcomes` | Manually trigger outcome reconciliation | `checked`, `updated` |

---

## 7. Frontend Architecture

### 7.1 `nse-swing-screener.html` — Main SPA

A single-file, self-contained SPA (~106 KB). No build step required.

**Proxy URL configuration:**
```html
<meta name="proxy-url" content="https://your-backend.render.com">
```
Falls back to `http://localhost:5001` if the tag is absent.

**Three tabs:**

| Tab | Contents |
|-----|---------|
| Scan | Universe selector, filter params, Start Scan button, progress bar, results table |
| Trades | Active open trades, outcomes, PnL, sector breakdown |
| Portfolio | Holdings table, analytics cards, history timeline |

**Market strip** (top bar): Real-time Nifty50, BankNifty, India VIX prices + current regime badge.

### 7.2 `frontend/portfolio/` — Portfolio Module

A modular JavaScript component system embedded in the SPA.

#### Data Flow

```
window.initPortfolioModule()
    → mount(root)
        → createPortfolioStore()       # reactive state store
        → refreshAll()                 # parallel fetch of all data
            → portfolioApi.getRecommendations()
            → portfolioApi.getPositions()
            → portfolioApi.getHistory()
            → portfolioApi.getAnalytics()
        → store.subscribe(renderAll)   # re-render on state change
```

#### `portfolioStore.js` — Reactive Store

Implements a minimal observer pattern (similar to Svelte stores):

```javascript
const store = createPortfolioStore()
store.set({ loading: true })          // mutate + notify all listeners
store.subscribe(state => render(state)) // register listener
const unsubscribe = store.subscribe(fn) // returns cleanup function
```

**Default state:**
```javascript
{ loading: false, error: "", recommendations: [], positions: [], history: [], analytics: null }
```

#### `portfolioApi.js` — HTTP Client

All API calls share a single `request()` wrapper with:
- 12-second timeout (`AbortSignal.timeout`)
- `Content-Type: application/json` header
- Error extraction from `data.error` field

#### Components

| Component | Input Props | Renders |
|-----------|-------------|---------|
| `RecommendationsTable` | `recommendations[]`, `onCreate`, `onEdit`, `onDelete` | Table of Buy/Strong Buy recs with CRUD buttons |
| `ActivePositionsTable` | `positions[]`, `onCreate`, `onEdit`, `onDelete` | Holdings table with live P&L calculation |
| `PortfolioAnalyticsCards` | `analytics` | 6 KPI cards (positions, invested, value, unrealized PnL, realized PnL, win rate) |
| `HistoryTimeline` | `history[]` | Chronological event log table |

---

## 8. Caching & Global State

All state is in-process (RAM). Restarts clear all caches.

| Variable | TTL | Thread-safe | Purpose |
|----------|-----|-------------|---------|
| `_access_token` | 4 hours | `_cred_lock` | Groww bearer token |
| `_session_api_key/secret` | Session | `_cred_lock` | User-provided override credentials |
| `_regime_cache` | 15 min | `_regime_lock` | Market regime + VIX + A/D ratio |
| `_universe_cache` | 1 hour | `_universe_lock` | NSE stock list for a given scope |
| `_jobs` | Until restart | `_jobs_lock` | All scan job state (progress, results) |

> **Note:** Because `_jobs` lives in memory, scan jobs are lost on server restart. Use a single Gunicorn worker (`--workers 1`) to avoid job state being split across processes.

---

## 9. NSE Universe Fallback Chain

Fetching the NSE stock list uses a 3-layer fallback, tried in order:

```
Layer 1 — NSE Archive CSV (Akamai CDN)
    https://archives.nseindia.com/content/indices/ind_nifty500list.csv
    Often blocked on cloud IPs. Works on local development.

Layer 2 — NSE Direct API (requires cookie session)
    GET https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500
    _nse_session() pre-fetches the homepage to obtain required cookies.

Layer 3 — Hardcoded Fallback (always works)
    _NIFTY50_FALLBACK    — 50 stocks
    _NIFTY_NEXT50_FALLBACK — 50 stocks
    _NIFTY_MIDCAP_FALLBACK — ~100 stocks
    Combined: ~200 stocks covering most market-cap segments
```

**GitHub Mirror (optional):** Populate `NSE_GITHUB_URLS` dict with raw CSV URLs from a self-hosted GitHub repo for a reliable Layer 1 alternative.

---

## 10. Deployment Guide

### Environment Variables (Required)

| Variable | Description |
|----------|-------------|
| `SUPABASE_URL` | Supabase project URL (e.g. `https://xyz.supabase.co`) |
| `SUPABASE_KEY` | Supabase `anon` or `service_role` key |
| `GROWW_API_KEY` | Groww broker API key (optional — LTP falls back to yfinance) |
| `GROWW_API_SECRET` | Groww broker API secret (optional) |

### Backend (Render / Heroku)

```bash
# Procfile
web: gunicorn groww_proxy:app --timeout 120 --workers 1
```

> **Important:** Use `--workers 1`. Scan jobs are stored in process memory; multiple workers would split job state across processes.
> **Timeout 120s:** Scans of 500 stocks can take 60–90 seconds.

### Frontend (Netlify)

1. Deploy repo root to Netlify (configured in `netlify.toml`)
2. Edit `nse-swing-screener.html`:
   ```html
   <meta name="proxy-url" content="https://your-app.onrender.com">
   ```
3. All routes redirect to the SPA (configured in `netlify.toml` redirects)

### Database Setup

Run the migration file on your Supabase project:
```sql
-- In Supabase SQL Editor:
\i db/migrations/20260326_001_portfolio_module.sql
```

Then manually create the `trades` table (see Section 4.1 schema above) if it does not already exist.

### Running Locally

```bash
pip install flask flask-cors requests yfinance pandas numpy gunicorn

export SUPABASE_URL="https://xyz.supabase.co"
export SUPABASE_KEY="your-key"
export GROWW_API_KEY="your-key"       # optional
export GROWW_API_SECRET="your-secret" # optional

python groww_proxy.py
# → Listening on http://localhost:5001

# Open nse-swing-screener.html in a browser
# (or serve it: python -m http.server 8080)
```

---

*Documentation generated April 2026. Reflects `groww_proxy.py` v3 (Adaptive Regime Engine).*
