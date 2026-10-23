"""
CME Multi-Asset Options & Vol2Vol Tracker v2.0 (Hardened)
─────────────────────────────────────────────────────────
รองรับ: Gold, Silver, Oil, S&P 500, Nasdaq, Bitcoin

Changelog v2.0 (2026-10-23):
  - [FIX] BTC IV: ใช้ Deribit DVOL แทน ^VIX ที่ผิดชนิด (38% vs 19%)
  - [FIX] Per-Expiry Parsing: แยก front-month (≈30 DTE) + front-week (1-5 DTE)
  - [FIX] Accurate DTE: แกะ DTE จาก inner font-size <span> ไม่สับสนกับเลขปี 2026
  - [FIX] Timezone: ใช้ Asia/Bangkok แทน UTC ช้ากว่า 7 ชม.
  - [FIX] Spot Gold: ใช้ data-api.binance.vision + sanity check (<2% deviation)
  - [HARDEN] ค้นหาตารางด้วย CSS class 'grid-thm' + Header content แทน tables[6]
  - [HARDEN] ตรวจ content verification ป้องกัน challenge/error page
  - [HARDEN] ลด max_workers=2 + jitter ป้องกัน Akamai WAF rate-limit
  - [HARDEN] Discord raise_for_status + 429 retry backoff
  - [HARDEN] IV fallback แสดง ⚠️ ไม่ซ่อนความล้มเหลว
  - [HARDEN] safe_process_asset isolate error ต่อ asset
  - [HARDEN] Robust Walls: fallback หา strike ที่มี max OI ป้องกัน N/A
  - [DATA] เก็บ Prior-Day OI ไว้ใน data structure (ΔOI-ready)
"""

import os
import re
import sys
import math
import time
import random
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Logging ────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tracker")

# ── Config ─────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
TZ = ZoneInfo("Asia/Bangkok")

ASSETS = {
    "gold": {
        "name": "Gold (COMEX GC)",
        "icon": "🥇",
        "pid": 40,
        "color": 16766720,       # Gold
        "iv_source": "yahoo",
        "iv_symbol": "^GVZ",
        "iv_fallback": 0.231,
        "unit": "$",
        "decimals": 1,
        "cal_days": 365,
    },
    "silver": {
        "name": "Silver (COMEX SI)",
        "icon": "🥈",
        "pid": 41,
        "color": 12632256,       # Silver
        "iv_source": "yahoo",
        "iv_symbol": "^VXSLV",
        "iv_fallback": 0.300,
        "unit": "$",
        "decimals": 3,
        "cal_days": 365,
    },
    "oil": {
        "name": "Crude Oil WTI (NYMEX CL)",
        "icon": "🛢️",
        "pid": 30,
        "color": 3447003,        # Blue
        "iv_source": "yahoo",
        "iv_symbol": "^OVX",
        "iv_fallback": 0.350,
        "unit": "$",
        "decimals": 2,
        "cal_days": 365,
    },
    "sp500": {
        "name": "E-mini S&P 500 (CME ES)",
        "icon": "📈",
        "pid": 103,
        "color": 3066993,        # Green
        "iv_source": "yahoo",
        "iv_symbol": "^VIX",
        "iv_fallback": 0.160,
        "unit": "pts",
        "decimals": 2,
        "cal_days": 365,
    },
    "nasdaq": {
        "name": "E-mini Nasdaq 100 (CME NQ)",
        "icon": "💻",
        "pid": 121,
        "color": 10181046,       # Purple
        "iv_source": "yahoo",
        "iv_symbol": "^VXN",
        "iv_fallback": 0.200,
        "unit": "pts",
        "decimals": 2,
        "cal_days": 365,
    },
    "btc": {
        "name": "Bitcoin Futures (CME BTC)",
        "icon": "₿",
        "pid": 360,
        "color": 16098851,       # Bitcoin Orange
        "iv_source": "deribit",  # ← v2.0: Deribit DVOL แทน ^VIX
        "iv_symbol": "BTC",
        "iv_fallback": 0.520,
        "unit": "$",
        "decimals": 0,
        "cal_days": 365,         # crypto trades 24/7 → calendar days
    },
}

HEADERS_CME = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.cmegroup.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ══════════════════════════════════════════════════════
#  Utilities
# ══════════════════════════════════════════════════════

def make_session(retries=3, backoff=0.8):
    """Create requests.Session with automatic retry + backoff."""
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def safe_int(text):
    """Convert text → int อย่างปลอดภัย (รองรับ '', '1,234', '1234.0')."""
    if not text:
        return 0
    text = text.strip().replace(",", "")
    try:
        return int(float(text))
    except (ValueError, OverflowError):
        return 0


def fmt(val, d):
    """Format ตัวเลขตามจำนวนทศนิยมของ asset."""
    if val is None:
        return "N/A"
    if d == 0:
        return str(int(round(val)))
    return str(round(val, d))


# ══════════════════════════════════════════════════════
#  CME QuikStrike Parser (Hardened)
# ══════════════════════════════════════════════════════

def find_oi_table(soup):
    """ค้นหาตาราง OI heatmap จาก CSS class + content ไม่ใช่จาก index."""
    for table in soup.find_all("table"):
        classes = table.get("class", [])
        if "grid-thm" in classes:
            text = table.get_text()
            if "Strike" in text and "DTE" in text:
                return table
    return None


def parse_expiry_headers(table):
    """
    แกะ header ของตาราง เพื่อดึงข้อมูล expiration แต่ละอันอย่างแม่นยำ
    สกัด DTE จาก inner span เพื่อไม่ให้เลขปี (เช่น GCZ6) ปนกับเลข DTE (เช่น 1 DTE)
    Returns: list of dict {code, dte, expires_date, underlying, underlying_price}
    """
    rows = table.find_all("tr")
    if len(rows) < 3:
        return []

    r0_cells = rows[0].find_all(["th", "td"])   # Underlying futures
    r1_cells = rows[1].find_all(["th", "td"])   # Expiry codes + DTE

    expirations = []
    for i in range(1, len(r1_cells)):            # ข้าม cell แรก "Strike"
        th = r1_cells[i]
        span = th.find("span")

        dte = 999
        code = ""
        expires_date = ""

        if span:
            # ดึงวันหมดอายุจาก title attribute
            title = span.get("title", "")
            date_match = re.search(r"Expires?:\s*(.+)", title)
            if date_match:
                expires_date = date_match.group(1).strip()

            # สกัด DTE จาก inner span font-size:90% โดยตรง
            inner_dte = span.find("span", style=lambda s: s and "font-size" in s)
            if inner_dte:
                try:
                    dte = int(inner_dte.get_text(strip=True))
                except ValueError:
                    pass

            # สกัด code โดยการตัด DTE และตัวเลข DTE ท้ายข้อความ
            full_span_text = span.get_text(strip=True)
            clean = re.sub(r"DTE$", "", full_span_text, flags=re.IGNORECASE).strip()
            if dte != 999 and clean.endswith(str(dte)):
                code = clean[:-len(str(dte))].strip()
            else:
                m = re.match(r"^(.*?)(?:\s*\d+)?$", clean)
                code = m.group(1).strip() if m else clean
        else:
            code = th.get_text(strip=True)

        # ดึงราคา underlying futures จาก Row 0
        underlying_price = 0.0
        if i < len(r0_cells):
            for sp in r0_cells[i].find_all("span"):
                inner = sp.find("span")
                if inner and inner.text.strip():
                    try:
                        underlying_price = float(
                            inner.text.strip().replace(",", "")
                        )
                        break
                    except ValueError:
                        pass

        expirations.append({
            "code": code,
            "dte": dte,
            "expires_date": expires_date,
            "underlying_price": underlying_price,
        })

    return expirations


def parse_oi_data(table, num_expirations):
    """
    แกะข้อมูล OI ต่อ strike ต่อ expiry (พร้อม prior-day OI จาก title)
    Returns: {strike: [{call_oi, put_oi, prior_call, prior_put}, ...]}
    """
    rows = table.find_all("tr")
    data = {}

    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        # ตรวจว่า cell แรกเป็น strike price หรือไม่
        strike_text = cells[0].get_text(strip=True).replace(",", "")
        try:
            strike = float(strike_text)
            if strike <= 0:
                continue
        except ValueError:
            continue

        # Cell ที่เหลือคือคู่ (Call OI, Put OI) ต่อ expiry
        data_cells = cells[1:]
        expiry_data = []

        for exp_idx in range(num_expirations):
            c_idx = exp_idx * 2
            p_idx = exp_idx * 2 + 1

            call_oi = 0
            put_oi = 0
            prior_call = 0
            prior_put = 0

            if c_idx < len(data_cells):
                c_text = data_cells[c_idx].get_text(strip=True).replace(",", "")
                call_oi = safe_int(c_text)
                prior_call = safe_int(
                    data_cells[c_idx].get("title", "").replace(",", "")
                )

            if p_idx < len(data_cells):
                p_text = data_cells[p_idx].get_text(strip=True).replace(",", "")
                put_oi = safe_int(p_text)
                prior_put = safe_int(
                    data_cells[p_idx].get("title", "").replace(",", "")
                )

            expiry_data.append({
                "call_oi": call_oi,
                "put_oi": put_oi,
                "prior_call": prior_call,
                "prior_put": prior_put,
            })

        data[strike] = expiry_data

    return data


def select_front_month(expirations, oi_data=None):
    """
    เลือก Front-Month expiry (DTE ใกล้ 30 ที่สุดในช่วง 15–60 ที่มีสภาพคล่อง OI จริง)
    """
    exp_ois = {}
    if oi_data:
        for idx in range(len(expirations)):
            tot = 0
            for strike, elist in oi_data.items():
                if idx < len(elist):
                    tot += elist[idx]["call_oi"] + elist[idx]["put_oi"]
            exp_ois[idx] = tot

    candidates = []
    for idx, e in enumerate(expirations):
        if 15 <= e["dte"] <= 60:
            oi = exp_ois.get(idx, 1)
            candidates.append((idx, e, oi))

    # ถ้ามีตัวเลือกที่มีสภาพคล่อง (OI >= 100 สัญญา) ให้เลือกตัวที่ใกล้ 30 DTE ที่สุด
    active = [c for c in candidates if c[2] >= 100]
    if active:
        # เรียงตามความใกล้ 30 DTE และตามด้วยปริมาณ OI
        best = min(active, key=lambda c: (abs(c[1]["dte"] - 30), -c[2]))
        return best[1]

    if candidates:
        best = min(candidates, key=lambda c: abs(c[1]["dte"] - 30))
        return best[1]

    if expirations:
        return min(expirations, key=lambda e: abs(e["dte"] - 30))
    return None


def select_front_week(expirations):
    """เลือก Front-Week expiry (DTE ต่ำสุดในช่วง 1–7 DTE)."""
    candidates = [e for e in expirations if 1 <= e["dte"] <= 7]
    if candidates:
        return min(candidates, key=lambda e: e["dte"])
    return None


def extract_expiry_oi(oi_data, exp_index):
    """ดึง Call/Put OI dict สำหรับ expiry ที่ระบุ."""
    calls = {}
    puts = {}
    for strike, expiry_list in oi_data.items():
        if exp_index < len(expiry_list):
            d = expiry_list[exp_index]
            calls[strike] = d["call_oi"]
            puts[strike] = d["put_oi"]
    return calls, puts


def fetch_cme_data(pid):
    """
    ดึงและแกะข้อมูล CME QuikStrike VOI Heatmap
    Returns: (expirations, oi_data, price) หรือ None เมื่อล้มเหลว
    """
    url = (
        f"https://cmegroup-tools.quikstrike.net/User/QuikStrikeView.aspx"
        f"?pid={pid}&viewitemid=IntegratedVOIHeatMap"
    )

    session = make_session(retries=3, backoff=1.0)
    session.headers.update(HEADERS_CME)

    try:
        resp = session.get(url, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"CME request failed (pid={pid}): {e}")
        return None

    # ── ตรวจ challenge/error page ──
    if "QuikStrike Error" in resp.text or "Access Denied" in resp.text:
        log.error(f"CME returned error/challenge page (pid={pid}), len={len(resp.text)}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── ค้นหาตารางด้วย class ไม่ใช่ index ──
    table = find_oi_table(soup)
    if not table:
        log.error(f"OI heatmap table not found (pid={pid}), total tables={len(soup.find_all('table'))}")
        return None

    expirations = parse_expiry_headers(table)
    if not expirations:
        log.error(f"No expirations parsed (pid={pid})")
        return None

    oi_data = parse_oi_data(table, len(expirations))
    if not oi_data:
        log.error(f"No OI data parsed (pid={pid})")
        return None

    # ใช้ราคา underlying ของ front-month เป็นราคาอ้างอิง
    front = select_front_month(expirations, oi_data)
    price = front["underlying_price"] if front else 0.0

    # Fallback: ราคาจาก expiry แรก
    if price == 0.0 and expirations:
        price = expirations[0]["underlying_price"]

    log.info(f"CME pid={pid}: price={price}, expirations={len(expirations)}, strikes={len(oi_data)}")
    return expirations, oi_data, price


# ══════════════════════════════════════════════════════
#  IV Sources
# ══════════════════════════════════════════════════════

def get_iv_yahoo(symbol, fallback):
    """ดึง IV จาก Yahoo Finance (GVZ, OVX, VIX, VXN)."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        resp = requests.get(
            url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6
        )
        resp.raise_for_status()
        p = resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        iv = float(p) / 100
        log.info(f"Yahoo IV ({symbol}): {iv:.3f}")
        return iv, False   # (value, is_fallback)
    except Exception as e:
        log.warning(f"Yahoo IV failed ({symbol}): {e} → fallback {fallback}")
        return fallback, True


def get_iv_deribit(currency="BTC", fallback=0.52):
    """ดึง 30-day IV จาก Deribit DVOL (สำหรับ crypto)."""
    try:
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 86400 * 1000   # 24 ชม. ก่อน
        url = (
            f"https://www.deribit.com/api/v2/public/get_volatility_index_data"
            f"?currency={currency}"
            f"&start_timestamp={start_ms}"
            f"&end_timestamp={now_ms}"
            f"&resolution=1D"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()["result"]["data"]
        if data:
            dvol = data[-1][4]   # close value
            iv = dvol / 100      # DVOL มาเป็น % (เช่น 37.8 → 0.378)
            log.info(f"Deribit DVOL ({currency}): {iv:.3f}")
            return iv, False
    except Exception as e:
        log.warning(f"Deribit DVOL failed ({currency}): {e} → fallback {fallback}")
    return fallback, True


def get_volatility(cfg):
    """เลือก IV source ตาม asset config."""
    source = cfg.get("iv_source", "yahoo")
    if source == "deribit":
        return get_iv_deribit(cfg["iv_symbol"], cfg["iv_fallback"])
    return get_iv_yahoo(cfg["iv_symbol"], cfg["iv_fallback"])


# ══════════════════════════════════════════════════════
#  Spot Gold
# ══════════════════════════════════════════════════════

def get_spot_gold():
    """ดึงราคาทอง Spot (XAU/USD) พร้อม fallback chain ป้องกัน US IP block."""
    sources = [
        (
            "Binance Vision (PAXG)",
            "https://data-api.binance.vision/api/v3/ticker/price?symbol=PAXGUSDT",
            lambda d: float(d["price"]),
        ),
        (
            "Gold-API.com",
            "https://api.gold-api.com/price/XAU",
            lambda d: float(d["price"]),
        ),
    ]

    for name, url, parser in sources:
        try:
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            price = round(parser(r.json()), 1)
            log.info(f"Spot gold from {name}: {price}")
            return price, name
        except Exception as e:
            log.warning(f"Spot gold {name} failed: {e}")

    return None, None


# ══════════════════════════════════════════════════════
#  Calculations
# ══════════════════════════════════════════════════════

def calc_ranges(price, iv, cal_days, decimals):
    """คำนวณ Expected Range (1-day, 5-day, 30-day)."""
    d = decimals
    m_1d = price * iv * math.sqrt(1 / cal_days)
    m_5d = price * iv * math.sqrt(5 / cal_days)
    m_30d = price * iv * math.sqrt(30 / cal_days)
    return (
        (round(price - m_1d, d), round(price + m_1d, d), round(m_1d, d)),
        (round(price - m_5d, d), round(price + m_5d, d), round(m_5d, d)),
        (round(price - m_30d, d), round(price + m_30d, d), round(m_30d, d)),
    )


def calc_max_pain(calls, puts, price):
    """คำนวณ Max Pain (strike ที่ option writer เจ็บน้อยสุด)."""
    strikes = sorted(set(calls.keys()) | set(puts.keys()))
    strikes = [s for s in strikes if price * 0.85 <= s <= price * 1.15]
    if not strikes:
        strikes = sorted(set(calls.keys()) | set(puts.keys()))
    if not strikes:
        return None

    pain = {}
    for test in strikes:
        loss = 0.0
        for s, oi in calls.items():
            if s < test:
                loss += (test - s) * oi
        for s, oi in puts.items():
            if s > test:
                loss += (s - test) * oi
        pain[test] = loss

    return min(pain, key=pain.get)


def find_walls(calls, puts, price):
    """
    หา Call Wall (max Call OI เหนือราคา) & Put Wall (max Put OI ใต้ราคา)
    พร้อม fallback หา strike ที่มี max OI รวมกรณีราคากระโดดหลุดขอบตาราง 31 strikes
    """
    calls_above = {s: oi for s, oi in calls.items() if s >= price and oi > 0}
    call_wall = max(calls_above, key=calls_above.get) if calls_above else None
    if call_wall is None:
        calls_any = {s: oi for s, oi in calls.items() if oi > 0}
        call_wall = max(calls_any, key=calls_any.get) if calls_any else None

    puts_below = {s: oi for s, oi in puts.items() if s <= price and oi > 0}
    put_wall = max(puts_below, key=puts_below.get) if puts_below else None
    if put_wall is None:
        puts_any = {s: oi for s, oi in puts.items() if oi > 0}
        put_wall = max(puts_any, key=puts_any.get) if puts_any else None

    return call_wall, put_wall


# ══════════════════════════════════════════════════════
#  ASCII Chart
# ══════════════════════════════════════════════════════

def make_ascii_chart(calls, puts, price, max_pain, call_wall, put_wall, n=10):
    """สร้าง ASCII OI chart (เรียงจากบนลงล่าง ตรงกับ TradingView)."""
    strikes = sorted(set(calls.keys()) | set(puts.keys()))
    strikes = [s for s in strikes if calls.get(s, 0) > 0 or puts.get(s, 0) > 0]
    if not strikes:
        return ""

    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - price))
    start = max(0, atm_idx - n // 2)
    subset = strikes[start : start + n][::-1]   # High → Low (TradingView style)

    max_oi = max(
        (max(calls.get(s, 0), puts.get(s, 0)) for s in subset), default=1
    ) or 1

    lines = ["```", f"{'STRIKE':>8} | {'CALL OI':^9} | {'PUT OI':^9}"]
    lines.append("-" * 34)

    for s in subset:
        c_bar = "█" * int(calls.get(s, 0) / max_oi * 7)
        p_bar = "█" * int(puts.get(s, 0) / max_oi * 7)
        tag = ""
        if abs(s - price) == min(abs(x - price) for x in subset):
            tag = " ←NOW"
        elif max_pain is not None and s == max_pain:
            tag = " ←PAIN"
        elif call_wall is not None and s == call_wall:
            tag = " ←CALL"
        elif put_wall is not None and s == put_wall:
            tag = " ←PUT"
        lines.append(f"{s:>8} | {c_bar:<9} | {p_bar:<9}{tag}")

    lines.append("```")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════
#  Main Asset Processor
# ══════════════════════════════════════════════════════

def process_asset(key):
    """ดึงข้อมูล CME → คำนวณ → สร้าง Discord embed สำหรับ 1 asset."""
    cfg = ASSETS.get(key)
    if not cfg:
        log.error(f"Unknown asset key: {key}")
        return None

    log.info(f"▶ Processing {cfg['name']} ...")

    # ─── 1. ดึงข้อมูล CME ───
    result = fetch_cme_data(cfg["pid"])
    if not result:
        log.error(f"CME data unavailable for {cfg['name']}")
        return None

    expirations, oi_data, price = result
    d = cfg["decimals"]
    unit = cfg["unit"]

    # ─── 2. เลือก Front-Month Expiry ───
    front = select_front_month(expirations, oi_data)
    if not front:
        log.error(f"No front-month expiry for {cfg['name']}")
        return None

    front_idx = expirations.index(front)
    front_calls, front_puts = extract_expiry_oi(oi_data, front_idx)

    # ─── 2b. Front-Week (ถ้ามี) ───
    front_week = select_front_week(expirations)
    fw_info = ""
    if front_week and front_week != front:
        fw_idx = expirations.index(front_week)
        fw_calls, fw_puts = extract_expiry_oi(oi_data, fw_idx)
        fw_cw, fw_pw = find_walls(fw_calls, fw_puts, price)
        if fw_cw is not None and fw_pw is not None:
            fw_info = (
                f"\n📌 *Front-Week ({front_week['code']} | "
                f"{front_week['dte']} DTE):*  "
                f"Call `{fmt(fw_cw, d)}` / Put `{fmt(fw_pw, d)}`"
            )

    # ─── 3. ดึง IV ───
    iv, iv_is_fallback = get_volatility(cfg)
    iv_pct = f"{iv * 100:.1f}%"
    if iv_is_fallback:
        iv_pct += " ⚠️(fallback)"

    # ─── 4. คำนวณ Expected Ranges ───
    r_1d, r_5d, r_30d = calc_ranges(price, iv, cfg["cal_days"], d)

    # ─── 5. Max Pain & Walls (front-month) ───
    max_pain = calc_max_pain(front_calls, front_puts, price)
    call_wall, put_wall = find_walls(front_calls, front_puts, price)

    # ─── 6. ASCII chart (front-month OI) ───
    chart = make_ascii_chart(
        front_calls, front_puts, price, max_pain, call_wall, put_wall
    )

    # ─── 7. Spot Gold + Spread (เฉพาะทองคำ) ───
    spot_note = ""
    if key == "gold":
        spot, spot_source = get_spot_gold()
        if spot:
            spread = round(price - spot, d)
            # Sanity check: spread ไม่ควรเกิน 2% ของ futures
            if abs(spread) < price * 0.02:
                spread_sign = f"+{spread}" if spread >= 0 else f"{spread}"
                price_display = (
                    f"💰 **Futures (CME):** `{fmt(price, d)}`\n"
                    f"🪙 **Spot (XAU):** `{spot}` "
                    f"*(Spread: `{spread_sign}{unit}`)*"
                )
                spot_note = f"\n*Spot via {spot_source} (PAXG proxy)*"
            else:
                log.warning(f"Spot/Futures spread too large ({spread}), ignoring spot")
                price_display = f"💰 **Futures Price:** `{fmt(price, d)}`"
        else:
            price_display = f"💰 **Futures Price:** `{fmt(price, d)}`"
    else:
        price_display = f"💰 **Price:** `{fmt(price, d)}`"

    # ─── 8. สร้าง Discord Embed ───
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    expiry_label = f"{front['code']} | {front['dte']} DTE"
    if front["expires_date"]:
        expiry_label += f" | Exp: {front['expires_date']}"

    embed = {
        "title": f"{cfg['icon']} {cfg['name']} — Options & Vol2Vol",
        "description": (
            f"อัปเดตข้อมูลตรงจาก **CME QuikStrike** | "
            f"เวลา: `{now}`\n"
            f"📅 **Expiry:** `{expiry_label}`"
        ),
        "color": cfg["color"],
        "fields": [
            {
                "name": "📍 KEY LEVELS",
                "value": (
                    f"{price_display}\n"
                    f"⚖️ **Max Pain:** `{fmt(max_pain, d)}`\n"
                    f"🟢 **Call Wall:** `{fmt(call_wall, d)}`\n"
                    f"🔴 **Put Wall:** `{fmt(put_wall, d)}`\n"
                    f"📈 **Volatility:** `{iv_pct}` "
                    f"(เหวี่ยง ±{fmt(r_1d[2], d)}{unit}/วัน)"
                    f"{fw_info}{spot_note}"
                ),
                "inline": False,
            },
            {
                "name": "📐 EXPECTED RANGES (Vol2Vol)",
                "value": (
                    f"☀️ **1-Day:** `{fmt(r_1d[0], d)}` — "
                    f"`{fmt(r_1d[1], d)}`\n"
                    f"📅 **5-Day:** `{fmt(r_5d[0], d)}` — "
                    f"`{fmt(r_5d[1], d)}`\n"
                    f"🗓️ **30-Day:** `{fmt(r_30d[0], d)}` — "
                    f"`{fmt(r_30d[1], d)}`"
                ),
                "inline": False,
            },
            {
                "name": (
                    f"📊 OPEN INTEREST — {front['code']} (High → Low)"
                ),
                "value": chart if chart else "ไม่มีข้อมูล OI สำหรับ expiry นี้",
                "inline": False,
            },
            {
                "name": "📋 Pine Script (TradingView)",
                "value": (
                    f"```pinescript\n"
                    f"// {front['code']} ({front['dte']} DTE) "
                    f"— Futures-based levels\n"
                    f"fair_value = {fmt(max_pain, d)}  // Max Pain\n"
                    f"call_wall  = {fmt(call_wall, d)}\n"
                    f"put_wall   = {fmt(put_wall, d)}\n"
                    f"daily_high = {fmt(r_1d[1], d)}\n"
                    f"daily_low  = {fmt(r_1d[0], d)}\n"
                    f"```"
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text": (
                f"Data Source: CME QuikStrike • "
                f"{front['code']} ({front['dte']} DTE) • "
                f"Real Exchange Data"
            )
        },
    }

    log.info(
        f"✅ {cfg['name']}: Price={fmt(price, d)}, "
        f"MaxPain={fmt(max_pain, d)}, "
        f"CW={fmt(call_wall, d)}, PW={fmt(put_wall, d)}, "
        f"IV={iv_pct}, Expiry={front['code']}({front['dte']}DTE)"
    )
    return embed


# ══════════════════════════════════════════════════════
#  Error-Isolated Wrapper & Discord Sender
# ══════════════════════════════════════════════════════

def safe_process_asset(key):
    """Wrap process_asset → 1 asset ล้มเหลวไม่กระทบตัวอื่น."""
    try:
        return key, process_asset(key)
    except Exception as e:
        log.error(f"Fatal error processing {key}: {e}", exc_info=True)
        return key, None


def send_to_discord(embeds):
    """ส่ง embed ไป Discord webhook พร้อม rate-limit handling."""
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping send")
        return

    session = make_session(retries=2, backoff=1.0)

    for embed in embeds:
        title = embed.get("title", "?")
        try:
            resp = session.post(
                DISCORD_WEBHOOK_URL,
                json={"embeds": [embed]},
                timeout=15,
            )
            resp.raise_for_status()
            log.info(f"✅ Sent: {title}")

            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and int(remaining) < 2:
                reset_after = float(
                    resp.headers.get("X-RateLimit-Reset-After", 2)
                )
                log.info(f"Rate limit approaching, waiting {reset_after}s")
                time.sleep(reset_after)
            else:
                time.sleep(0.5)

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429:
                retry_after = float(
                    e.response.headers.get("Retry-After", 5)
                )
                log.warning(
                    f"Discord rate-limited, waiting {retry_after}s ..."
                )
                time.sleep(retry_after)
                try:
                    resp2 = session.post(
                        DISCORD_WEBHOOK_URL,
                        json={"embeds": [embed]},
                        timeout=15,
                    )
                    resp2.raise_for_status()
                    log.info(f"✅ Sent (retry): {title}")
                except Exception as e2:
                    log.error(f"❌ Failed after retry: {title}: {e2}")
            else:
                body = (e.response.text[:200]
                        if e.response is not None else "")
                log.error(f"❌ Discord HTTP {status}: {title} — {body}")

        except Exception as e:
            log.error(f"❌ Discord send failed: {title}: {e}")


# ══════════════════════════════════════════════════════
#  Entry Point
# ══════════════════════════════════════════════════════

def main():
    target = (
        sys.argv[1].lower()
        if len(sys.argv) > 1
        else os.environ.get("TARGET_ASSET", "gold").lower()
    )

    log.info(
        f"═══ CME Options Tracker v2.0 | Target: {target} | "
        f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} ═══"
    )

    embeds = []
    failed = []

    if target == "all":
        # Concurrency=2 + jitter เพื่อลด rate-limit risk
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}
            for key in ASSETS:
                time.sleep(random.uniform(0.5, 1.2))   # Jitter
                f = executor.submit(safe_process_asset, key)
                futures[f] = key

            for f in as_completed(futures):
                key, embed = f.result()
                if embed:
                    embeds.append(embed)
                else:
                    failed.append(key)
                    log.warning(f"⚠️ {key}: No data returned")
    else:
        key, embed = safe_process_asset(target)
        if embed:
            embeds.append(embed)
        else:
            failed.append(target)

    # Report & Send
    total = len(ASSETS) if target == "all" else 1
    log.info(
        f"Results: {len(embeds)}/{total} success"
        + (f", failed: {failed}" if failed else "")
    )

    if embeds:
        send_to_discord(embeds)
        log.info(f"🏁 Done. Sent {len(embeds)} embed(s).")
    else:
        log.error("❌ No embeds generated. Nothing sent to Discord.")
        sys.exit(1)


if __name__ == "__main__":
    main()
