"""
NSE Swing Screener — Extended Proxy Server v2
----------------------------------------------
Run:  python groww_proxy.py
Deps: pip install flask flask-cors requests yfinance pandas numpy

Endpoints:
  GET  /health                — health + yfinance status
  GET  /ltp?symbols=...       — live prices via Groww API
  GET  /quote/<symbol>        — single quote via Groww API
  POST /refresh-token         — force Groww token refresh
  GET  /universe?index=...    — NSE stock universe with sectors
  POST /screen                — start background scan → {job_id, total}
  GET  /scan-progress/<id>    — scan status + partial results
  POST /cancel-scan/<id>      — cancel a running scan
  GET  /market-data           — Nifty50 / BankNifty / VIX + breadth
"""

import hashlib, os, time, requests, threading, uuid
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
        self._base    = base
        self._headers = headers
        self._table   = table
        self._filters = {}
        self._upd     = None

    def select(self, cols="*"):
        self._cols = cols
        return self

    def eq(self, col, val):
        self._filters[col] = f"eq.{val}"
        return self

    def order(self, col, desc=False):
        self._order = f"{col}.{'desc' if desc else 'asc'}"
        return self

    def insert(self, rows):
        self._rows = rows
        return self

    def update(self, data):
        self._upd = data
        return self

    def execute(self):
        url = f"{self._base}/rest/v1/{self._table}"
        params = {k: v for k, v in self._filters.items()}
        h = {**self._headers, "Prefer": "return=representation"}

        if self._upd is not None:                          # PATCH
            r = requests.patch(url, headers=h, params=params,
                               json=self._upd, timeout=15)
        elif hasattr(self, "_rows"):                       # POST insert
            r = requests.post(url, headers=h, json=self._rows, timeout=15)
        else:                                              # GET select
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
        self._h    = {"apikey": key, "Authorization": f"Bearer {key}",
                      "Content-Type": "application/json"}
    def table(self, name):
        return _SBTable(self._base, self._h, name)


try:
    _sb   = _SBClient(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    # quick connectivity probe
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

# ── GROWW CREDENTIALS ────────────────────────────────────────────
API_KEY    = "eyJraWQiOiJaTUtjVXciLCJhbGciOiJFUzI1NiJ9.eyJleHAiOjI1NjI3NzIwMzUsImlhdCI6MTc3NDM3MjAzNSwibmJmIjoxNzc0MzcyMDM1LCJzdWIiOiJ7XCJ0b2tlblJlZklkXCI6XCJmOGYyNmQ3YS05ZWU5LTQ3OTMtYjhlNi03YjQ1MDJmMTQwODJcIixcInZlbmRvckludGVncmF0aW9uS2V5XCI6XCJlMzFmZjIzYjA4NmI0MDZjODg3NGIyZjZkODQ5NTMxM1wiLFwidXNlckFjY291bnRJZFwiOlwiMGFlMDJiYmMtMjdhOS00OGQ3LWFjNWUtZjkxODA0ZTlhMDc1XCIsXCJkZXZpY2VJZFwiOlwiODZkZjhkOTYtMTU1Ni01MjkxLTk2YTgtOTZkN2U4MzgwZmM0XCIsXCJzZXNzaW9uSWRcIjpcImYzNGFkZjMyLTcxNTMtNDg4MS04MjY4LThkOTQ3YzYyY2M1ZFwiLFwiYWRkaXRpb25hbERhdGFcIjpcIno1NC9NZzltdjE2WXdmb0gvS0EwYklUT2hnRmFrdHd6V21OL0FPUDFwdkJSTkczdTlLa2pWZDNoWjU1ZStNZERhWXBOVi9UOUxIRmtQejFFQisybTdRPT1cIixcInJvbGVcIjpcImF1dGgtdG90cFwiLFwic291cmNlSXBBZGRyZXNzXCI6XCIyNDAxOjQ5MDA6MWM1YzpiOTYwOjhkNzg6YTNkYTphYmY5OjEwMTgsMTcyLjY5LjIwMy4zNiwzNS4yNDEuMjMuMTIzXCIsXCJ0d29GYUV4cGlyeVRzXCI6MjU2Mjc3MjAzNTc4NixcInZlbmRvck5hbWVcIjpcImdyb3d3QXBpXCJ9IiwiaXNzIjoiYXBleC1hdXRoLXByb2QtYXBwIn0.12dGLN64_um4ggPlAxG9l958Qbc7o9qx61E9sH7R91cy7_bsbSQrxYlj7-itxd28RQ3yoCVr83NbyTuon76_Cw"
API_SECRET = "w-Op1u#i8^sgwL1cYfuK2Lb9ee12qGwu"
BASE_URL   = "https://api.groww.in/v1"

app = Flask(__name__)
CORS(app)

# ── GROWW AUTH ───────────────────────────────────────────────────
_access_token     = None
_token_fetched_at = 0

_session_api_key    = None
_session_api_secret = None
_cred_lock          = threading.Lock()

def generate_checksum(secret, timestamp):
    return hashlib.sha256((secret + timestamp).encode()).hexdigest()

def get_access_token(force=False):
    global _access_token, _token_fetched_at
    if _access_token and not force and (time.time() - _token_fetched_at) < 14400:
        return _access_token
    with _cred_lock:
        key    = _session_api_key    or API_KEY
        secret = _session_api_secret or API_SECRET
    ts   = str(int(time.time()))
    resp = requests.post(
        f"{BASE_URL}/token/api/access",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"key_type": "approval", "checksum": generate_checksum(secret, ts), "timestamp": ts},
        timeout=10,
    )
    d = resp.json()
    if "token" in d:
        _access_token, _token_fetched_at = d["token"], time.time()
        print("[Auth] Token refreshed OK")
        return _access_token
    print(f"[Auth] Token fetch failed: {d}")
    return None

def groww_headers():
    return {"Authorization": f"Bearer {get_access_token()}",
            "Accept": "application/json", "X-API-VERSION": "1.0"}


# ══════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════

def calc_rsi(closes, period=14):
    """Wilder's smoothed RSI"""
    s     = pd.Series(closes, dtype=float)
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
    ema_f = closes.ewm(span=fast,       adjust=False).mean()
    ema_s = closes.ewm(span=slow,       adjust=False).mean()
    macd  = ema_f - ema_s
    sig   = macd.ewm(span=sig_period,   adjust=False).mean()
    hist  = macd - sig
    if float(macd.iloc[-1]) > float(sig.iloc[-1]) and float(hist.iloc[-1]) > 0:
        return "bullish"
    if float(macd.iloc[-1]) < float(sig.iloc[-1]) and float(hist.iloc[-1]) < 0:
        return "bearish"
    return "neutral"

def calc_atr(highs, lows, closes, period=14):
    """ATR(14) and ATR/price %"""
    h = np.array(highs,  dtype=float)
    l = np.array(lows,   dtype=float)
    c = np.array(closes, dtype=float)
    if len(c) < period + 1:
        return None, None
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]),
                    np.abs(l[1:] - c[:-1])))
    atr     = float(pd.Series(tr).ewm(alpha=1/period, adjust=False).mean().iloc[-1])
    atr_pct = atr / c[-1] * 100
    return round(atr, 2), round(atr_pct, 2)

def calc_adx(highs, lows, closes, period=14):
    """Average Directional Index"""
    h = np.array(highs,  dtype=float)
    l = np.array(lows,   dtype=float)
    c = np.array(closes, dtype=float)
    if len(c) < period * 2 + 1:
        return None
    tr     = np.maximum(h[1:] - l[1:],
             np.maximum(np.abs(h[1:] - c[:-1]),
                        np.abs(l[1:] - c[:-1])))
    up     = h[1:] - h[:-1]
    down   = l[:-1] - l[1:]
    dm_p   = np.where((up > down) & (up > 0), up,   0.0)
    dm_m   = np.where((down > up) & (down > 0), down, 0.0)
    atr14  = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean()
    dip14  = pd.Series(dm_p).ewm(alpha=1/period, adjust=False).mean()
    dim14  = pd.Series(dm_m).ewm(alpha=1/period, adjust=False).mean()
    di_p   = 100 * dip14 / atr14.replace(0, np.nan)
    di_m   = 100 * dim14 / atr14.replace(0, np.nan)
    denom  = (di_p + di_m).replace(0, np.nan)
    dx     = 100 * np.abs(di_p - di_m) / denom
    adx    = dx.ewm(alpha=1/period, adjust=False).mean()
    val    = float(adx.iloc[-1])
    return round(val, 1) if not np.isnan(val) else None

def calc_bollinger_position(closes, period=20, std_dev=2):
    """
    Returns (position_str, upper, middle, lower)
    position: 'sweet_spot' | 'above_upper' | 'below_middle'
    """
    s = pd.Series(closes, dtype=float)
    if len(s) < period:
        return "unknown", None, None, None
    mid = float(s.rolling(period).mean().iloc[-1])
    std = float(s.rolling(period).std().iloc[-1])
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    price = float(s.iloc[-1])
    if price > upper:
        pos = "above_upper"
    elif price >= mid:
        pos = "sweet_spot"
    else:
        pos = "below_middle"
    return pos, round(upper, 2), round(mid, 2), round(lower, 2)

def check_higher_lows(lows, n=3):
    """Last n candle lows are strictly ascending"""
    vals = [float(x) for x in lows[-(n+1):] if not np.isnan(float(x))]
    if len(vals) < n:
        return False
    vals = vals[-n:]
    return all(vals[i] > vals[i-1] for i in range(1, n))

def check_no_gap_down(opens, closes, threshold=0.015, n=5):
    """True if no gap-down > threshold% in last n sessions"""
    o = list(opens[-(n):])
    c = list(closes[-(n+1):-1])
    for op, pc in zip(o, c):
        if float(pc) > 0 and (float(pc) - float(op)) / float(pc) > threshold:
            return False
    return True

def calc_relative_strength_10d(stock_closes, nifty_closes):
    """Returns (stock_ret%, nifty_ret%, is_outperforming)"""
    if len(stock_closes) < 11 or len(nifty_closes) < 11:
        return None, None, None
    sc = stock_closes[-11:]
    nc = nifty_closes[-11:]
    s_ret = (float(sc[-1]) - float(sc[0])) / float(sc[0]) * 100
    n_ret = (float(nc[-1]) - float(nc[0])) / float(nc[0]) * 100
    return round(s_ret, 2), round(n_ret, 2), s_ret > n_ret

def calc_52wk_proximity(closes_1y, highs_1y):
    """(is_within_25pct, pct_below_52wk_high)"""
    price    = float(closes_1y[-1])
    hi52     = float(np.nanmax(highs_1y))
    pct_below = (hi52 - price) / hi52 * 100 if hi52 > 0 else 0
    return round(pct_below, 1)


# ══════════════════════════════════════════════════════════════════
# NSE UNIVERSE (NSE India public API)
# ══════════════════════════════════════════════════════════════════

_universe_cache = {"data": None, "index": None, "fetched_at": 0}
_universe_lock  = threading.Lock()
UNIVERSE_TTL    = 3600

NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-US,en;q=0.9",
    "Referer":          "https://www.nseindia.com/market-data/live-equity-market",
    "X-Requested-With": "XMLHttpRequest",
}

UNIVERSE_INDICES = {
    "NIFTY 50":  ["NIFTY 50"],
    "NIFTY 100": ["NIFTY 100"],
    "NIFTY 200": ["NIFTY 200"],
    "NIFTY 500": ["NIFTY 500"],
    "ALL":       ["NIFTY 500", "NIFTY MIDCAP 150", "NIFTY SMALLCAP 250", "NIFTY NEXT 50"],
}

# Sector → Yahoo Finance index symbol
SECTOR_YF = {
    "INFORMATION TECHNOLOGY": "^CNXIT",
    "IT":                     "^CNXIT",
    "METALS - FERROUS":       "^CNXMETAL",
    "METALS":                 "^CNXMETAL",
    "NON FERROUS METALS":     "^CNXMETAL",
    "BANKING":                "^NSEBANK",
    "PSU BANK":               "^CNXPSUBANK",
    "FINANCIAL SERVICES":     "^CNXFIN",
    "PHARMA":                 "^CNXPHARMA",
    "PHARMACEUTICAL":         "^CNXPHARMA",
    "HEALTHCARE":             "^CNXPHARMA",
    "AUTOMOBILE":             "^CNXAUTO",
    "AUTOMOBILES":            "^CNXAUTO",
    "AUTO":                   "^CNXAUTO",
    "FMCG":                   "^CNXFMCG",
    "CONSUMER GOODS":         "^CNXFMCG",
    "ENERGY":                 "^CNXENERGY",
    "OIL & GAS":              "^CNXENERGY",
    "REALTY":                 "^CNXREALTY",
    "MEDIA":                  "^CNXMEDIA",
    "INFRASTRUCTURE":         "^CNXINFRA",
    "CONSTRUCTION":           "^CNXINFRA",
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
                "symbol":   sym,
                "name":     (meta.get("companyName") or sym).strip(),
                "sector":   sector,
                "industry": (meta.get("industry") or "").strip(),
            }
        return stocks
    except Exception as e:
        print(f"[NSE] {index_name}: {e}")
        return {}

def fetch_universe(scope="NIFTY 500"):
    indices = UNIVERSE_INDICES.get(scope, ["NIFTY 500"])
    session = _nse_session()
    merged  = {}
    for idx in indices:
        merged.update(_fetch_index(session, idx))
        time.sleep(0.4)
    result = list(merged.values())
    print(f"[Universe] {scope}: {len(result)} stocks")
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


# ══════════════════════════════════════════════════════════════════
# CONTEXT PRE-FETCH (market-level data, fetched once per scan)
# ══════════════════════════════════════════════════════════════════

def _prefetch_nifty_closes():
    try:
        hist = yf.Ticker("^NSEI").history(period="3mo", interval="1d", auto_adjust=True)
        return hist["Close"].dropna().values.astype(float)
    except Exception as e:
        print(f"[Ctx] Nifty: {e}")
        return np.array([])

def _prefetch_sector_ema(sectors):
    """Returns {sector_name: bool (above EMA20)}"""
    needed = {}   # yf_sym → set of sector names
    for s in sectors:
        yf_sym = SECTOR_YF.get(s.upper())
        if yf_sym:
            needed.setdefault(yf_sym, set()).add(s)

    result = {}
    for yf_sym, sector_names in needed.items():
        try:
            hist   = yf.Ticker(yf_sym).history(period="2mo", interval="1d", auto_adjust=True)
            closes = hist["Close"].dropna().values.astype(float)
            above  = (len(closes) >= 20
                      and float(closes[-1]) > float(calc_ema(closes, 20).iloc[-1]))
        except Exception:
            above = True   # unknown → don't penalise
        for s in sector_names:
            result[s] = above
    return result

def _prefetch_earnings_symbols():
    """Returns set of symbols with board meetings/results in next 5 days"""
    syms = set()
    try:
        session = _nse_session()
        r = session.get("https://www.nseindia.com/api/event-calendar",
                        headers=NSE_HEADERS, timeout=10)
        if r.status_code == 200:
            today   = pd.Timestamp.now().normalize()
            cutoff  = today + pd.Timedelta(days=5)
            for ev in r.json():
                sym  = ev.get("symbol", "")
                ds   = ev.get("date") or ev.get("bm_date", "")
                if not sym or not ds:
                    continue
                try:
                    if today <= pd.to_datetime(ds) <= cutoff:
                        syms.add(sym)
                except Exception:
                    pass
    except Exception as e:
        print(f"[Earnings] {e}")
    print(f"[Earnings] {len(syms)} symbols with upcoming results")
    return syms

def _prefetch_institutional_symbols():
    """Returns set of symbols with bulk/block deal activity (last 3 sessions)"""
    syms = set()
    try:
        session = _nse_session()
        for endpoint in ["block-deal", "bulk-deal"]:
            r = session.get(f"https://www.nseindia.com/api/{endpoint}",
                            headers=NSE_HEADERS, timeout=10)
            if r.status_code == 200:
                for deal in r.json().get("data", []):
                    sym = deal.get("symbol", "")
                    if sym:
                        syms.add(sym)
    except Exception as e:
        print(f"[Institutional] {e}")
    print(f"[Institutional] {len(syms)} symbols with recent deals")
    return syms

def _prefetch_market_breadth():
    """Advance/Decline ratio from NSE 500 index response"""
    try:
        session = _nse_session()
        r = session.get("https://www.nseindia.com/api/equity-stockIndices",
                        params={"index": "NIFTY 500"}, headers=NSE_HEADERS, timeout=15)
        if r.status_code == 200:
            adv = r.json().get("advance", {}) or {}
            advances  = int(adv.get("advances",  0) or 0)
            declines  = int(adv.get("declines",  0) or 0)
            unchanged = int(adv.get("unchanged", 0) or 0)
            ratio     = round(advances / max(declines, 1), 2)
            print(f"[Breadth] A:{advances} D:{declines} ratio:{ratio}")
            return {"advances": advances, "declines": declines,
                    "unchanged": unchanged, "ratio": ratio,
                    "breadth_ok": ratio >= 1.2}
    except Exception as e:
        print(f"[Breadth] {e}")
    return {"advances": 0, "declines": 0, "unchanged": 0,
            "ratio": 1.0, "breadth_ok": True}

def _fetch_delivery_pct(symbol, session):
    """Delivery-to-traded % for one symbol from NSE trade-info API"""
    try:
        r = session.get("https://www.nseindia.com/api/quote-equity",
                        params={"symbol": symbol, "type": "trade_info"},
                        headers=NSE_HEADERS, timeout=8)
        if r.status_code == 200:
            ti = (r.json().get("marketDeptOrderBook", {}) or {}).get("tradeInfo", {}) or {}
            pct = ti.get("deliveryToTradedQuantity")
            if pct is not None:
                return float(pct)
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════
# PER-STOCK ANALYSIS
# ══════════════════════════════════════════════════════════════════

def fetch_ohlcv(symbol):
    if not YF_OK:
        return None
    try:
        df = yf.Ticker(f"{symbol}.NS").history(
            period="1y", interval="1d", auto_adjust=True, timeout=10)
        if df is None or df.empty or len(df) < 20:
            return None
        return df
    except Exception:
        return None


def analyze_stock(sym_info, df, params, context):
    """
    Full analysis with all 12 new filters + original filters.

    params keys:
      # ── Original ────────────────────────────────────────────────
      rsi_min / rsi_max       RSI(14) range
      vol_min                 volume ratio vs 20-day avg
      ema_filter              "any" | "at_or_above" | "above"
      macd_filter             "any" | "bullish_neutral" | "bullish"
      min_score               minimum score (0–100)

      # ── Tier 1 ──────────────────────────────────────────────────
      rs_filter               bool — stock 10d > Nifty 10d
      high52_max_pct          float — max % below 52-week high (0 = off)
      higher_lows             bool — last 3 candle lows ascending

      # ── Tier 2 ──────────────────────────────────────────────────
      atr_min_pct             float — ATR(14)/price min % (0 = off)
      atr_max_pct             float — ATR(14)/price max % (0 = off)
      sector_momentum         bool — sector index above its EMA20
      earnings_filter         bool — exclude stocks with results in 5d
      fii_dii_filter          bool — require recent bulk/block deal
      delivery_min            float — min delivery % (0 = off)

      # ── Tier 3 ──────────────────────────────────────────────────
      bb_filter               bool — price between BB mid and upper
      adx_min                 int  — minimum ADX(14) value (0 = off)
      no_gap_down             bool — no gap-down >1.5% in last 5d
      breadth_filter          bool — market A/D ratio ≥ 1.2

    context keys:
      nifty_closes            np.array
      sector_ema              dict sector→bool
      earnings_syms           set
      inst_syms               set
      breadth                 dict
    """
    try:
        closes  = df["Close"].dropna().values.astype(float)
        volumes = df["Volume"].dropna().values.astype(float)
        highs   = df["High"].dropna().values.astype(float)
        lows    = df["Low"].dropna().values.astype(float)
        opens   = df["Open"].dropna().values.astype(float)
    except KeyError:
        return None

    n = min(len(closes), len(volumes), len(highs), len(lows), len(opens))
    if n < 20:
        return None
    closes, volumes, highs, lows, opens = (
        closes[-n:], volumes[-n:], highs[-n:], lows[-n:], opens[-n:])

    sym    = sym_info["symbol"]
    sector = sym_info.get("sector", "Unknown")
    price  = float(closes[-1])

    # ── Core indicators ─────────────────────────────────────────
    rsi_val  = float(calc_rsi(closes).iloc[-1])
    ema20    = float(calc_ema(closes, 20).iloc[-1])
    macd_sig = calc_macd_signal(closes)

    avg_vol  = float(np.mean(volumes[-21:-1])) if len(volumes) > 21 else float(np.mean(volumes[:-1]))
    vol_ratio = float(volumes[-1]) / avg_vol if avg_vol > 0 else 0.0

    diff_pct = (price - ema20) / ema20 * 100
    ema_pos  = "above" if diff_pct > 0.5 else ("at" if diff_pct > -0.5 else "below")

    recent_high = float(np.max(highs[-21:-1])) if len(highs) > 21 else float(np.max(highs[:-1]))
    breakout    = price > recent_high

    # ── New indicators ───────────────────────────────────────────
    atr_val, atr_pct = calc_atr(highs, lows, closes)
    adx_val          = calc_adx(highs, lows, closes)
    bb_pos, bb_up, bb_mid, bb_lo = calc_bollinger_position(closes)
    higher_lows_ok   = check_higher_lows(lows)
    no_gap_ok        = check_no_gap_down(opens, closes)
    s_ret, n_ret, rs_ok = calc_relative_strength_10d(
        closes, context.get("nifty_closes", np.array([])))
    pct_below_52 = calc_52wk_proximity(closes, highs)   # uses full 1y data

    # ── FILTER GATES ─────────────────────────────────────────────
    def rej(reason):
        return {"rejected": True, "reason": reason,
                "sym": sym, "name": sym_info.get("name", sym),
                "sector": sector, "price": round(price, 2),
                "rsi": round(rsi_val, 1), "vol_ratio": round(vol_ratio, 2),
                "ema_pos": ema_pos, "macd": macd_sig}

    # Original filters
    rsi_min = float(params.get("rsi_min", 45))
    rsi_max = float(params.get("rsi_max", 70))
    if not (rsi_min <= rsi_val <= rsi_max):
        return rej(f"RSI {rsi_val:.1f} out of [{rsi_min:.0f}–{rsi_max:.0f}]")
    vol_min = float(params.get("vol_min", 1.4))
    if vol_ratio < vol_min:
        return rej(f"Volume {vol_ratio:.2f}x < {vol_min}x")
    ef = params.get("ema_filter", "at_or_above")
    if ef == "above" and ema_pos != "above":
        return rej(f"Price {ema_pos} EMA20 (need above)")
    if ef == "at_or_above" and ema_pos == "below":
        return rej("Price below EMA20")
    mf = params.get("macd_filter", "bullish_neutral")
    if mf == "bullish"         and macd_sig != "bullish":
        return rej(f"MACD {macd_sig} (need bullish)")
    if mf == "bullish_neutral" and macd_sig == "bearish":
        return rej("MACD bearish")

    # Tier 1
    if params.get("rs_filter") and rs_ok is False:
        return rej(f"RS: stock {s_ret:.1f}% vs Nifty {n_ret:.1f}% (underperforming)")

    h52_max = float(params.get("high52_max_pct", 0) or 0)
    if h52_max > 0 and pct_below_52 > h52_max:
        return rej(f"{pct_below_52:.1f}% below 52wk high (limit {h52_max:.0f}%)")

    if params.get("higher_lows") and not higher_lows_ok:
        return rej("No higher lows in last 3 candles")

    # Tier 2
    atr_mn = float(params.get("atr_min_pct", 0) or 0)
    atr_mx = float(params.get("atr_max_pct", 0) or 0)
    if atr_pct is not None:
        if atr_mn > 0 and atr_pct < atr_mn:
            return rej(f"ATR% {atr_pct:.2f} < min {atr_mn:.2f}")
        if atr_mx > 0 and atr_pct > atr_mx:
            return rej(f"ATR% {atr_pct:.2f} > max {atr_mx:.2f}")

    if params.get("sector_momentum"):
        sect_ema = context.get("sector_ema", {})
        sect_ok  = sect_ema.get(sector)
        if sect_ok is False:           # only block if explicitly False; None = unknown
            return rej(f"Sector '{sector}' below EMA20")

    if params.get("earnings_filter") and sym in context.get("earnings_syms", set()):
        return rej("Earnings / board meeting due in 5 days")

    if params.get("fii_dii_filter") and sym not in context.get("inst_syms", set()):
        return rej("No recent FII/DII bulk/block deal activity")

    del_pct = context.get("delivery_cache", {}).get(sym)   # pre-fetched if available
    del_min = float(params.get("delivery_min", 0) or 0)
    if del_min > 0 and (del_pct is None or del_pct < del_min):
        return rej(f"Delivery% {del_pct if del_pct is not None else 'N/A'} < {del_min:.0f}%")

    # Tier 3
    if params.get("bb_filter") and bb_pos not in ("sweet_spot",):
        return rej(f"BB position: {bb_pos} (need sweet_spot)")

    adx_min_val = int(params.get("adx_min", 0) or 0)
    if adx_min_val > 0 and (adx_val is None or adx_val < adx_min_val):
        return rej(f"ADX {adx_val if adx_val is not None else 'N/A'} < {adx_min_val}")

    if params.get("no_gap_down") and not no_gap_ok:
        return rej("Gap-down >1.5% detected in last 5 sessions")

    # Market breadth gate
    if params.get("breadth_filter"):
        breadth = context.get("breadth", {})
        if not breadth.get("breadth_ok", True):
            # Breadth weak → downgrade (don't block entirely, just cap signals later)
            pass  # handled in signal assignment below

    # ── SCORE (updated weights as specified) ─────────────────────
    score = 0

    # RSI 50–65: 20 pts
    if 50 <= rsi_val <= 65:   score += 20
    elif rsi_val >= 45:       score += 12
    else:                     score += 4

    # Volume ≥ 2×: 20 pts
    if vol_ratio >= 2.0:      score += 20
    elif vol_ratio >= 1.5:    score += 15
    else:                     score += 8

    # EMA position: 15 pts
    if ema_pos == "above":    score += 15
    elif ema_pos == "at":     score += 8

    # MACD: 15 pts
    if macd_sig == "bullish":  score += 15
    elif macd_sig == "neutral": score += 6

    # Relative strength vs Nifty: 10 pts
    if rs_ok:                  score += 10
    elif rs_ok is None:        score += 5   # unknown

    # ADX ≥ 20: 8 pts
    if adx_val is not None:
        if adx_val >= 25:      score += 8
        elif adx_val >= 20:    score += 5

    # Delivery % ≥ 40%: 7 pts
    if del_pct is not None:
        if del_pct >= 60:      score += 7
        elif del_pct >= 40:    score += 5
        elif del_pct >= 25:    score += 2

    # Higher low pattern: 5 pts
    if higher_lows_ok:        score += 5

    # Breakout bonus: inherent from existing logic, keep at 5
    if breakout:              score += 5

    score = min(score, 100)

    min_score = int(params.get("min_score", 0) or 0)
    if score < min_score:
        return rej(f"Score {score} < min {min_score}")

    # ── SIGNAL ───────────────────────────────────────────────────
    breadth_weak = params.get("breadth_filter") and not context.get("breadth", {}).get("breadth_ok", True)

    if (score >= 75 and macd_sig == "bullish"
            and ema_pos == "above" and vol_ratio >= 1.8 and not breadth_weak):
        sig = "STRONG BUY"
    elif (score >= 50
            and macd_sig in ("bullish", "neutral")
            and ema_pos in ("above", "at")
            and not breadth_weak):
        sig = "BUY"
    elif breadth_weak and score >= 65:
        sig = "BUY"       # downgrade from STRONG BUY when breadth weak
    else:
        sig = "WATCH"

    result = {
        "sym":         sym,
        "name":        sym_info.get("name", sym),
        "sector":      sector,
        "industry":    sym_info.get("industry", ""),
        "price":       round(price, 2),
        # Core
        "rsi":         round(rsi_val, 1),
        "vol_ratio":   round(vol_ratio, 2),
        "ema_pos":     ema_pos,
        "ema20":       round(ema20, 2),
        "macd":        macd_sig,
        # New
        "atr_pct":     atr_pct,
        "adx":         adx_val,
        "bb_pos":      bb_pos,
        "higher_lows": higher_lows_ok,
        "no_gap_down": no_gap_ok,
        "rs_stock":    s_ret,
        "rs_nifty":    n_ret,
        "rs_ok":       rs_ok,
        "pct_below_52": pct_below_52,
        "delivery_pct": del_pct,
        "breakout":    breakout,
        "score":       score,
        "sig":         sig,
        "breadth_downgraded": breadth_weak,
    }

    # ── TARGETS & SL — only for BUY / STRONG BUY ─────────────────
    if sig in ("STRONG BUY", "BUY"):
        tp_pct   = 4.0 if sig == "STRONG BUY" else 3.5
        support  = float(np.min(lows[-11:-1])) if len(lows) > 11 else float(np.min(lows[:-1]))
        sl_price = max(support, price * 0.98)
        sl_pct   = (price - sl_price) / price * 100
        tp_pct   = max(tp_pct, sl_pct * 2)   # enforce min 1:2 R:R
        result.update({
            "t1":     round(price * (1 + tp_pct / 100), 2),
            "t2":     round(price * (1 + (tp_pct + 0.5) / 100), 2),
            "sl":     round(sl_price, 2),
            "tp_pct": round(tp_pct, 2),
            "sl_pct": round(sl_pct, 2),
            "rr":     round(tp_pct / max(sl_pct, 0.01), 2),
        })

    return result


# ══════════════════════════════════════════════════════════════════
# SCAN JOB
# ══════════════════════════════════════════════════════════════════

_jobs      = {}
_jobs_lock = threading.Lock()


def scan_worker(job_id, stocks, params):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return

    job.update({"total": len(stocks), "status": "prefetch", "progress": 0,
                "results": [], "status_msg": "Fetching market context…"})

    # ── Phase 1: Pre-fetch context ────────────────────────────────
    print(f"[Scan {job_id}] Pre-fetching context…")

    nifty_closes = _prefetch_nifty_closes()
    job["status_msg"] = "Nifty data ready"

    unique_sectors = list({s.get("sector", "") for s in stocks})
    sector_ema = _prefetch_sector_ema(unique_sectors) if params.get("sector_momentum") else {}
    job["status_msg"] = "Sector EMA computed"

    earnings_syms = _prefetch_earnings_symbols()  if params.get("earnings_filter")  else set()
    inst_syms     = _prefetch_institutional_symbols() if params.get("fii_dii_filter") else set()
    breadth       = _prefetch_market_breadth()

    job["breadth"]     = breadth
    job["status_msg"]  = f"Breadth A/D={breadth['ratio']} — scanning {len(stocks)} stocks…"
    job["status"]      = "running"

    context = {
        "nifty_closes":   nifty_closes,
        "sector_ema":     sector_ema,
        "earnings_syms":  earnings_syms,
        "inst_syms":      inst_syms,
        "breadth":        breadth,
        "delivery_cache": {},   # populated lazily per stock if needed
    }

    # Delivery % requires NSE session; reuse it across stocks if needed
    delivery_session = _nse_session() if float(params.get("delivery_min", 0) or 0) > 0 else None

    # ── Phase 2: Per-stock scan ───────────────────────────────────
    passed     = []
    rejected   = []
    completed  = [0]
    inner_lock = threading.Lock()

    def process(sym_info):
        if job.get("cancelled"):
            return None
        sym = sym_info["symbol"]

        # Optionally pre-fetch delivery % now if filter is active
        if delivery_session and float(params.get("delivery_min", 0) or 0) > 0:
            dp = _fetch_delivery_pct(sym, delivery_session)
            if dp is not None:
                context["delivery_cache"][sym] = dp

        df = fetch_ohlcv(sym)
        if df is None:
            return None
        return analyze_stock(sym_info, df, params, context)

    workers = min(15, max(1, len(stocks)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fmap = {ex.submit(process, s): s for s in stocks}
        for fut in as_completed(fmap):
            if job.get("cancelled"):
                job["status"] = "cancelled"
                return
            res = fut.result()
            with inner_lock:
                completed[0] += 1
                if res is None:
                    pass  # no data / error
                elif res.get("rejected"):
                    rejected.append(res)
                else:
                    passed.append(res)
                job["progress"] = completed[0]
                job["results"]  = sorted(passed, key=lambda x: x["score"], reverse=True)
                job["rejected"] = rejected

    job["status"]   = "done"
    job["results"]  = sorted(passed, key=lambda x: x["score"], reverse=True)
    job["rejected"] = rejected
    print(f"[Scan {job_id}] Done — {len(passed)}/{len(stocks)} passed, {len(rejected)} rejected")


# ══════════════════════════════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════════════════════════════

def fetch_market_data():
    if not YF_OK:
        return {}
    result = {}
    for name, sym in [("nifty50", "^NSEI"), ("banknifty", "^NSEBANK"), ("indiavix", "^INDIAVIX")]:
        try:
            hist = yf.Ticker(sym).history(period="1mo", interval="1d", auto_adjust=True)
            if hist.empty:
                continue
            closes = hist["Close"].dropna().values.astype(float)
            price  = float(closes[-1])
            prev   = float(closes[-2]) if len(closes) > 1 else price
            chg    = price - prev
            result[name] = {
                "price":     round(price, 2),
                "change":    round(chg, 2),
                "chg_pct":   round(chg / prev * 100, 2),
                "rsi":       round(float(calc_rsi(closes).iloc[-1]), 1) if len(closes) >= 15 else None,
                "direction": "up" if chg >= 0 else "down",
            }
        except Exception as e:
            print(f"[Market] {name}: {e}")

    # Add market breadth
    try:
        result["breadth"] = _prefetch_market_breadth()
    except Exception:
        pass
    return result


# ══════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({"status": "ok", "yf_available": YF_OK, "time": time.time(),
                    "session_creds_active": bool(_session_api_key),
                    "supabase_ok": SB_OK, "build": "v3-requests"})

@app.route("/ltp")
def get_ltp():
    symbols = [s.strip() for s in request.args.get("symbols", "").split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "No symbols"}), 400
    results, failed = {}, []
    for sym in symbols:
        try:
            r = requests.get(f"{BASE_URL}/live-data/quote",
                             params={"exchange": "NSE", "segment": "CASH", "trading_symbol": sym},
                             headers=groww_headers(), timeout=5)
            d = r.json()
            if d.get("status") == "SUCCESS":
                p = d["payload"]
                results[sym] = {
                    "ltp":        p.get("last_price") or p.get("ltp"),
                    "change":     round(p.get("day_change") or 0, 2),
                    "change_pct": round(p.get("day_change_perc") or 0, 2),
                }
            else:
                failed.append(sym)
        except Exception as e:
            failed.append(sym); print(f"[LTP] {sym}: {e}")
    return jsonify({"prices": results, "failed": failed})

@app.route("/quote/<symbol>")
def get_quote(symbol):
    try:
        r = requests.get(f"{BASE_URL}/live-data/quote",
                         params={"exchange": "NSE", "segment": "CASH", "trading_symbol": symbol},
                         headers=groww_headers(), timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    if not YF_OK:
        return jsonify({"error": "yfinance not installed — pip install yfinance pandas numpy"}), 503
    params = request.get_json(silent=True) or {}
    scope  = params.pop("universe", "NIFTY 500")
    stocks = get_universe(scope)
    if not stocks:
        return jsonify({"error": "Could not fetch NSE universe"}), 503
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "progress": 0, "total": len(stocks),
                         "results": [], "rejected": [], "cancelled": False, "status_msg": "Queued…"}
    threading.Thread(target=scan_worker, args=(job_id, stocks, params), daemon=True).start()
    return jsonify({"job_id": job_id, "total": len(stocks), "scope": scope})

@app.route("/scan-progress/<job_id>")
def scan_progress(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":     job["status"],
        "status_msg": job.get("status_msg", ""),
        "progress":   job["progress"],
        "total":      job["total"],
        "found":      len(job["results"]),
        "results":    job["results"],
        "rejected":   job.get("rejected", []),
        "breadth":    job.get("breadth", {}),
    })

@app.route("/cancel-scan/<job_id>", methods=["POST"])
def cancel_scan(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
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
    global _session_api_key, _session_api_secret, _access_token
    data   = request.get_json(silent=True) or {}
    key    = data.get("api_key", "").strip()
    secret = data.get("api_secret", "").strip()
    if not key or not secret:
        return jsonify({"ok": False, "error": "api_key and api_secret required"}), 400
    with _cred_lock:
        _session_api_key    = key
        _session_api_secret = secret
        _access_token       = None   # force refresh
    tok = get_access_token(force=True)
    if tok:
        return jsonify({"ok": True})
    with _cred_lock:
        _session_api_key    = None
        _session_api_secret = None
    return jsonify({"ok": False, "error": "Groww rejected the credentials (401)"}), 401


@app.route("/save-trades", methods=["POST"])
def save_trades():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    rows = request.get_json(silent=True) or []
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "expected array"}), 400
    buyable = [r for r in rows if r.get("sig") in ("BUY", "STRONG BUY")]
    if not buyable:
        return jsonify({"ok": True, "saved": 0})
    to_insert = []
    for r in buyable:
        price = float(r.get("price") or 0)
        to_insert.append({
            "symbol":         r.get("sym") or r.get("symbol"),
            "name":           r.get("name"),
            "sector":         r.get("sector"),
            "signal":         r.get("sig"),
            "score":          r.get("score"),
            "entry_price":    price,
            "buy_range_low":  round(price * 0.990, 2),
            "buy_range_high": round(price * 1.005, 2),
            "target1":        r.get("t1"),
            "target2":        r.get("t2"),
            "stop_loss":      r.get("sl"),
            "tp_pct":         r.get("tp_pct"),
            "sl_pct":         r.get("sl_pct"),
            "rr":             r.get("rr"),
            "status":         "open",
        })
    try:
        _sb.table("trades").insert(to_insert).execute()
        return jsonify({"ok": True, "saved": len(to_insert)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/trades")
def get_trades():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    try:
        resp = _sb.table("trades").select("*").order("scanned_at", desc=True).execute()
        return jsonify({"ok": True, "trades": resp.data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/check-outcomes", methods=["POST"])
def check_outcomes():
    if not SB_OK:
        return jsonify({"ok": False, "error": "Supabase not configured"}), 503
    if not YF_OK:
        return jsonify({"ok": False, "error": "yfinance not available"}), 503
    try:
        resp = _sb.table("trades").select("*").eq("status", "open").execute()
        open_trades = [t for t in resp.data
                       if t.get("target1") is not None and t.get("stop_loss") is not None]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if not open_trades:
        return jsonify({"ok": True, "checked": 0, "updated": 0})

    updates    = []
    upd_lock   = threading.Lock()

    def check_one(trade):
        sym = trade["symbol"]
        t1  = float(trade["target1"])
        sl  = float(trade["stop_loss"])
        raw = trade.get("scanned_at", "")
        try:
            start = pd.Timestamp(raw).normalize() + pd.Timedelta(days=1)
        except Exception:
            return
        today = pd.Timestamp.now().normalize()
        if start > today:
            return
        try:
            df = yf.Ticker(f"{sym}.NS").history(
                start=start.strftime("%Y-%m-%d"),
                end=(today + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                interval="1d", auto_adjust=True)
        except Exception:
            return
        if df is None or df.empty:
            return
        status = outcome_price = outcome_date = days = None
        for i, (dt, row) in enumerate(df.iterrows()):
            low  = float(row["Low"])
            high = float(row["High"])
            if low <= sl:
                status        = "sl_hit"
                outcome_price = sl
                outcome_date  = dt.date().isoformat()
                days          = i + 1
                break
            elif high >= t1:
                status        = "target_hit"
                outcome_price = t1
                outcome_date  = dt.date().isoformat()
                days          = i + 1
                break
        if status:
            with upd_lock:
                updates.append({
                    "id":              trade["id"],
                    "status":          status,
                    "outcome_price":   outcome_price,
                    "outcome_date":    outcome_date,
                    "days_to_outcome": days,
                })

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(check_one, open_trades))

    if updates:
        try:
            for upd in updates:
                tid = upd.pop("id")
                _sb.table("trades").update(upd).eq("id", tid).execute()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "checked": len(open_trades), "updated": len(updates)})


if __name__ == "__main__":
    print("=" * 55)
    print("  NSE Swing Screener — Extended Proxy v2")
    print("  Listening on http://localhost:5001")
    print("=" * 55)
    tok = get_access_token()
    print(f"  Groww Auth : {'OK' if tok else 'FAILED'}")
    print(f"  yfinance   : {'OK' if YF_OK else 'MISSING — pip install yfinance pandas numpy'}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5001, debug=False)
