"""
KRYPTOS Backend Server v2 — Supabase Edition
Runs 24/7 on Railway. Uses Supabase REST API for persistent storage.

FIRST TIME SETUP — Run this SQL in Supabase SQL Editor:

CREATE TABLE IF NOT EXISTS predictions (
    id BIGSERIAL PRIMARY KEY,
    window_start BIGINT NOT NULL,
    window_end BIGINT NOT NULL,
    signal_time TEXT,
    direction TEXT NOT NULL,
    confidence INTEGER,
    score INTEGER,
    open_price REAL,
    close_price REAL,
    price_diff REAL,
    result TEXT DEFAULT 'pending',
    factors TEXT,
    market_data TEXT,
    odds INTEGER,
    bet_size REAL,
    potential_payout REAL,
    actual_pnl REAL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    date TEXT NOT NULL,
    time TEXT,
    direction TEXT,
    outcome TEXT,
    bet_amount REAL,
    odds INTEGER,
    pnl REAL,
    open_price REAL,
    confidence INTEGER,
    score INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

import os, json, time, threading, requests
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

PORT         = int(os.environ.get("PORT", 8080))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
HTML_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kryptos_v3.html")
WIN_SECS     = 14400
SIG_AT       = 1800

# ── SUPABASE HTTP ─────────────────────────────────────────────────────────────
def sh():
    return {"apikey":SUPABASE_KEY,"Authorization":f"Bearer {SUPABASE_KEY}",
            "Content-Type":"application/json","Prefer":"return=representation"}

def supa_url(table):
    # Remove any trailing /rest/v1 from SUPABASE_URL before adding our own
    base = SUPABASE_URL.rstrip("/")
    if base.endswith("/rest/v1"): base = base[:-8]
    return f"{base}/rest/v1/{table}"

def supa_get(table, params=None):
    r = requests.get(supa_url(table), headers=sh(), params=params, timeout=10)
    r.raise_for_status(); return r.json()

def supa_post(table, data):
    r = requests.post(supa_url(table), headers=sh(), json=data, timeout=10)
    r.raise_for_status(); return r.json()

def supa_patch(table, filter_str, data):
    r = requests.patch(f"{supa_url(table)}?{filter_str}", headers=sh(), json=data, timeout=10)
    r.raise_for_status(); return r.json()

def init_db():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("WARNING: SUPABASE_URL or SUPABASE_KEY not set"); return
    try:
        supa_get("predictions", {"limit":"1"})
        print("✓ Supabase connected successfully")
    except Exception as e:
        print(f"✗ Supabase error: {e}")
        print("  Create tables using the SQL in server.py comments")

# ── BINANCE ───────────────────────────────────────────────────────────────────
def fetch_candles(interval="4h", limit=60):
    r = requests.get(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}", timeout=10)
    r.raise_for_status(); return r.json()

def fetch_funding():
    r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1", timeout=10)
    r.raise_for_status(); d = r.json()
    return float(d[0]["fundingRate"]) if d else None

def fetch_oi():
    r = requests.get("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", timeout=10)
    r.raise_for_status(); return float(r.json()["openInterest"])

def fetch_fear_greed():
    r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
    r.raise_for_status(); return int(r.json()["data"][0]["value"])

def fetch_ls():
    r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=4h&limit=1", timeout=10)
    r.raise_for_status(); d = r.json()
    return float(d[0]["longShortRatio"]) if d else None

# ── INDICATORS ────────────────────────────────────────────────────────────────
def closes(c): return [float(x[4]) for x in c]
def highs(c):  return [float(x[2]) for x in c]
def lows(c):   return [float(x[3]) for x in c]
def opens(c):  return [float(x[1]) for x in c]
def vols(c):   return [float(x[5]) for x in c]

def calc_rsi(candles, p=14):
    cl = closes(candles)
    if len(cl) < p+1: return None
    g = l = 0
    for i in range(1, p+1):
        d = cl[-p-1+i] - cl[-p-2+i]
        if d > 0: g += d
        else: l -= d
    ag = g/p; al = l/p
    return 100 if al == 0 else 100-(100/(1+ag/al))

def calc_ema_list(candles, p):
    """Returns list of EMA values, one per candle"""
    cl = closes(candles)
    if len(cl) < p: return []
    k = 2/(p+1); e = sum(cl[:p])/p
    result = [None]*(p-1) + [e]
    for v in cl[p:]:
        e = v*k+e*(1-k)
        result.append(e)
    return result

def calc_ema(candles, p):
    cl = closes(candles)
    if len(cl) < p: return None
    k = 2/(p+1); e = sum(cl[:p])/p
    for v in cl[p:]: e = v*k+e*(1-k)
    return e

def calc_macd(c):
    e12=calc_ema(c,12); e26=calc_ema(c,26)
    return e12-e26 if e12 and e26 else None

def calc_atr(candles, p=14):
    """Average True Range"""
    if len(candles) < p+1: return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2]); l = float(candles[i][3]); pc = float(candles[i-1][4])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    if len(trs) < p: return None
    return sum(trs[-p:]) / p

def calc_bollinger(candles, p=20, k=2.0):
    """Returns (upper, mid, lower, %B position 0-1)"""
    cl = closes(candles)
    if len(cl) < p: return None,None,None,None
    sl = cl[-p:]; mid = sum(sl)/p
    std = (sum((x-mid)**2 for x in sl)/p)**0.5
    upper = mid + k*std; lower = mid - k*std
    price = cl[-1]
    pct_b = (price-lower)/(upper-lower) if upper!=lower else 0.5
    return upper, mid, lower, pct_b

def calc_rsi_divergence(candles, lookback=10):
    """
    Bullish divergence: price makes lower low but RSI makes higher low → bullish
    Bearish divergence: price makes higher high but RSI makes lower high → bearish
    Returns: +1 bull div, -1 bear div, 0 none
    """
    if len(candles) < lookback+14: return 0
    cl = closes(candles); hi = highs(candles); lo = lows(candles)
    # Calculate RSI for each candle in lookback window
    rsi_vals = []
    for i in range(lookback):
        idx = -(lookback-i)
        sub = candles[:len(candles)+idx+1] if idx < -1 else candles
        r = calc_rsi(sub[-20:], 14)
        rsi_vals.append(r)
    if not all(r is not None for r in rsi_vals): return 0
    # Bullish: price lower low, RSI higher low
    price_low_now = min(lo[-lookback//2:])
    price_low_prev = min(lo[-lookback:-lookback//2])
    rsi_low_now = min(rsi_vals[lookback//2:])
    rsi_low_prev = min(rsi_vals[:lookback//2])
    if price_low_now < price_low_prev and rsi_low_now > rsi_low_prev: return 1
    # Bearish: price higher high, RSI lower high
    price_hi_now = max(hi[-lookback//2:])
    price_hi_prev = max(hi[-lookback:-lookback//2])
    rsi_hi_now = max(rsi_vals[lookback//2:])
    rsi_hi_prev = max(rsi_vals[:lookback//2])
    if price_hi_now > price_hi_prev and rsi_hi_now < rsi_hi_prev: return -1
    return 0

def calc_volume_trend(candles, p=10):
    """
    Is volume increasing or decreasing on up vs down moves?
    Bull: up-candle volume > down-candle volume recently
    """
    if len(candles) < p: return 0
    recent = candles[-p:]
    up_vol = sum(float(c[5]) for c in recent if float(c[4])>=float(c[1]))
    dn_vol = sum(float(c[5]) for c in recent if float(c[4])<float(c[1]))
    if up_vol+dn_vol == 0: return 0
    ratio = up_vol/(up_vol+dn_vol)
    if ratio > 0.6: return 1   # buyers dominating volume
    if ratio < 0.4: return -1  # sellers dominating volume
    return 0

def calc_sr_proximity(candles, lookback=20, threshold_pct=0.003):
    """
    Detect if price is near a significant support or resistance level.
    Near resistance → bearish (-1 to -2)
    Near support → bullish (+1 to +2)
    """
    if len(candles) < lookback+1: return 0, "—"
    hi = highs(candles); lo = lows(candles)
    price = float(candles[-1][4])
    # Find swing highs and lows in lookback window (excluding last candle)
    swing_highs = [hi[i] for i in range(-lookback-1, -1) if hi[i]>hi[i-1] and hi[i]>hi[i+1]]
    swing_lows  = [lo[i] for i in range(-lookback-1, -1) if lo[i]<lo[i-1] and lo[i]<lo[i+1]]
    if not swing_highs and not swing_lows: return 0, "No S/R found"
    # Find closest resistance above and support below
    resistances = [h for h in swing_highs if h > price]
    supports = [l for l in swing_lows if l < price]
    near_res = min(resistances, default=None)
    near_sup = max(supports, default=None)
    if near_res and (near_res-price)/price < threshold_pct:
        return -2, f"Near resistance ${near_res:,.0f}"
    if near_sup and (price-near_sup)/price < threshold_pct:
        return 2, f"Near support ${near_sup:,.0f}"
    if near_res and near_sup:
        dist_res = (near_res-price)/price
        dist_sup = (price-near_sup)/price
        if dist_sup < dist_res*0.5: return 1, f"Closer to support"
        if dist_res < dist_sup*0.5: return -1, f"Closer to resistance"
    return 0, "Mid-range"

def calc_mean_reversion(candles, streak_len=4):
    """
    After N same-direction candles in a row, statistics favor reversal.
    After 4+ up: bearish bias. After 4+ down: bullish bias.
    Also checks if extreme run (6+) for stronger signal.
    """
    if len(candles) < streak_len+1: return 0, "—"
    cl = closes(candles); op = opens(candles)
    streak = 0; direction = None
    for i in range(-1, -len(candles), -1):
        is_up = cl[i] >= op[i]
        if direction is None: direction = is_up
        if is_up == direction: streak += 1
        else: break
    if streak >= 6:
        return (2 if not direction else -2), f"{streak} {'up' if direction else 'down'} streak → strong reversal"
    if streak >= 4:
        return (1 if not direction else -1), f"{streak} {'up' if direction else 'down'} streak → reversal likely"
    # Short streak (1-3): slight continuation bias
    if streak >= 2 and direction: return 1, f"{streak} up candles → mild continuation"
    if streak >= 2 and not direction: return -1, f"{streak} down candles → mild continuation"
    return 0, "Mixed candles"

def calc_weekly_bias(candles_1w):
    """Weekly candle direction as macro context"""
    if not candles_1w or len(candles_1w) < 2: return 0, "—"
    last = candles_1w[-2]  # last CLOSED weekly candle
    op = float(last[1]); cl = float(last[4])
    pct = (cl-op)/op*100
    if pct > 2: return 2, f"+{pct:.1f}% Bull week"
    if pct > 0.5: return 1, f"+{pct:.1f}% Mild bull week"
    if pct < -2: return -2, f"{pct:.1f}% Bear week"
    if pct < -0.5: return -1, f"{pct:.1f}% Mild bear week"
    return 0, f"{pct:.1f}% Flat week"

def calc_atr_ratio(candles, p=14):
    """
    Current candle range vs ATR average.
    Expanding range in direction = strong signal.
    Contracting = indecision.
    """
    atr = calc_atr(candles, p)
    if not atr: return 0, "—"
    last = candles[-1]
    rng = float(last[2])-float(last[3])
    ratio = rng/atr
    is_up = float(last[4])>=float(last[1])
    if ratio > 1.5 and is_up: return 2, f"Range {ratio:.1f}x ATR — bull expansion"
    if ratio > 1.5 and not is_up: return -2, f"Range {ratio:.1f}x ATR — bear expansion"
    if ratio < 0.5: return 0, f"Range {ratio:.1f}x ATR — contraction/indecision"
    if ratio > 1.0 and is_up: return 1, f"Range {ratio:.1f}x ATR — mild bull"
    if ratio > 1.0 and not is_up: return -1, f"Range {ratio:.1f}x ATR — mild bear"
    return 0, f"Range {ratio:.1f}x ATR — neutral"

def fmtk(v):
    v=float(v)
    if abs(v)>=1e9: return f"{v/1e9:.2f}B"
    if abs(v)>=1e6: return f"{v/1e6:.2f}M"
    if abs(v)>=1e3: return f"{v/1e3:.1f}K"
    return f"{v:.0f}"

# ── SIGNAL ────────────────────────────────────────────────────────────────────
def run_analysis():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Running analysis...")
    try:
        c4h = fetch_candles("4h", 80)[:-1]  # exclude current open candle
    except Exception as e:
        print(f"Candle fetch error: {e}"); return None
    if len(c4h) < 50: return None

    # Fetch weekly candles for macro bias
    try:
        c1w = fetch_candles("1w", 5)
    except:
        c1w = []

    price = float(c4h[-1][4])
    items = []; total = 0

    def add(n, sc, val):
        nonlocal total
        items.append({"n":n,"sc":sc,"val":val}); total += sc

    # ── FACTOR 1: EMA Stack (9/21/50) ─────────────────────────────────────────
    # Trend structure — most reliable single indicator
    e9=calc_ema(c4h,9); e21=calc_ema(c4h,21); e50=calc_ema(c4h,50)
    if e9 and e21 and e50:
        if price>e9>e21>e50:   add("EMA Stack",2,f"9>{e9:.0f} 21>{e21:.0f} 50>{e50:.0f} — full bull")
        elif price<e9<e21<e50: add("EMA Stack",-2,f"9<{e9:.0f} 21<{e21:.0f} 50<{e50:.0f} — full bear")
        elif price>e50 and e9>e21: add("EMA Stack",1,f"Above EMA50, short EMAs bullish")
        elif price>e50:        add("EMA Stack",0,f"Above EMA50 but EMAs mixed")
        else:                  add("EMA Stack",-1,f"Below EMA50 — bearish structure")
    else: add("EMA Stack",0,"—")

    # ── FACTOR 2: MACD Histogram ───────────────────────────────────────────────
    # Momentum — measures speed of trend change, not trend itself
    mac=calc_macd(c4h)
    if mac is not None:
        # Scale by ATR to normalize the signal
        atr=calc_atr(c4h,14) or 1
        mac_norm=mac/atr*100  # normalized
        if mac_norm>2:    add("MACD",2,f"{mac:.0f} Strong bull momentum")
        elif mac_norm>0:  add("MACD",1,f"{mac:.0f} Mild bull momentum")
        elif mac_norm<-2: add("MACD",-2,f"{mac:.0f} Strong bear momentum")
        else:             add("MACD",-1,f"{mac:.0f} Mild bear momentum")
    else: add("MACD",0,"—")

    # ── FACTOR 3: RSI 4H Zones ────────────────────────────────────────────────
    # Mean reversion — extreme values predict reversal, not continuation
    r4=calc_rsi(c4h,14)
    if r4 is not None:
        if r4<25:       add("RSI 4H",2,f"{r4:.1f} — Extreme oversold, reversal imminent")
        elif r4<40:     add("RSI 4H",1,f"{r4:.1f} — Oversold zone, bullish bias")
        elif r4>75:     add("RSI 4H",-2,f"{r4:.1f} — Extreme overbought, reversal likely")
        elif r4>60:     add("RSI 4H",-1,f"{r4:.1f} — Overbought zone, bearish bias")
        else:           add("RSI 4H",0,f"{r4:.1f} — Neutral 40-60 zone")
    else: add("RSI 4H",0,"—")

    # ── FACTOR 4: RSI Divergence ──────────────────────────────────────────────
    # Price vs RSI disagreement — strongest reversal signal in technical analysis
    div=calc_rsi_divergence(c4h,10)
    if div==1:   add("RSI Divergence",2,"Bullish divergence — price down, RSI up")
    elif div==-1: add("RSI Divergence",-2,"Bearish divergence — price up, RSI down")
    else:        add("RSI Divergence",0,"No divergence detected")

    # ── FACTOR 5: Bollinger Band Position ─────────────────────────────────────
    # Statistical volatility bands — price reverts to mean
    upper,mid,lower,pct_b=calc_bollinger(c4h,20,2.0)
    if pct_b is not None:
        if pct_b<0.05:   add("Bollinger",2,f"%B={pct_b:.2f} — Near lower band, oversold")
        elif pct_b<0.25: add("Bollinger",1,f"%B={pct_b:.2f} — Lower half, mild bullish")
        elif pct_b>0.95: add("Bollinger",-2,f"%B={pct_b:.2f} — Near upper band, overbought")
        elif pct_b>0.75: add("Bollinger",-1,f"%B={pct_b:.2f} — Upper half, mild bearish")
        else:            add("Bollinger",0,f"%B={pct_b:.2f} — Mid-band, neutral")
    else: add("Bollinger",0,"—")

    # ── FACTOR 6: Volume Trend ────────────────────────────────────────────────
    # Is smart money buying or selling? Volume on up vs down moves
    vt=calc_volume_trend(c4h,12)
    if vt==1:   add("Volume Trend",1,"Up-candle volume dominant — buyers in control")
    elif vt==-1: add("Volume Trend",-1,"Down-candle volume dominant — sellers in control")
    else:        add("Volume Trend",0,"Volume balanced between buyers/sellers")

    # ── FACTOR 7: ATR Expansion/Contraction ───────────────────────────────────
    # Volatility context — expanding range confirms move, contracting = indecision
    atr_sc,atr_val=calc_atr_ratio(c4h,14)
    add("ATR Ratio",atr_sc,atr_val)

    # ── FACTOR 8: Weekly Candle Bias ──────────────────────────────────────────
    # Macro context — weekly trend is hardest to fight
    wsc,wval=calc_weekly_bias(c1w)
    add("Weekly Bias",wsc,wval)

    # ── FACTOR 9: Support/Resistance Proximity ────────────────────────────────
    # Key price levels from swing highs/lows — strongest technical factor
    sr_sc,sr_val=calc_sr_proximity(c4h,30,0.004)
    add("S/R Proximity",sr_sc,sr_val)

    # ── FACTOR 10: Mean Reversion Counter ─────────────────────────────────────
    # Pure statistics: after N consecutive same-direction candles, reversal probability rises
    # This prevents 20 same signals in a row
    mr_sc,mr_val=calc_mean_reversion(c4h,4)
    add("Mean Reversion",mr_sc,mr_val)

    # ── SCORING ───────────────────────────────────────────────────────────────
    # Max possible score = 20 (10 factors × max ±2)
    # Signal fires if |score| >= 4 AND confidence >= 58%
    # This gives ~60-70% fire rate with balanced UP/DOWN
    conf=min(90,round(50+(abs(total)/20)*40))
    direction="SKIP"
    if abs(total)>=4:
        direction="UP" if total>0 else "DOWN"

    print(f"  Score: {total}/20 | Conf: {conf}% | Direction: {direction}")
    for item in items:
        print(f"  {item['n']}: {item['sc']:+d} — {item['val']}")

    return {"direction":direction,"confidence":conf,"score":total,
            "open_price":price,"factors":items,
            "market_data":{"funding":None,"oi":None,"fg":None,"ls":None}}

# ── POLYMARKET ODDS ───────────────────────────────────────────────────────────
poly_odds_history = []
poly_token_up = None
poly_token_dn = None

def fetch_poly_odds():
    global poly_token_up, poly_token_dn
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=200",timeout=10)
        if not r.ok: return None,None
        lst = r.json() if isinstance(r.json(),list) else r.json().get("markets",[])
        m = next((x for x in lst if
            ("bitcoin" in (x.get("question","") or "").lower()) and
            ("4-hour" in (x.get("question","") or "").lower() or "4hr" in (x.get("question","") or "").lower()) and
            "up or down" in (x.get("question","") or "").lower()
        ),None)
        if not m: return None,None
        for t in (m.get("tokens") or []):
            o=(t.get("outcome") or "").lower()
            if o=="up": poly_token_up=t.get("token_id") or t.get("id")
            elif o=="down": poly_token_dn=t.get("token_id") or t.get("id")
        prices=m.get("outcomePrices")
        if prices:
            if isinstance(prices,str): prices=json.loads(prices)
            up=round(float(prices[0])*100); dn=round(float(prices[1])*100)
            if 0<up<100:
                poly_odds_history.append({"ts":int(time.time()),"up":up,"dn":dn})
                if len(poly_odds_history)>2000: poly_odds_history.pop(0)
                print(f"Odds: Up {up}¢ Down {dn}¢")
                return up,dn
    except Exception as e:
        print(f"Polymarket error: {e}")
    return None,None

def get_odds_at(ts_s):
    if not poly_odds_history: return None,None
    c=min(poly_odds_history,key=lambda x:abs(x["ts"]-ts_s))
    return (c["up"],c["dn"]) if abs(c["ts"]-ts_s)<3600 else (None,None)

def poly_loop():
    while True:
        try: fetch_poly_odds()
        except: pass
        time.sleep(300)

# ── SAVE PREDICTION ───────────────────────────────────────────────────────────
def save_prediction(result, ws_s):
    if not SUPABASE_URL: return
    ws_ms=ws_s*1000; we_ms=ws_ms+WIN_SECS*1000
    sig_time=datetime.fromtimestamp(ws_s+SIG_AT,tz=timezone.utc).strftime("%H:%M UTC")
    try:
        if supa_get("predictions",{"window_start":f"eq.{ws_ms}","select":"id"}): return
    except: pass
    direction=result["direction"]; conf=result["confidence"]
    ou,od=get_odds_at(ws_s+SIG_AT)
    if not ou: ou,od=fetch_poly_odds()
    sig_odds=ou if direction=="UP" else od
    bet=round(100*(0.05 if conf>=80 else 0.03 if conf>=75 else 0.02 if conf>=70 else 0.01),2)
    payout=round((100-sig_odds)/sig_odds*bet,2) if sig_odds and sig_odds>0 else None
    try:
        supa_post("predictions",{"window_start":ws_ms,"window_end":we_ms,"signal_time":sig_time,
            "direction":direction,"confidence":conf,"score":result["score"],
            "open_price":result["open_price"],"factors":json.dumps(result["factors"]),
            "market_data":json.dumps(result["market_data"]),
            "odds":sig_odds,"bet_size":bet,"potential_payout":payout})
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] 📊 {direction} {result['score']}/20 {conf}% odds:{sig_odds}¢ bet:${bet} payout:+${payout}")
    except Exception as e:
        print(f"save error: {e}")

# ── RESOLVE ───────────────────────────────────────────────────────────────────
def resolve_pending():
    if not SUPABASE_URL: return
    now_ms=int(time.time()*1000)
    try:
        rows=supa_get("predictions",{"result":"eq.pending","select":"id,window_start,window_end,direction,open_price,bet_size,potential_payout"})
    except Exception as e:
        print(f"resolve fetch error: {e}"); return
    for row in rows:
        pid=row["id"]; ws=row["window_start"]; we=row["window_end"]
        if now_ms<we: continue
        try:
            raw=fetch_candles("4h",3); cp=None
            for c in raw:
                if abs(int(c[0])-ws)<300000: cp=float(c[4]); break
            if cp is None: cp=float(raw[-2][4]) if len(raw)>=2 else None
            if cp is None: continue
            diff=cp-row["open_price"]; up=cp>=row["open_price"]
            res="correct" if ((row["direction"]=="UP" and up) or (row["direction"]=="DOWN" and not up)) else "wrong"
            pnl=round(float(row.get("potential_payout") or 0),2) if res=="correct" else -round(float(row.get("bet_size") or 0),2)
            supa_patch("predictions",f"id=eq.{pid}",{"result":res,"close_price":cp,"price_diff":diff,"actual_pnl":pnl})
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {'✅'if res=='correct' else '❌'} #{pid} {row['direction']} {res.upper()} ${cp:,.2f}")
        except Exception as e:
            print(f"resolve #{pid}: {e}")

# ── SCHEDULER ─────────────────────────────────────────────────────────────────
def get_ws():
    return (int(time.time()*1000)//(WIN_SECS*1000))*(WIN_SECS*1000)

def scheduler_loop():
    print("Scheduler started"); last=None
    while True:
        try:
            now=time.time(); ws_s=int(get_ws()/1000); el=now-ws_s
            if SIG_AT<=el<SIG_AT+30 and last!=ws_s:
                last=ws_s; r=run_analysis()
                if r: save_prediction(r,ws_s)
            resolve_pending()
        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(30)

# ── HTTP SERVER ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self,f,*a): pass

    def send_json(self,data,status=200):
        body=json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Content-Length",len(body))
        self.send_header("Cache-Control","no-store")
        self.end_headers(); self.wfile.write(body)

    def send_html(self,content,status=200):
        body=content if isinstance(content,bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",len(body))
        self.send_header("Cache-Control","no-store,no-cache,must-revalidate,max-age=0")
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def do_GET(self):
        p=urlparse(self.path); path=p.path; qs=parse_qs(p.query)

        if path in ("/","/kryptos_v3.html"):
            try:
                with open(HTML_PATH,"rb") as f: self.send_html(f.read())
            except FileNotFoundError:
                self.send_html(b"<h1>Dashboard file not found</h1>",404)
            return

        if path=="/api/predictions":
            try:
                rows=supa_get("predictions",{"select":"*","order":"window_start.desc","limit":"50"})
                self.send_json({"predictions":[{
                    "id":r["id"],"windowStart":r["window_start"],"windowEnd":r["window_end"],
                    "signalTime":r["signal_time"],"dir":r["direction"],"conf":r["confidence"],
                    "score":r["score"],"openPrice":r["open_price"],"closePrice":r["close_price"],
                    "priceDiff":r["price_diff"],"result":r["result"],
                    "factors":json.loads(r["factors"]) if r.get("factors") else [],
                    "marketData":json.loads(r["market_data"]) if r.get("market_data") else {},
                    "odds":r.get("odds"),"betSize":r.get("bet_size"),
                    "potentialPayout":r.get("potential_payout"),"actualPnl":r.get("actual_pnl"),
                } for r in rows]})
            except Exception as e:
                self.send_json({"predictions":[],"error":str(e)})
            return

        if path=="/api/trades":
            try:
                rows=supa_get("trades",{"select":"*","order":"created_at.desc","limit":"200"})
                self.send_json({"trades":[{
                    "id":r["id"],"date":r["date"],"time":r["time"],"dir":r["direction"],
                    "outcome":r["outcome"],"betAmount":r["bet_amount"],"odds":r["odds"],
                    "pnl":r["pnl"],"openPrice":r["open_price"],"conf":r["confidence"],"score":r["score"],
                } for r in rows]})
            except Exception as e:
                self.send_json({"trades":[],"error":str(e)})
            return

        if path=="/api/status":
            ws_ms=get_ws(); el=int((int(time.time()*1000)-ws_ms)/1000)
            ws_dt=datetime.fromtimestamp(ws_ms/1000,tz=timezone.utc)
            we_dt=datetime.fromtimestamp((ws_ms+WIN_SECS*1000)/1000,tz=timezone.utc)
            self.send_json({"status":"running","windowStart":ws_dt.strftime("%H:%M UTC"),
                "windowEnd":we_dt.strftime("%H:%M UTC"),"elapsed":el,
                "signalIn":max(0,SIG_AT-el),"serverTime":datetime.now(timezone.utc).strftime("%H:%M:%S UTC")})
            return

        if path=="/api/price":
            try:
                r=requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT",timeout=5)
                d=r.json()
                self.send_json({"price":float(d["lastPrice"]),"change":float(d["priceChangePercent"]),
                    "high":float(d["highPrice"]),"low":float(d["lowPrice"])})
            except Exception as e:
                self.send_json({"error":str(e)},500)
            return

        if path=="/api/odds":
            up,dn=fetch_poly_odds()
            self.send_json({"up":up,"dn":dn,"snapshots":len(poly_odds_history),
                "latest":poly_odds_history[-1] if poly_odds_history else None})
            return

        if path=="/api/proxy":
            target=unquote(qs.get("url",[""])[0])
            allowed=["api.binance.com","fapi.binance.com","api.alternative.me",
                     "gamma-api.polymarket.com","clob.polymarket.com","api.coinbase.com"]
            domain=urlparse(target).netloc
            if not target or not any(domain.endswith(a) for a in allowed):
                self.send_json({"error":"Not allowed"},403); return
            try:
                r=requests.get(target,timeout=8,headers={"User-Agent":"Mozilla/5.0"})
                r.raise_for_status(); self.send_json(r.json())
            except Exception as e:
                self.send_json({"error":str(e)},500)
            return

        if path=="/api/trigger":
            try:
                r=run_analysis()
                if r:
                    ws_s=int(get_ws()/1000)
                    save_prediction(r,ws_s)
                    self.send_json({"direction":r["direction"],"confidence":r["confidence"],
                        "score":r["score"],"openPrice":r["open_price"],"factors":r["factors"]})
                else:
                    self.send_json({"error":"Analysis failed"},500)
            except Exception as e:
                self.send_json({"error":str(e)},500)
            return

        if path=="/api/wipe":
            # Wipe all predictions so we can backfill fresh
            try:
                requests.delete(f"{SUPABASE_URL.rstrip('/').rstrip('/rest/v1')}/rest/v1/predictions?id=gte.0",
                    headers={**sh(),"Prefer":"return=minimal"}, timeout=10)
                self.send_json({"success":True,"message":"All predictions wiped"})
            except Exception as e:
                self.send_json({"error":str(e)},500)
            return

        if path=="/api/backfill":
            try:
                candles=fetch_candles("4h",200)
                try: c1w=fetch_candles("1w",10)
                except: c1w=[]
                saved=0
                for i in range(1,len(candles)-1):
                    raw_c=candles[-(i+1)]
                    ws_ms=int(raw_c[0]); we_ms=ws_ms+WIN_SECS*1000
                    op=float(raw_c[1]); cp=float(raw_c[4])
                    hist=candles[:-(i+1)]
                    if len(hist)<50: continue
                    try:
                        if supa_get("predictions",{"window_start":f"eq.{ws_ms}","select":"id"}): continue
                    except: pass

                    # Use full honest analysis on historical slice
                    price=cp; items=[]; total=0
                    def add_b(n,sc,val):
                        nonlocal total
                        items.append({"n":n,"sc":sc,"val":val}); total+=sc

                    e9=calc_ema(hist,9); e21=calc_ema(hist,21); e50=calc_ema(hist,50)
                    if e9 and e21 and e50:
                        if price>e9>e21>e50: add_b("EMA Stack",2,"Full bull stack")
                        elif price<e9<e21<e50: add_b("EMA Stack",-2,"Full bear stack")
                        elif price>e50 and e9>e21: add_b("EMA Stack",1,"Above EMA50, bull")
                        elif price>e50: add_b("EMA Stack",0,"Above EMA50, mixed")
                        else: add_b("EMA Stack",-1,"Below EMA50")
                    else: add_b("EMA Stack",0,"—")

                    mac=calc_macd(hist)
                    if mac is not None:
                        atr=calc_atr(hist,14) or 1; mn=mac/atr*100
                        if mn>2: add_b("MACD",2,f"{mac:.0f} Strong bull")
                        elif mn>0: add_b("MACD",1,f"{mac:.0f} Mild bull")
                        elif mn<-2: add_b("MACD",-2,f"{mac:.0f} Strong bear")
                        else: add_b("MACD",-1,f"{mac:.0f} Mild bear")
                    else: add_b("MACD",0,"—")

                    r4=calc_rsi(hist,14)
                    if r4:
                        if r4<25: add_b("RSI 4H",2,f"{r4:.1f} Extreme oversold")
                        elif r4<40: add_b("RSI 4H",1,f"{r4:.1f} Oversold")
                        elif r4>75: add_b("RSI 4H",-2,f"{r4:.1f} Extreme overbought")
                        elif r4>60: add_b("RSI 4H",-1,f"{r4:.1f} Overbought")
                        else: add_b("RSI 4H",0,f"{r4:.1f} Neutral")
                    else: add_b("RSI 4H",0,"—")

                    div=calc_rsi_divergence(hist,10)
                    if div==1: add_b("RSI Divergence",2,"Bullish divergence")
                    elif div==-1: add_b("RSI Divergence",-2,"Bearish divergence")
                    else: add_b("RSI Divergence",0,"No divergence")

                    _,_,_,pct_b=calc_bollinger(hist,20,2.0)
                    if pct_b is not None:
                        if pct_b<0.05: add_b("Bollinger",2,f"%B={pct_b:.2f} Near lower")
                        elif pct_b<0.25: add_b("Bollinger",1,f"%B={pct_b:.2f} Lower half")
                        elif pct_b>0.95: add_b("Bollinger",-2,f"%B={pct_b:.2f} Near upper")
                        elif pct_b>0.75: add_b("Bollinger",-1,f"%B={pct_b:.2f} Upper half")
                        else: add_b("Bollinger",0,f"%B={pct_b:.2f} Mid")
                    else: add_b("Bollinger",0,"—")

                    vt=calc_volume_trend(hist,12)
                    if vt==1: add_b("Volume Trend",1,"Buyers dominant")
                    elif vt==-1: add_b("Volume Trend",-1,"Sellers dominant")
                    else: add_b("Volume Trend",0,"Balanced")

                    asc,aval=calc_atr_ratio(hist,14)
                    add_b("ATR Ratio",asc,aval)

                    wsc,wval=calc_weekly_bias(c1w)
                    add_b("Weekly Bias",wsc,wval)

                    ssc,sval=calc_sr_proximity(hist,30,0.004)
                    add_b("S/R Proximity",ssc,sval)

                    msc,mval=calc_mean_reversion(hist,4)
                    add_b("Mean Reversion",msc,mval)

                    conf=min(90,round(50+(abs(total)/20)*40))
                    direction="SKIP" if abs(total)<4 else ("UP" if total>0 else "DOWN")
                    up=cp>=op
                    if direction=="SKIP": res="skip"
                    else: res="correct" if ((direction=="UP" and up) or (direction=="DOWN" and not up)) else "wrong"
                    sig_time=datetime.fromtimestamp((ws_ms/1000)+SIG_AT,tz=timezone.utc).strftime("%H:%M UTC")
                    bet=1.0 if direction!="SKIP" else 0
                    try:
                        supa_post("predictions",{"window_start":ws_ms,"window_end":we_ms,
                            "signal_time":sig_time,"direction":direction,"confidence":conf,"score":total,
                            "open_price":op,"close_price":cp,"price_diff":cp-op,"result":res,
                            "bet_size":bet,"actual_pnl":round(bet*0.8,2) if res=="correct" else (-bet if res=="wrong" else 0),
                            "factors":json.dumps(items),"market_data":json.dumps({})})
                        saved+=1
                    except Exception as e:
                        print(f"backfill save: {e}")
                self.send_json({"success":True,"saved":saved})
            except Exception as e:
                self.send_json({"error":str(e)},500)
            return

        self.send_json({"error":"Not found"},404)

    def do_POST(self):
        p=urlparse(self.path); path=p.path
        if path=="/api/trades":
            ln=int(self.headers.get("Content-Length",0))
            body=self.rfile.read(ln)
            try:
                data=json.loads(body); now=datetime.now(timezone.utc)
                supa_post("trades",{"date":now.strftime("%Y-%m-%d"),"time":now.strftime("%H:%M UTC"),
                    "direction":data.get("dir"),"outcome":data.get("outcome"),
                    "bet_amount":data.get("betAmount"),"odds":data.get("odds"),
                    "pnl":data.get("pnl"),"open_price":data.get("openPrice"),
                    "confidence":data.get("conf"),"score":data.get("score")})
                self.send_json({"success":True})
            except Exception as e:
                self.send_json({"error":str(e)},400)
            return
        self.send_json({"error":"Not found"},404)

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__=="__main__":
    print("="*50)
    print("  KRYPTOS Backend v2.0 — Supabase Edition")
    print(f"  Port: {PORT}")
    print(f"  Supabase: {'✓' if SUPABASE_URL else '✗ NOT SET'}")
    print("="*50)
    init_db()
    threading.Thread(target=poly_loop,daemon=True).start()
    threading.Thread(target=scheduler_loop,daemon=True).start()
    server=HTTPServer(("",PORT),Handler)
    print(f"Server running on port {PORT}")
    server.serve_forever()
