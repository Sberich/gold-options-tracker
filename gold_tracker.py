"""
CME Gold Options Tracker & Discord Notifier
ดึงข้อมูล Options ตรงจาก CME Group (QuikStrike) + ส่ง Discord Webhook
"""

import os
import re
import math
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ── Config ───────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

CME_URL = "https://cmegroup-tools.quikstrike.net/User/QuikStrikeView.aspx?pid=40&pf=6&viewitemid=IntegratedVOIHeatMap"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.cmegroup.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch_cme_options():
    print("📡 Fetching Gold Options from CME QuikStrike...")
    session = requests.Session()
    session.headers.update(HEADERS)
    
    resp = session.get(CME_URL, timeout=20)
    if resp.status_code != 200:
        print(f"❌ Failed to fetch CME: {resp.status_code}")
        return None
    
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    if len(tables) < 7:
        print("❌ Cannot find options table in page")
        return None
    
    # Table 6 คือ Heatmap Table
    t = tables[6]
    rows = t.find_all("tr")
    if len(rows) < 4:
        print("❌ Table rows incomplete")
        return None

    # ราคาทอง Futures
    price_match = re.search(r"(\d{4}\.\d+)", rows[0].text)
    gold_price = float(price_match.group(1)) if price_match else 0.0

    calls_by_strike = {}
    puts_by_strike = {}

    for row in rows[3:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        try:
            strike_text = cells[0].get_text(strip=True).replace(",", "")
            strike = float(strike_text)
        except ValueError:
            continue

        total_calls = 0
        total_puts = 0
        idx = 1
        while idx < len(cells):
            c_text = cells[idx].get_text(strip=True).replace(",", "")
            p_text = cells[idx+1].get_text(strip=True).replace(",", "") if idx+1 < len(cells) else "0"
            
            c_val = int(c_text) if c_text.isdigit() else 0
            p_val = int(p_text) if p_text.isdigit() else 0
            
            total_calls += c_val
            total_puts += p_val
            idx += 2

        calls_by_strike[strike] = total_calls
        puts_by_strike[strike] = total_puts

    return gold_price, calls_by_strike, puts_by_strike


def get_gvz_iv():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGVZ"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        price = resp.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return float(price) / 100
    except Exception:
        return 0.20  # default 20%


def calc_expected_range(price, iv, days=30):
    if not iv or price <= 0:
        return "N/A", "N/A"
    m1 = price * iv * math.sqrt(days / 365)
    m2 = m1 * 2
    r1 = f"`{round(price - m1, 1)}` — `{round(price + m1, 1)}`"
    r2 = f"`{round(price - m2, 1)}` — `{round(price + m2, 1)}`"
    return r1, r2


def calc_max_pain(calls, puts, gold_price):
    strikes = sorted(set(calls.keys()) | set(puts.keys()))
    strikes = [s for s in strikes if gold_price * 0.85 <= s <= gold_price * 1.15]
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


def find_walls(calls, puts, gold_price):
    calls_above = {s: oi for s, oi in calls.items() if s >= gold_price}
    call_wall = max(calls_above, key=calls_above.get) if calls_above else gold_price * 1.05

    puts_below = {s: oi for s, oi in puts.items() if s <= gold_price}
    put_wall = max(puts_below, key=puts_below.get) if puts_below else gold_price * 0.95

    return int(call_wall), int(put_wall)


def make_ascii_chart(calls, puts, gold_price, max_pain, call_wall, put_wall, n=12):
    strikes = sorted(set(calls.keys()) | set(puts.keys()))
    if not strikes:
        return ""
    
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - gold_price))
    start = max(0, atm_idx - n // 2)
    subset = strikes[start: start + n]
    
    max_oi = max((max(calls.get(s, 0), puts.get(s, 0)) for s in subset), default=1) or 1
    
    lines = ["```", f"{'STRIKE':>7} | {'CALL OI':^10} | {'PUT OI':^10}"]
    lines.append("-" * 34)
    
    for s in subset:
        c_oi = calls.get(s, 0)
        p_oi = puts.get(s, 0)
        c_bar = "█" * int(c_oi / max_oi * 8)
        p_bar = "█" * int(p_oi / max_oi * 8)
        
        tag = ""
        if abs(s - gold_price) == min(abs(x - gold_price) for x in subset):
            tag = " ←NOW"
        elif max_pain and s == max_pain:
            tag = " ←PAIN"
        elif s == call_wall:
            tag = " ←CALL"
        elif s == put_wall:
            tag = " ←PUT"
            
        lines.append(f"{int(s):>7} | {c_bar:<10} | {p_bar:<10}{tag}")
    lines.append("```")
    return "\n".join(lines)


def send_discord(gold_price, max_pain, call_wall, put_wall, iv_pct, r1, r2, chart_text):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK_URL is not set")
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    payload = {
        "username": "CME Gold Tracker",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2583/2583890.png",
        "embeds": [
            {
                "title": "🥇 CME COMEX Gold Options Analysis",
                "description": f"ข้อมูลจริงจาก **CME QuikStrike Heatmap**\nเวลาอัปเดต: `{now}`",
                "color": 16766720,  # Gold Color
                "fields": [
                    {
                        "name": "📍 KEY LEVELS (COMEX GC)",
                        "value": f"💰 **Futures Price:** `{gold_price}`\n"
                                 f"⚖️ **Max Pain:** `{max_pain}`\n"
                                 f"🟢 **Call Wall (แนวต้าน):** `{call_wall}`\n"
                                 f"🔴 **Put Wall (แนวรับ):** `{put_wall}`\n"
                                 f"📈 **GVZ Volatility:** `{iv_pct}`",
                        "inline": False
                    },
                    {
                        "name": "📐 EXPECTED RANGE (30 Days)",
                        "value": f"1️⃣ **1SD (68%):** {r1}\n"
                                 f"2️⃣ **2SD (95%):** {r2}",
                        "inline": False
                    },
                    {
                        "name": "📊 OPEN INTEREST MATRIX",
                        "value": chart_text,
                        "inline": False
                    },
                    {
                        "name": "📋 Pine Script (TradingView)",
                        "value": f"```pinescript\nfair_value = {max_pain}\ncall_wall  = {call_wall}\nput_wall   = {put_wall}\n```",
                        "inline": False
                    }
                ],
                "footer": {
                    "text": "Data Source: CME Group QuikStrike • Real COMEX Data"
                }
            }
        ]
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    if resp.status_code in [200, 204]:
        print("✅ Discord notification sent successfully!")
        return True
    else:
        print(f"❌ Failed to send Discord: {resp.status_code} {resp.text}")
        return False


def main():
    data = fetch_cme_options()
    if not data:
        print("❌ Could not get data from CME")
        return

    gold_price, calls, puts = data
    print(f"💰 Gold Futures Price: {gold_price}")

    gvz_iv = get_gvz_iv()
    iv_pct = f"{gvz_iv * 100:.1f}%"
    r1, r2 = calc_expected_range(gold_price, gvz_iv, days=30)

    max_pain = calc_max_pain(calls, puts, gold_price)
    call_wall, put_wall = find_walls(calls, puts, gold_price)
    chart = make_ascii_chart(calls, puts, gold_price, max_pain, call_wall, put_wall)

    send_discord(gold_price, max_pain, call_wall, put_wall, iv_pct, r1, r2, chart)
    print("🎯 Execution Completed!")

if __name__ == "__main__":
    main()
