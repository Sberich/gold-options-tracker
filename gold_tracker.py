"""
Gold Options Tracker v5
✅ Auto Schedule (Cron)
✅ Telegram Bot Command (/gold, /help, /status)
"""

import requests
import numpy as np
from datetime import datetime
import math
import os
import time
import threading
import schedule

# ── Config ───────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")

# ⏰ ตั้งเวลาส่งอัตโนมัติ (เวลาไทย UTC+7)
# ปรับได้ตามต้องการ
AUTO_SCHEDULE = [
    "08:00",   # เช้า — ก่อนตลาด NY เปิด
    "13:00",   # บ่าย — กลางช่วง NY
    "20:00",   # เย็น — ก่อนตลาดปิด
]

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

# ── Telegram Polling State ────────────────────────────
last_update_id = 0
bot_running    = False


# ══════════════════════════════════════════════════════
#  YAHOO FINANCE FUNCTIONS (เหมือนเดิม)
# ══════════════════════════════════════════════════════

def init_session():
    global CRUMB
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
                return True
        except Exception:
            time.sleep(2)
    return False


def get_price(symbol="GLD"):
    url    = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": "5d"}
    if CRUMB:
        params["crumb"] = CRUMB
    for attempt in range(3):
        try:
            resp  = SESSION.get(url, params=params, timeout=15)
            data  = resp.json()
            price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
            return float(price)
        except Exception:
            time.sleep(3)
    return None


def get_expiry_list(symbol="GLD"):
    url    = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
    params = {"crumb": CRUMB} if CRUMB else {}
    for attempt in range(3):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
            data = resp.json()
            if "optionChain" not in data:
                time.sleep(3)
                continue
            return data["optionChain"]["result"][0]["expirationDates"]
        except Exception:
            time.sleep(3)
    return []


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
        except Exception:
            time.sleep(3)
    return [], []


def pick_best_expiry(expiry_timestamps):
    today    = datetime.now()
    best_ts  = None
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


def find_walls(calls_dict, puts_dict, current_price):
    calls_above = {s: v for s, v in calls_dict.items() if s >= current_price}
    call_wall   = max(calls_above, key=lambda s: calls_above[s]["oi"]) if calls_above else current_price + 20
    puts_below  = {s: v for s, v in puts_dict.items() if s <= current_price}
    put_wall    = max(puts_below, key=lambda s: puts_below[s]["oi"]) if puts_below else current_price - 20
    return float(call_wall), float(put_wall)


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


def calc_range(price, iv, days):
    if not iv or not days or days <= 0:
        return None, None, None, None
    m1 = price * iv * math.sqrt(days / 365)
    m2 = m1 * 2
    return (
        round(price + m1, 1), round(price - m1, 1),
        round(price + m2, 1), round(price - m2, 1),
    )


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
        c_oi  = calls_dict.get(s, {}).get("oi", 0)
        p_oi  = puts_dict.get(s,  {}).get("oi", 0)
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


# ══════════════════════════════════════════════════════
#  CORE — สร้าง Report
# ══════════════════════════════════════════════════════

def build_report(trigger="auto"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n🚀 Gold Tracker v5 — {now} [{trigger}]")

    init_session()

    gold_price = get_price("GC=F") or get_price("XAUUSD=X")
    if not gold_price:
        return "❌ ดึงราคาทองไม่ได้ — Yahoo Finance อาจ timeout"
    gold_price = round(gold_price, 1)

    gld_price = get_price("GLD") or gold_price / 10

    gvz_iv = get_price("^GVZ")
    if gvz_iv:
        gvz_iv = gvz_iv / 100

    expiry_list = get_expiry_list("GLD")
    if not expiry_list:
        return "❌ ดึง Expiry list ไม่ได้"

    best_ts, days_left = pick_best_expiry(expiry_list)
    exp_str = datetime.fromtimestamp(best_ts).strftime("%Y-%m-%d")

    calls_raw, puts_raw = get_options_chain("GLD", best_ts)
    if not calls_raw or not puts_raw:
        return "❌ ดึง Options Chain ไม่ได้"

    calls = parse_chain(calls_raw)
    puts  = parse_chain(puts_raw)

    max_pain_raw                = calc_max_pain(calls, puts)
    call_wall_raw, put_wall_raw = find_walls(calls, puts, gld_price)
    iv_atm                      = get_atm_iv(calls, puts, gld_price)

    if gvz_iv:
        iv_atm = gvz_iv

    iv_pct    = f"{iv_atm * 100:.1f}%" if iv_atm else "N/A"
    max_pain  = round(max_pain_raw  * 10) if max_pain_raw else None
    call_wall = round(call_wall_raw * 10)
    put_wall  = round(put_wall_raw  * 10)

    u1, l1, u2, l2 = calc_range(gold_price, iv_atm, days_left)
    chart     = make_chart(calls, puts, gld_price, max_pain_raw, mult=10.0, n=10)

    pain_str  = f"`{max_pain}`" if max_pain else "N/A"
    r1        = f"`{l1}` — `{u1}`" if u1 else "N/A"
    r2        = f"`{l2}` — `{u2}`" if u2 else "N/A"

    pine_fair = round(max_pain  / 10) * 10 if max_pain else 0
    pine_call = round(call_wall / 10) * 10
    pine_put  = round(put_wall  / 10) * 10

    trigger_icon = "⏰" if trigger == "auto" else "👤"

    msg = f"""
🥇 *GOLD PRO v5 — Options Update* {trigger_icon}
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
    return msg


# ══════════════════════════════════════════════════════
#  TELEGRAM — ส่งข้อความ
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
    print("✅ Sent!" if ok else f"❌ {resp.text[:100]}")
    return ok


# ══════════════════════════════════════════════════════
#  TELEGRAM BOT — Long Polling
# ══════════════════════════════════════════════════════

def get_updates(offset=0):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={
            "offset" : offset,
            "timeout": 30
        }, timeout=35)
        return resp.json().get("result", [])
    except Exception:
        return []


def handle_command(text: str, chat_id: str):
    text = text.strip().lower()

    if text in ["/gold", "/g"]:
        send_telegram("⏳ กำลังดึงข้อมูล รอสักครู่...", chat_id)
        msg = build_report(trigger="manual")
        send_telegram(msg, chat_id)

    elif text in ["/help", "/start"]:
        help_msg = """
🥇 *Gold Options Bot — คำสั่ง*
━━━━━━━━━━━━━━━━━━━
/gold  — ดึงข้อมูล Options ทันที
/g     — เหมือน /gold (shortcut)
/status — ดูสถานะ Bot + ตารางเวลา
/help  — แสดงคำสั่งทั้งหมด
━━━━━━━━━━━━━━━━━━━
⏰ *ส่งอัตโนมัติ:* """ + ", ".join(AUTO_SCHEDULE) + " น."
        send_telegram(help_msg, chat_id)

    elif text == "/status":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status_msg = f"""
✅ *Bot Online*
🕐 เวลาปัจจุบัน: `{now}`
⏰ ส่งอัตโนมัติ: {", ".join(AUTO_SCHEDULE)} น.
📡 Session: {"✅ Active" if CRUMB else "⚠️ No crumb"}
"""
        send_telegram(status_msg, chat_id)

    else:
        send_telegram(
            "❓ ไม่รู้จักคำสั่ง — ลอง /help เพื่อดูคำสั่งทั้งหมด",
            chat_id
        )


def bot_polling_loop():
    global last_update_id, bot_running
    bot_running = True
    print(f"🤖 Bot polling started — รับคำสั่ง /gold ได้แล้ว")

    while bot_running:
        updates = get_updates(offset=last_update_id)
        for update in updates:
            last_update_id = update["update_id"] + 1
            msg = update.get("message", {})
            text    = msg.get("text", "")
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if text.startswith("/"):
                print(f"📩 [{chat_id}] {text}")
                threading.Thread(
                    target=handle_command,
                    args=(text, chat_id),
                    daemon=True
                ).start()

        time.sleep(1)


# ══════════════════════════════════════════════════════
#  SCHEDULER — ส่งอัตโนมัติ
# ══════════════════════════════════════════════════════

def scheduled_job():
    print(f"\n⏰ Scheduled run — {datetime.now().strftime('%H:%M')}")
    msg = build_report(trigger="auto")
    send_telegram(msg)


def setup_schedule():
    for t in AUTO_SCHEDULE:
        schedule.every().day.at(t).do(scheduled_job)
        print(f"✅ Scheduled: {t} น.")


def schedule_loop():
    while True:
        schedule.run_pending()
        time.sleep(30)


# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════

def main():
    print("=" * 50)
    print("🥇 Gold Options Tracker v5")
    print("=" * 50)

    if not TELEGRAM_TOKEN:
        print("⚠️  TELEGRAM_TOKEN ไม่ได้ตั้งค่า — จะ print แทน")
    if not CHAT_ID:
        print("⚠️  CHAT_ID ไม่ได้ตั้งค่า")

    # ── 1. ส่งรายงานแรกทันทีตอนเริ่ม ─────────────────
    print("\n📡 กำลังส่งรายงานแรก...")
    msg = build_report(trigger="startup")
    send_telegram(msg)

    # ── 2. ตั้ง Schedule อัตโนมัติ ────────────────────
    print("\n⏰ ตั้งเวลาอัตโนมัติ...")
    setup_schedule()

    # ── 3. เริ่ม Scheduler Thread ──────────────────────
    sched_thread = threading.Thread(target=schedule_loop, daemon=True)
    sched_thread.start()
    print("✅ Scheduler เริ่มทำงาน")

    # ── 4. เริ่ม Telegram Bot Polling ─────────────────
    print("\n🤖 เริ่ม Telegram Bot...")
    bot_polling_loop()   # blocking — รันตลอดเวลา


if __name__ == "__main__":
    main()


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
