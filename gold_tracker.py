"""
Gold Options Tracker v6
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
รวมทุกอย่างจาก v4 + bot ของ gold_tracker.py

จาก v4 (คำนวณแม่นขึ้น):
  [FIX 1] get_price → interval=1m/range=1d (real-time)
  [FIX 2] ลอง GC=F options ก่อน fallback GLD
  [FIX 3] find_walls → filter ±15% รอบ current_price
  [FIX 4] make_chart → n=14, tag ←CALL / ←PUT
  [FIX 5] dynamic multiplier (gold/gld จริง ไม่ hardcode ×10)
  [FIX 6] calc_range ใช้ gold_price จริงเสมอ
  [FIX 7] ATM debug range dynamic จาก use_price จริง

เพิ่มใหม่ใน v6 (bot):
  [NEW 1] trigger_github_action() → กด workflow_dispatch ผ่าน API
  [NEW 2] handle_command() → /gold /help /status
  [NEW 3] bot_polling() → รับคำสั่ง Telegram ตลอดเวลา
  [NEW 4] bot รันเฉพาะนอก GitHub Actions (ไม่กระทบ cron)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import requests
import numpy as np
from datetime import datetime
import math
import os
import time

# ── Config ───────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
GH_TOKEN       = os.environ.get("GH_TOKEN", "")
GH_REPO        = os.environ.get("GH_REPO", "")   # username/repo

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
#  0. Init Session
# ══════════════════════════════════════════════════════
def init_session():
    global CRUMB
    print("🔑 Initializing Yahoo Finance session...")
    try:
        SESSION.get("https://fc.yahoo.com", timeout=10, allow_redirects=True)
    except Exception:
        pass
    try:
        SESSION.post(
            "https://consent.yahoo.com/v2/collectConsent",
            data={"agree": "agree", "consentid": ""},
            timeout=10
        )
    except Exception:
        pass
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
#  1. ดึงราคาปัจจุบัน
#  [FIX 1] interval=1m range=1d → real-time (v3 ใช้ 1d/5d = ราคาเมื่อวาน)
# ══════════════════════════════════════════════════════
def get_price(symbol="GLD"):
    url    = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
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
#  2. ดึง Options Expiry List
# ══════════════════════════════════════════════════════
def get_expiry_list(symbol="GLD"):
    url    = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
    params = {"crumb": CRUMB} if CRUMB else {}
    for attempt in range(3):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
            print(f"   [expiry:{symbol}] status: {resp.status_code}")
            data = resp.json()
            if "optionChain" not in data:
                print(f"⚠️ Unexpected keys: {list(data.keys())}")
                time.sleep(3)
                continue
            return data["optionChain"]["result"][0]["expirationDates"]
        except Exception as e:
            print(f"⚠️ get_expiry({symbol}) attempt {attempt+1} failed: {e}")
            time.sleep(3)
    return []


# ══════════════════════════════════════════════════════
#  3. ดึง Options Chain
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
#  4. หา Expiry ที่เหมาะสม (7-45 วัน)
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
    if not best_ts and expiry_timestamps:
        best_ts   = expiry_timestamps[0]
        best_days = (datetime.fromtimestamp(best_ts) - today).days
    return best_ts, best_days


# ══════════════════════════════════════════════════════
#  5. แปลง Options List → Dict by strike
# ══════════════════════════════════════════════════════
def parse_chain(options_list):
    result = {}
    for opt in options_list:
        strike = float(opt.get("strike", 0))
        oi     = int(opt.get("openInterest", 0) or 0)
        vol    = int(opt.get("volume", 0) or 0)
        iv     = float(opt.get("impliedVolatility", 0) or 0)
        oi     = oi if oi > 0 else vol
        result[strike] = {"oi": oi, "iv": iv}
    return result


# ══════════════════════════════════════════════════════
#  6. คำนวณ Max Pain
# ══════════════════════════════════════════════════════
def calc_max_pain(calls_dict, puts_dict, current_price=None, pct=0.10):
    all_strikes = sorted(set(calls_dict.keys()) | set(puts_dict.keys()))
    if not all_strikes:
        return None

    # ถ้ามี current_price → filter เฉพาะ ±10% รอบราคา
    if current_price:
        lo = current_price * (1 - pct)
        hi = current_price * (1 + pct)
        all_strikes = [s for s in all_strikes if lo <= s <= hi]

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
#  7. หา Call Wall / Put Wall
#  [FIX 3] filter ±15% รอบราคา → Wall ไม่หนีไปไกล ATM
# ══════════════════════════════════════════════════════
def find_walls(calls_dict, puts_dict, current_price, pct=0.08):
    lo = current_price * (1 - pct)
    hi = current_price * (1 + pct)

    calls_above = {s: v for s, v in calls_dict.items() if current_price <= s <= hi}
    call_wall   = max(calls_above, key=lambda s: calls_above[s]["oi"]) \
                  if calls_above else current_price * 1.05

    puts_below  = {s: v for s, v in puts_dict.items() if lo <= s <= current_price}
    put_wall    = max(puts_below, key=lambda s: puts_below[s]["oi"]) \
                  if puts_below else current_price * 0.95

    return float(call_wall), float(put_wall)


# ══════════════════════════════════════════════════════
#  8. หา ATM IV
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
#  9. คำนวณ Expected Range
#  [FIX 6] ใช้ gold_price จริงเสมอ
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
#  10. OI Bar Chart
#  [FIX 4] n=14, tag ←CALL / ←PUT / ←PAIN / ←NOW
# ══════════════════════════════════════════════════════
def make_chart(calls_dict, puts_dict, current_price, max_pain,
               call_wall_raw, put_wall_raw, mult=1.0, n=14):
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
             "-" * 38]
    for s in subset:
        c_oi  = calls_dict.get(s, {}).get("oi", 0)
        p_oi  = puts_dict.get(s,  {}).get("oi", 0)
        c_bar = "█" * int(c_oi / max_oi * 8)
        p_bar = "█" * int(p_oi / max_oi * 8)
        real  = round(s * mult)
        tag   = ""
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
#  11. ส่ง Telegram
# ══════════════════════════════════════════════════════
def send_telegram(msg: str, chat_id=None):
    if not TELEGRAM_TOKEN:
        print(msg)
        return False
    target = chat_id or CHAT_ID
    if not target:
        print(msg)
        return False
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id"   : target,
        "text"      : msg,
        "parse_mode": "Markdown"
    }, timeout=15)
    ok = resp.status_code == 200
    print("✅ Telegram sent!" if ok else f"❌ {resp.text}")
    return ok


# ══════════════════════════════════════════════════════
#  [NEW 1] Trigger GitHub Actions ผ่าน API
# ══════════════════════════════════════════════════════
def trigger_github_action():
    if not GH_TOKEN or not GH_REPO:
        return False
    url  = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/gold.yml/dispatches"
    resp = requests.post(url,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept"       : "application/vnd.github+json"
        },
        json={"ref": "main"},
        timeout=10
    )
    return resp.status_code == 204


# ══════════════════════════════════════════════════════
#  [NEW 2] จัดการคำสั่ง Telegram
# ══════════════════════════════════════════════════════
def handle_command(text: str, chat_id: str):
    text = text.strip().lower()

    if text in ["/gold", "/g"]:
        send_telegram("⏳ กำลังดึงข้อมูล รอ ~1 นาที...", chat_id)
        ok = trigger_github_action()
        if ok:
            send_telegram("✅ GitHub Actions ทำงานแล้ว\nผลจะส่งมาใน Telegram เดี๋ยวนี้", chat_id)
        else:
            send_telegram("❌ Trigger ไม่ได้ — ตรวจสอบ GH_TOKEN และ GH_REPO", chat_id)

    elif text in ["/help", "/start"]:
        send_telegram(
            "🥇 *Gold Options Bot v6*\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "/gold — ดึงข้อมูลทันที\n"
            "/g    — shortcut /gold\n"
            "/help — คำสั่งทั้งหมด\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "⏰ อัตโนมัติ: 08:00 / 13:00 / 20:00 น.",
            chat_id
        )

    elif text == "/status":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        send_telegram(
            f"✅ *Bot Online — v6*\n"
            f"🕐 เวลา: `{now}`\n"
            f"📡 Session: {'✅ Active' if CRUMB else '⚠️ No crumb'}\n"
            f"🔗 GH_REPO: `{GH_REPO or 'ไม่ได้ตั้งค่า'}`",
            chat_id
        )
    else:
        send_telegram("❓ ไม่รู้จักคำสั่ง — ลอง /help", chat_id)


# ══════════════════════════════════════════════════════
#  [NEW 3] Telegram Bot Long Polling
# ══════════════════════════════════════════════════════
def bot_polling():
    last_id = 0
    print("🤖 Bot polling started — พิมพ์ /gold ใน Telegram ได้เลย")
    while True:
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            resp = requests.get(url,
                params={"offset": last_id, "timeout": 30},
                timeout=35
            )
            for update in resp.json().get("result", []):
                last_id = update["update_id"] + 1
                msg     = update.get("message", {})
                text    = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/"):
                    print(f"📩 [{chat_id}] {text}")
                    handle_command(text, chat_id)
        except Exception as e:
            print(f"⚠️ polling error: {e}")
        time.sleep(1)


# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"🚀 Gold Tracker v6 — {now}")

    # ── Init Session ──────────────────────────────────
    init_session()

    # ── ราคาทองจริง ───────────────────────────────────
    print("📡 Fetching Gold price...")
    gold_price = get_price("GC=F")
    if not gold_price:
        gold_price = get_price("XAUUSD=X")
    if not gold_price:
        send_telegram("❌ ดึงราคาทองไม่ได้ — Yahoo Finance อาจ timeout")
        return
    gold_price = round(gold_price, 1)
    print(f"💰 Gold Futures (GC=F): {gold_price}")

    # ── ราคา GLD ETF จริง ─────────────────────────────
    gld_price = get_price("GLD")
    if not gld_price:
        gld_price = gold_price * 0.0941
        print(f"⚠️ GLD fallback (×0.0941): {gld_price:.2f}")
    else:
        print(f"💰 GLD actual: {gld_price:.2f}")

    # [FIX 5] Dynamic multiplier
    mult = gold_price / gld_price
    print(f"🔢 Dynamic mult: {mult:.4f}")

    # ── GVZ Volatility ────────────────────────────────
    gvz_iv = get_price("^GVZ")
    if gvz_iv:
        gvz_iv = gvz_iv / 100
        print(f"📈 GVZ IV: {gvz_iv*100:.1f}%")

    # ── [FIX 2] ลอง GC=F options ก่อน ────────────────
    print("\n📅 Trying GC=F options first...")
    expiry_list = get_expiry_list("GC=F")
    use_symbol  = "GC=F"
    use_price   = gold_price
    mult_final  = 1.0

    if not expiry_list:
        print("⚠️ GC=F options ไม่มี — fallback to GLD")
        expiry_list = get_expiry_list("GLD")
        use_symbol  = "GLD"
        use_price   = gld_price
        mult_final  = mult   # [FIX 5] dynamic

    if not expiry_list:
        send_telegram("❌ ดึง Expiry list ไม่ได้ (ทั้ง GC=F และ GLD)")
        return

    best_ts, days_left = pick_best_expiry(expiry_list)
    exp_str = datetime.fromtimestamp(best_ts).strftime("%Y-%m-%d")
    print(f"📅 Symbol: {use_symbol} | Expiry: {exp_str} ({days_left}d)")

    # ── Options Chain ─────────────────────────────────
    print(f"📊 Fetching options chain ({use_symbol})...")
    calls_raw, puts_raw = get_options_chain(use_symbol, best_ts)
    if not calls_raw or not puts_raw:
        send_telegram(f"❌ ดึง Options Chain ไม่ได้ ({use_symbol})")
        return

    calls = parse_chain(calls_raw)
    puts  = parse_chain(puts_raw)
    print(f"📊 Calls: {len(calls)}, Puts: {len(puts)}")

    # ── DEBUG ─────────────────────────────────────────
    atm_lo    = use_price * 0.97
    atm_hi    = use_price * 1.03
    atm_calls = {k: v for k, v in calls.items() if atm_lo <= k <= atm_hi}
    atm_puts  = {k: v for k, v in puts.items()  if atm_lo <= k <= atm_hi}
    print(f"🔍 ATM range: {atm_lo:.1f} – {atm_hi:.1f}")
    print(f"🔍 ATM calls: {sorted(atm_calls.items())}")
    print(f"🔍 ATM puts:  {sorted(atm_puts.items())}")

    # ── คำนวณ ─────────────────────────────────────────
    max_pain_raw                = calc_max_pain(calls, puts, current_price=use_price, pct=0.10)
    call_wall_raw, put_wall_raw = find_walls(calls, puts, use_price, pct=0.15)
    iv_atm                      = get_atm_iv(calls, puts, use_price)

    if gvz_iv:
        iv_atm = gvz_iv

    iv_pct    = f"{iv_atm * 100:.1f}%" if iv_atm else "N/A"
    max_pain  = round(max_pain_raw  * mult_final) if max_pain_raw else None
    call_wall = round(call_wall_raw * mult_final)
    put_wall  = round(put_wall_raw  * mult_final)

    print(f"🔍 max_pain={max_pain} | call_wall={call_wall} | put_wall={put_wall}")

    # [FIX 6] calc_range ใช้ gold_price จริงเสมอ
    u1, l1, u2, l2 = calc_range(gold_price, iv_atm, days_left)

    # [FIX 4] chart n=14 + tag wall
    chart = make_chart(
        calls, puts,
        current_price = use_price,
        max_pain      = max_pain_raw,
        call_wall_raw = call_wall_raw,
        put_wall_raw  = put_wall_raw,
        mult          = mult_final,
        n             = 14
    )

    pain_str  = f"`{max_pain}`" if max_pain else "N/A"
    r1        = f"`{l1}` — `{u1}`" if u1 else "N/A"
    r2        = f"`{l2}` — `{u2}`" if u2 else "N/A"
    pine_fair = round(max_pain  / 10) * 10 if max_pain else 0
    pine_call = round(call_wall / 10) * 10
    pine_put  = round(put_wall  / 10) * 10
    src_tag   = f"[{use_symbol}]"

    msg = f"""
🥇 *GOLD PRO v6 — Options Update* {src_tag}
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

    # ── ส่ง Report ────────────────────────────────────
    send_telegram(msg)
    print("✅ Done!")

    # ── [NEW 4] Bot Polling (นอก GitHub Actions เท่านั้น) ──
    if not os.environ.get("GITHUB_ACTIONS"):
        bot_polling()


if __name__ == "__main__":
    main()
