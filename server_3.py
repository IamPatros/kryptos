"""
KRYPTOS Backend Server
Runs 24/7 on Railway. Handles:
- Serving the dashboard HTML
- Running signal analysis every 4 hours
- Auto-resolving predictions
- REST API for the frontend to fetch predictions
- SQLite database for persistent storage
"""

import os
import json
import math
import time
import threading
import sqlite3
import requests
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kryptos.db")
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kryptos_v3.html")

WIN_SECS = 14400  # 4 hours
SIG_AT = 1800     # signal at +30 min


# ── DATABASE ────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_start INTEGER NOT NULL,
            window_end INTEGER NOT NULL,
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print("Database initialized")


def get_db():
    return sqlite3.connect(DB_PATH)


# ── BINANCE API ─────────────────────────────────────────────────────────────
def fetch_candles(interval="4h", limit=60):
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_funding():
    url = "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    d = r.json()
    return float(d[0]["fundingRate"]) if d else None


def fetch_oi():
    url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return float(r.json()["openInterest"])


def fetch_fear_greed():
    url = "https://api.alternative.me/fng/?limit=1"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return int(r.json()["data"][0]["value"])


def fetch_ls_ratio():
    url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=4h&limit=1"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    d = r.json()
    return float(d[0]["longShortRatio"]) if d else None


# ── INDICATORS ───────────────────────────────────────────────────────────────
def calc_rsi(candles, period=14):
    closes = [float(c[4]) for c in candles]
    if len(closes) < period + 1:
        return None
    sl = closes[-(period+1):]
    gains = losses = 0
    for i in range(1, len(sl)):
        d = sl[i] - sl[i-1]
        if d > 0:
            gains += d
        else:
            losses -= d
    ag = gains / period
    al = losses / period
    if al == 0:
        return 100
    return 100 - (100 / (1 + ag/al))


def calc_ema(candles, period):
    closes = [float(c[4]) for c in candles]
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def calc_macd(candles):
    e12 = calc_ema(candles, 12)
    e26 = calc_ema(candles, 26)
    if e12 and e26:
        return e12 - e26
    return None


def calc_cvd(candles):
    total = 0
    for c in candles:
        o, cl, v = float(c[1]), float(c[4]), float(c[5])
        total += v if cl >= o else -v
    return total


def fmt_k(v):
    v = float(v)
    if abs(v) >= 1e9: return f"{v/1e9:.2f}B"
    if abs(v) >= 1e6: return f"{v/1e6:.2f}M"
    if abs(v) >= 1e3: return f"{v/1e3:.1f}K"
    return f"{v:.0f}"


# ── SIGNAL ANALYSIS ─────────────────────────────────────────────────────────
def run_analysis():
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Running signal analysis...")

    try:
        candles_4h = fetch_candles("4h", 60)
        candles_4h = candles_4h[:-1]  # exclude current open candle
    except Exception as e:
        print(f"Failed to fetch candles: {e}")
        return None

    if len(candles_4h) < 30:
        print("Not enough candle data")
        return None

    price = float(candles_4h[-1][4])
    open4h = float(candles_4h[-1][1])
    items = []
    total = 0

    # 1. EMA Stack
    e9 = calc_ema(candles_4h, 9)
    e21 = calc_ema(candles_4h, 21)
    e50 = calc_ema(candles_4h, 50)
    sc, val = 0, "—"
    if e9 and e21 and e50:
        if price > e9 > e21 > e50:     sc, val = 2, f"Full bull stack"
        elif price < e9 < e21 < e50:   sc, val = -2, f"Full bear stack"
        elif price > e50:               sc, val = 1, f"Above EMA50"
        else:                           sc, val = -1, f"Below EMA50"
    items.append({"n": "EMA Stack", "sc": sc, "val": val})
    total += sc

    # 2. MACD
    mac = calc_macd(candles_4h)
    sc, val = 0, "—"
    if mac is not None:
        if mac > 200:    sc, val = 2, f"+{mac:.0f} Strong bull"
        elif mac > 0:    sc, val = 1, f"+{mac:.0f} Mild bull"
        elif mac < -200: sc, val = -2, f"{mac:.0f} Strong bear"
        else:            sc, val = -1, f"{mac:.0f} Mild bear"
    items.append({"n": "MACD", "sc": sc, "val": val})
    total += sc

    # 3. RSI 4H
    r4 = calc_rsi(candles_4h, 14)
    sc, val = 0, "—"
    if r4 is not None:
        if r4 < 30:      sc, val = 2, f"{r4:.1f} Oversold"
        elif r4 > 70:    sc, val = -2, f"{r4:.1f} Overbought"
        elif r4 > 50:    sc, val = 1, f"{r4:.1f} Bullish zone"
        else:            sc, val = -1, f"{r4:.1f} Bearish zone"
    items.append({"n": "RSI 4H", "sc": sc, "val": val})
    total += sc

    # 4. Funding rate
    sc, val = 0, "—"
    try:
        funding = fetch_funding()
        if funding is not None:
            f = funding * 100
            if f < -0.03:   sc, val = 2, f"{f:.4f}% Extreme neg"
            elif f < 0:     sc, val = 1, f"{f:.4f}% Negative"
            elif f > 0.05:  sc, val = -2, f"{f:.4f}% Overheated"
            elif f > 0.01:  sc, val = -1, f"{f:.4f}% Positive"
            else:           val = f"{f:.4f}% Neutral"
    except: funding = None
    items.append({"n": "Funding Rate", "sc": sc, "val": val})
    total += sc

    # 5. Open Interest
    sc, val = 0, "—"
    try:
        oi = fetch_oi()
        pct = (price - open4h) / open4h * 100
        val = fmt_k(oi)
        if pct > 0:   sc, val = 1, val + " Bull confirm"
        elif pct < 0: sc, val = -1, val + " Bear confirm"
    except: oi = None
    items.append({"n": "Open Interest", "sc": sc, "val": val})
    total += sc

    # 6. Long/Short
    sc, val = 0, "—"
    try:
        ls = fetch_ls_ratio()
        if ls is not None:
            if ls < 0.8:   sc, val = 2, f"{ls:.3f} Too many shorts"
            elif ls > 1.5: sc, val = -2, f"{ls:.3f} Too many longs"
            elif ls < 1:   sc, val = 1, f"{ls:.3f} Short lean"
            else:          sc, val = -1, f"{ls:.3f} Long lean"
    except: ls = None
    items.append({"n": "Long/Short", "sc": sc, "val": val})
    total += sc

    # 7. CVD
    cvd = calc_cvd(candles_4h[-20:])
    sc = 2 if cvd > 0 else -2
    val = f"{'+' if cvd > 0 else ''}{fmt_k(cvd)} Net {'buying' if cvd > 0 else 'selling'}"
    items.append({"n": "CVD", "sc": sc, "val": val})
    total += sc

    # 8. Fear & Greed
    sc, val = 0, "—"
    try:
        fg = fetch_fear_greed()
        if fg < 20:   sc, val = 2, f"{fg} Extreme fear"
        elif fg < 40: sc, val = 1, f"{fg} Fear"
        elif fg > 80: sc, val = -2, f"{fg} Extreme greed"
        elif fg > 60: sc, val = -1, f"{fg} Greed"
        else:         val = f"{fg} Neutral"
    except: fg = None
    items.append({"n": "Fear & Greed", "sc": sc, "val": val})
    total += sc

    # 9. Candle streak
    streak = sum(1 if float(c[4]) >= float(c[1]) else -1 for c in candles_4h[-4:])
    sc = 2 if streak >= 3 else 1 if streak > 0 else -2 if streak <= -3 else -1
    val = f"{'+' if streak > 0 else ''}{streak} {'Bull' if streak > 0 else 'Bear'} streak"
    items.append({"n": "Candle Streak", "sc": sc, "val": val})
    total += sc

    # 10. RSI 1H alignment
    try:
        candles_1h = fetch_candles("1h", 20)
        r1 = calc_rsi(candles_1h, 14)
        sc, val = 0, "—"
        if r1 is not None:
            if r1 < 35:    sc, val = 2, f"{r1:.1f} 1H oversold"
            elif r1 > 65:  sc, val = -2, f"{r1:.1f} 1H overbought"
            elif r1 > 50:  sc, val = 1, f"{r1:.1f} 1H bullish"
            else:          sc, val = -1, f"{r1:.1f} 1H bearish"
    except: sc, val = 0, "—"
    items.append({"n": "RSI 1H", "sc": sc, "val": val})
    total += sc

    # Direction
    conf = min(90, round(50 + (abs(total) / 20) * 40))
    direction = "SKIP"
    if conf >= 65 and abs(total) >= 6:
        direction = "UP" if total > 0 else "DOWN"

    market_data = {
        "funding": funding if 'funding' in dir() else None,
        "oi": oi if 'oi' in dir() else None,
        "fg": fg if 'fg' in dir() else None,
        "ls": ls if 'ls' in dir() else None,
    }

    return {
        "direction": direction,
        "confidence": conf,
        "score": total,
        "open_price": price,
        "factors": items,
        "market_data": market_data,
    }


# ── SCHEDULER ───────────────────────────────────────────────────────────────
def get_window_start():
    now_ms = int(time.time() * 1000)
    return (now_ms // (WIN_SECS * 1000)) * (WIN_SECS * 1000)


def resolve_pending():
    """Check all pending predictions and resolve them if window has closed."""
    conn = get_db()
    c = conn.cursor()
    now_ms = int(time.time() * 1000)
    c.execute("SELECT id, window_start, window_end, direction, open_price, bet_size, potential_payout FROM predictions WHERE result='pending'")
    rows = c.fetchall()
    for row in rows:
        pid, ws, we, direction, open_price, bet_size, potential_payout = row
        if now_ms < we:
            continue  # window not closed yet
        try:
            # Fetch the actual 4H candle that covers this window
            raw = fetch_candles("4h", 2)
            # Find the candle matching this window
            close_price = None
            for candle in raw:
                candle_ts = int(candle[0])
                if abs(candle_ts - ws) < 60000:  # within 1 minute
                    close_price = float(candle[4])
                    break
            if close_price is None:
                # Use the previous closed candle
                close_price = float(raw[-2][4]) if len(raw) >= 2 else None

            if close_price is None:
                continue

            price_diff = close_price - open_price
            actual_up = close_price >= open_price
            result = "correct" if (
                (direction == "UP" and actual_up) or
                (direction == "DOWN" and not actual_up)
            ) else "wrong"

            c.execute("""
                UPDATE predictions SET result=?, close_price=?, price_diff=?, actual_pnl=?
                WHERE id=?
            """, (result, close_price, price_diff,
                  round(float(potential_payout or 0), 2) if result == "correct" else -round(float(bet_size or 0), 2),
                  pid))
            conn.commit()
            emoji = "✅" if result == "correct" else "❌"
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {emoji} Resolved prediction #{pid}: {direction} was {result.upper()} (close ${close_price:,.2f})")
        except Exception as e:
            print(f"Failed to resolve prediction #{pid}: {e}")
    conn.close()


# ── POLYMARKET ODDS ──────────────────────────────────────────────────────────
poly_token_up = None
poly_token_dn = None
poly_odds_history = []  # list of {ts, up, dn} snapshots

def fetch_poly_market_ids():
    """Fetch BTC 4H market token IDs from Gamma API."""
    global poly_token_up, poly_token_dn
    try:
        urls = [
            "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=200",
            "https://gamma-api.polymarket.com/markets?active=true&closed=false&tag_slug=crypto&limit=200",
        ]
        for url in urls:
            try:
                r = requests.get(url, timeout=10)
                if not r.ok:
                    continue
                markets = r.json()
                lst = markets if isinstance(markets, list) else markets.get("markets", [])
                m = next((x for x in lst if
                    ("bitcoin" in (x.get("question","") or x.get("title","")).lower() or "btc" in (x.get("question","") or "").lower()) and
                    ("4-hour" in (x.get("question","") or "").lower() or "4 hour" in (x.get("question","") or "").lower() or "4hr" in (x.get("question","") or "").lower()) and
                    ("up or down" in (x.get("question","") or "").lower() or "up/down" in (x.get("question","") or "").lower())
                ), None)
                if not m:
                    continue
                # Try to get token IDs
                tokens = m.get("tokens", [])
                for t in tokens:
                    outcome = (t.get("outcome") or "").lower()
                    if outcome == "up":
                        poly_token_up = t.get("token_id") or t.get("id")
                    elif outcome == "down":
                        poly_token_dn = t.get("token_id") or t.get("id")
                # Also try to get current odds directly
                prices = m.get("outcomePrices")
                if prices:
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    if len(prices) >= 2:
                        up = round(float(prices[0]) * 100)
                        dn = round(float(prices[1]) * 100)
                        if 0 < up < 100:
                            snapshot = {"ts": int(time.time()), "up": up, "dn": dn}
                            poly_odds_history.append(snapshot)
                            if len(poly_odds_history) > 2000:
                                poly_odds_history.pop(0)
                            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Polymarket odds: Up {up}¢ Down {dn}¢")
                            return up, dn
                if poly_token_up:
                    return fetch_poly_odds_from_clob()
            except Exception:
                continue
    except Exception as e:
        print(f"Polymarket market fetch error: {e}")
    return None, None


def fetch_poly_odds_from_clob():
    """Fetch live odds from CLOB API using stored token IDs."""
    global poly_token_up, poly_token_dn
    if not poly_token_up or not poly_token_dn:
        return None, None
    try:
        ru = requests.get(f"https://clob.polymarket.com/price?token_id={poly_token_up}&side=buy", timeout=8)
        rd = requests.get(f"https://clob.polymarket.com/price?token_id={poly_token_dn}&side=buy", timeout=8)
        if ru.ok and rd.ok:
            up = round(float(ru.json()["price"]) * 100)
            dn = round(float(rd.json()["price"]) * 100)
            if 0 < up < 100:
                snapshot = {"ts": int(time.time()), "up": up, "dn": dn}
                poly_odds_history.append(snapshot)
                if len(poly_odds_history) > 2000:
                    poly_odds_history.pop(0)
                return up, dn
    except Exception as e:
        print(f"CLOB odds error: {e}")
    return None, None


def get_odds_at_time(ts_s):
    """Get closest odds snapshot to a given Unix timestamp."""
    if not poly_odds_history:
        return None, None
    closest = min(poly_odds_history, key=lambda x: abs(x["ts"] - ts_s))
    if abs(closest["ts"] - ts_s) < 3600:  # within 1 hour
        return closest["up"], closest["dn"]
    return None, None


def poly_odds_loop():
    """Background thread — fetch Polymarket odds every 5 minutes."""
    while True:
        try:
            if poly_token_up and poly_token_dn:
                fetch_poly_odds_from_clob()
            else:
                fetch_poly_market_ids()
        except Exception as e:
            print(f"Odds loop error: {e}")
        time.sleep(300)  # every 5 minutes


def scheduler_loop():
    """Main scheduler — runs signal analysis at the right time each window."""
    print("Scheduler started")
    last_signal_window = None

    while True:
        try:
            now_s = time.time()
            ws_s = int(get_window_start() / 1000)
            elapsed = now_s - ws_s

            # Fire signal at +30 min (between 1800s and 1830s elapsed)
            if SIG_AT <= elapsed < SIG_AT + 30:
                if last_signal_window != ws_s:
                    last_signal_window = ws_s
                    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Signal window: running analysis...")
                    result = run_analysis()
                    if result and result["direction"] != "SKIP":
                        save_prediction(result, ws_s)
                    elif result:
                        print(f"Signal: SKIP (score {result['score']}/20, conf {result['confidence']}%)")

            # Resolve pending predictions every 5 minutes
            resolve_pending()

        except Exception as e:
            print(f"Scheduler error: {e}")

        time.sleep(30)


def save_prediction(result, ws_s):
    conn = get_db()
    c = conn.cursor()
    ws_ms = ws_s * 1000
    we_ms = ws_ms + WIN_SECS * 1000
    signal_time = datetime.fromtimestamp(ws_s + SIG_AT, tz=timezone.utc).strftime("%H:%M UTC")
    signal_ts = ws_s + SIG_AT

    # Check if we already have a prediction for this window
    c.execute("SELECT id FROM predictions WHERE window_start=?", (ws_ms,))
    if c.fetchone():
        conn.close()
        return

    # Get odds at signal time
    direction = result["direction"]
    conf = result["confidence"]
    odds_up, odds_dn = get_odds_at_time(signal_ts)
    # If no stored odds, try to fetch live
    if not odds_up:
        odds_up, odds_dn = fetch_poly_market_ids()
    signal_odds = odds_up if direction == "UP" else odds_dn

    # Calculate bet size and potential payout
    bet_pct = 0.05 if conf >= 80 else 0.03 if conf >= 75 else 0.02 if conf >= 70 else 0.01
    bankroll = 100
    bet_size = round(bankroll * bet_pct, 2)
    potential_payout = round((100 - signal_odds) / signal_odds * bet_size, 2) if signal_odds and signal_odds > 0 else None

    c.execute("""
        INSERT INTO predictions
        (window_start, window_end, signal_time, direction, confidence, score, open_price,
         factors, market_data, odds, bet_size, potential_payout)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ws_ms, we_ms, signal_time,
        direction, conf, result["score"],
        result["open_price"],
        json.dumps(result["factors"]),
        json.dumps(result["market_data"]),
        signal_odds, bet_size, potential_payout,
    ))
    conn.commit()
    conn.close()
    odds_str = f"Odds: {signal_odds}¢ · Bet: ${bet_size} · Potential: +${potential_payout}" if signal_odds else "Odds: not available"
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] 📊 {direction} | Score {result['score']}/20 | Conf {conf}% | {odds_str}")


# ── HTTP SERVER ──────────────────────────────────────────────────────────────
class KryptosHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default logs

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, content, status=200):
        body = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Serve dashboard
        if path == "/" or path == "/kryptos_v3.html":
            try:
                with open(HTML_PATH, "rb") as f:
                    self.send_html(f.read())
            except FileNotFoundError:
                self.send_html(b"<h1>Dashboard file not found</h1>", 404)
            return

        # API: get predictions
        if path == "/api/predictions":
            conn = get_db()
            c = conn.cursor()
            c.execute("""
                SELECT id, window_start, window_end, signal_time, direction,
                       confidence, score, open_price, close_price, price_diff,
                       result, factors, market_data, created_at,
                       odds, bet_size, potential_payout, actual_pnl
                FROM predictions
                ORDER BY window_start DESC
                LIMIT 50
            """)
            rows = c.fetchall()
            conn.close()
            predictions = []
            for row in rows:
                predictions.append({
                    "id": row[0],
                    "windowStart": row[1],
                    "windowEnd": row[2],
                    "signalTime": row[3],
                    "dir": row[4],
                    "conf": row[5],
                    "score": row[6],
                    "openPrice": row[7],
                    "closePrice": row[8],
                    "priceDiff": row[9],
                    "result": row[10],
                    "factors": json.loads(row[11]) if row[11] else [],
                    "marketData": json.loads(row[12]) if row[12] else {},
                    "createdAt": row[13],
                    "odds": row[14],
                    "betSize": row[15],
                    "potentialPayout": row[16],
                    "actualPnl": row[17],
                })
            self.send_json({"predictions": predictions})
            return

        # API: get trades
        if path == "/api/trades":
            conn = get_db()
            c = conn.cursor()
            c.execute("""
                SELECT id, date, time, direction, outcome, bet_amount, odds, pnl,
                       open_price, confidence, score, created_at
                FROM trades
                ORDER BY created_at DESC
                LIMIT 200
            """)
            rows = c.fetchall()
            conn.close()
            trades = []
            for row in rows:
                trades.append({
                    "id": row[0], "date": row[1], "time": row[2],
                    "dir": row[3], "outcome": row[4], "betAmount": row[5],
                    "odds": row[6], "pnl": row[7], "openPrice": row[8],
                    "conf": row[9], "score": row[10], "createdAt": row[11],
                })
            self.send_json({"trades": trades})
            return

        # API: status
        if path == "/api/status":
            now_ms = int(time.time() * 1000)
            ws_ms = get_window_start()
            elapsed = (now_ms - ws_ms) / 1000
            ws_dt = datetime.fromtimestamp(ws_ms/1000, tz=timezone.utc)
            we_dt = datetime.fromtimestamp((ws_ms + WIN_SECS*1000)/1000, tz=timezone.utc)
            self.send_json({
                "status": "running",
                "windowStart": ws_dt.strftime("%H:%M UTC"),
                "windowEnd": we_dt.strftime("%H:%M UTC"),
                "elapsed": int(elapsed),
                "signalIn": max(0, int(SIG_AT - elapsed)),
                "serverTime": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            })
            return

        # API: general proxy for external requests (avoids browser CORS/geo-block)
        if path == "/api/proxy":
            from urllib.parse import unquote
            qs = parse_qs(parsed.query)
            target = qs.get("url", [None])[0]
            if not target:
                self.send_json({"error": "No URL"}, 400)
                return
            target = unquote(target)
            # Security: only allow whitelisted domains
            allowed = ["api.binance.com","fapi.binance.com","api.alternative.me",
                      "gamma-api.polymarket.com","clob.polymarket.com","api.coinbase.com"]
            from urllib.parse import urlparse as up2
            domain = up2(target).netloc
            if not any(domain.endswith(a) for a in allowed):
                self.send_json({"error": "Domain not allowed"}, 403)
                return
            try:
                r = requests.get(target, timeout=8, headers={"User-Agent":"Mozilla/5.0"})
                r.raise_for_status()
                self.send_json(r.json())
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # API: current BTC price proxy (avoids browser CORS issues)
        if path == "/api/price":
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=5)
                d = r.json()
                self.send_json({
                    "price": float(d["lastPrice"]),
                    "change": float(d["priceChangePercent"]),
                    "high": float(d["highPrice"]),
                    "low": float(d["lowPrice"]),
                    "volume": float(d["volume"]),
                })
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # API: current odds
        if path == "/api/odds":
            up, dn = fetch_poly_odds_from_clob() or (None, None)
            if not up:
                up, dn = fetch_poly_market_ids()
            self.send_json({
                "up": up, "dn": dn,
                "history_count": len(poly_odds_history),
                "latest": poly_odds_history[-1] if poly_odds_history else None,
            })
            return

        # API: trigger signal now (for testing)
        if path == "/api/trigger":
            try:
                result = run_analysis()
                if result:
                    ws_s = int(get_window_start() / 1000)
                    if result["direction"] != "SKIP":
                        save_prediction(result, ws_s)
                    self.send_json({
                        "direction": result["direction"],
                        "confidence": result["confidence"],
                        "score": result["score"],
                        "openPrice": result["open_price"],
                        "factors": result["factors"],
                    })
                else:
                    self.send_json({"error": "Analysis failed"}, 500)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        # API: backfill last N 4H candles as predictions
        if path == "/api/backfill":
            try:
                candles = fetch_candles("4h", 60)  # ~60 candles = 10 days
                saved = 0
                for i in range(1, len(candles)-1):  # all closed candles
                    candle = candles[-(i+1)]  # closed candles
                    ws_ms = int(candle[0])
                    we_ms = ws_ms + WIN_SECS * 1000
                    open_price = float(candle[1])
                    close_price = float(candle[4])

                    # Run analysis using candles up to this point
                    hist = candles[:-(i)]
                    if len(hist) < 15:
                        continue

                    # Quick score calculation for historical candle
                    streak = sum(1 if float(c[4]) >= float(c[1]) else -1 for c in hist[-4:])
                    cvd_val = calc_cvd(hist[-20:])
                    rsi_val = calc_rsi(hist, 14)

                    score = 0
                    if streak >= 2: score += 2
                    elif streak > 0: score += 1
                    elif streak <= -2: score -= 2
                    else: score -= 1

                    if cvd_val > 0: score += 2
                    else: score -= 2

                    if rsi_val:
                        if rsi_val < 30: score += 2
                        elif rsi_val > 70: score -= 2
                        elif rsi_val > 50: score += 1
                        else: score -= 1

                    conf = min(90, round(50 + (abs(score) / 10) * 40))
                    direction = "SKIP"
                    if conf >= 60 and abs(score) >= 3:
                        direction = "UP" if score > 0 else "DOWN"

                    if direction == "SKIP":
                        continue

                    price_diff = close_price - open_price
                    actual_up = close_price >= open_price
                    result_str = "correct" if (
                        (direction == "UP" and actual_up) or
                        (direction == "DOWN" and not actual_up)
                    ) else "wrong"

                    signal_time = datetime.fromtimestamp((ws_ms/1000) + SIG_AT, tz=timezone.utc).strftime("%H:%M UTC")

                    conn = get_db()
                    c = conn.cursor()
                    c.execute("SELECT id FROM predictions WHERE window_start=?", (ws_ms,))
                    if not c.fetchone():
                        c.execute("""
                            INSERT INTO predictions
                            (window_start, window_end, signal_time, direction, confidence, score,
                             open_price, close_price, price_diff, result, factors, market_data)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            ws_ms, we_ms, signal_time, direction, conf, score,
                            open_price, close_price, price_diff, result_str,
                            json.dumps([
                                {"n": "Candle Streak", "sc": 2 if streak >= 2 else 1 if streak > 0 else -2 if streak <= -2 else -1, "val": f"{streak:+d} streak"},
                                {"n": "CVD", "sc": 2 if cvd_val > 0 else -2, "val": f"Net {'buying' if cvd_val > 0 else 'selling'}"},
                                {"n": "RSI 4H", "sc": 2 if rsi_val and rsi_val < 30 else -2 if rsi_val and rsi_val > 70 else 1 if rsi_val and rsi_val > 50 else -1, "val": f"{rsi_val:.1f}" if rsi_val else "—"},
                            ]),
                            json.dumps({})
                        ))
                        conn.commit()
                        saved += 1
                    conn.close()

                self.send_json({"success": True, "saved": saved})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # API: log a trade
        if path == "/api/trades":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                conn = get_db()
                c = conn.cursor()
                now = datetime.now(timezone.utc)
                c.execute("""
                    INSERT INTO trades (date, time, direction, outcome, bet_amount, odds, pnl, open_price, confidence, score)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    now.strftime("%Y-%m-%d"),
                    now.strftime("%H:%M UTC"),
                    data.get("dir"),
                    data.get("outcome"),
                    data.get("betAmount"),
                    data.get("odds"),
                    data.get("pnl"),
                    data.get("openPrice"),
                    data.get("conf"),
                    data.get("score"),
                ))
                conn.commit()
                conn.close()
                self.send_json({"success": True})
            except Exception as e:
                self.send_json({"error": str(e)}, 400)
            return

        self.send_json({"error": "Not found"}, 404)


# ── MAIN ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  KRYPTOS Backend v1.0")
    print(f"  Port: {PORT}")
    print("=" * 50)

    init_db()

    # Start odds fetcher in background thread
    t_odds = threading.Thread(target=poly_odds_loop, daemon=True)
    t_odds.start()

    # Start scheduler in background thread
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

    # Start HTTP server
    server = HTTPServer(("", PORT), KryptosHandler)
    print(f"Server running on port {PORT}")
    server.serve_forever()
