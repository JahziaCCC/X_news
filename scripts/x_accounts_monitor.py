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

# RSSHub official/public instance by default
# You can override via GitHub Secret: RSSHUB_BASE
RSSHUB_BASE = os.getenv("RSSHUB_BASE", "https://rsshub.app").rstrip("/")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# State file to prevent duplicates
STATE_PATH = Path("state_x_accounts.json")

# Timezone (Saudi Arabia)
KSA_TZ = ZoneInfo("Asia/Riyadh")

# Limits
MAX_ITEMS_PER_ACCOUNT = 3          # max sends per account each run
SEND_NO_NEW_SUMMARY = False        # set True if you want "no new tweets" message


def now_ksa_str() -> str:
    dt = datetime.now(tz=KSA_TZ)
    # Example: الثلاثاء | 2026-03-03 | 23:48 KSA
    # We'll output day name in Arabic-ish style? GitHub runner locale is EN, so keep it numeric + KSA label.
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
    s = re.sub(r"<[^>]+>", "", s)  # strip html tags
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
    # RSSHub route: /twitter/user/:id
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


def main():
    state = load_state()
    sent_any = False

    for username in ACCOUNTS:
        last_seen = state.get(username)  # stores the newest entry id/link we recorded

        feed, feed_url = fetch_feed(username)

        # If RSS parsing failed
        if getattr(feed, "bozo", False):
            err = getattr(feed, "bozo_exception", None)
            msg = (
                "⚠️ رصد منصة X (RSS) — تعذر قراءة التغريدات\n"
                f"🕒 {now_ksa_str()}\n"
                "════════════════════\n"
                f"📌 الحساب: @{username}\n"
                f"🔗 المصدر: {feed_url}\n"
                f"🧾 السبب: {str(err)[:180] if err else 'Unknown'}\n"
                "════════════════════"
            )
            telegram_send(msg)
            time.sleep(1.0)
            continue

        entries = list(getattr(feed, "entries", []) or [])

        # Collect new entries until we hit last_seen
        new_entries = []
        for e in entries[: MAX_ITEMS_PER_ACCOUNT * 6]:
            eid = getattr(e, "id", None) or getattr(e, "link", None)
            if not eid:
                continue
            if last_seen and eid == last_seen:
                break
            new_entries.append(e)

        # Send from oldest to newest
        new_entries = list(reversed(new_entries))[:MAX_ITEMS_PER_ACCOUNT]

        for e in new_entries:
            link = getattr(e, "link", "") or ""
            title = clean_text(getattr(e, "title", "") or "")
            summary = clean_text(getattr(e, "summary", "") or "")

            # Prefer title if it looks like actual tweet text
            text = title if len(title) >= 10 else summary
            if not text:
                text = "(لم يظهر نص واضح من مصدر RSS)"

            telegram_send(build_message(username, text, link))
            sent_any = True
            time.sleep(1.2)

        # Update state to newest item in feed (most recent)
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
