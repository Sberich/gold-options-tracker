"""
Gold Options Tracker v2
ใช้ GLD ETF Options (x10 = ราคาทอง approximate)
"""

import yfinance as yf
import numpy as np
import requests
from datetime import datetime
import math
import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")

# ── GLD Multiplier (GLD ≈ Gold/10) ──────────────────
GLD_MULT = 10.0

# ── 1. หา Expiry ที่เหมาะสม ──────────────────────────
def get_best_expiry(ticker_obj):
    today = datetime.now().date()
    best_exp   = None
    best_days  = None

    for exp in ticker_obj.options:
        exp_date  = datetime.strptime(exp, "%Y-%m-%d").date()
        days_left = (exp_date - today).days

        if 7 <= days_left <= 45:
            if best_exp is None or days_left < best_days:
                best_exp  = exp
                best_days = days_left

    # fallback: expiry แรกสุด
    if not best_exp and ticker_obj.options:
        best_exp  = ticker_obj.options[0]
        exp_date  = datetime.strptime(best_exp, "%Y-%m-%d").date()
        best_days = (exp_date - today).days

    return best_exp, best_days

# ── 2. คำนวณ Max Pain ────────────────────────────────
def calculate_max_pain(calls_df, puts_df):
    all_strikes = sorted(
        set(calls_df.index.tolist()) | set(puts_df.index.tolist())
    )

    if not all_strikes:
        return None

    pain = {}

    for test_price in all_strikes:
        total = 0.0

        for s in all_strikes:
            c_oi = float(calls_df.loc[s, "openInterest"]) if s in calls_df.index else 0
            p_oi = float(puts_df.loc[s, "openInterest"])  if s in puts_df.index else 0

            if s < test_price:
                total += (test_price - s) * c_oi
            if s > test_price:
                total += (s - test_price) * p_oi

        pain[test_price] = total

    return min(pain, key=pain.get)

# ── 3. หา Call Wall / Put Wall ───────────────────────
def find_walls(calls_df, puts_df, current_price):
    # Call Wall = strike เหนือราคา ที่มี OI สูงสุด
    calls_above = calls_df[calls_df.index >= current_price]
    if not calls_above.empty:
        call_wall = float(calls_above["openInterest"].idxmax())
    else:
        call_wall = current_price + 20

    # Put Wall = strike ใต้ราคา ที่มี OI สูงสุด
    puts_below = puts_df[puts_df.index <= current_price]
    if not puts_below.empty:
        put_wall = float(puts_below["openInterest"].idxmax())
    else:
        put_wall = current_price - 20

    return call_wall, put_wall

# ── 4. หา ATM IV ─────────────────────────────────────
def get_atm_iv(calls_df, puts_df, current_price):
    all_strikes = sorted(
        set(calls_df.index.tolist()) | set(puts_df.index.tolist())
    )
    if not all_strikes:
        return None

    atm = min(all_strikes, key=lambda x: abs(x - current_price))
    ivs = []

    for df in [calls_df, puts_df]:
        if atm in df.index:
            iv = df.loc[atm, "impliedVolatility"]
            if iv and float(iv) > 0.01:
                ivs.append(float(iv))

    return float(np.mean(ivs)) if ivs else None

# ── 5. คำนวณ Expected Range ──────────────────────────
def calc_range(price, iv, days):
    if not iv or not days:
        return None, None, None, None

    move_1sd = price * iv * math.sqrt(days / 365)
    move_2sd = move_1sd * 2

    return (
        round(price + move_1sd, 1),
        round(price - move_1sd, 1),
        round(price + move_2sd, 1),
        round(price - move_2sd, 1),
    )

# ── 6. OI Bar Chart ──────────────────────────────────
def make_chart(calls_df, puts_df, current_price, max_pain, mult=1.0, n=10):
    all_strikes = sorted(
        set(calls_df.index.tolist()) | set(puts_df.index.tolist())
    )
    if not all_strikes:
        return ""

    atm_idx = min(range(len(all_strikes)),
                  key=lambda i: abs(all_strikes[i] - current_price))
    start  = max(0, atm_idx - n // 2)
    end    = min(len(all_strikes), start + n)
    subset = all_strikes[start:end]

    max_oi = 1
    for s in subset:
        c = float(calls_df.loc[s, "openInterest"]) if s in calls_df.index else 0
        p = float(puts_df.loc[s, "openInterest"])  if s in puts_df.index else 0
        max_oi = max(max_oi, c, p)

    lines = ["```",
             f"{'Strike':>7} | {'CALL OI':<10} | PUT OI",
             "-" * 38]

    for s in subset:
        c_oi = float(calls_df.loc[s, "openInterest"]) if s in calls_df.index else 0
        p_oi = float(puts_df.loc[s, "openInterest"])  if s in puts_df.index else 0

        c_bar = "█" * int(c_oi / max_oi * 8)
        p_bar = "█" * int(p_oi / max_oi * 8)

        real_price = round(s * mult, 0)

        tag = ""
        if abs(s * mult - current_price * mult) < mult * 3:
            tag = " ←NOW"
        elif max_pain and abs(s * mult - max_pain * mult) < mult * 3:
            tag = " ←PAIN"

        lines.append(f"{int(real_price):>7} | {c_bar:<10} | {p_bar}{tag}")

    lines.append("```")
    return "\n".join(lines)

# ── 7. ส่ง Telegram ──────────────────────────────────
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("⚠️ ไม่มี TOKEN/CHAT_ID")
        print(msg)
        return

    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id"    : CHAT_ID,
        "text"       : msg,
        "parse_mode" : "Markdown"
    }

    resp = requests.post(url, json=payload)
    if resp.status_code == 200:
        print("✅ Telegram sent!")
    else:
        print(f"❌ Telegram error: {resp.text}")

# ── 8. Main ──────────────────────────────────────────
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"🚀 Gold Tracker started — {now}")

    # ── ดึงข้อมูล GLD ───────────────────────────────
    print("📡 Fetching GLD data...")
    gld = yf.Ticker("GLD")

    hist = gld.history(period="2d")
    if hist.empty:
        send_telegram("❌ ดึงราคา GLD ไม่ได้ กรุณาตรวจสอบ")
        return

    gld_price    = float(hist["Close"].iloc[-1])
    gold_price   = round(gld_price * GLD_MULT, 1)
    print(f"💰 GLD: {gld_price:.2f} → Gold ~{gold_price}")

    # ── หา Expiry ────────────────────────────────────
    expiry, days_left = get_best_expiry(gld)
    if not expiry:
        send_telegram("❌ ไม่พบ Options expiry")
        return
    print(f"📅 Expiry: {expiry} ({days_left}d)")

    # ── ดึง Options Chain ────────────────────────────
    try:
        chain = gld.option_chain(expiry)
        calls = chain.calls.set_index("strike")
        puts  = chain.puts.set_index("strike")
        print(f"📊 Calls: {len(calls)}, Puts: {len(puts)}")
    except Exception as e:
        send_telegram(f"❌ ดึง Options Chain ไม่ได้: {e}")
        return

    # ── คำนวณ ────────────────────────────────────────
    max_pain_gld = calculate_max_pain(calls, puts)
    call_wall_gld, put_wall_gld = find_walls(calls, puts, gld_price)
    iv_atm = get_atm_iv(calls, puts, gld_price)

    # แปลงกลับเป็นราคาทอง (x10)
    max_pain  = round(max_pain_gld  * GLD_MULT) if max_pain_gld  else None
    call_wall = round(call_wall_gld * GLD_MULT)
    put_wall  = round(put_wall_gld  * GLD_MULT)

    iv_pct    = f"{iv_atm * 100:.1f}%" if iv_atm else "N/A"

    # Expected Range (คำนวณจากราคาทองจริง)
    u1, l1, u2, l2 = calc_range(gold_price, iv_atm, days_left)

    # Pine Script values (round 10)
    pine_fair = round(max_pain / 10) * 10 if max_pain else 0
    pine_call = round(call_wall / 10) * 10
    pine_put  = round(put_wall  / 10) * 10

    # OI Chart
    chart = make_chart(calls, puts, gld_price, max_pain_gld,
                       mult=GLD_MULT, n=10)

    # ── สร้างข้อความ ──────────────────────────────────
    pain_str = f"`{max_pain}`" if max_pain else "N/A"
    r1 = f"`{l1}` — `{u1}`" if u1 else "N/A"
    r2 = f"`{l2}` — `{u2}`" if u2 else "N/A"

    msg = f"""
🥇 *GOLD PRO — Options Update*
📅 `{now}` | Expiry `{expiry}` ({days_left}d)

━━━━━━━━━━━━━━━━━━━
📍 *KEY LEVELS*
💰 Gold Price:   `{gold_price}`
⚖️  Max Pain:     {pain_str}
🟢 Call Wall:    `{call_wall}`
🔴 Put Wall:     `{put_wall}`
📈 ATM IV:       `{iv_pct}`

━━━━━━━━━━━━━━━━━━━
📐 *EXPECTED RANGE*
1️⃣  1SD (68%): {r1}
2️⃣  2SD (95%): {r2}

━━━━━━━━━━━━━━━━━━━
{chart}
━━━━━━━━━━━━━━━━━━━
📋 *COPY → Pine Script:*
