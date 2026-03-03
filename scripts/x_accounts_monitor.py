import os
import json
import time
import html
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import feedparser

# =========================
# CONFIG
# =========================
ACCOUNTS = [
    "SaudiNews50",
    "alekhbariyatv",
    "alekhbariyatv7",
    "MOISaudiArabia",
    "modgovksa",
    "KSAMOFA",
]

# IMPORTANT FIX:
# If RSSHUB_BASE env var is missing OR empty, fallback to default.
RSSHUB_BASE = (os.getenv("RSSHUB_BASE") or "https://rsshub.app").strip().rstrip("/")

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

STATE_PATH = Path("state_x_accounts.json")
KSA_TZ = ZoneInfo("Asia/Riyadh")

MAX_ITEMS_PER_ACCOUNT = 3
SEND_NO_NEW_SUMMARY = False  # اجعلها True إذا تبي رسالة "لا يوجد جديد"


def now_ksa_str() -> str:
    dt = datetime.now(tz=KSA_TZ)
    return dt.strftime("%Y-%m-%d | %H:%M") + " KSA"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", "", s)  # strip html
    s = re.sub(r"\s+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def telegram_send(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": False,
    }
    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()


def fetch_feed(username: str):
    feed_url = f"{RSSHUB_BASE}/twitter/user/{username}"
    return feedparser.parse(feed_url), feed_url


def build_message(username: str, text: str, link: str) -> str:
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {now_ksa_str()}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n\n"
        "📝 التغريدة:\n"
        f"{text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{link}\n"
        "════════════════════"
    )


def build_error_message(username: str, feed_url: str, err: str) -> str:
    return (
        "⚠️ رصد منصة X (RSS) — تعذر قراءة التغريدات\n"
        f"🕒 {now_ksa_str()}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🔗 المصدر: {feed_url}\n"
        f"🧾 السبب: {err[:180] if err else 'Unknown'}\n"
        "════════════════════"
    )


def main():
    state = load_state()
    sent_any = False

    for username in ACCOUNTS:
        last_seen = state.get(username)

        feed, feed_url = fetch_feed(username)

        if getattr(feed, "bozo", False):
            err = getattr(feed, "bozo_exception", None)
            telegram_send(build_error_message(username, feed_url, str(err)))
            time.sleep(1.0)
            continue

        entries = list(getattr(feed, "entries", []) or [])

        # Collect new entries until last_seen
        new_entries = []
        for e in entries[: MAX_ITEMS_PER_ACCOUNT * 6]:
            eid = getattr(e, "id", None) or getattr(e, "link", None)
            if not eid:
                continue
            if last_seen and eid == last_seen:
                break
            new_entries.append(e)

        # Send oldest -> newest
        new_entries = list(reversed(new_entries))[:MAX_ITEMS_PER_ACCOUNT]

        for e in new_entries:
            link = (getattr(e, "link", "") or "").strip()
            title = clean_text(getattr(e, "title", "") or "")
            summary = clean_text(getattr(e, "summary", "") or "")

            text = title if len(title) >= 10 else summary
            if not text:
                text = "(لم يظهر نص واضح من مصدر RSS)"

            telegram_send(build_message(username, text, link))
            sent_any = True
            time.sleep(1.2)

        # Update state with newest entry
        if entries:
            newest = entries[0]
            newest_id = getattr(newest, "id", None) or getattr(newest, "link", None)
            if newest_id:
                state[username] = newest_id

    save_state(state)

    if (not sent_any) and SEND_NO_NEW_SUMMARY:
        telegram_send(
            "✅ رصد منصة X — حسابات محددة\n"
            f"🕒 {now_ksa_str()}\n"
            "════════════════════\n"
            "لا توجد تغريدات جديدة منذ آخر فحص.\n"
            "════════════════════"
        )


if __name__ == "__main__":
    main()
