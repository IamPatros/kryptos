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
def calc_rsi(candles, p=14):
    cl = [float(c[4]) for c in candles]
    if len(cl) < p+1: return None
    sl = cl[-(p+1):]; g = l = 0
    for i in range(1,len(sl)):
        d = sl[i]-sl[i-1]
        if d > 0: g += d
        else: l -= d
    ag = g/p; al = l/p
    return 100 if al == 0 else 100-(100/(1+ag/al))

def calc_ema(candles, p):
    cl = [float(c[4]) for c in candles]
    if len(cl) < p: return None
    k = 2/(p+1); e = sum(cl[:p])/p
    for v in cl[p:]: e = v*k+e*(1-k)
    return e

def calc_macd(c):
    e12=calc_ema(c,12); e26=calc_ema(c,26)
    return e12-e26 if e12 and e26 else None

def calc_cvd(c):
    return sum(float(x[5]) if float(x[4])>=float(x[1]) else -float(x[5]) for x in c)

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
        c4h = fetch_candles("4h", 60)[:-1]
    except Exception as e:
        print(f"Candle fetch error: {e}"); return None
    if len(c4h) < 30: return None

    price = float(c4h[-1][4]); open4 = float(c4h[-1][1])
    items = []; total = 0

    def add(n, sc, val):
        nonlocal total
        items.append({"n":n,"sc":sc,"val":val}); total += sc

    e9=calc_ema(c4h,9); e21=calc_ema(c4h,21); e50=calc_ema(c4h,50)
    if e9 and e21 and e50:
        if price>e9>e21>e50: add("EMA Stack",2,"Full bull stack")
        elif price<e9<e21<e50: add("EMA Stack",-2,"Full bear stack")
        elif price>e50: add("EMA Stack",1,"Above EMA50")
        else: add("EMA Stack",-1,"Below EMA50")
    else: add("EMA Stack",0,"—")

    mac = calc_macd(c4h)
    if mac is not None:
        if mac>200: add("MACD",2,f"+{mac:.0f} Strong bull")
        elif mac>0: add("MACD",1,f"+{mac:.0f} Mild bull")
        elif mac<-200: add("MACD",-2,f"{mac:.0f} Strong bear")
        else: add("MACD",-1,f"{mac:.0f} Mild bear")
    else: add("MACD",0,"—")

    r4 = calc_rsi(c4h,14)
    if r4 is not None:
        if r4<30: add("RSI 4H",2,f"{r4:.1f} Oversold")
        elif r4>70: add("RSI 4H",-2,f"{r4:.1f} Overbought")
        elif r4>50: add("RSI 4H",1,f"{r4:.1f} Bullish zone")
        else: add("RSI 4H",-1,f"{r4:.1f} Bearish zone")
    else: add("RSI 4H",0,"—")

    funding=None
    try:
        funding=fetch_funding(); f=funding*100
        if f<-0.03: add("Funding",2,f"{f:.4f}% Extreme neg")
        elif f<0: add("Funding",1,f"{f:.4f}% Negative")
        elif f>0.05: add("Funding",-2,f"{f:.4f}% Overheated")
        elif f>0.01: add("Funding",-1,f"{f:.4f}% Positive")
        else: add("Funding",0,f"{f:.4f}% Neutral")
    except: add("Funding",0,"—")

    oi=None
    try:
        oi=fetch_oi(); pct=(price-open4)/open4*100
        if pct>0: add("Open Interest",1,fmtk(oi)+" Bull")
        elif pct<0: add("Open Interest",-1,fmtk(oi)+" Bear")
        else: add("Open Interest",0,fmtk(oi))
    except: add("Open Interest",0,"—")

    ls=None
    try:
        ls=fetch_ls()
        if ls<0.8: add("Long/Short",2,f"{ls:.3f} Too many shorts")
        elif ls>1.5: add("Long/Short",-2,f"{ls:.3f} Too many longs")
        elif ls<1: add("Long/Short",1,f"{ls:.3f} Short lean")
        else: add("Long/Short",-1,f"{ls:.3f} Long lean")
    except: add("Long/Short",0,"—")

    cvd = calc_cvd(c4h[-20:])
    add("CVD",2 if cvd>0 else -2,f"{'+'if cvd>0 else ''}{fmtk(cvd)} Net {'buying'if cvd>0 else 'selling'}")

    fg=None
    try:
        fg=fetch_fear_greed()
        if fg<20: add("Fear & Greed",2,f"{fg} Extreme fear")
        elif fg<40: add("Fear & Greed",1,f"{fg} Fear")
        elif fg>80: add("Fear & Greed",-2,f"{fg} Extreme greed")
        elif fg>60: add("Fear & Greed",-1,f"{fg} Greed")
        else: add("Fear & Greed",0,f"{fg} Neutral")
    except: add("Fear & Greed",0,"—")

    streak = sum(1 if float(c[4])>=float(c[1]) else -1 for c in c4h[-4:])
    add("Candle Streak",2 if streak>=3 else 1 if streak>0 else -2 if streak<=-3 else -1,
        f"{'+' if streak>0 else ''}{streak} {'Bull'if streak>0 else 'Bear'} streak")

    try:
        c1h=fetch_candles("1h",20); r1=calc_rsi(c1h,14)
        if r1:
            if r1<35: add("RSI 1H",2,f"{r1:.1f} Oversold")
            elif r1>65: add("RSI 1H",-2,f"{r1:.1f} Overbought")
            elif r1>50: add("RSI 1H",1,f"{r1:.1f} Bullish")
            else: add("RSI 1H",-1,f"{r1:.1f} Bearish")
        else: add("RSI 1H",0,"—")
    except: add("RSI 1H",0,"—")

    conf = min(90,round(50+(abs(total)/20)*40))
    direction = "SKIP"
    if conf>=60 and abs(total)>=4:
        direction = "UP" if total>0 else "DOWN"

    return {"direction":direction,"confidence":conf,"score":total,
            "open_price":price,"factors":items,
            "market_data":{"funding":funding,"oi":oi,"fg":fg,"ls":ls}}

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
                if r and r["direction"]!="SKIP": save_prediction(r,ws_s)
                elif r: print(f"SKIP score:{r['score']} conf:{r['confidence']}%")
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
                    if r["direction"]!="SKIP": save_prediction(r,ws_s)
                    self.send_json({"direction":r["direction"],"confidence":r["confidence"],
                        "score":r["score"],"openPrice":r["open_price"],"factors":r["factors"]})
                else:
                    self.send_json({"error":"Analysis failed"},500)
            except Exception as e:
                self.send_json({"error":str(e)},500)
            return

        if path=="/api/backfill":
            try:
                candles=fetch_candles("4h",60); saved=0
                for i in range(1,len(candles)-1):
                    c=candles[-(i+1)]; ws_ms=int(c[0]); we_ms=ws_ms+WIN_SECS*1000
                    op=float(c[1]); cp=float(c[4]); hist=candles[:-(i)]
                    if len(hist)<15: continue
                    streak=sum(1 if float(x[4])>=float(x[1]) else -1 for x in hist[-4:])
                    cvd=sum(float(x[5]) if float(x[4])>=float(x[1]) else -float(x[5]) for x in hist[-20:])
                    rsi=calc_rsi(hist,14); score=0
                    if streak>=2: score+=2
                    elif streak>0: score+=1
                    elif streak<=-2: score-=2
                    else: score-=1
                    if cvd>0: score+=2
                    else: score-=2
                    if rsi:
                        if rsi<30: score+=2
                        elif rsi>70: score-=2
                        elif rsi>50: score+=1
                        else: score-=1
                    conf=min(90,round(50+(abs(score)/10)*40))
                    if conf<60 or abs(score)<3: continue
                    direction="UP" if score>0 else "DOWN"
                    up=cp>=op
                    res="correct" if ((direction=="UP" and up) or (direction=="DOWN" and not up)) else "wrong"
                    sig_time=datetime.fromtimestamp((ws_ms/1000)+SIG_AT,tz=timezone.utc).strftime("%H:%M UTC")
                    try:
                        if supa_get("predictions",{"window_start":f"eq.{ws_ms}","select":"id"}): continue
                        bet=1.0
                        supa_post("predictions",{"window_start":ws_ms,"window_end":we_ms,
                            "signal_time":sig_time,"direction":direction,"confidence":conf,"score":score,
                            "open_price":op,"close_price":cp,"price_diff":cp-op,"result":res,
                            "bet_size":bet,"actual_pnl":round(bet*0.8,2) if res=="correct" else -bet,
                            "factors":json.dumps([
                                {"n":"Candle Streak","sc":2 if streak>=2 else 1 if streak>0 else -2 if streak<=-2 else -1,"val":f"{streak:+d} streak"},
                                {"n":"CVD","sc":2 if cvd>0 else -2,"val":f"Net {'buying'if cvd>0 else 'selling'}"},
                                {"n":"RSI 4H","sc":2 if rsi and rsi<30 else -2 if rsi and rsi>70 else 1 if rsi and rsi>50 else -1,"val":f"{rsi:.1f}" if rsi else "—"},
                            ]),"market_data":json.dumps({})})
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
