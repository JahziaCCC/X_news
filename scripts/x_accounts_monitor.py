import os
import json
import feedparser
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

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

def now_ksa():
    return datetime.now(KSA_TZ).strftime("%Y-%m-%d | %H:%M KSA")


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
    STATE_PATH.write_text(json.dumps(state))


def main():

    state = load_state()

    sent = 0

    for acc in ACCOUNTS:

        try:

            rss = f"https://rsshub.app/twitter/user/{acc}"

            feed = feedparser.parse(rss)

            if not feed.entries:
                continue

            last_id = state.get(acc)

            for entry in feed.entries[:3]:

                link = entry.link

                tweet_id = link.split("/")[-1]

                if tweet_id == last_id:
                    break

                text = entry.title

                msg = f"""🚨 رصد منصة X — حسابات محددة
🕒 {now_ksa()}
════════════════════
📌 الحساب: @{acc}

📝 التغريدة:
{text}

🔗 رابط التغريدة:
{link}
════════════════════
"""

                telegram_send(msg)

                sent += 1

            state[acc] = feed.entries[0].link.split("/")[-1]

        except Exception:
            continue

    save_state(state)

    telegram_send(
        f"""🧪 ملخص تشغيل X_news
🕒 {now_ksa()}
════════════════════
Checked: {len(ACCOUNTS)}
Sent: {sent}
════════════════════"""
    )


if __name__ == "__main__":
    main()
