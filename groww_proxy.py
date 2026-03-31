"""
NSE Swing Screener — Adaptive Market Regime Engine v3
------------------------------------------------------
Run:  python groww_proxy.py
Deps: pip install flask flask-cors requests yfinance pandas numpy
"""

import csv, hashlib, io, os, time, requests, threading, uuid
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import yfinance as yf
    YF_OK = True
except ImportError:
    YF_OK = False
    print("[ERROR] yfinance missing — run: pip install yfinance pandas numpy")

# ── SUPABASE (plain requests — avoids httpx HTTP/2 issues on Render) ──
class _SBTable:
    def __init__(self, base, headers, table):
        self._base = base; self._headers = headers; self._table = table
        self._filters = {}; self._upd = None

    def select(self, cols="*"):
        self._cols = cols; return self
    def eq(self, col, val):
        self._filters[col] = f"eq.{val}"; return self
    def order(self, col, desc=False):
        self._order = f"{col}.{'desc' if desc else 'asc'}"; return self
    def insert(self, rows):
        self._rows = rows; return self
    def update(self, data):
        self._upd = data; return self
    def delete(self):
        self._del = True; return self

    def execute(self):
        url = f"{self._base}/rest/v1/{self._table}"
        params = {k: v for k, v in self._filters.items()}
        h = {**self._headers, "Prefer": "return=representation"}
        if getattr(self, "_del", False):
            r = requests.delete(url, headers=h, params=params, timeout=15)
        elif self._upd is not None:
            r = requests.patch(url, headers=h, params=params, json=self._upd, timeout=15)
        elif hasattr(self, "_rows"):
            r = requests.post(url, headers=h, json=self._rows, timeout=15)
        else:
            params["select"] = getattr(self, "_cols", "*")
            if hasattr(self, "_order"):
                params["order"] = self._order
            r = requests.get(url, headers=h, params=params, timeout=15)
        r.raise_for_status()
        data = r.json() if r.content else []
        return type("R", (), {"data": data if isinstance(data, list) else []})()


class _SBClient:
    def __init__(self, url, key):
        self._base = url.rstrip("/")
        self._h = {"apikey": key, "Authorization": f"Bearer {key}",
                   "Content-Type": "application/json"}
    def table(self, name):
        return _SBTable(self._base, self._h, name)


try:
    _sb = _SBClient(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    _probe = requests.get(
        f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/trades?select=id&limit=1",
        headers={"apikey": os.environ["SUPABASE_KEY"],
                 "Authorization": f"Bearer {os.environ['SUPABASE_KEY']}"},
        timeout=8)
    _probe.raise_for_status()
    SB_OK = True
    print("[Supabase] Connected OK")
except Exception as _sb_err:
    _sb = None; SB_OK = False
    print(f"[Supabase] Not configured — {_sb_err}")

# ── GROWW CREDENTIALS ────────────────────────────────────────────────
# Keep credentials out of source; load from env if available.
API_KEY    = os.environ.get("GROWW_API_KEY", "").strip()
API_SECRET = os.environ.get("GROWW_API_SECRET", "").strip()
BASE_URL   = "https://api.groww.in/v1"

app = Flask(__name__)
CORS(app)

# ── GROWW AUTH ───────────────────────────────────────────────────────
_access_token = None; _token_fetched_at = 0
_session_api_key = None; _session_api_secret = None
_cred_lock = threading.Lock()

def generate_checksum(secret, timestamp):
    return hashlib.sha256((secret + timestamp).encode()).hexdigest()

def _token_error_details(resp):
    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": (resp.text or "")[:400]}
    return {
        "status_code": resp.status_code,
        "payload": payload,
    }

def _request_access_token(key, secret):
    if not key or not secret:
        return None, {"message": "Missing API key/secret"}
    ts = str(int(time.time()))
    checksum = generate_checksum(secret, ts)
    bodies = [
        {"key_type": "approval", "checksum": checksum, "timestamp": ts},
        {"checksum": checksum, "timestamp": ts},
        {"keyType": "approval", "checksum": checksum, "timestamp": ts},
    ]
    attempt_errors = []
    for body in bodies:
        try:
            resp = requests.post(
                f"{BASE_URL}/token/api/access",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
                timeout=10)
        except Exception as e:
            attempt_errors.append({"request_body": body, "error": str(e)})
            continue
        details = _token_error_details(resp)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            details["message"] = "Groww rate limit reached. Please wait and retry."
            if retry_after:
                details["retry_after_seconds"] = retry_after
            details["request_body"] = body
            return None, details
        payload = details.get("payload") or {}
        token = payload.get("token")
        if token:
            return token, None
        details["request_body"] = body
        attempt_errors.append(details)
    return None, {
        "message": "Groww token endpoint rejected credentials",
        "attempts": attempt_errors[:3],
    }

def get_access_token(force=False):
    global _access_token, _token_fetched_at
    if _access_token and not force and (time.time() - _token_fetched_at) < 14400:
        return _access_token
    with _cred_lock:
        key = _session_api_key or API_KEY
        secret = _session_api_secret or API_SECRET
    tok, err = _request_access_token(key, secret)
    if tok:
        _access_token, _token_fetched_at = tok, time.time()
        print("[Auth] Token refreshed OK")
        return _access_token
    print(f"[Auth] Token fetch failed: {err}")
    return None

def groww_headers():
    return {"Authorization": f"Bearer {get_access_token()}",
            "Accept": "application/json", "X-API-VERSION": "1.0"}


# ══════════════════════════════════════════════════════════════════════
# MARKET REGIME DETECTION
# ══════════════════════════════════════════════════════════════════════

REGIME_BULL    = "BULL"
REGIME_NEUTRAL = "NEUTRAL"
REGIME_BEAR    = "BEAR"
REGIME_CRISIS  = "CRISIS"

_regime_cache = {"regime": None, "data": {}, "fetched_at": 0}
_regime_lock  = threading.Lock()
REGIME_TTL    = 900


def detect_market_regime():
    with _regime_lock:
        c = _regime_cache
        if c["regime"] is not None and time.time() - c["fetched_at"] < REGIME_TTL:
            return c

    regime_data = {}
    regime      = REGIME_NEUTRAL

    try:
        nifty = yf.Ticker("^NSEI").history(period="1y", interval="1d", auto_adjust=True)
        if not nifty.empty:
            closes    = nifty["Close"].dropna().values.astype(float)
            price     = float(closes[-1])
            ema50     = float(pd.Series(closes).ewm(span=50,  adjust=False).mean().iloc[-1])
            ema200    = float(pd.Series(closes).ewm(span=200, adjust=False).mean().iloc[-1])
            ret_10d   = (price - float(closes[-11])) / float(closes[-11]) * 100 if len(closes) > 11 else 0
            above_50  = price > ema50
            above_200 = price > ema200
            regime_data.update({
                "nifty_price": round(price, 2), "nifty_50dma": round(ema50, 2),
                "nifty_200dma": round(ema200, 2), "above_50dma": above_50,
                "above_200dma": above_200, "nifty_10d_ret": round(ret_10d, 2),
            })

        vix_hist = yf.Ticker("^INDIAVIX").history(period="5d", interval="1d", auto_adjust=True)
        vix_val  = float(vix_hist["Close"].dropna().iloc[-1]) if not vix_hist.empty else 18.0
        regime_data["vix"] = round(vix_val, 2)

        breadth  = _prefetch_market_breadth()
        ad_ratio = breadth.get("ratio", 1.0)
        regime_data["ad_ratio"] = ad_ratio

        above_50  = regime_data.get("above_50dma",  True)
        above_200 = regime_data.get("above_200dma", True)

        if vix_val > 28 and not above_200:
            regime = REGIME_CRISIS
        elif (not above_50) and vix_val > 20:
            regime = REGIME_BEAR
        elif above_50 and vix_val < 15 and ad_ratio > 1.5:
            regime = REGIME_BULL
        else:
            regime = REGIME_NEUTRAL

        regime_data["regime"] = regime
        print(f"[Regime] {regime} | VIX={vix_val:.1f} | above50={above_50} | AD={ad_ratio:.2f}")

    except Exception as e:
        print(f"[Regime] Detection failed: {e} — defaulting to NEUTRAL")
        regime_data = {"regime": REGIME_NEUTRAL, "vix": 18, "error": str(e)}

    result = {"regime": regime, "data": regime_data, "fetched_at": time.time()}
    with _regime_lock:
        _regime_cache.update(result)
    return result


def get_regime_config(regime):
    configs = {
        REGIME_BULL: {
            "rsi_min": 45, "rsi_max": 68, "vol_min": 1.2,
            "rs_required": False, "higher_lows_req": False,
            "no_gap_req": False, "bb_filter_req": False, "atr_max_pct": 7.0, "min_score": 50,
            "w_rsi": 20, "w_volume": 20, "w_ema": 15, "w_macd": 15,
            "w_rs": 10, "w_adx": 8, "w_delivery": 7, "w_pattern": 5,
            "strong_buy_score": 75, "buy_score": 50, "strong_buy_vol": 1.8,
            "sl_atr_mult": 1.5, "t1_atr_mult": 2.5, "t2_atr_mult": 3.5,
            "min_rr": 1.5, "allow_strong_buy": True,
        },
        REGIME_NEUTRAL: {
            "rsi_min": 45, "rsi_max": 68, "vol_min": 1.2,
            "rs_required": False, "higher_lows_req": False,
            "no_gap_req": False, "bb_filter_req": False, "atr_max_pct": 6.0, "min_score": 52,
            "w_rsi": 20, "w_volume": 20, "w_ema": 15, "w_macd": 15,
            "w_rs": 12, "w_adx": 8, "w_delivery": 7, "w_pattern": 3,
            "strong_buy_score": 75, "buy_score": 52, "strong_buy_vol": 1.8,
            "sl_atr_mult": 1.5, "t1_atr_mult": 2.5, "t2_atr_mult": 3.0,
            "min_rr": 1.5, "allow_strong_buy": True,
        },
        REGIME_BEAR: {
            "rsi_min": 46, "rsi_max": 62, "vol_min": 1.2,
            "rs_required": True, "higher_lows_req": False,
            "no_gap_req": True, "bb_filter_req": False, "atr_max_pct": 5.0, "min_score": 60,
            "w_rsi": 15, "w_volume": 25, "w_ema": 10, "w_macd": 12,
            "w_rs": 18, "w_adx": 10, "w_delivery": 7, "w_pattern": 3,
            "strong_buy_score": 78, "buy_score": 62, "strong_buy_vol": 2.0,
            "sl_atr_mult": 1.2, "t1_atr_mult": 2.0, "t2_atr_mult": 2.8,
            "min_rr": 1.8, "allow_strong_buy": True,
        },
        REGIME_CRISIS: {
            "rsi_min": 48, "rsi_max": 60, "vol_min": 2.0,
            "rs_required": True, "higher_lows_req": True,
            "no_gap_req": True, "bb_filter_req": True, "atr_max_pct": 4.5, "min_score": 68,
            "w_rsi": 10, "w_volume": 25, "w_ema": 8, "w_macd": 8,
            "w_rs": 22, "w_adx": 12, "w_delivery": 8, "w_pattern": 7,
            "strong_buy_score": 999, "buy_score": 68, "strong_buy_vol": 2.5,
            "sl_atr_mult": 1.0, "t1_atr_mult": 1.8, "t2_atr_mult": 2.5,
            "min_rr": 1.8, "allow_strong_buy": False,
        },
    }
    return configs.get(regime, configs[REGIME_NEUTRAL])


# ══════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════

def calc_rsi(closes, period=14):
    s = pd.Series(closes, dtype=float)
    delta = s.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def calc_ema(closes, period):
    return pd.Series(closes, dtype=float).ewm(span=period, adjust=False).mean()

def calc_macd_signal(closes, fast=12, slow=26, sig_period=9):
    closes = pd.Series(closes, dtype=float)
    if len(closes) < slow + sig_period:
        return "neutral"
    ema_f = closes.ewm(span=fast, adjust=False).mean()
    ema_s = closes.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=sig_period, adjust=False).mean()
    hist  = macd - sig
    if float(macd.iloc[-1]) > float(sig.iloc[-1]) and float(hist.iloc[-1]) > 0:
        return "bullish"
    if float(macd.iloc[-1]) < float(sig.iloc[-1]) and float(hist.iloc[-1]) < 0:
        return "bearish"
    return "neutral"

def calc_macd_full(closes, fast=12, slow=26, sig_period=9):
    """Returns (signal, histogram_val, crossover_in_last_5d) per new requirements."""
    closes = pd.Series(closes, dtype=float)
    if len(closes) < slow + sig_period:
        return "neutral", 0.0, False
    ema_f = closes.ewm(span=fast, adjust=False).mean()
    ema_s = closes.ewm(span=slow, adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=sig_period, adjust=False).mean()
    hist  = macd - sig
    hist_now = float(hist.iloc[-1])
    macd_now = float(macd.iloc[-1]); sig_now = float(sig.iloc[-1])
    # Crossover in last 5 days: MACD crossed above signal
    crossover_5d = False
    n = min(5, len(macd) - 1)
    for i in range(-n, 0):
        if float(macd.iloc[i]) > float(sig.iloc[i]) and float(macd.iloc[i-1]) <= float(sig.iloc[i-1]):
            crossover_5d = True; break
    if macd_now > sig_now and hist_now > 0:
        signal = "bullish"
    elif macd_now < sig_now and hist_now < 0:
        signal = "bearish"
    else:
        signal = "neutral"
    return signal, round(hist_now, 4), crossover_5d

def calc_atr(highs, lows, closes, period=14):
    h = np.array(highs, dtype=float)
    l = np.array(lows,  dtype=float)
    c = np.array(closes, dtype=float)
    if len(c) < period + 1:
        return None, None
    tr  = np.maximum(h[1:] - l[1:],
          np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr     = float(pd.Series(tr).ewm(alpha=1/period, adjust=False).mean().iloc[-1])
    atr_pct = atr / c[-1] * 100
    return round(atr, 2), round(atr_pct, 2)

def calc_adx(highs, lows, closes, period=14):
    h = np.array(highs,  dtype=float)
    l = np.array(lows,   dtype=float)
    c = np.array(closes, dtype=float)
    if len(c) < period * 2 + 1:
        return None
    tr    = np.maximum(h[1:] - l[1:],
            np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    up    = h[1:] - h[:-1]
    down  = l[:-1] - l[1:]
    dm_p  = np.where((up > down) & (up > 0),   up,   0.0)
    dm_m  = np.where((down > up) & (down > 0), down, 0.0)
    atr14 = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean()
    dip14 = pd.Series(dm_p).ewm(alpha=1/period, adjust=False).mean()
    dim14 = pd.Series(dm_m).ewm(alpha=1/period, adjust=False).mean()
    di_p  = 100 * dip14 / atr14.replace(0, np.nan)
    di_m  = 100 * dim14 / atr14.replace(0, np.nan)
    dx    = 100 * np.abs(di_p - di_m) / (di_p + di_m).replace(0, np.nan)
    adx   = dx.ewm(alpha=1/period, adjust=False).mean()
    val   = float(adx.iloc[-1])
    return round(val, 1) if not np.isnan(val) else None

def calc_bollinger_position(closes, period=20, std_dev=2):
    s = pd.Series(closes, dtype=float)
    if len(s) < period:
        return "unknown", None, None, None
    mid   = float(s.rolling(period).mean().iloc[-1])
    std   = float(s.rolling(period).std().iloc[-1])
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    price = float(s.iloc[-1])
    if price > upper:   pos = "above_upper"
    elif price >= mid:  pos = "sweet_spot"
    else:               pos = "below_middle"
    return pos, round(upper, 2), round(mid, 2), round(lower, 2)

def check_higher_lows(lows, n=3):
    vals = [float(x) for x in lows[-(n+1):] if not np.isnan(float(x))]
    if len(vals) < n:
        return False
    vals = vals[-n:]
    return all(vals[i] > vals[i-1] for i in range(1, n))

def check_no_gap_down(opens, closes, threshold=0.015, n=5):
    o = list(opens[-(n):]); c = list(closes[-(n+1):-1])
    for op, pc in zip(o, c):
        if float(pc) > 0 and (float(pc) - float(op)) / float(pc) > threshold:
            return False
    return True

def calc_relative_strength(stock_closes, nifty_closes):
    """Step 2: Relative strength — 5-day AND 10-day vs Nifty (OR condition)."""
    res = {"s_ret_5d": None, "n_ret_5d": None, "rs_5d_ok": None,
           "s_ret_10d": None, "n_ret_10d": None, "rs_10d_ok": None, "rs_ok": False}
    if len(stock_closes) >= 6 and len(nifty_closes) >= 6:
        s5 = (float(stock_closes[-1]) - float(stock_closes[-6])) / float(stock_closes[-6]) * 100
        n5 = (float(nifty_closes[-1]) - float(nifty_closes[-6])) / float(nifty_closes[-6]) * 100
        res.update({"s_ret_5d": round(s5, 2), "n_ret_5d": round(n5, 2),
                    "rs_5d_ok": s5 > n5 + 1.5})  # outperform by +1.5%
    if len(stock_closes) >= 11 and len(nifty_closes) >= 11:
        s10 = (float(stock_closes[-1]) - float(stock_closes[-11])) / float(stock_closes[-11]) * 100
        n10 = (float(nifty_closes[-1]) - float(nifty_closes[-11])) / float(nifty_closes[-11]) * 100
        res.update({"s_ret_10d": round(s10, 2), "n_ret_10d": round(n10, 2),
                    "rs_10d_ok": s10 > n10 + 2.0})  # outperform by +2%
    r5 = res.get("rs_5d_ok"); r10 = res.get("rs_10d_ok")
    res["rs_ok"] = bool((r5 is True) or (r10 is True))
    return res

def calc_relative_strength_10d(stock_closes, nifty_closes):
    """Backward-compat alias — use calc_relative_strength instead."""
    rs = calc_relative_strength(stock_closes, nifty_closes)
    return rs["s_ret_10d"], rs["n_ret_10d"], rs["rs_10d_ok"]

def calc_rs_vs_sector(stock_closes, sector_closes):
    if sector_closes is None or len(sector_closes) < 11 or len(stock_closes) < 11:
        return None, None, None
    sc      = stock_closes[-11:]; sec = sector_closes[-11:]
    s_ret   = (float(sc[-1]) - float(sc[0])) / float(sc[0]) * 100
    sec_ret = (float(sec[-1]) - float(sec[0])) / float(sec[0]) * 100
    return round(s_ret, 2), round(sec_ret, 2), s_ret > sec_ret

def calc_52wk_proximity(closes_1y, highs_1y):
    price     = float(closes_1y[-1])
    hi52      = float(np.nanmax(highs_1y))
    pct_below = (hi52 - price) / hi52 * 100 if hi52 > 0 else 0
    return round(pct_below, 1)

def calc_52wk_position_pctile(closes_1y, highs_1y, lows_1y):
    """Position in 52-week range as percentile (0–100). Step 8/9."""
    price = float(closes_1y[-1])
    hi52  = float(np.nanmax(highs_1y))
    lo52  = float(np.nanmin(lows_1y))
    if hi52 <= lo52: return 50
    return round((price - lo52) / (hi52 - lo52) * 100, 1)

def check_ma_structure(closes, ema20_s, ema50_s, ema200_s, rsi_s):
    """Step 4: MA structure — at least ONE of 4 OR-conditions must be true."""
    price = float(closes[-1])
    e20   = float(ema20_s.iloc[-1]); e50 = float(ema50_s.iloc[-1]); e200 = float(ema200_s.iloc[-1])
    # a) Price > 20EMA AND 20EMA > 50EMA
    cond_a = price > e20 and e20 > e50
    # b) Price crossed above 20EMA within last 3 trading days
    cond_b = False
    n = min(len(closes), len(ema20_s))
    for i in range(-min(3, n-1), 0):
        if float(closes[i]) > float(ema20_s.iloc[i]) and float(closes[i-1]) <= float(ema20_s.iloc[i-1]):
            cond_b = True; break
    # c) Price > 200EMA
    cond_c = price > e200
    # d) Price between 20EMA and 50EMA with RSI rising
    between = (min(e20, e50) <= price <= max(e20, e50))
    rsi_rising = len(rsi_s) >= 4 and float(rsi_s.iloc[-1]) > float(rsi_s.iloc[-4])
    cond_d = between and rsi_rising
    met = sum([cond_a, cond_b, cond_c, cond_d])
    return met >= 1, met, {"a": cond_a, "b": cond_b, "c": cond_c, "d": cond_d}

# Step 7: Sector bonus scoring
_SECTOR_BONUS_RULES = [
    (["energy", "coal", "oil & gas", "oil and gas", "crude", "petroleum", "power"], 3),
    (["pharma", "pharmaceutical", "healthcare", "health care", "medicine", "drug"], 3),
    (["metal", "steel", "mining", "aluminium", "aluminum", "copper", "zinc", "iron"], 2),
    (["psu", "defence", "defense", "railway", "railroad", "public sector", "shipbuilding"], 2),
    (["fmcg", "consumer staples", "consumer goods", "food", "beverage", "household"], 1),
    (["information technology", "software", "computer", "tech"], -1),
    (["realty", "real estate", "housing", "property"], -2),
]

def get_sector_bonus(sector):
    s = (sector or "").lower()
    for keywords, pts in _SECTOR_BONUS_RULES:
        if any(kw in s for kw in keywords):
            return pts
    return 0

# IT sector symbols to hard-exclude (Step 1)
_IT_SECTOR_NAMES = {"information technology", "it", "software", "technology"}

def fetch_fundamentals_quick(symbol):
    """Step 8: Fetch PE, D/E, ROE, market cap. Slow — use only for shortlisted stocks."""
    try:
        info = yf.Ticker(f"{symbol}.NS").info
        pe   = info.get("trailingPE"); roe = info.get("returnOnEquity"); de = info.get("debtToEquity")
        mcap = info.get("marketCap", 0)
        return {
            "pe": round(float(pe), 1) if pe else None,
            "roe": round(float(roe) * 100, 1) if roe else None,  # store as %
            "de":  round(float(de), 2) if de else None,
            "market_cap_cr": round(mcap / 1e7, 0) if mcap else None,  # in crore
            "pe_flag": bool(pe and float(pe) > 60),
            "de_flag": bool(de and float(de) > 2.0),
            "roe_ok":  bool(roe and float(roe) > 0.12),
            "mcap_ok": bool(mcap and mcap > 5_000 * 1e7),
        }
    except Exception:
        return {}

def generate_rationale(r):
    """Step 10: Generate 'Why this stock' and 'Key Risk' strings."""
    reasons = []
    # Relative strength
    s5 = r.get("s_ret_5d") or 0; n5 = r.get("n_ret_5d") or 0; diff5 = round(s5 - n5, 1)
    s10 = r.get("s_ret_10d") or 0; n10 = r.get("n_ret_10d") or 0; diff10 = round(s10 - n10, 1)
    if diff5 >= 1.5:
        reasons.append(f"Outperforming Nifty by {diff5:+.1f}% (5d) — institutional accumulation")
    elif diff10 >= 2:
        reasons.append(f"Outperforming Nifty by {diff10:+.1f}% (10d) — sustained RS strength")
    # MACD
    if r.get("macd") == "bullish":
        reasons.append("MACD bullish — positive momentum confirmed")
    elif r.get("macd_crossover_5d"):
        reasons.append("Fresh MACD crossover (last 5d) — early momentum entry")
    # MA
    ma_d = r.get("ma_detail", {})
    if ma_d.get("a"):
        reasons.append("Price > EMA20 > EMA50 — short-term bullish alignment")
    elif ma_d.get("c"):
        reasons.append("Price above 200 EMA — long-term uptrend intact")
    elif ma_d.get("b"):
        reasons.append("Price just crossed above EMA20 — breakout signal")
    # Sector
    sb = r.get("sector_bonus", 0)
    if sb >= 2:
        reasons.append(f"Sector '{r.get('sector','')}' has FII inflow/geopolitical tailwind")
    # RSI
    rsi = r.get("rsi", 0)
    if 55 <= rsi <= 65:
        reasons.append(f"RSI {rsi:.0f} — momentum building in sweet spot")
    # Key risk
    fund = r.get("fundamentals") or {}
    pe = fund.get("pe"); de = fund.get("de"); atr_pct = r.get("atr_pct", 0)
    pct_below = r.get("pct_below_52wk", 50)
    if pe and pe > 60:
        risk = f"High valuation (PE {pe:.0f}x) — expensive, use caution"
    elif de and de > 2.0:
        risk = f"High leverage (D/E {de:.1f}) — interest burden risk"
    elif atr_pct and atr_pct > 4:
        risk = f"High volatility (ATR {atr_pct:.1f}%) — wider SL may be needed"
    elif pct_below < 5:
        risk = "Near 52-week high — limited upside, watch for reversal"
    else:
        risk = "Market reversal could invalidate setup — honour stop loss strictly"
    return " | ".join(reasons[:3]) if reasons else "Passes all technical filters", risk

def calc_volume_quality(volumes, n_recent=3, n_avg=20):
    if len(volumes) < n_avg + n_recent:
        return 0.0, False
    avg    = float(np.mean(volumes[-(n_avg + n_recent):-(n_recent)]))
    recent = volumes[-n_recent:]
    ratios = [float(v) / avg for v in recent if avg > 0]
    avg_ratio   = float(np.mean(ratios)) if ratios else 0.0
    consistency = all(r > 1.0 for r in ratios)
    return round(avg_ratio, 2), consistency

def calc_price_momentum(closes, periods=(5, 10, 20)):
    result = {}; price = float(closes[-1])
    for p in periods:
        if len(closes) > p:
            base = float(closes[-(p+1)])
            result[f"{p}d"] = round((price - base) / base * 100, 2) if base > 0 else 0
        else:
            result[f"{p}d"] = None
    return result

def detect_consolidation_breakout(closes, highs, lows, lookback=15):
    if len(closes) < lookback + 2:
        return False, None
    past_highs = highs[-(lookback+1):-1]; past_lows = lows[-(lookback+1):-1]
    h_max      = float(np.max(past_highs)); l_min = float(np.min(past_lows))
    range_pct  = (h_max - l_min) / l_min * 100 if l_min > 0 else 999
    is_breakout = range_pct < 8.0 and float(closes[-1]) > h_max * 1.005
    return is_breakout, round(range_pct, 2)


# ══════════════════════════════════════════════════════════════════════
# NSE UNIVERSE
# ══════════════════════════════════════════════════════════════════════

_universe_cache = {"data": None, "index": None, "fetched_at": 0}
_universe_lock  = threading.Lock()
UNIVERSE_TTL    = 3600

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
    "X-Requested-With": "XMLHttpRequest",
}

# NSE archive CSVs — attempt 1 (Akamai-blocked on most cloud IPs, worth trying)
NSE_CSV_URLS = {
    "NIFTY 50":          "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY 100":         "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY 200":         "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY 500":         "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "NIFTY NEXT 50":     "https://archives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "NIFTY MIDCAP 150":  "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "NIFTY SMALLCAP 250":"https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}

# Alternative NSE CSV endpoints (some may work from cloud IPs)
# Add your own GitHub repo URL here if you host the CSVs:
# https://raw.githubusercontent.com/YOUR_USERNAME/nse-data/main/nifty500.csv
NSE_GITHUB_URLS = {}   # populated once you host the CSVs; see README

# Hardcoded Nifty 50 fallback — always works, only needs updating quarterly
_NIFTY50_FALLBACK = {
    "RELIANCE":    "Energy","TCS":         "Information Technology",
    "HDFCBANK":    "Banking","BHARTIARTL":  "Telecom",
    "ICICIBANK":   "Banking","INFY":        "Information Technology",
    "SBIN":        "Banking","HINDUNILVR":  "FMCG",
    "ITC":         "FMCG","KOTAKBANK":   "Banking",
    "LT":          "Construction","BAJFINANCE":  "Financial Services",
    "HCLTECH":     "Information Technology","MARUTI":      "Automobile",
    "AXISBANK":    "Banking","SUNPHARMA":   "Pharmaceutical",
    "TITAN":       "Consumer Goods","ASIANPAINT":  "Consumer Goods",
    "ULTRACEMCO":  "Cement","WIPRO":        "Information Technology",
    "NTPC":        "Energy","ONGC":         "Energy",
    "POWERGRID":   "Energy","COALINDIA":    "Metals & Mining",
    "M&M":         "Automobile","BAJAJ-AUTO":  "Automobile",
    "EICHERMOT":   "Automobile","HEROMOTOCO":  "Automobile",
    "TATAMOTORS":  "Automobile","TATASTEEL":   "Metals",
    "JSWSTEEL":    "Metals","HINDALCO":    "Metals",
    "ADANIENT":    "Diversified","ADANIPORTS":  "Logistics",
    "INDUSINDBK":  "Banking","GRASIM":      "Diversified",
    "CIPLA":       "Pharmaceutical","DRREDDY":     "Pharmaceutical",
    "DIVISLAB":    "Pharmaceutical","BPCL":        "Energy",
    "TATACONSUM":  "FMCG","NESTLEIND":   "FMCG",
    "BRITANNIA":   "FMCG","LTIM":         "Information Technology",
    "TECHM":       "Information Technology","HDFCLIFE":    "Insurance",
    "SBILIFE":     "Insurance","BAJAJFINSV":  "Financial Services",
}

_NIFTY_NEXT50_FALLBACK = {
    "ABB":"Capital Goods","ADANIGREEN":"Energy","ADANITRANS":"Energy",
    "AMBUJACEM":"Cement","AUROPHARMA":"Pharmaceutical","BANDHANBNK":"Banking",
    "BERGEPAINT":"Consumer Goods","BEL":"Capital Goods","BOSCHLTD":"Automobile",
    "CHOLAFIN":"Financial Services","COLPAL":"FMCG","CONCOR":"Logistics",
    "CUMMINSIND":"Capital Goods","DABUR":"FMCG","DMART":"Retail",
    "DLF":"Real Estate","FEDERALBNK":"Banking","GAIL":"Energy",
    "GODREJCP":"FMCG","GODREJPROP":"Real Estate","HAVELLS":"Capital Goods",
    "ICICIPRULI":"Insurance","IDFCFIRSTB":"Banking","IGL":"Energy",
    "INDHOTEL":"Hospitality","INDUSTOWER":"Telecom","IRCTC":"Logistics",
    "JINDALSTEL":"Metals","JUBLFOOD":"Consumer Goods","LICI":"Insurance",
    "LUPIN":"Pharmaceutical","MARICO":"FMCG","MUTHOOTFIN":"Financial Services",
    "NAUKRI":"Information Technology","NMDC":"Metals & Mining","OFSS":"Information Technology",
    "PAGEIND":"Consumer Goods","PERSISTENT":"Information Technology","PIIND":"Chemicals",
    "PNB":"Banking","POLYCAB":"Capital Goods","RECLTD":"Financial Services",
    "SAIL":"Metals","SHREECEM":"Cement","SIEMENS":"Capital Goods",
    "SUNTV":"Media","TORNTPHARM":"Pharmaceutical","TRENT":"Retail",
    "UPL":"Chemicals","VEDL":"Metals","VBL":"FMCG",
    "VOLTAS":"Consumer Goods","ZOMATO":"Consumer Services","NYKAA":"Retail",
    "PAYTM":"Financial Services","POLICYBZR":"Insurance","MOTHERSON":"Automobile",
}

_NIFTY_MIDCAP_FALLBACK = {
    "AAVAS":"Financial Services","ACC":"Cement","ABCAPITAL":"Financial Services",
    "ALKEM":"Pharmaceutical","AMARAJABAT":"Automobile","APOLLOTYRE":"Automobile",
    "ASHOKLEY":"Automobile","ASTRAL":"Construction","AUBANK":"Banking",
    "BALKRISIND":"Automobile","BATAINDIA":"Consumer Goods","BLUEDART":"Logistics",
    "BRIGADE":"Real Estate","CEATLTD":"Automobile","CGPOWER":"Capital Goods",
    "COFORGE":"Information Technology","CROMPTON":"Consumer Goods","CYIENT":"Information Technology",
    "DEEPAKNITRI":"Chemicals","DELHIVERY":"Logistics","DIXON":"Consumer Goods",
    "ELGIEQUIP":"Capital Goods","EMAMILTD":"FMCG","ENDURANCE":"Automobile",
    "ESCORTS":"Automobile","EXIDEIND":"Automobile","FINEORG":"Chemicals",
    "FORTIS":"Healthcare","GLAND":"Pharmaceutical","GLAXO":"Pharmaceutical",
    "GNFC":"Chemicals","GODAWARI":"Metals","GRINDWELL":"Capital Goods",
    "HFCL":"Telecom","HLEGLAS":"Capital Goods","HONAUT":"Capital Goods",
    "INDIAMART":"Information Technology","IPCALAB":"Pharmaceutical","JBCHEPHARM":"Pharmaceutical",
    "JKCEMENT":"Cement","KAJARIACER":"Construction","KALPATPOWR":"Energy",
    "KANSAINER":"Consumer Goods","KEC":"Capital Goods","KNRCON":"Construction",
    "KPITTECH":"Information Technology","LAURUSLABS":"Pharmaceutical","LTF":"Financial Services",
    "LTTS":"Information Technology","MACROTECH":"Real Estate","MAHABANK":"Banking",
    "MANAPPURAM":"Financial Services","MAXHEALTH":"Healthcare","MCX":"Financial Services",
    "MGL":"Energy","MPHASIS":"Information Technology","MRF":"Automobile",
    "NCC":"Construction","NOCIL":"Chemicals","OBEROIRLTY":"Real Estate",
    "PATANJALI":"FMCG","PETRONET":"Energy","PFIZER":"Pharmaceutical",
    "PHOENIXLTD":"Real Estate","PRESTIGE":"Real Estate","PVR":"Media",
    "RBLBANK":"Banking","ROUTE":"Information Technology","SANOFI":"Pharmaceutical",
    "SHYAMMETL":"Metals","SONACOMS":"Automobile","SRF":"Chemicals",
    "SUNDRMFAST":"Automobile","SUNTECK":"Real Estate","SUPREMEIND":"Chemicals",
    "TATACHEM":"Chemicals","TATACOMM":"Telecom","TATAELXSI":"Information Technology",
    "THERMAX":"Capital Goods","TORNTPOWER":"Energy","TRIDENT":"Textiles",
    "TVSMOTORS":"Automobile","UNIONBANK":"Banking","VGUARD":"Consumer Goods",
    "VINATI":"Chemicals","WELCORP":"Metals","WHIRLPOOL":"Consumer Goods",
    "ZEEL":"Media","ZENSAR":"Information Technology","APLAPOLLO":"Metals",
    "CAMPUS":"Consumer Goods","CMSINFO":"Information Technology","DALBHARAT":"Cement",
    "ERIS":"Pharmaceutical","GLENMARK":"Pharmaceutical","HOMEFIRST":"Financial Services",
    "KFINTECH":"Financial Services","KRBL":"FMCG","LATENTVIEW":"Information Technology",
    "NUVOCO":"Cement","RAMCOCEM":"Cement","RATNAMANI":"Metals",
    "REDINGTON":"Information Technology","SAFARI":"Consumer Goods","SCHAEFFLER":"Automobile",
    "SHOPERSTOP":"Retail","STARHEALTH":"Insurance","STLTECH":"Telecom",
    "SUVENPHAR":"Pharmaceutical","SWANENERGY":"Energy","TANLA":"Information Technology",
    "TIINDIA":"Automobile","TIMKEN":"Capital Goods","VMART":"Retail",
    "WELSPUNIND":"Textiles","YESBANK":"Banking","ZYDUSWELL":"Pharmaceutical",
}

def _build_fallback_universe(scope):
    """Return a hardcoded universe when all live sources fail."""
    base = {s: {"symbol": s, "name": s, "sector": sec, "industry": sec}
            for s, sec in _NIFTY50_FALLBACK.items()}
    if scope in ("NIFTY 50",):
        return list(base.values())
    # Expand for larger indices
    extra = {**_NIFTY_NEXT50_FALLBACK}
    if scope in ("NIFTY 200", "NIFTY 500", "ALL", "NIFTY MIDCAP 150", "NIFTY SMALLCAP 250"):
        extra.update(_NIFTY_MIDCAP_FALLBACK)
    merged = {**base}
    merged.update({s: {"symbol": s, "name": s, "sector": sec, "industry": sec}
                   for s, sec in extra.items()})
    print(f"[Universe] Using hardcoded fallback: {len(merged)} stocks for {scope}")
    return list(merged.values())

UNIVERSE_INDICES = {
    "NIFTY 50":  ["NIFTY 50"],
    "NIFTY 100": ["NIFTY 100"],
    "NIFTY 200": ["NIFTY 200"],
    "NIFTY 500": ["NIFTY 500"],
    "ALL":       ["NIFTY 500", "NIFTY MIDCAP 150", "NIFTY SMALLCAP 250", "NIFTY NEXT 50"],
}

SECTOR_YF = {
    "INFORMATION TECHNOLOGY": "^CNXIT", "IT": "^CNXIT",
    "METALS - FERROUS": "^CNXMETAL", "METALS": "^CNXMETAL", "NON FERROUS METALS": "^CNXMETAL",
    "BANKING": "^NSEBANK", "PSU BANK": "^CNXPSUBANK",
    "FINANCIAL SERVICES": "^CNXFIN", "PHARMA": "^CNXPHARMA",
    "PHARMACEUTICAL": "^CNXPHARMA", "HEALTHCARE": "^CNXPHARMA",
    "AUTOMOBILE": "^CNXAUTO", "AUTOMOBILES": "^CNXAUTO", "AUTO": "^CNXAUTO",
    "FMCG": "^CNXFMCG", "CONSUMER GOODS": "^CNXFMCG",
    "ENERGY": "^CNXENERGY", "OIL & GAS": "^CNXENERGY",
    "REALTY": "^CNXREALTY", "MEDIA": "^CNXMEDIA",
    "INFRASTRUCTURE": "^CNXINFRA", "CONSTRUCTION": "^CNXINFRA",
}


def _nse_session():
    s = requests.Session()
    try:
        s.get("https://www.nseindia.com",
              headers={"User-Agent": NSE_HEADERS["User-Agent"]}, timeout=10)
        time.sleep(0.5)
    except Exception:
        pass
    return s

def _fetch_index(session, index_name):
    try:
        r = session.get("https://www.nseindia.com/api/equity-stockIndices",
                        params={"index": index_name}, headers=NSE_HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
        stocks = {}
        for item in r.json().get("data", []):
            sym = item.get("symbol", "").strip()
            if not sym or len(sym) > 20:
                continue
            meta   = item.get("meta", {}) or {}
            sector = (meta.get("sector") or "Unknown").strip() or "Unknown"
            stocks[sym] = {
                "symbol": sym,
                "name":   (meta.get("companyName") or sym).strip(),
                "sector": sector,
                "industry": (meta.get("industry") or "").strip(),
            }
        return stocks
    except Exception as e:
        print(f"[NSE] {index_name}: {e}")
        return {}

def _parse_nse_csv(text):
    """Parse NSE-format CSV (Symbol, Company Name, Industry columns)."""
    stocks = {}
    try:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            sym = (row.get("Symbol") or "").strip()
            if not sym or len(sym) > 20:
                continue
            sector = (row.get("Industry") or "Unknown").strip() or "Unknown"
            stocks[sym] = {
                "symbol":   sym,
                "name":     (row.get("Company Name") or sym).strip(),
                "sector":   sector,
                "industry": sector,
            }
    except Exception:
        pass
    return stocks

def _fetch_csv_url(url, label):
    """Fetch a CSV from any URL; return parsed dict or {}."""
    try:
        r = requests.get(url, timeout=20,
                         headers={"User-Agent": NSE_HEADERS["User-Agent"],
                                  "Accept": "text/csv,text/plain,*/*"})
        if r.status_code == 200 and r.text.strip():
            stocks = _parse_nse_csv(r.text)
            if stocks:
                print(f"[{label}] {len(stocks)} stocks")
                return stocks
        print(f"[{label}] HTTP {r.status_code} or empty")
    except Exception as e:
        print(f"[{label}] {e}")
    return {}

def _fetch_index_csv(index_name):
    """Try NSE archive CSV, then GitHub mirror."""
    # 1. NSE archives (blocked on many cloud IPs, worth a try)
    url = NSE_CSV_URLS.get(index_name)
    if url:
        data = _fetch_csv_url(url, f"NSE-CSV/{index_name}")
        if data:
            return data
    # 2. GitHub mirror
    gurl = NSE_GITHUB_URLS.get(index_name)
    if gurl:
        data = _fetch_csv_url(gurl, f"GitHub/{index_name}")
        if data:
            return data
    return {}

def fetch_universe(scope="NIFTY 500"):
    indices = UNIVERSE_INDICES.get(scope, ["NIFTY 500"])
    merged = {}
    for idx in indices:
        # Layer 1: CSV sources (NSE archive + GitHub mirror)
        data = _fetch_index_csv(idx)
        if data:
            merged.update(data)
            continue
        # Layer 2: NSE API with cookie session
        print(f"[Universe] CSV/GitHub failed for {idx}, trying NSE API…")
        session = _nse_session()
        api_data = _fetch_index(session, idx)
        if api_data:
            merged.update(api_data)
            time.sleep(0.3)
            continue
        # Layer 3: hardcoded fallback (always works)
        print(f"[Universe] NSE API also failed for {idx}, using hardcoded fallback")
        for stock in _build_fallback_universe(scope):
            merged[stock["symbol"]] = stock
        break   # fallback covers entire scope, skip remaining indices
    result = list(merged.values())
    print(f"[Universe] {scope}: {len(result)} stocks total")
    return result

def get_universe(scope="NIFTY 500"):
    with _universe_lock:
        c = _universe_cache
        if (c["data"] is not None and c["index"] == scope
                and time.time() - c["fetched_at"] < UNIVERSE_TTL):
            return c["data"]
    data = fetch_universe(scope)
    with _universe_lock:
        _universe_cache.update({"data": data, "index": scope, "fetched_at": time.time()})
    return data


# ══════════════════════════════════════════════════════════════════════
# CONTEXT PRE-FETCH
# ══════════════════════════════════════════════════════════════════════

def _prefetch_nifty_closes():
    try:
        hist = yf.Ticker("^NSEI").history(period="3mo", interval="1d", auto_adjust=True)
        return hist["Close"].dropna().values.astype(float)
    except Exception as e:
        print(f"[Ctx] Nifty: {e}"); return np.array([])

def _prefetch_sector_closes():
    result = {}; fetched_syms = {}
    for sector, yf_sym in SECTOR_YF.items():
        if yf_sym in fetched_syms:
            result[sector] = fetched_syms[yf_sym]; continue
        try:
            hist   = yf.Ticker(yf_sym).history(period="2mo", interval="1d", auto_adjust=True)
            closes = hist["Close"].dropna().values.astype(float)
            fetched_syms[yf_sym] = closes; result[sector] = closes
        except Exception:
            result[sector] = None
    return result

def _prefetch_sector_ema(sectors):
    needed = {}
    for s in sectors:
        yf_sym = SECTOR_YF.get(s.upper())
        if yf_sym: needed.setdefault(yf_sym, set()).add(s)
    result = {}
    for yf_sym, sector_names in needed.items():
        try:
            hist   = yf.Ticker(yf_sym).history(period="2mo", interval="1d", auto_adjust=True)
            closes = hist["Close"].dropna().values.astype(float)
            above  = len(closes) >= 20 and float(closes[-1]) > float(calc_ema(closes, 20).iloc[-1])
        except Exception:
            above = True
        for s in sector_names: result[s] = above
    return result

def _prefetch_earnings_symbols():
    syms = set()
    try:
        session = _nse_session()
        r = session.get("https://www.nseindia.com/api/event-calendar",
                        headers=NSE_HEADERS, timeout=10)
        if r.status_code == 200:
            today  = pd.Timestamp.now().normalize()
            cutoff = today + pd.Timedelta(days=5)
            for ev in r.json():
                sym = ev.get("symbol", ""); ds = ev.get("date") or ev.get("bm_date", "")
                if not sym or not ds: continue
                try:
                    if today <= pd.to_datetime(ds) <= cutoff: syms.add(sym)
                except Exception: pass
    except Exception as e:
        print(f"[Earnings] {e}")
    print(f"[Earnings] {len(syms)} symbols with upcoming results")
    return syms

def _prefetch_institutional_symbols():
    syms = set()
    try:
        session = _nse_session()
        for endpoint in ["block-deal", "bulk-deal"]:
            r = session.get(f"https://www.nseindia.com/api/{endpoint}",
                            headers=NSE_HEADERS, timeout=10)
            if r.status_code == 200:
                for deal in r.json().get("data", []):
                    sym = deal.get("symbol", "")
                    if sym: syms.add(sym)
    except Exception as e:
        print(f"[Institutional] {e}")
    print(f"[Institutional] {len(syms)} symbols with recent deals")
    return syms

def _prefetch_market_breadth():
    try:
        session = _nse_session()
        r = session.get("https://www.nseindia.com/api/equity-stockIndices",
                        params={"index": "NIFTY 500"}, headers=NSE_HEADERS, timeout=15)
        if r.status_code == 200:
            adv       = r.json().get("advance", {}) or {}
            advances  = int(adv.get("advances",  0) or 0)
            declines  = int(adv.get("declines",  0) or 0)
            unchanged = int(adv.get("unchanged", 0) or 0)
            ratio     = round(advances / max(declines, 1), 2)
            print(f"[Breadth] A:{advances} D:{declines} ratio:{ratio}")
            return {"advances": advances, "declines": declines,
                    "unchanged": unchanged, "ratio": ratio, "breadth_ok": ratio >= 1.2}
    except Exception as e:
        print(f"[Breadth] {e}")
    return {"advances": 0, "declines": 0, "unchanged": 0, "ratio": 1.0, "breadth_ok": True}

def _fetch_delivery_pct(symbol, session):
    try:
        r = session.get("https://www.nseindia.com/api/quote-equity",
                        params={"symbol": symbol, "type": "trade_info"},
                        headers=NSE_HEADERS, timeout=8)
        if r.status_code == 200:
            ti  = (r.json().get("marketDeptOrderBook", {}) or {}).get("tradeInfo", {}) or {}
            pct = ti.get("deliveryToTradedQuantity")
            if pct is not None: return float(pct)
    except Exception: pass
    return None


# ══════════════════════════════════════════════════════════════════════
# PER-STOCK ANALYSIS — v3 REGIME-ADAPTIVE ENGINE
# ══════════════════════════════════════════════════════════════════════

def fetch_ohlcv(symbol):
    if not YF_OK: return None
    try:
        df = yf.Ticker(f"{symbol}.NS").history(
            period="1y", interval="1d", auto_adjust=True, timeout=10)
        if df is None or df.empty or len(df) < 20: return None
        return df
    except Exception: return None


def analyze_stock(sym_info, df, params, context):
    try:
        closes  = df["Close"].dropna().values.astype(float)
        volumes = df["Volume"].dropna().values.astype(float)
        highs   = df["High"].dropna().values.astype(float)
        lows    = df["Low"].dropna().values.astype(float)
        opens   = df["Open"].dropna().values.astype(float)
    except KeyError:
        return None

    n = min(len(closes), len(volumes), len(highs), len(lows), len(opens))
    if n < 20: return None
    closes, volumes, highs, lows, opens = (
        closes[-n:], volumes[-n:], highs[-n:], lows[-n:], opens[-n:])

    sym    = sym_info["symbol"]
    sector = sym_info.get("sector", "Unknown")
    price  = float(closes[-1])

    regime = context.get("regime", REGIME_NEUTRAL)
    rcfg   = context.get("regime_config", get_regime_config(REGIME_NEUTRAL))

    # ── Core indicators ───────────────────────────────────────────
    rsi_s    = calc_rsi(closes)
    rsi_val  = float(rsi_s.iloc[-1])
    ema20_s  = calc_ema(closes, 20)
    ema50_s  = calc_ema(closes, 50)
    ema200_s = calc_ema(closes, 200) if len(closes) >= 200 else calc_ema(closes, min(len(closes), 50))
    ema20    = float(ema20_s.iloc[-1])
    ema50    = float(ema50_s.iloc[-1])
    ema200   = float(ema200_s.iloc[-1])

    macd_sig, macd_hist, macd_cross_5d = calc_macd_full(closes)

    vol_ratio, vol_consistent = calc_volume_quality(volumes)
    avg_vol        = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes[:-1]))
    last_vol_ratio = float(volumes[-1]) / avg_vol if avg_vol > 0 else 0.0

    diff_pct = (price - ema20) / ema20 * 100
    ema_pos  = "above" if diff_pct > 0.5 else ("at" if diff_pct > -0.5 else "below")
    above_ema50 = price > ema50

    recent_high  = float(np.max(highs[-21:-1])) if len(highs) > 21 else float(np.max(highs[:-1]))
    breakout_20d = price > recent_high

    atr_val, atr_pct             = calc_atr(highs, lows, closes)
    adx_val                      = calc_adx(highs, lows, closes)
    bb_pos, bb_up, bb_mid, bb_lo = calc_bollinger_position(closes)
    higher_lows_ok               = check_higher_lows(lows)
    no_gap_ok                    = check_no_gap_down(opens, closes)
    pct_below_52                 = calc_52wk_proximity(closes, highs)
    pctile_52wk                  = calc_52wk_position_pctile(closes, highs, lows)
    momentum                     = calc_price_momentum(closes)
    consol_break, consol_pct     = detect_consolidation_breakout(closes, highs, lows)

    # Step 4: MA structure (4 OR-conditions)
    ma_ok, ma_conds_met, ma_detail = check_ma_structure(closes, ema20_s, ema50_s, ema200_s, rsi_s)

    # Step 2: Relative strength (5d AND 10d, OR condition)
    rs_data = calc_relative_strength(closes, context.get("nifty_closes", np.array([])))
    s_ret_5d  = rs_data.get("s_ret_5d"); n_ret_5d  = rs_data.get("n_ret_5d")
    s_ret_10d = rs_data.get("s_ret_10d"); n_ret_10d = rs_data.get("n_ret_10d")
    rs_5d_ok  = rs_data.get("rs_5d_ok");  rs_10d_ok = rs_data.get("rs_10d_ok")
    rs_ok     = rs_data.get("rs_ok", False)

    # legacy aliases
    s_ret = s_ret_10d; n_ret = n_ret_10d; rs_nifty_ok = rs_10d_ok

    sector_closes = context.get("sector_closes", {}).get(sector.upper())
    rs_s, rs_sec, rs_sector_ok = calc_rs_vs_sector(closes, sector_closes)

    if regime in (REGIME_BEAR, REGIME_CRISIS):
        rs_ok = rs_ok and (rs_sector_ok is not False)

    del_pct = context.get("delivery_cache", {}).get(sym)

    # Step 7: Sector bonus
    sector_bonus = get_sector_bonus(sector)

    def rej(reason):
        return {
            "rejected": True, "reason": reason,
            "sym": sym, "name": sym_info.get("name", sym),
            "sector": sector, "price": round(price, 2),
            "rsi": round(rsi_val, 1), "vol_ratio": round(last_vol_ratio, 2),
            "ema_pos": ema_pos, "macd": macd_sig, "regime": regime,
        }

    # ── STEP 1: UNIVERSE FILTER GATES ─────────────────────────────
    # Hard-exclude IT sector (Step 1)
    if sector.lower() in _IT_SECTOR_NAMES:
        return rej("IT sector excluded (separate trade running)")

    # Exclude within 2% of 52-week high (overstretched)
    if pct_below_52 < 2.0:
        return rej(f"Within 2% of 52-week high ({pct_below_52:.1f}% below) — overstretched")

    # Exclude falling knives: >35% below 52wk high with no recovery signal
    if pct_below_52 > 35 and not (macd_sig == "bullish" or ema_pos in ("above", "at") or ma_ok):
        return rej(f"{pct_below_52:.1f}% below 52wk high — no recovery signal (falling knife)")

    # ── STEP 3: RSI FILTER ────────────────────────────────────────
    rsi_min = float(params.get("rsi_min", rcfg["rsi_min"]))
    rsi_max = float(params.get("rsi_max", rcfg["rsi_max"]))
    if not (rsi_min <= rsi_val <= rsi_max):
        return rej(f"RSI {rsi_val:.1f} out of [{rsi_min:.0f}–{rsi_max:.0f}] ({regime})")

    # ── STEP 4: MA STRUCTURE ──────────────────────────────────────
    ef = params.get("ema_filter", "at_or_above")
    if ef == "above"       and ema_pos != "above":  return rej(f"Price {ema_pos} EMA20 (need above)")
    if ef == "at_or_above" and ema_pos == "below" and not ma_ok:
        return rej("Price below EMA20 and no alternative MA condition met")

    # ── STEP 5: MACD ──────────────────────────────────────────────
    # Histogram > 0 OR crossover in last 5 days (OR condition)
    macd_ok = (macd_hist > 0) or macd_cross_5d
    mf = params.get("macd_filter", "bullish_neutral")
    if mf == "bullish" and macd_sig != "bullish":
        return rej(f"MACD {macd_sig} (need bullish)")
    if mf == "bullish_neutral" and macd_sig == "bearish" and not macd_cross_5d:
        return rej("MACD bearish with no recent crossover")

    # ── STEP 6: VOLUME CONFIRMATION ───────────────────────────────
    vol_min = float(params.get("vol_min", rcfg["vol_min"]))
    if vol_ratio < vol_min:
        return rej(f"Avg vol {vol_ratio:.2f}x < {vol_min:.1f}x ({regime})")

    # ── STEP 2: RELATIVE STRENGTH FILTER ─────────────────────────
    rs_required = rcfg["rs_required"] or params.get("rs_filter", False)
    if rs_required and not rs_ok:
        return rej(f"RS: 5d stock {s_ret_5d}% vs Nifty {n_ret_5d}%, 10d {s_ret_10d}% vs {n_ret_10d}% (underperforming)")

    # ── OTHER OPTIONAL GATES ──────────────────────────────────────
    hl_required = rcfg["higher_lows_req"] or params.get("higher_lows", False)
    if hl_required and not higher_lows_ok:
        return rej(f"Higher lows required in {regime}")

    ngd_required = rcfg["no_gap_req"] or params.get("no_gap_down", False)
    if ngd_required and not no_gap_ok:
        return rej(f"Gap-down detected ({regime})")

    bb_req = rcfg["bb_filter_req"] or params.get("bb_filter", False)
    if bb_req and bb_pos not in ("sweet_spot",):
        return rej(f"BB {bb_pos} — need sweet_spot ({regime})")

    atr_max = float(rcfg.get("atr_max_pct", 0) or params.get("atr_max_pct", 0) or 0)
    if atr_max > 0 and atr_pct is not None and atr_pct > atr_max:
        return rej(f"ATR% {atr_pct:.2f} > {atr_max:.1f} ({regime})")
    atr_min_user = float(params.get("atr_min_pct", 0) or 0)
    if atr_min_user > 0 and atr_pct is not None and atr_pct < atr_min_user:
        return rej(f"ATR% {atr_pct:.2f} < min {atr_min_user:.2f}")

    adx_min_val = int(params.get("adx_min", 0) or 0)
    if adx_min_val > 0 and (adx_val is None or adx_val < adx_min_val):
        return rej(f"ADX {adx_val} < {adx_min_val}")

    h52_max = float(params.get("high52_max_pct", 0) or 0)
    if h52_max > 0 and pct_below_52 > h52_max:
        return rej(f"{pct_below_52:.1f}% below 52wk high (limit {h52_max:.0f}%)")

    if params.get("sector_momentum"):
        if context.get("sector_ema", {}).get(sector) is False:
            return rej(f"Sector '{sector}' below EMA20")

    if params.get("earnings_filter") and sym in context.get("earnings_syms", set()):
        return rej("Earnings due in 5 days")

    if params.get("fii_dii_filter") and sym not in context.get("inst_syms", set()):
        return rej("No recent FII/DII bulk/block deal")

    del_min = float(params.get("delivery_min", 0) or 0)
    if del_min > 0 and (del_pct is None or del_pct < del_min):
        return rej(f"Delivery% {del_pct if del_pct is not None else 'N/A'} < {del_min:.0f}%")

    # ── STEP 8: FUNDAMENTALS (optional, slow) ─────────────────────
    fundamentals = {}
    if params.get("fetch_fundamentals"):
        fundamentals = fetch_fundamentals_quick(sym)

    # ── STEP 9: FINAL SCORING (100-pt fixed breakdown) ────────────
    score = 0

    # RS vs Nifty 5-day: max 25 pts
    diff5 = ((s_ret_5d or 0) - (n_ret_5d or 0))
    if diff5 >= 5:    score += 25
    elif diff5 >= 2:  score += 15
    elif diff5 >= 1:  score += 10
    elif diff5 >= 0:  score += 5

    # RSI sweet spot: max 15 pts
    if 55 <= rsi_val <= 65:    score += 15
    elif 50 <= rsi_val < 55:   score += 10
    elif 45 <= rsi_val < 50 or 65 < rsi_val <= 68: score += 5

    # MA alignment: max 15 pts (proportional to conditions met)
    score += min(15, ma_conds_met * 5)

    # MACD: max 10 pts
    if macd_sig == "bullish":    score += 10
    elif macd_cross_5d:          score += 7
    elif macd_hist > 0:          score += 4

    # Volume confirmation: max 10 pts
    if vol_ratio >= 2.0:         score += 10
    elif vol_ratio >= 1.5:       score += 7
    elif vol_ratio >= 1.2:       score += 5

    # Sector bonus: max 10 pts (scaled from -2..+3 raw)
    sector_pts = {3: 10, 2: 7, 1: 3, 0: 0, -1: -3, -2: -7}.get(sector_bonus, 0)
    score = max(0, score + sector_pts)

    # Fundamental score: max 10 pts
    if fundamentals:
        f_score = 4  # start at midpoint
        pe = fundamentals.get("pe"); roe = fundamentals.get("roe"); de = fundamentals.get("de")
        mcap_ok = fundamentals.get("mcap_ok", True)
        if pe and pe <= 30:       f_score += 3
        elif pe and pe <= 50:     f_score += 1
        elif pe and pe > 60:      f_score -= 2
        if roe and roe > 20:      f_score += 3
        elif roe and roe > 12:    f_score += 1
        if de and de > 2.0:       f_score -= 2
        if mcap_ok:               f_score += 1
        score += max(0, min(10, f_score))
    else:
        score += 5  # neutral when fundamentals not fetched

    # 52-week range position: max 5 pts (prefer 40–80th %ile)
    if 40 <= pctile_52wk <= 80:       score += 5
    elif 30 <= pctile_52wk < 40 or 80 < pctile_52wk <= 90: score += 3
    else:                              score += 1

    score = min(score, 100)

    min_score = int(params.get("min_score", 0) or 0)
    if score < min_score:
        return rej(f"Score {score} < min {min_score}")

    # ── SIGNAL CLASSIFICATION ─────────────────────────────────────
    breadth_ok = context.get("breadth", {}).get("breadth_ok", True)

    if (rcfg["allow_strong_buy"]
            and score >= rcfg["strong_buy_score"]
            and macd_sig == "bullish" and ema_pos == "above"
            and last_vol_ratio >= rcfg["strong_buy_vol"]
            and rs_ok is not False
            and (regime not in (REGIME_BEAR, REGIME_CRISIS) or higher_lows_ok)):
        sig = "STRONG BUY"
    elif score >= rcfg["buy_score"] and (macd_sig in ("bullish", "neutral") or macd_cross_5d) and ema_pos in ("above", "at"):
        sig = "BUY"
    else:
        sig = "WATCH"

    if sig == "STRONG BUY" and not breadth_ok and regime in (REGIME_BEAR, REGIME_CRISIS):
        sig = "BUY"

    # ── BUILD RESULT ──────────────────────────────────────────────
    result = {
        "sym": sym, "name": sym_info.get("name", sym),
        "sector": sector, "industry": sym_info.get("industry", ""),
        "price": round(price, 2),
        "regime": regime,
        "rsi": round(rsi_val, 1),
        "vol_ratio": round(vol_ratio, 2), "vol_consistent": vol_consistent,
        "ema_pos": ema_pos, "ema20": round(ema20, 2), "ema50": round(ema50, 2), "ema200": round(ema200, 2),
        "above_ema50": above_ema50, "macd": macd_sig,
        "macd_hist": macd_hist, "macd_crossover_5d": macd_cross_5d,
        "ma_ok": ma_ok, "ma_conds_met": ma_conds_met, "ma_detail": ma_detail,
        "atr": atr_val, "atr_pct": atr_pct, "adx": adx_val,
        "bb_pos": bb_pos, "bb_upper": bb_up, "bb_middle": bb_mid,
        "higher_lows": higher_lows_ok, "no_gap_down": no_gap_ok,
        "momentum": momentum, "consol_breakout": consol_break, "consol_range_pct": consol_pct,
        "rs_vs_nifty": s_ret_10d, "nifty_10d": n_ret_10d, "rs_nifty_ok": rs_nifty_ok,
        "s_ret_5d": s_ret_5d, "n_ret_5d": n_ret_5d, "rs_5d_ok": rs_5d_ok,
        "rs_vs_sector": rs_s, "sector_10d": rs_sec, "rs_sector_ok": rs_sector_ok,
        "pct_below_52wk": pct_below_52, "pctile_52wk": pctile_52wk,
        "delivery_pct": del_pct, "sector_bonus": sector_bonus,
        "breakout_20d": breakout_20d, "score": score, "sig": sig,
        "fundamentals": fundamentals if fundamentals else None,
        "breadth_downgraded": (sig != "STRONG BUY" and score >= rcfg["strong_buy_score"]),
        # ── Backward-compat aliases ────────────────────────────────
        "rs_stock":     s_ret_10d,
        "rs_nifty":     n_ret_10d,
        "rs_ok":        rs_ok,
        "pct_below_52": pct_below_52,
        "breakout":     breakout_20d,
    }

    # ── STEP 9 ADDITIONAL: Why rationale + Key Risk ───────────────
    why, key_risk = generate_rationale(result)
    result["why_rationale"] = why
    result["key_risk"]      = key_risk

    # ── STEP 10: TARGETS AND STOP LOSS ───────────────────────────
    if sig in ("STRONG BUY", "BUY"):
        # Buy Range: Lower = max(price×0.98, 20EMA), Upper = price×1.01
        buy_low   = round(max(price * 0.98, ema20), 2)
        buy_high  = round(price * 1.01, 2)
        entry     = (buy_low + buy_high) / 2  # midpoint entry for calculations

        # Target: entry×1.05 (5% gain in 1 week per requirement)
        t1_price  = round(entry * 1.05, 2)
        # Cap at 50EMA or 200EMA if they're closer (resistance)
        resistances = [r for r in [ema50, ema200] if r > price * 1.01]
        if resistances:
            nearest_res = min(resistances)
            if nearest_res < t1_price:
                t1_price = round(nearest_res * 0.998, 2)

        # SL: entry×0.965 (3.5% below), floored at 20EMA−0.5%
        sl_price  = round(max(entry * 0.965, ema20 * 0.995), 2)
        sl_pct    = round((entry - sl_price) / entry * 100, 2)
        t1_pct    = round((t1_price - entry) / entry * 100, 2)
        actual_rr = round(t1_pct / sl_pct, 2) if sl_pct > 0 else 0

        # T2 as 6.5% above entry (stretch target)
        t2_price  = round(entry * 1.065, 2)

        shares   = int(20000 // price)
        result.update({
            "t1": t1_price, "t2": t2_price, "sl": sl_price,
            "sl_pct": sl_pct, "t1_pct": t1_pct, "rr": actual_rr,
            "buy_range_low": buy_low, "buy_range_high": buy_high,
            "shares_20k": shares,
            "exp_profit_20k": round(shares * (t1_price - price), 2),
            "max_loss_20k":   round(shares * (price - sl_price), 2),
            "tp_pct": t1_pct,  # backward-compat alias
        })

    return result


# ══════════════════════════════════════════════════════════════════════
# SCAN JOB
# ══════════════════════════════════════════════════════════════════════

_jobs = {}; _jobs_lock = threading.Lock()


def scan_worker(job_id, stocks, params):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None: return

    job.update({"total": len(stocks), "status": "prefetch", "progress": 0,
                "results": [], "status_msg": "Detecting market regime…"})

    regime_info = detect_market_regime()
    regime      = regime_info.get("regime", REGIME_NEUTRAL)
    rcfg        = get_regime_config(regime)
    job["regime"] = regime; job["regime_data"] = regime_info.get("data", {})
    job["status_msg"] = f"Regime: {regime} | Fetching market context…"

    nifty_closes   = _prefetch_nifty_closes()
    unique_sectors = list({s.get("sector", "").upper() for s in stocks})
    sector_ema     = _prefetch_sector_ema(unique_sectors) if params.get("sector_momentum") else {}
    sector_closes  = _prefetch_sector_closes()
    job["status_msg"] = "Sector data ready"

    earnings_syms = _prefetch_earnings_symbols()  if params.get("earnings_filter")  else set()
    inst_syms     = _prefetch_institutional_symbols() if params.get("fii_dii_filter") else set()
    breadth       = _prefetch_market_breadth()
    job["breadth"] = breadth
    job["status_msg"] = (f"Regime={regime} | VIX={regime_info['data'].get('vix','?')} | "
                         f"AD={breadth['ratio']} | Scanning {len(stocks)} stocks…")
    job["status"] = "running"

    context = {
        "regime": regime, "regime_config": rcfg,
        "nifty_closes": nifty_closes, "sector_ema": sector_ema,
        "sector_closes": sector_closes, "earnings_syms": earnings_syms,
        "inst_syms": inst_syms, "breadth": breadth, "delivery_cache": {},
    }

    delivery_session = _nse_session() if float(params.get("delivery_min", 0) or 0) > 0 else None
    passed = []; rejected = []; completed = [0]; inner_lock = threading.Lock()

    def process(sym_info):
        if job.get("cancelled"): return None
        sym = sym_info["symbol"]
        if delivery_session and float(params.get("delivery_min", 0) or 0) > 0:
            dp = _fetch_delivery_pct(sym, delivery_session)
            if dp is not None: context["delivery_cache"][sym] = dp
        df = fetch_ohlcv(sym)
        if df is None: return None
        return analyze_stock(sym_info, df, params, context)

    workers = min(15, max(1, len(stocks)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fmap = {ex.submit(process, s): s for s in stocks}
        for fut in as_completed(fmap):
            if job.get("cancelled"): job["status"] = "cancelled"; return
            res = fut.result()
            with inner_lock:
                completed[0] += 1
                if res is None: pass
                elif res.get("rejected"): rejected.append(res)
                else: passed.append(res)
                job["progress"] = completed[0]
                job["results"]  = sorted(passed, key=lambda x: x["score"], reverse=True)
                job["rejected"] = rejected

    job["status"]  = "done"
    job["results"] = sorted(passed, key=lambda x: x["score"], reverse=True)
    job["rejected"] = rejected
    print(f"[Scan {job_id}] Done — {len(passed)}/{len(stocks)} passed | regime={regime}")


# ══════════════════════════════════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════════════════════════════════

def fetch_market_data():
    if not YF_OK: return {}
    result = {}
    for name, sym in [("nifty50", "^NSEI"), ("banknifty", "^NSEBANK"), ("indiavix", "^INDIAVIX")]:
        try:
            hist   = yf.Ticker(sym).history(period="1mo", interval="1d", auto_adjust=True)
            if hist.empty: continue
            closes = hist["Close"].dropna().values.astype(float)
            price  = float(closes[-1]); prev = float(closes[-2]) if len(closes) > 1 else price
            chg    = price - prev
            result[name] = {
                "price": round(price, 2), "change": round(chg, 2),
                "chg_pct": round(chg / prev * 100, 2),
                "rsi": round(float(calc_rsi(closes).iloc[-1]), 1) if len(closes) >= 15 else None,
                "direction": "up" if chg >= 0 else "down",
            }
        except Exception as e:
            print(f"[Market] {name}: {e}")
    try:
        result["breadth"] = _prefetch_market_breadth()
        regime_info = detect_market_regime()
        result["regime"] = regime_info.get("regime")
        result["regime_data"] = regime_info.get("data", {})
    except Exception: pass
    return result

def _fetch_ltp_batch(symbols):
    if not symbols:
        return {}, []
    prices, failed = {}, []
    groww_available = True
    token = get_access_token()
    if not token:
        groww_available = False
    for sym in symbols:
        got = False
        if groww_available:
            try:
                r = requests.get(
                    f"{BASE_URL}/live-data/quote",
                    params={"exchange": "NSE", "segment": "CASH", "trading_symbol": sym},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "X-API-VERSION": "1.0",
                    },
                    timeout=5,
                )
                d = r.json()
                if r.status_code == 429:
                    # Circuit break Groww for remaining symbols in this batch.
                    groww_available = False
                elif d.get("status") == "SUCCESS":
                    p = d["payload"]
                    ltp = p.get("last_price") or p.get("ltp")
                    if ltp:
                        prices[sym] = {
                            "ltp": float(ltp),
                            "day_high": float(p.get("high_price") or p.get("day_high") or ltp),
                            "day_low": float(p.get("low_price") or p.get("day_low") or ltp),
                            "change": round(p.get("day_change") or 0, 2),
                            "change_pct": round(p.get("day_change_perc") or 0, 2),
                            "source": "groww",
                        }
                        got = True
                else:
                    err_code = str((d.get("error") or {}).get("code", ""))
                    if err_code in ("401", "403", "429"):
                        groww_available = False
            except Exception:
                pass
        if not got and YF_OK:
            try:
                hist = yf.Ticker(f"{sym}.NS").history(period="5d", interval="1d", auto_adjust=True)
                if hist is not None and not hist.empty:
                    last = hist.iloc[-1]
                    ltp = float(last["Close"])
                    prev_close = float(hist.iloc[-2]["Close"]) if len(hist) > 1 else ltp
                    chg = ltp - prev_close
                    chg_pct = (chg / prev_close * 100) if prev_close else 0.0
                    prices[sym] = {
                        "ltp": round(ltp, 2),
                        "day_high": round(float(last["High"]), 2),
                        "day_low": round(float(last["Low"]), 2),
                        "change": round(chg, 2),
                        "change_pct": round(chg_pct, 2),
                        "source": "yfinance",
                    }
                    got = True
            except Exception:
                pass
        if not got:
            failed.append(sym)
    return prices, failed


# ══════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({"status": "ok", "yf_available": YF_OK, "time": time.time(),
                    "session_creds_active": bool(_session_api_key),
                    "supabase_ok": SB_OK, "build": "v5-regime-adaptive"})

@app.route("/regime")
def regime_route():
    try:
        info = detect_market_regime()
        info["config"] = get_regime_config(info.get("regime", REGIME_NEUTRAL))
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ltp")
def get_ltp():
    symbols = [s.strip() for s in request.args.get("symbols", "").split(",") if s.strip()]
    if not symbols: return jsonify({"error": "No symbols"}), 400
    results, failed = _fetch_ltp_batch(symbols)
    return jsonify({"prices": results, "failed": failed})

@app.route("/quote/<symbol>")
def get_quote(symbol):
    token = get_access_token()
    if token:
        try:
            r = requests.get(
                f"{BASE_URL}/live-data/quote",
                params={"exchange": "NSE", "segment": "CASH", "trading_symbol": symbol},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "X-API-VERSION": "1.0",
                },
                timeout=5,
            )
            d = r.json()
            if d.get("status") == "SUCCESS":
                return jsonify(d)
        except Exception:
            pass
    if YF_OK:
        try:
            hist = yf.Ticker(f"{symbol}.NS").history(period="5d", interval="1d", auto_adjust=True)
            if hist is not None and not hist.empty:
                closes = hist["Close"].dropna().values.astype(float)
                ltp = float(closes[-1])
                prev = float(closes[-2]) if len(closes) > 1 else ltp
                chg = ltp - prev
                chg_pct = (chg / prev * 100) if prev else 0.0
                return jsonify({
                    "status": "SUCCESS",
                    "payload": {
                        "last_price": round(ltp, 2),
                        "day_change": round(chg, 2),
                        "day_change_perc": round(chg_pct, 2),
                        "source": "yfinance",
                    }
                })
        except Exception as e:
            return jsonify({"error": f"Quote fallback failed: {e}"}), 500
    return jsonify({"error": "Quote unavailable from Groww and yfinance"}), 503

@app.route("/refresh-token", methods=["POST"])
def refresh_token():
    return jsonify({"ok": bool(get_access_token(force=True))})

@app.route("/universe")
def universe_route():
    scope = request.args.get("index", "NIFTY 500")
    try:
        data    = get_universe(scope)
        sectors = sorted({s["sector"] for s in data if s["sector"] not in ("Unknown", "")})
        return jsonify({"count": len(data), "stocks": data, "sectors": sectors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/screen", methods=["POST"])
def start_screen():
    if not YF_OK: return jsonify({"error": "yfinance not installed"}), 503
    params = request.get_json(silent=True) or {}
    scope  = params.pop("universe", "NIFTY 500")
    stocks = get_universe(scope)
    if not stocks: return jsonify({"error": "Could not fetch NSE universe"}), 503
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "progress": 0, "total": len(stocks),
                         "results": [], "rejected": [], "cancelled": False,
                         "status_msg": "Queued…", "regime": None}
    threading.Thread(target=scan_worker, args=(job_id, stocks, params), daemon=True).start()
    return jsonify({"job_id": job_id, "total": len(stocks), "scope": scope})

@app.route("/scan-progress/<job_id>")
def scan_progress(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None: return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"], "status_msg": job.get("status_msg", ""),
        "progress": job["progress"], "total": job["total"],
        "found": len(job["results"]), "results": job["results"],
        "rejected": job.get("rejected", []), "breadth": job.get("breadth", {}),
        "regime": job.get("regime"), "regime_data": job.get("regime_data", {}),
    })

@app.route("/cancel-scan/<job_id>", methods=["POST"])
def cancel_scan(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job: return jsonify({"error": "not found"}), 404
    job["cancelled"] = True
    return jsonify({"ok": True})

@app.route("/market-data")
def market_data_route():
    try:
        return jsonify(fetch_market_data())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/set-credentials", methods=["POST"])
def set_credentials():
    global _session_api_key, _session_api_secret, _access_token, _token_fetched_at
    data   = request.get_json(silent=True) or {}
    key    = data.get("api_key", "").strip()
    secret = data.get("api_secret", "").strip()
    if not key or not secret:
        return jsonify({"ok": False, "error": "api_key and api_secret required"}), 400
    tok, err = _request_access_token(key, secret)
    if tok:
        with _cred_lock:
            _session_api_key = key
            _session_api_secret = secret
            _access_token = tok
            _token_fetched_at = time.time()
        return jsonify({"ok": True})
    with _cred_lock:
        _session_api_key = None; _session_api_secret = None
    return jsonify({
        "ok": False,
        "error": "Groww rejected credentials",
        "details": err or {},
    }), 401

@app.route("/save-trades", methods=["POST"])
def save_trades():
    if not SB_OK: return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    rows = request.get_json(silent=True) or []
    if not isinstance(rows, list): return jsonify({"ok": False, "error": "expected array"}), 400
    buyable = [r for r in rows if r.get("sig") in ("BUY", "STRONG BUY")]
    if not buyable: return jsonify({"ok": True, "saved": 0})
    inserted = 0
    updated = 0
    for r in buyable:
        price = float(r.get("price") or 0)
        symbol = r.get("sym") or r.get("symbol")
        if not symbol:
            continue
        target1 = r.get("t1")
        target2 = r.get("t2")
        stop_loss = r.get("sl")
        if target1 is not None:
            target1 = _to_float(target1, None)
        if target2 is not None:
            target2 = _to_float(target2, None)
        if stop_loss is not None:
            stop_loss = _to_float(stop_loss, None)
        if target1 is not None and target1 <= price:
            return jsonify({"ok": False, "error": f"Invalid target1 for {symbol}: must be greater than entry price"}), 400
        if target2 is not None and target1 is not None and target2 < target1:
            return jsonify({"ok": False, "error": f"Invalid target2 for {symbol}: must be greater than or equal to target1"}), 400
        if stop_loss is not None and stop_loss >= price:
            return jsonify({"ok": False, "error": f"Invalid stop_loss for {symbol}: must be below entry price"}), 400
        payload = {
            "symbol":         r.get("sym") or r.get("symbol"),
            "name":           r.get("name"),
            "sector":         r.get("sector"),
            "signal":         r.get("sig"),
            "score":          r.get("score"),
            "regime":         r.get("regime"),
            "entry_price":    price,
            "buy_range_low":  r.get("buy_range_low",  round(price * 0.990, 2)),
            "buy_range_high": r.get("buy_range_high", round(price * 1.005, 2)),
            "target1":        target1,
            "target2":        target2,
            "stop_loss":      stop_loss,
            "tp_pct":         r.get("t1_pct") or r.get("tp_pct"),
            "sl_pct":         r.get("sl_pct"),
            "rr":             r.get("rr"),
            "shares_20k":     r.get("shares_20k"),
            "exp_profit_20k": r.get("exp_profit_20k"),
            "max_loss_20k":   r.get("max_loss_20k"),
            "status":         "open",
        }
        try:
            existing = (_sb.table("trades")
                        .select("*")
                        .eq("symbol", symbol)
                        .eq("status", "open")
                        .order("scanned_at", desc=True)
                        .execute()).data
            force_entry_update = bool(r.get("force_entry_update"))
            auto_sync = bool(r.get("auto_sync"))
            if existing:
                # During autosync, preserve trader-entered entry/qty unless explicitly overridden.
                if auto_sync and not force_entry_update:
                    payload["entry_price"] = existing[0].get("entry_price")
                    payload["shares_20k"] = existing[0].get("shares_20k")
                # If user explicitly updates entry, reset entry timestamp for outcome tracking.
                if force_entry_update:
                    payload["scanned_at"] = pd.Timestamp.now().isoformat()
                _sb.table("trades").update(payload).eq("id", existing[0]["id"]).execute()
                updated += 1
            else:
                # Stamp the exact entry time so holding-period OHLC checks
                # know precisely when the trade was initiated.
                payload["scanned_at"] = pd.Timestamp.now().isoformat()
                _sb.table("trades").insert([payload]).execute()
                inserted += 1
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    try:
        return jsonify({"ok": True, "saved": inserted + updated, "inserted": inserted, "updated": updated})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/trades")
def get_trades():
    if not SB_OK: return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        outcome_res, outcome_status = run_trade_outcome_check()
        resp = _sb.table("trades").select("*").order("scanned_at", desc=True).execute()
        if outcome_status >= 400:
            return jsonify({"ok": True, "trades": resp.data, "outcome_reconciled": outcome_res, "outcome_reconcile_warning": True})
        return jsonify({"ok": True, "trades": resp.data, "outcome_reconciled": outcome_res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/portfolio-summary")
def portfolio_summary():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        resp = _sb.table("trades").select("*").order("scanned_at", desc=True).execute()
        trades = resp.data or []
        open_trades = [t for t in trades if t.get("status") == "open"]
        closed_trades = [t for t in trades if t.get("status") != "open"]

        symbols = sorted({t.get("symbol") for t in open_trades if t.get("symbol")})
        prices, _ = _fetch_ltp_batch(symbols)

        total_value = 0.0
        total_cost = 0.0
        holdings = []
        sector_cost = {}
        for t in open_trades:
            sym = t.get("symbol")
            qty = float(t.get("shares_20k") or 0)
            entry = float(t.get("entry_price") or 0)
            ltp = float((prices.get(sym, {}) or {}).get("ltp") or entry or 0)
            position_cost = entry * qty
            position_value = ltp * qty
            pnl = position_value - position_cost
            pnl_pct = (pnl / position_cost * 100) if position_cost > 0 else 0
            total_value += position_value
            total_cost += position_cost
            sector = t.get("sector") or "Unknown"
            sector_cost[sector] = sector_cost.get(sector, 0.0) + position_cost
            holdings.append({
                "symbol": sym,
                "name": t.get("name"),
                "sector": sector,
                "qty": qty,
                "avg_price": round(entry, 2),
                "market_price": round(ltp, 2),
                "position_value": round(position_value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "signal": t.get("signal"),
            })

        pnl_total = total_value - total_cost
        pnl_pct_total = (pnl_total / total_cost * 100) if total_cost > 0 else 0
        allocation = []
        for sector, cost in sorted(sector_cost.items(), key=lambda x: x[1], reverse=True):
            weight = (cost / total_cost * 100) if total_cost > 0 else 0
            allocation.append({"sector": sector, "weight": round(weight, 1)})

        wins = len([t for t in closed_trades if t.get("status") == "target_hit"])
        losses = len([t for t in closed_trades if t.get("status") == "sl_hit"])
        closed_count = len(closed_trades)
        win_rate = round((wins / closed_count * 100), 1) if closed_count else 0.0

        return jsonify({
            "ok": True,
            "summary": {
                "open_positions": len(open_trades),
                "closed_positions": closed_count,
                "portfolio_value": round(total_value, 2),
                "invested_cost": round(total_cost, 2),
                "pnl_total": round(pnl_total, 2),
                "pnl_pct_total": round(pnl_pct_total, 2),
                "win_rate": win_rate,
                "wins": wins,
                "losses": losses,
            },
            "allocation": allocation,
            "holdings": holdings,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _to_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def run_trade_outcome_check():
    """Reconcile open trades against historical OHLC and close hit outcomes."""
    if not SB_OK:
        return {"ok": False, "error": "Supabase not configured"}, 503
    if not YF_OK:
        return {"ok": False, "error": "yfinance not available"}, 503
    try:
        resp = _sb.table("trades").select("*").eq("status", "open").execute()
        open_trades = [t for t in resp.data
                       if t.get("target1") is not None and t.get("stop_loss") is not None]
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500
    if not open_trades:
        return {"ok": True, "checked": 0, "updated": 0}, 200

    updates_by_id = {}
    upd_lock = threading.Lock()
    today = pd.Timestamp.now().normalize()

    def check_one(trade):
        """Historical OHLC check: entry day through yesterday (today exclusive).

        entry_date / entered_at = actual purchase date → check from that day.
        scanned_at only (EOD signal) → actual entry is next trading day, so
        start from scanned_at + 1 calendar day to avoid false hits on the
        scan-day candle (before the trade was entered).
        """
        try:
            sym = trade.get("symbol")
            tid = trade.get("id")
            if not sym or not tid:
                return
            t1 = float(trade["target1"])
            sl = float(trade["stop_loss"])
            if t1 <= 0 or sl <= 0:
                return
            explicit_entry = trade.get("entry_date") or trade.get("entered_at")
            if explicit_entry:
                entry_day = pd.Timestamp(explicit_entry).normalize()
            else:
                scan_ts = trade.get("scanned_at", "")
                if not scan_ts:
                    return
                # EOD scan → actual holding starts the next calendar day.
                entry_day = (pd.Timestamp(scan_ts) + pd.Timedelta(days=1)).normalize()
            # end is exclusive in yfinance – passing today gives candles up to yesterday.
            if entry_day >= today:
                return
            df = yf.Ticker(f"{sym}.NS").history(
                start=entry_day.strftime("%Y-%m-%d"),
                end=today.strftime("%Y-%m-%d"),
                interval="1d", auto_adjust=True)
            if df is None or df.empty:
                return
            for dt, row in df.iterrows():
                low = float(row["Low"])
                high = float(row["High"])
                days = max((dt.date() - entry_day.date()).days + 1, 1)
                # Target takes precedence: if both T1 and SL were touched on
                # the same candle, assume the limit-sell at T1 was filled first.
                if high >= t1:
                    with upd_lock:
                        updates_by_id[tid] = {"id": tid, "status": "target_hit",
                                              "outcome_price": t1,
                                              "outcome_date": dt.date().isoformat(),
                                              "days_to_outcome": days}
                    break
                if low <= sl:
                    with upd_lock:
                        updates_by_id[tid] = {"id": tid, "status": "sl_hit",
                                              "outcome_price": sl,
                                              "outcome_date": dt.date().isoformat(),
                                              "days_to_outcome": days}
                    break
        except Exception as e:
            print(f"[check_one] {trade.get('symbol','?')}: {e}")

    # Pass 1: Historical OHLC (entry → yesterday).  Must run first so accurate
    # past outcomes take priority over today's live LTP in Pass 2.
    try:
        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(check_one, open_trades))
    except Exception as e:
        return {"ok": False, "error": f"ThreadPool: {e}"}, 500

    # Pass 2: Live LTP check – only for trades not resolved by historical data.
    # Catches today's intraday SL/target hits that yfinance doesn't yet have.
    remaining = [t for t in open_trades if t.get("id") not in updates_by_id]
    if remaining:
        try:
            symbols = sorted({t.get("symbol") for t in remaining if t.get("symbol")})
            ltp_prices, _ = _fetch_ltp_batch(symbols)
        except Exception:
            ltp_prices = {}

        for trade in remaining:
            try:
                tid = trade.get("id")
                sym = trade.get("symbol")
                if not tid or not sym:
                    continue
                t1 = float(trade["target1"])
                sl = float(trade["stop_loss"])
                # Determine the first day the trade is actually in the holding
                # period so we don't check today's range for a trade that will
                # only be entered tomorrow (EOD-scan → next-day entry).
                explicit_entry = trade.get("entry_date") or trade.get("entered_at")
                if explicit_entry:
                    actual_entry_day = pd.Timestamp(explicit_entry).normalize()
                else:
                    scan_ts = trade.get("scanned_at", "")
                    if not scan_ts:
                        continue
                    actual_entry_day = (pd.Timestamp(scan_ts) + pd.Timedelta(days=1)).normalize()
                if actual_entry_day > today:
                    continue  # not entered yet; skip today's intraday check
                quote = ltp_prices.get(sym) or {}
                ltp = float(quote.get("ltp") or 0)
                if ltp <= 0:
                    continue
                # Use intraday high/low so a T1/SL touch during the day is
                # caught even when the current price has since moved away.
                day_high = float(quote.get("day_high") or ltp)
                day_low = float(quote.get("day_low") or ltp)
                days_to_outcome = max((today.date() - actual_entry_day.date()).days + 1, 1)
                # Target takes precedence: if both T1 and SL touched today,
                # assume the limit-sell at T1 was filled first.
                if day_high >= t1:
                    updates_by_id[tid] = {
                        "id": tid,
                        "status": "target_hit",
                        "outcome_price": t1,
                        "outcome_date": today.date().isoformat(),
                        "days_to_outcome": days_to_outcome,
                    }
                elif day_low <= sl:
                    updates_by_id[tid] = {
                        "id": tid,
                        "status": "sl_hit",
                        "outcome_price": sl,
                        "outcome_date": today.date().isoformat(),
                        "days_to_outcome": days_to_outcome,
                    }
            except Exception:
                continue

    updates = list(updates_by_id.values())
    for upd in updates:
        tid = upd.pop("id")
        try:
            _sb.table("trades").update(upd).eq("id", tid).execute()
        except Exception as e:
            print(f"[outcomes] update failed: {e}")

    return {"ok": True, "checked": len(open_trades), "updated": len(updates)}, 200


def _record_portfolio_event(event_type, symbol, position_id=None, payload=None, message=None):
    if not SB_OK:
        return
    try:
        _sb.table("portfolio_events").insert([{
            "position_id": position_id,
            "symbol": symbol,
            "event_type": event_type,
            "payload_json": payload or {},
            "message": message or "",
        }]).execute()
    except Exception as e:
        print(f"[portfolio_events] {e}")


@app.route("/portfolio/recommendations")
def portfolio_recommendations():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        recs = _sb.table("recommendations").select("*").order("created_at", desc=True).execute().data
        return jsonify({"ok": True, "recommendations": recs or []})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/recommendations", methods=["POST"])
def create_portfolio_recommendation():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    rating = (data.get("rating") or "buy").strip().lower()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    if rating not in ("buy", "strong_buy"):
        return jsonify({"ok": False, "error": "rating must be buy or strong_buy"}), 400
    payload = {
        "symbol": symbol,
        "rating": rating,
        "target_price": _to_float(data.get("target_price")),
        "stop_loss": _to_float(data.get("stop_loss")),
        "status": (data.get("status") or "active").strip().lower(),
        "rationale": data.get("rationale"),
        "updated_at": pd.Timestamp.now().isoformat(),
    }
    try:
        inserted = _sb.table("recommendations").insert([payload]).execute().data
        _record_portfolio_event("create", symbol, payload=payload, message="Recommendation created")
        return jsonify({"ok": True, "recommendation": (inserted or [None])[0]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/recommendations/<rec_id>", methods=["PUT"])
def update_portfolio_recommendation(rec_id):
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    data = request.get_json(silent=True) or {}
    payload = {
        "symbol": (data.get("symbol") or "").strip().upper() or None,
        "rating": (data.get("rating") or "").strip().lower() or None,
        "target_price": _to_float(data.get("target_price")) if data.get("target_price") is not None else None,
        "stop_loss": _to_float(data.get("stop_loss")) if data.get("stop_loss") is not None else None,
        "status": (data.get("status") or "").strip().lower() or None,
        "rationale": data.get("rationale"),
        "updated_at": pd.Timestamp.now().isoformat(),
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        updated = _sb.table("recommendations").update(payload).eq("id", rec_id).execute().data
        if not updated:
            return jsonify({"ok": False, "error": "recommendation not found"}), 404
        symbol = updated[0].get("symbol")
        _record_portfolio_event("update", symbol, payload=payload, message="Recommendation updated")
        return jsonify({"ok": True, "recommendation": updated[0]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/recommendations/<rec_id>", methods=["DELETE"])
def delete_portfolio_recommendation(rec_id):
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        deleted = _sb.table("recommendations").delete().eq("id", rec_id).execute().data
        if not deleted:
            return jsonify({"ok": False, "error": "recommendation not found"}), 404
        symbol = deleted[0].get("symbol")
        _record_portfolio_event("delete", symbol, payload={"id": rec_id}, message="Recommendation deleted")
        return jsonify({"ok": True, "deleted": rec_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/positions")
def portfolio_positions():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        positions = _sb.table("portfolio_positions").select("*").order("opened_at", desc=True).execute().data or []
        active = [p for p in positions if p.get("status") == "active" and p.get("symbol")]
        if active:
            symbols = sorted({p.get("symbol") for p in active})
            prices, _ = _fetch_ltp_batch(symbols)
            for p in positions:
                sym = p.get("symbol")
                if p.get("status") == "active" and sym in prices:
                    ltp = _to_float((prices.get(sym, {}) or {}).get("ltp"), _to_float(p.get("current_price")))
                    p["current_price"] = round(ltp, 2)
        return jsonify({"ok": True, "positions": positions})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/positions", methods=["POST"])
def create_portfolio_position():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400
    payload = {
        "symbol": symbol,
        "qty": _to_float(data.get("qty")),
        "entry_price": _to_float(data.get("entry_price")),
        "current_price": _to_float(data.get("current_price"), _to_float(data.get("entry_price"))),
        "status": (data.get("status") or "active").strip().lower(),
        "notes": data.get("notes"),
        "updated_at": pd.Timestamp.now().isoformat(),
    }
    try:
        inserted = _sb.table("portfolio_positions").insert([payload]).execute().data
        inserted_row = (inserted or [None])[0]
        _record_portfolio_event("create", symbol, position_id=(inserted_row or {}).get("id"), payload=payload, message="Position created")
        return jsonify({"ok": True, "position": inserted_row})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/positions/<position_id>", methods=["PUT"])
def update_portfolio_position(position_id):
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower() or None
    payload = {
        "symbol": (data.get("symbol") or "").strip().upper() or None,
        "qty": _to_float(data.get("qty")) if data.get("qty") is not None else None,
        "entry_price": _to_float(data.get("entry_price")) if data.get("entry_price") is not None else None,
        "current_price": _to_float(data.get("current_price")) if data.get("current_price") is not None else None,
        "status": status,
        "notes": data.get("notes"),
        "updated_at": pd.Timestamp.now().isoformat(),
    }
    if status == "closed":
        payload["closed_at"] = pd.Timestamp.now().isoformat()
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        updated = _sb.table("portfolio_positions").update(payload).eq("id", position_id).execute().data
        if not updated:
            return jsonify({"ok": False, "error": "position not found"}), 404
        row = updated[0]
        event_type = "close" if row.get("status") == "closed" else "update"
        _record_portfolio_event(event_type, row.get("symbol"), position_id=position_id, payload=payload, message=f"Position {event_type}d")
        return jsonify({"ok": True, "position": row})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/positions/<position_id>", methods=["DELETE"])
def delete_portfolio_position(position_id):
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        deleted = _sb.table("portfolio_positions").delete().eq("id", position_id).execute().data
        if not deleted:
            return jsonify({"ok": False, "error": "position not found"}), 404
        row = deleted[0]
        _record_portfolio_event("delete", row.get("symbol"), position_id=position_id, payload={"id": position_id}, message="Position deleted")
        return jsonify({"ok": True, "deleted": position_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/history")
def portfolio_history():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        events = _sb.table("portfolio_events").select("*").order("created_at", desc=True).execute().data
        return jsonify({"ok": True, "history": events or []})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/portfolio/analytics")
def portfolio_analytics():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        positions = _sb.table("portfolio_positions").select("*").execute().data or []
        active = [p for p in positions if p.get("status") == "active"]
        closed = [p for p in positions if p.get("status") == "closed"]

        total_invested = sum(_to_float(p.get("qty")) * _to_float(p.get("entry_price")) for p in active)
        current_value = sum(_to_float(p.get("qty")) * _to_float(p.get("current_price"), _to_float(p.get("entry_price"))) for p in active)
        unrealized_pnl = current_value - total_invested

        realized_values = [
            (_to_float(p.get("qty")) * (_to_float(p.get("current_price")) - _to_float(p.get("entry_price"))))
            for p in closed
        ]
        realized_pnl = sum(realized_values)
        wins = len([v for v in realized_values if v > 0])
        closed_count = len(closed)
        win_rate = (wins / closed_count * 100.0) if closed_count else 0.0

        return jsonify({
            "ok": True,
            "analytics": {
                "active_positions": len(active),
                "closed_positions": closed_count,
                "total_invested": round(total_invested, 2),
                "current_value": round(current_value, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "realized_pnl": round(realized_pnl, 2),
                "win_rate": round(win_rate, 1),
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/check-outcomes", methods=["POST"])
def check_outcomes():
    payload, status = run_trade_outcome_check()
    return jsonify(payload), status


if __name__ == "__main__":
    print("=" * 60)
    print("  NSE Swing Screener — Adaptive Regime Engine v3")
    print("  Listening on http://localhost:5001")
    print("=" * 60)
    tok = get_access_token()
    print(f"  Groww Auth  : {'OK' if tok else 'FAILED'}")
    print(f"  yfinance    : {'OK' if YF_OK else 'MISSING'}")
    print(f"  Supabase    : {'OK' if SB_OK else 'Not configured'}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5001, debug=False)
