"""
Gold Options Tracker v3
ใช้ Yahoo Finance API ตรงๆ (ไม่ผ่าน yfinance library)
"""

import requests
import numpy as np
from datetime import datetime
import math
import os
import time
import json

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")

GLD_MULT = 10.0  # GLD × 10 ≈ ราคาทอง

# ── Headers กัน Block ────────────────────────────────
HEADERS = {
    "User-Agent"      : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept"          : "application/json, text/plain, */*",
    "Accept-Language" : "en-US,en;q=0.9",
    "Referer"         : "https://finance.yahoo.com",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ── 1. ดึงราคาปัจจุบัน ───────────────────────────────
def get_price(symbol="GLD"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": "5d"}

    for attempt in range(3):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
            data = resp.json()

            result = data["chart"]["result"][0]
            price  = result["meta"]["regularMarketPrice"]
            return float(price)

        except Exception as e:
            print(f"⚠️ get_price attempt {attempt+1} failed: {e}")
            time.sleep(3)

    return None

# ── 2. ดึง Options Expiry List ───────────────────────
def get_expiry_list(symbol="GLD"):
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"

    for attempt in range(3):
        try:
            resp  = SESSION.get(url, timeout=15)
            data  = resp.json()
            dates = data["optionChain"]["result"][0]["expirationDates"]
            return dates  # Unix timestamps

        except Exception as e:
            print(f"⚠️ get_expiry attempt {attempt+1} failed: {e}")
            time.sleep(3)

    return []

# ── 3. ดึง Options Chain ─────────────────────────────
def get_options_chain(symbol="GLD", expiry_ts=None):
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
    params = {}
    if expiry_ts:
        params["date"] = expiry_ts

    for attempt in range(3):
        try:
            resp   = SESSION.get(url, params=params, timeout=15)
            data   = resp.json()
            result = data["optionChain"]["result"][0]["options"][0]

            calls = result.get("calls", [])
            puts  = result.get("puts",  [])
            return calls, puts

        except Exception as e:
            print(f"⚠️ get_chain attempt {attempt+1} failed: {e}")
            time.sleep(3)

    return [], []

# ── 4. หา Expiry ที่เหมาะสม ──────────────────────────
def pick_best_expiry(expiry_timestamps):
    today     = datetime.now()
    best_ts   = None
    best_days = None

    for ts in expiry_timestamps:
        exp_date  = datetime.fromtimestamp(ts)
        days_left = (exp_date - today).days

        if 7 <= days_left <= 45:
            if best_days is None or days_left < best_days:
                best_ts   = ts
                best_days = days_left

    # fallback: อันแรก
    if not best_ts and expiry_timestamps:
        best_ts   = expiry_timestamps[0]
        best_days = (datetime.fromtimestamp(best_ts) - today).days

    return best_ts, best_days

# ── 5. แปลง Options List → Dict by strike ────────────
def parse_chain(options_list):
    result = {}
    for opt in options_list:
        strike = float(opt.get("strike", 0))
        oi     = int(opt.get("openInterest", 0) or 0)
        iv     = float(opt.get("impliedVolatility", 0) or 0)
        result[strike] = {"oi": oi, "iv": iv}
    return result

# ── 6. คำนวณ Max Pain ────────────────────────────────
def calc_max_pain(calls_dict, puts_dict):
    all_strikes = sorted(set(calls_dict.keys()) | set(puts_dict.keys()))
    if not all_strikes:
        return None

    pain = {}
    for test in all_strikes:
        total = 0.0
        for s, v in calls_dict.items():
            if s < test:
                total += (test - s) * v["oi"]
        for s, v in puts_dict.items():
            if s > test:
                total += (s - test) * v["oi"]
        pain[test] = total

    return min(pain, key=pain.get)

# ── 7. หา Call Wall / Put Wall ───────────────────────
def find_walls(calls_dict, puts_dict, current_price):
    # Call Wall = strike เหนือราคา OI สูงสุด
    calls_above = {s: v for s, v in calls_dict.items() if s >= current_price}
    call_wall   = max(calls_above, key=lambda s: calls_above[s]["oi"]) if calls_above else current_price + 20

    # Put Wall = strike ใต้ราคา OI สูงสุด
    puts_below = {s: v for s, v in puts_dict.items() if s <= current_price}
    put_wall   = max(puts_below, key=lambda s: puts_below[s]["oi"]) if puts_below else current_price - 20

    return float(call_wall), float(put_wall)

# ── 8. หา ATM IV ─────────────────────────────────────
def get_atm_iv(calls_dict, puts_dict, current_price):
    all_strikes = sorted(set(calls_dict.keys()) | set(puts_dict.keys()))
    if not all_strikes:
        return None

    atm  = min(all_strikes, key=lambda x: abs(x - current_price))
    ivs  = []

    for d in [calls_dict, puts_dict]:
        if atm in d and d[atm]["iv"] > 0.01:
            ivs.append(d[atm]["iv"])

    return float(np.mean(ivs)) if ivs else None

# ── 9. คำนวณ Expected Range ──────────────────────────
def calc_range(price, iv, days):
    if not iv or not days or days <= 0:
        return None, None, None, None

    m1 = price * iv * math.sqrt(days / 365)
    m2 = m1 * 2

    return (
        round(price + m1, 1), round(price - m1, 1),
        round(price + m2, 1), round(price - m2, 1),
    )

# ── 10. OI Bar Chart ─────────────────────────────────
def make_chart(calls_dict, puts_dict, current_price, max_pain, mult=1.0, n=10):
    all_strikes = sorted(set(calls_dict.keys()) | set(puts_dict.keys()))
    if not all_strikes:
        return ""

    atm_idx = min(range(len(all_strikes)),
                  key=lambda i: abs(all_strikes[i] - current_price))
    start  = max(0, atm_idx - n // 2)
    subset = all_strikes[start: start + n]

    max_oi = max(
        (max(calls_dict.get(s, {}).get("oi", 0),
             puts_dict.get(s,  {}).get("oi", 0)) for s in subset),
        default=1
    ) or 1

    lines = ["```",
             f"{'Strike':>7} | {'CALL':^10} | PUT",
             "-" * 36]

    for s in subset:
        c_oi = calls_dict.get(s, {}).get("oi", 0)
        p_oi = puts_dict.get(s,  {}).get("oi", 0)

        c_bar = "█" * int(c_oi / max_oi * 8)
        p_bar = "█" * int(p_oi / max_oi * 8)

        real  = round(s * mult)
        tag   = ""
        if abs(s - current_price) == min(abs(x - current_price) for x in subset):
            tag = " ←NOW"
        elif max_pain and abs(s - max_pain) < 1:
            tag = " ←PAIN"

        lines.append(f"{real:>7} | {c_bar:<10} | {p_bar}{tag}")

    lines.append("```")
    return "\n".join(lines)

# ── 11. ส่ง Telegram ─────────────────────────────────
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(msg)
        return False

    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id"   : CHAT_ID,
        "text"      : msg,
        "parse_mode": "Markdown"
    }, timeout=15)

    ok = resp.status_code == 200
    print("✅ Telegram sent!" if ok else f"❌ {resp.text}")
    return ok

# ── MAIN ─────────────────────────────────────────────
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"🚀 Gold Tracker v3 — {now}")

    # ── ราคา GLD ─────────────────────────────────────
    print("📡 Fetching GLD price...")
    gld_price = get_price("GLD")

    if not gld_price:
        send_telegram("❌ ดึงราคา GLD ไม่ได้ — Yahoo Finance อาจ timeout")
        return

    gold_price = round(gld_price * GLD_MULT, 1)
    print(f"💰 GLD: {gld_price:.2f} → Gold ~{gold_price}")

    # ── หา Expiry ─────────────────────────────────────
    print("📅 Fetching expiry list...")
    expiry_list = get_expiry_list("GLD")

    if not expiry_list:
        send_telegram("❌ ดึง Expiry list ไม่ได้")
        return

    best_ts, days_left = pick_best_expiry(expiry_list)
    exp_str = datetime.fromtimestamp(best_ts).strftime("%Y-%m-%d")
    print(f"📅 Expiry: {exp_str} ({days_left}d)")

    # ── Options Chain ──────────────────────────────────
    print("📊 Fetching options chain...")
    calls_raw, puts_raw = get_options_chain("GLD", best_ts)

    if not calls_raw or not puts_raw:
        send_telegram("❌ ดึง Options Chain ไม่ได้")
        return

    calls = parse_chain(calls_raw)
    puts  = parse_chain(puts_raw)
    print(f"📊 Calls: {len(calls)}, Puts: {len(puts)}")

    # ── คำนวณ ──────────────────────────────────────────
    max_pain_gld             = calc_max_pain(calls, puts)
    call_wall_gld, put_wall_gld = find_walls(calls, puts, gld_price)
    iv_atm                   = get_atm_iv(calls, puts, gld_price)

    max_pain  = round(max_pain_gld  * GLD_MULT) if max_pain_gld else None
    call_wall = round(call_wall_gld * GLD_MULT)
    put_wall  = round(put_wall_gld  * GLD_MULT)
    iv_pct    = f"{iv_atm * 100:.1f}%" if iv_atm else "N/A"

    u1, l1, u2, l2 = calc_range(gold_price, iv_atm, days_left)

    pine_fair = round(max_pain  / 10) * 10 if max_pain else 0
    pine_call = round(call_wall / 10) * 10
    pine_put  = round(put_wall  / 10) * 10

    chart = make_chart(calls, puts, gld_price, max_pain_gld,
                       mult=GLD_MULT, n=10)

    pain_str = f"`{max_pain}`" if max_pain else "N/A"
    r1 = f"`{l1}` — `{u1}`" if u1 else "N/A"
    r2 = f"`{l2}` — `{u2}`" if u2 else "N/A"

    msg = f"""
🥇 *GOLD PRO — Options Update*
📅 `{now}` | Expiry `{exp_str}` ({days_left}d)

━━━━━━━━━━━━━━━━━━━
📍 *KEY LEVELS*
💰 Gold Price:  `{gold_price}`
⚖️  Max Pain:    {pain_str}
🟢 Call Wall:   `{call_wall}`
🔴 Put Wall:    `{put_wall}`
📈 ATM IV:      `{iv_pct}`

━━━━━━━━━━━━━━━━━━━
📐 *EXPECTED RANGE*
1️⃣  1SD (68%): {r1}
2️⃣  2SD (95%): {r2}

━━━━━━━━━━━━━━━━━━━
{chart}
━━━━━━━━━━━━━━━━━━━
📋 *COPY → Pine Script:*

"""

    send_telegram(msg)
    print("✅ Done!")

if __name__ == "__main__":
    main()
