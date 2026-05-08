"""
Gold Options Tracker
ดึงข้อมูล COMEX Gold Options → คำนวณ Max Pain + Expected Range
ส่งผล Telegram ทุกเช้า
"""

import yfinance as yf
import numpy as np
import requests
from datetime import datetime, timedelta
import math
import os

# ── CONFIG (อ่านจาก Environment Variables) ──────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")

# ── 1. หา Expiry ที่ใกล้ที่สุด ───────────────────────
def get_nearest_expiry(ticker_obj):
    """หา expiry date ที่ใกล้ที่สุดที่ยังไม่หมดอายุ"""
    today = datetime.now().date()
    
    for exp in ticker_obj.options:
        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        days_left = (exp_date - today).days
        
        # เลือก expiry ที่มีอีก 5-40 วัน (ช่วง active trading)
        if 5 <= days_left <= 40:
            return exp, days_left
    
    # fallback: expiry แรกที่ใกล้ที่สุด
    if ticker_obj.options:
        return ticker_obj.options[0], None
    
    return None, None

# ── 2. คำนวณ Max Pain ────────────────────────────────
def calculate_max_pain(calls_df, puts_df):
    """
    Max Pain = ราคา Strike ที่ทำให้ options ขาดทุนมากสุด
    = ราคาที่ผู้ซื้อ options ขาดทุน / ผู้ขายกำไร
    """
    # รวม strikes ทั้งหมด
    all_strikes = sorted(
        set(calls_df.index.tolist()) | set(puts_df.index.tolist())
    )
    
    if not all_strikes:
        return None, None, None
    
    pain_by_strike = {}
    
    for test_price in all_strikes:
        total_pain = 0
        
        # Call pain: calls ที่ in-the-money (strike < test_price)
        for strike in all_strikes:
            if strike < test_price:
                oi = calls_df.loc[strike, "openInterest"] if strike in calls_df.index else 0
                total_pain += (test_price - strike) * float(oi or 0)
        
        # Put pain: puts ที่ in-the-money (strike > test_price)
        for strike in all_strikes:
            if strike > test_price:
                oi = puts_df.loc[strike, "openInterest"] if strike in puts_df.index else 0
                total_pain += (strike - test_price) * float(oi or 0)
        
        pain_by_strike[test_price] = total_pain
    
    # Max Pain = strike ที่ total pain ต่ำสุด
    max_pain = min(pain_by_strike, key=pain_by_strike.get)
    
    return max_pain, pain_by_strike, all_strikes

# ── 3. หา Heavy OI Strikes ──────────────────────────
def find_heavy_strikes(calls_df, puts_df, max_pain, current_price):
    """หา Strike ที่มี Open Interest สูงสุด = Call/Put Wall"""
    
    # Call Wall: strikes เหนือ current price ที่มี OI สูงสุด
    calls_above = calls_df[calls_df.index >= current_price].copy()
    if not calls_above.empty:
        calls_above = calls_above.sort_values("openInterest", ascending=False)
        call_wall = float(calls_above.index[0])
    else:
        call_wall = max_pain + 100
    
    # Put Wall: strikes ใต้ current price ที่มี OI สูงสุด
    puts_below = puts_df[puts_df.index <= current_price].copy()
    if not puts_below.empty:
        puts_below = puts_below.sort_values("openInterest", ascending=False)
        put_wall = float(puts_below.index[0])
    else:
        put_wall = max_pain - 100
    
    return call_wall, put_wall

# ── 4. คำนวณ Expected Range (1SD) ───────────────────
def calc_expected_range(current_price, iv_atm, days_to_expiry):
    """
    Expected Range = Current Price × IV × √(DTE/365)
    1SD = ราคามีโอกาส 68% อยู่ใน range นี้
    2SD = 95%
    """
    if not iv_atm or not days_to_expiry:
        return None, None, None, None
    
    # คำนวณ range
    move_1sd = current_price * iv_atm * math.sqrt(days_to_expiry / 365)
    move_2sd = move_1sd * 2
    
    upper_1sd = round(current_price + move_1sd, 1)
    lower_1sd = round(current_price - move_1sd, 1)
    upper_2sd = round(current_price + move_2sd, 1)
    lower_2sd = round(current_price - move_2sd, 1)
    
    return upper_1sd, lower_1sd, upper_2sd, lower_2sd

# ── 5. หา ATM Implied Volatility ────────────────────
def get_atm_iv(calls_df, puts_df, current_price):
    """หา IV ของ options ที่ใกล้ ATM ที่สุด"""
    ivs = []
    
    # หา strike ใกล้ ATM
    all_strikes = sorted(
        set(calls_df.index.tolist()) | set(puts_df.index.tolist())
    )
    
    atm_strike = min(all_strikes, key=lambda x: abs(x - current_price))
    
    # ดึง IV รอบๆ ATM (±3 strikes)
    atm_idx = all_strikes.index(atm_strike)
    nearby = all_strikes[max(0, atm_idx-2) : atm_idx+3]
    
    for strike in nearby:
        if strike in calls_df.index:
            iv = calls_df.loc[strike, "impliedVolatility"]
            if iv and float(iv) > 0.01:
                ivs.append(float(iv))
        if strike in puts_df.index:
            iv = puts_df.loc[strike, "impliedVolatility"]
            if iv and float(iv) > 0.01:
                ivs.append(float(iv))
    
    return np.mean(ivs) if ivs else None

# ── 6. สร้าง OI Bar Chart (text) ────────────────────
def make_oi_chart(calls_df, puts_df, current_price, max_pain, n=10):
    """สร้าง mini chart แสดง OI distribution"""
    all_strikes = sorted(
        set(calls_df.index.tolist()) | set(puts_df.index.tolist())
    )
    
    # หา strikes ใกล้ current price
    atm_idx = min(range(len(all_strikes)), 
                  key=lambda i: abs(all_strikes[i] - current_price))
    
    start = max(0, atm_idx - n//2)
    end   = min(len(all_strikes), start + n)
    show_strikes = all_strikes[start:end]
    
    max_oi = 1
    for s in show_strikes:
        c_oi = float(calls_df.loc[s, "openInterest"]) if s in calls_df.index else 0
        p_oi = float(puts_df.loc[s, "openInterest"])  if s in puts_df.index else 0
        max_oi = max(max_oi, c_oi, p_oi)
    
    lines = ["```", "Strike  | CALL OI    | PUT OI"]
    lines.append("--------|------------|----------")
    
    for s in show_strikes:
        c_oi = float(calls_df.loc[s, "openInterest"]) if s in calls_df.index else 0
        p_oi = float(puts_df.loc[s, "openInterest"])  if s in puts_df.index else 0
        
        c_bar = "█" * int(c_oi / max_oi * 8) if max_oi > 0 else ""
        p_bar = "█" * int(p_oi / max_oi * 8) if max_oi > 0 else ""
        
        marker = " ←NOW" if abs(s - current_price) < 10 else ""
        marker = " ←PAIN" if abs(s - max_pain) < 10 else marker
        
        lines.append(f"{int(s):7d} | {c_bar:<10s} | {p_bar}{marker}")
    
    lines.append("```")
    return "\n".join(lines)

# ── 7. Main Function ─────────────────────────────────
def main():
    print(f"🚀 Starting Gold Options Tracker — {datetime.now()}")
    
    # ดึงข้อมูล GC (Gold Futures) จาก Yahoo Finance
    gc = yf.Ticker("GC=F")
    
    # ราคาปัจจุบัน
    hist = gc.history(period="1d")
    if hist.empty:
        print("❌ ไม่สามารถดึงราคาได้")
        return
    
    current_price = float(hist["Close"].iloc[-1])
    print(f"💰 GC Price: {current_price}")
    
    # หา Expiry
    expiry, days_left = get_nearest_expiry(gc)
    if not expiry:
        print("❌ ไม่พบ expiry date")
        return
    
    print(f"📅 Expiry: {expiry} ({days_left} days)")
    
    # ดึง Options Chain
    opt_chain = gc.option_chain(expiry)
    calls = opt_chain.calls.set_index("strike")
    puts  = opt_chain.puts.set_index("strike")
    
    print(f"📊 Calls: {len(calls)} strikes, Puts: {len(puts)} strikes")
    
    # คำนวณ Max Pain
    max_pain, pain_map, all_strikes = calculate_max_pain(calls, puts)
    if not max_pain:
        print("❌ คำนวณ Max Pain ไม่ได้")
        return
    
    # หา Heavy Walls
    call_wall, put_wall = find_heavy_strikes(calls, puts, max_pain, current_price)
    
    # หา ATM IV
    iv_atm = get_atm_iv(calls, puts, current_price)
    iv_pct = f"{iv_atm*100:.1f}%" if iv_atm else "N/A"
    
    # คำนวณ Expected Range
    upper_1sd, lower_1sd, upper_2sd, lower_2sd = calc_expected_range(
        current_price, iv_atm, days_left
    )
    
    # OI Chart
    oi_chart = make_oi_chart(calls, puts, current_price, max_pain)
    
    # Pine Script values (round to nearest 10 สำหรับ options zone)
    pine_fair = round(max_pain / 10) * 10
    pine_call = round(call_wall / 10) * 10
    pine_put  = round(put_wall  / 10) * 10
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # ── ส่ง Telegram ───────────────────────────────────
    msg = f"""
🥇 *GOLD PRO — Daily Options Update*
📅 `{now_str}` | Expiry: `{expiry}` ({days_left}d)

━━━━━━━━━━━━━━━━━━━
📍 *KEY LEVELS*
💰 Current Price:  `{current_price:.1f}`
⚖️  Max Pain:       `{max_pain:.0f}`
🟢 Call Wall:      `{call_wall:.0f}`
🔴 Put Wall:       `{put_wall:.0f}`
📈 ATM IV:         `{iv_pct}`

━━━━━━━━━━━━━━━━━━━
📐 *EXPECTED RANGE* (ถึง Expiry)
1️⃣  1SD (68%):  `{lower_1sd}` — `{upper_1sd}`
2️⃣  2SD (95%):  `{lower_2sd}` — `{upper_2sd}`

━━━━━━━━━━━━━━━━━━━
{oi_chart}
━━━━━━━━━━━━━━━━━━━
📋 *COPY → Pine Script Inputs:*
