"""
Gold Options Tracker v4
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
สิ่งที่แก้จาก v3:
  [FIX 1] get_price → ใช้ interval="1m" range="1d" แทน 5d (ราคา real-time)
  [FIX 2] ลอง GC=F options ก่อน (native gold price ไม่ต้องคูณ)
           fallback → GLD พร้อม dynamic multiplier (gold/gld จริง)
  [FIX 3] find_walls → กรองเฉพาะช่วง ±15% รอบ current_price
           ป้องกัน Wall ห่างไกล ATM มากเกินไป
  [FIX 4] make_chart → n=14 ให้เห็นช่วงกว้างขึ้น + แสดง Call/Put Wall tag
  [FIX 5] คำนวณ max_pain / call_wall / put_wall ด้วย dynamic mult
           ไม่ hardcode ×10 อีกต่อไป
  [FIX 6] calc_range ใช้ gold_price (ราคาทองจริง) เสมอ
  [FIX 7] ATM debug range คำนวณจาก gld_price จริง (dynamic ไม่ hardcode 420-445)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import requests
import numpy as np
from datetime import datetime
import math
import os
import time

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")

HEADERS = {
    "User-Agent"      : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept"          : "application/json, text/plain, */*",
    "Accept-Language" : "en-US,en;q=0.9",
    "Referer"         : "https://finance.yahoo.com",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
CRUMB   = None


# ══════════════════════════════════════════════════════
# 0. Init Session
# ══════════════════════════════════════════════════════
def init_session():
    global CRUMB

    print("🔑 Initializing Yahoo Finance session...")

    # เยี่ยม fc.yahoo.com เพื่อรับ cookie ก่อน
    try:
        SESSION.get("https://fc.yahoo.com", timeout=10, allow_redirects=True)
    except Exception:
        pass

    # ยืนยัน consent
    try:
        SESSION.post(
            "https://consent.yahoo.com/v2/collectConsent",
            data={"agree": "agree", "consentid": ""},
            timeout=10
        )
    except Exception:
        pass

    # ดึง crumb สำหรับ authenticate API
    for attempt in range(3):
        try:
            resp = SESSION.get(
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
                timeout=15
            )
            if resp.status_code == 200 and resp.text.strip():
                CRUMB = resp.text.strip()
                print(f"✅ Crumb obtained: {CRUMB[:8]}...")
                return True
        except Exception as e:
            print(f"⚠️ Crumb attempt {attempt+1} failed: {e}")
            time.sleep(2)

    print("❌ Could not obtain crumb")
    return False


# ══════════════════════════════════════════════════════
# 1. ดึงราคาปัจจุบัน
# [FIX 1] เปลี่ยนเป็น interval=1m, range=1d → ราคา real-time
#          v3 ใช้ 1d/5d อาจได้ราคาปิดเมื่อวาน
# ══════════════════════════════════════════════════════
def get_price(symbol="GLD"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    # [FIX 1] ใช้ 1m/1d แทน 1d/5d → ได้ regularMarketPrice ล่าสุด
    params = {"interval": "1m", "range": "1d"}
    if CRUMB:
        params["crumb"] = CRUMB

    for attempt in range(3):
        try:
            resp  = SESSION.get(url, params=params, timeout=15)
            data  = resp.json()
            price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
            return float(price)
        except Exception as e:
            print(f"⚠️ get_price({symbol}) attempt {attempt+1} failed: {e}")
            time.sleep(3)
    return None


# ══════════════════════════════════════════════════════
# 2. ดึง Options Expiry List
# ══════════════════════════════════════════════════════
def get_expiry_list(symbol="GLD"):
    url    = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
    params = {}
    if CRUMB:
        params["crumb"] = CRUMB

    for attempt in range(3):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
            print(f"   [expiry:{symbol}] status: {resp.status_code}")
            print(f"   [expiry:{symbol}] body: {resp.text[:200]}")

            data = resp.json()

            if "optionChain" not in data:
                print(f"⚠️ Unexpected keys: {list(data.keys())}")
                time.sleep(3)
                continue

            dates = data["optionChain"]["result"][0]["expirationDates"]
            return dates

        except Exception as e:
            print(f"⚠️ get_expiry({symbol}) attempt {attempt+1} failed: {e}")
            time.sleep(3)
    return []


# ══════════════════════════════════════════════════════
# 3. ดึง Options Chain
# ══════════════════════════════════════════════════════
def get_options_chain(symbol="GLD", expiry_ts=None):
    url    = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
    params = {}
    if expiry_ts:
        params["date"] = expiry_ts
    if CRUMB:
        params["crumb"] = CRUMB

    for attempt in range(3):
        try:
            resp   = SESSION.get(url, params=params, timeout=15)
            data   = resp.json()
            result = data["optionChain"]["result"][0]["options"][0]
            return result.get("calls", []), result.get("puts", [])
        except Exception as e:
            print(f"⚠️ get_chain({symbol}) attempt {attempt+1} failed: {e}")
            time.sleep(3)
    return [], []


# ══════════════════════════════════════════════════════
# 4. หา Expiry ที่เหมาะสม (7-45 วัน)
# ══════════════════════════════════════════════════════
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

    # fallback: ใช้ expiry แรกสุดถ้าไม่มีใน range
    if not best_ts and expiry_timestamps:
        best_ts   = expiry_timestamps[0]
        best_days = (datetime.fromtimestamp(best_ts) - today).days

    return best_ts, best_days


# ══════════════════════════════════════════════════════
# 5. แปลง Options List → Dict by strike
# ══════════════════════════════════════════════════════
def parse_chain(options_list):
    result = {}
    for opt in options_list:
        strike = float(opt.get("strike", 0))
        oi     = int(opt.get("openInterest", 0) or 0)
        vol    = int(opt.get("volume", 0) or 0)
        iv     = float(opt.get("impliedVolatility", 0) or 0)
        # fallback: ถ้า OI = 0 ใช้ volume แทน
        oi = oi if oi > 0 else vol
        result[strike] = {"oi": oi, "iv": iv}
    return result


# ══════════════════════════════════════════════════════
# 6. คำนวณ Max Pain
# ══════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════
# 7. หา Call Wall / Put Wall
# [FIX 3] เพิ่ม pct=0.15 → กรองเฉพาะ ±15% รอบราคาปัจจุบัน
#          v3 ไม่มี filter → Wall ที่หา OI สูงสุดอาจอยู่ห่าง ATM มาก
#          เช่น Put Wall ที่ GLD 430 ทั้งที่ราคา 446 → คูณ 10 ได้ 4300 ผิด
# ══════════════════════════════════════════════════════
def find_walls(calls_dict, puts_dict, current_price, pct=0.15):
    lo = current_price * (1 - pct)   # ขอบล่าง
    hi = current_price * (1 + pct)   # ขอบบน

    # Call Wall = strike ≥ current_price (แต่ไม่เกิน hi) ที่มี OI สูงสุด
    calls_above = {s: v for s, v in calls_dict.items() if current_price <= s <= hi}
    call_wall   = max(calls_above, key=lambda s: calls_above[s]["oi"]) \
                  if calls_above else current_price * 1.05

    # Put Wall = strike ≤ current_price (แต่ไม่ต่ำกว่า lo) ที่มี OI สูงสุด
    puts_below  = {s: v for s, v in puts_dict.items() if lo <= s <= current_price}
    put_wall    = max(puts_below, key=lambda s: puts_below[s]["oi"]) \
                  if puts_below else current_price * 0.95

    return float(call_wall), float(put_wall)


# ══════════════════════════════════════════════════════
# 8. หา ATM IV
# ══════════════════════════════════════════════════════
def get_atm_iv(calls_dict, puts_dict, current_price):
    all_strikes = sorted(set(calls_dict.keys()) | set(puts_dict.keys()))
    if not all_strikes:
        return None

    atm = min(all_strikes, key=lambda x: abs(x - current_price))
    ivs = []

    for d in [calls_dict, puts_dict]:
        if atm in d and d[atm]["iv"] > 0.01:
            ivs.append(d[atm]["iv"])

    return float(np.mean(ivs)) if ivs else None


# ══════════════════════════════════════════════════════
# 9. คำนวณ Expected Range (1SD / 2SD)
# ══════════════════════════════════════════════════════
def calc_range(price, iv, days):
    if not iv or not days or days <= 0:
        return None, None, None, None

    m1 = price * iv * math.sqrt(days / 365)
    m2 = m1 * 2

    return (
        round(price + m1, 1), round(price - m1, 1),
        round(price + m2, 1), round(price - m2, 1),
    )


# ══════════════════════════════════════════════════════
# 10. OI Bar Chart
# [FIX 4] n=14 (จาก 10) → เห็นช่วง strike กว้างขึ้น
#          เพิ่ม tag ←CALL / ←PUT สำหรับ Wall
# ══════════════════════════════════════════════════════
def make_chart(calls_dict, puts_dict, current_price, max_pain,
               call_wall_raw, put_wall_raw, mult=1.0, n=14):
    all_strikes = sorted(set(calls_dict.keys()) | set(puts_dict.keys()))
    if not all_strikes:
        return ""

    # center chart ที่ ATM
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
             "-" * 38]

    for s in subset:
        c_oi  = calls_dict.get(s, {}).get("oi", 0)
        p_oi  = puts_dict.get(s,  {}).get("oi", 0)
        c_bar = "█" * int(c_oi / max_oi * 8)
        p_bar = "█" * int(p_oi / max_oi * 8)
        real  = round(s * mult)

        # tag priority: NOW > PAIN > CALL_WALL > PUT_WALL
        tag = ""
        if abs(s - current_price) == min(abs(x - current_price) for x in subset):
            tag = " ←NOW"
        elif max_pain and abs(s - max_pain) < 0.6:
            tag = " ←PAIN"
        elif call_wall_raw and abs(s - call_wall_raw) < 0.6:
            tag = " ←CALL"
        elif put_wall_raw and abs(s - put_wall_raw) < 0.6:
            tag = " ←PUT"

        lines.append(f"{real:>7} | {c_bar:<10} | {p_bar}{tag}")

    lines.append("```")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# 11. ส่ง Telegram
# ══════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"🚀 Gold Tracker v4 — {now}")

    # ── Init Session ────────────────────────────────────
    init_session()

    # ── ราคาทองจริง (GC=F → XAUUSD fallback) ──────────
    print("📡 Fetching Gold price...")
    gold_price = get_price("GC=F")
    if not gold_price:
        gold_price = get_price("XAUUSD=X")
    if not gold_price:
        send_telegram("❌ ดึงราคาทองไม่ได้ — Yahoo Finance อาจ timeout")
        return
    gold_price = round(gold_price, 1)
    print(f"💰 Gold Futures (GC=F): {gold_price}")

    # ── ราคา GLD ETF จริง ──────────────────────────────
    gld_price = get_price("GLD")
    if not gld_price:
        # fallback คร่าวๆ (GLD ≈ Gold × 0.0941)
        gld_price = gold_price * 0.0941
        print(f"⚠️ GLD price fallback (×0.0941): {gld_price:.2f}")
    else:
        print(f"💰 GLD actual: {gld_price:.2f}")

    # [FIX 5] Dynamic multiplier จาก actual prices
    # v3 hardcode mult=10.0 แต่จริงๆ GLD ≈ Gold × 0.0941 ≠ Gold / 10
    mult = gold_price / gld_price
    print(f"🔢 Dynamic mult (gold/gld): {mult:.4f}")
    print(f"🔍 Expected GLD from mult: {gold_price / mult:.2f} (should ≈ {gld_price:.2f})")

    # ── GVZ Volatility Index ────────────────────────────
    gvz_iv = get_price("^GVZ")
    if gvz_iv:
        gvz_iv = gvz_iv / 100
        print(f"📈 GVZ IV: {gvz_iv*100:.1f}%")

    # ══════════════════════════════════════════════════
    # [FIX 2] ลอง GC=F options ก่อน (native gold strike ไม่ต้องคูณ)
    #          GC=F = COMEX Gold Futures options → strike = USD/oz จริงๆ
    #          ถ้าได้ → mult_final = 1.0
    #          ถ้าไม่ได้ → fallback GLD + mult จาก dynamic คำนวณข้างต้น
    # ══════════════════════════════════════════════════
    print("\n📅 Trying GC=F options first...")
    expiry_list  = get_expiry_list("GC=F")
    use_symbol   = "GC=F"
    use_price    = gold_price   # ราคาที่ใช้ค้นหา ATM ใน options
    mult_final   = 1.0          # GC=F strike = ราคาทองจริง ไม่คูณ

    if not expiry_list:
        print("⚠️ GC=F options ไม่มี — fallback to GLD")
        expiry_list = get_expiry_list("GLD")
        use_symbol  = "GLD"
        use_price   = gld_price     # ใช้ราคา GLD หา ATM ใน GLD options
        mult_final  = mult          # [FIX 5] ใช้ dynamic mult แทน 10.0

    if not expiry_list:
        send_telegram("❌ ดึง Expiry list ไม่ได้ (ทั้ง GC=F และ GLD)")
        return

    best_ts, days_left = pick_best_expiry(expiry_list)
    exp_str = datetime.fromtimestamp(best_ts).strftime("%Y-%m-%d")
    print(f"📅 Symbol: {use_symbol} | Expiry: {exp_str} ({days_left}d)")

    # ── Options Chain ────────────────────────────────────
    print(f"📊 Fetching options chain ({use_symbol})...")
    calls_raw, puts_raw = get_options_chain(use_symbol, best_ts)
    if not calls_raw or not puts_raw:
        send_telegram(f"❌ ดึง Options Chain ไม่ได้ ({use_symbol})")
        return

    calls = parse_chain(calls_raw)
    puts  = parse_chain(puts_raw)
    print(f"📊 Calls: {len(calls)}, Puts: {len(puts)}")

    # ── DEBUG: ตรวจสอบ data quality ──────────────────────
    print(f"\n🔍 === DEBUG INFO ===")
    print(f"   use_symbol : {use_symbol}")
    print(f"   use_price  : {use_price:.2f}  (ATM reference)")
    print(f"   mult_final : {mult_final:.4f}")

    sample_calls = list(calls.items())[:3]
    sample_puts  = list(puts.items())[:3]
    print(f"   Sample calls: {sample_calls}")
    print(f"   Sample puts:  {sample_puts}")

    # [FIX 7] ATM range dynamic จาก use_price จริง (ไม่ hardcode 420-445)
    atm_lo = use_price * 0.97
    atm_hi = use_price * 1.03
    atm_calls = {k: v for k, v in calls.items() if atm_lo <= k <= atm_hi}
    atm_puts  = {k: v for k, v in puts.items()  if atm_lo <= k <= atm_hi}
    print(f"   ATM range: {atm_lo:.1f} – {atm_hi:.1f}")
    print(f"   ATM calls: {sorted(atm_calls.items())}")
    print(f"   ATM puts:  {sorted(atm_puts.items())}")

    # Top OI
    puts_sorted  = sorted(puts.items(),  key=lambda x: x[1]["oi"], reverse=True)[:10]
    calls_sorted = sorted(calls.items(), key=lambda x: x[1]["oi"], reverse=True)[:10]
    print(f"   Top puts OI:  {puts_sorted}")
    print(f"   Top calls OI: {calls_sorted}")

    # puts_below ก่อนคำนวณ Wall
    puts_below = {s: v for s, v in puts.items() if s <= use_price}
    puts_below_top5 = sorted(puts_below.items(), key=lambda x: x[1]["oi"], reverse=True)[:5]
    print(f"   puts_below top5: {puts_below_top5}")

    # ── คำนวณ Level สำคัญ ────────────────────────────────
    max_pain_raw                = calc_max_pain(calls, puts)
    # [FIX 3] ส่ง use_price ที่ถูกต้อง + pct filter
    call_wall_raw, put_wall_raw = find_walls(calls, puts, use_price, pct=0.15)
    iv_atm                      = get_atm_iv(calls, puts, use_price)

    print(f"\n🔍 === RAW RESULTS ===")
    print(f"   max_pain_raw : {max_pain_raw}")
    print(f"   call_wall_raw: {call_wall_raw}")
    print(f"   put_wall_raw : {put_wall_raw}")
    print(f"   iv_atm       : {iv_atm}")

    # ใช้ GVZ แทน ATM IV ถ้ามี (แม่นกว่า)
    if gvz_iv:
        iv_atm = gvz_iv
        print(f"   iv_atm (GVZ) : {iv_atm}")

    iv_pct = f"{iv_atm * 100:.1f}%" if iv_atm else "N/A"

    # [FIX 5] แปลงกลับเป็นราคาทองด้วย mult_final (dynamic)
    # GC=F → mult=1.0 → ไม่เปลี่ยน | GLD → mult=gold/gld ≈ 10.6x
    max_pain  = round(max_pain_raw  * mult_final) if max_pain_raw  else None
    call_wall = round(call_wall_raw * mult_final)
    put_wall  = round(put_wall_raw  * mult_final)

    print(f"\n🔍 === CONVERTED (×{mult_final:.2f}) ===")
    print(f"   max_pain : {max_pain}")
    print(f"   call_wall: {call_wall}")
    print(f"   put_wall : {put_wall}")

    # [FIX 6] calc_range ใช้ gold_price เสมอ (ราคาทองจริง USD/oz)
    u1, l1, u2, l2 = calc_range(gold_price, iv_atm, days_left)

    # [FIX 4] make_chart: ส่ง call/put wall raw + n=14
    chart = make_chart(
        calls, puts,
        current_price  = use_price,      # ATM ถูกต้อง
        max_pain       = max_pain_raw,
        call_wall_raw  = call_wall_raw,
        put_wall_raw   = put_wall_raw,
        mult           = mult_final,      # dynamic mult
        n              = 14
    )

    # ── ฟอร์แมต message ──────────────────────────────────
    pain_str = f"`{max_pain}`" if max_pain else "N/A"
    r1 = f"`{l1}` — `{u1}`" if u1 else "N/A"
    r2 = f"`{l2}` — `{u2}`" if u2 else "N/A"

    # Pine Script: round เป็น 10 ที่ใกล้ที่สุด
    pine_fair = round(max_pain  / 10) * 10 if max_pain else 0
    pine_call = round(call_wall / 10) * 10
    pine_put  = round(put_wall  / 10) * 10

    # แสดง source symbol ใน message
    src_tag = f"[{use_symbol}]"

    msg = f"""
🥇 *GOLD PRO v4 — Options Update* {src_tag}
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
```
fair_value = {pine_fair}
call_wall  = {pine_call}
put_wall   = {pine_put}
```
"""

    send_telegram(msg)
    print("✅ Done!")


if __name__ == "__main__":
    main()
