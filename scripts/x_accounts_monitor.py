import os
import re
import json
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import requests
import feedparser

ACCOUNTS = [
    "SaudiNews50",
    "alekhbariyatv",
    "alekhbariyatv7",
    "MOISaudiArabia",
    "modgovksa",
    "KSAMOFA",
]

STATE_PATH = Path("state_x_accounts.json")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

KSA_TZ = ZoneInfo("Asia/Riyadh")

GN_PARAMS = "hl=ar&gl=SA&ceid=SA:ar"

TEST_MODE = True

KEYWORDS = [
    "بيان",
    "عاجل",
    "تحذير",
    "تنويه",
    "إعلان",
    "alert",
    "warning",
]

def now_ksa():
    return datetime.now(tz=KSA_TZ)

def now_ksa_str():
    return now_ksa().strftime("%Y-%m-%d | %H:%M") + " KSA"

def telegram_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
    )

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))

def resolve_url(url):
    try:
        r = requests.get(url, allow_redirects=True, timeout=20)
        return r.url
    except:
        return ""

def build_rss(account):
    q = f"site:x.com/{account}"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&{GN_PARAMS}"

def keyword_match(text):
    t = text.lower()
    return any(k.lower() in t for k in KEYWORDS)

def build_msg(acc, title, link):
    return f"""
🚨 رصد منصة X

🕒 {now_ksa_str()}

📌 الحساب
@{acc}

📝 التغريدة
{title}

🔗 الرابط
{link}
"""

def main():

    state = load_state()

    total_sent = 0
    lines = []

    for acc in ACCOUNTS:

        scanned = 0
        sent = 0

        bad_url = 0

        rss = build_rss(acc)

        feed = feedparser.parse(rss)

        entries = feed.entries[:30]

        last = state.get(acc)

        for e in entries:

            scanned += 1

            link = e.link

            if last and link == last and not TEST_MODE:
                break

            final = resolve_url(link)

            if f"x.com/{acc}" not in final.lower():
                bad_url += 1
                continue

            title = e.title

            if not keyword_match(title) and not TEST_MODE:
                continue

            telegram_send(build_msg(acc, title, final))

            sent += 1
            total_sent += 1

            if TEST_MODE:
                break

        if entries:
            state[acc] = entries[0].link

        lines.append(f"@{acc}: scanned={scanned} sent={sent} bad_url={bad_url}")

    save_state(state)

    telegram_send(
        f"""🧪 ملخص تشغيل X_news
🕒 {now_ksa_str()}
════════════════════
Checked: {len(ACCOUNTS)}
Sent: {total_sent}
TEST_MODE: {"ON" if TEST_MODE else "OFF"}
════════════════════
""" + "\n".join(lines)
    )

if __name__ == "__main__":
    main()
