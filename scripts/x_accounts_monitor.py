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

# If you set a specific instance in secret RSSHUB_BASE, it will be tried FIRST.
PREFERRED_RSSHUB = (os.getenv("RSSHUB_BASE") or "").strip().rstrip("/")

# Fallback instances (we will try in order)
RSSHUB_INSTANCES = [
    PREFERRED_RSSHUB,
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.kenshinji.me",
    "https://rsshub.liumingye.cn",
]

# remove empties + duplicates (keep order)
_seen = set()
RSSHUB_INSTANCES = [x for x in RSSHUB_INSTANCES if x and (x not in _seen and not _seen.add(x))]

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

STATE_PATH = Path("state_x_accounts.json")
KSA_TZ = ZoneInfo("Asia/Riyadh")

MAX_ITEMS_PER_ACCOUNT = 3
SEND_NO_NEW_SUMMARY = False  # True = يرسل "لا يوجد جديد"
REQUEST_TIMEOUT = 35

UA = "Mozilla/5.0 (compatible; X_news/1.0; +https://github.com/)"

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
    r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()


def is_probably_xml(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:300].lower()
    if "<html" in head or "<!doctype html" in head:
        return False
    # RSS/Atom usually contains these markers
    return ("<?xml" in head) or ("<rss" in head) or ("<feed" in head)


def fetch_feed_with_fallback(username: str):
    """
    Tries multiple RSSHub instances.
    Returns: (feed, used_url, error_str_or_none)
    """
    last_err = None
    route = f"/twitter/user/{username}"

    for base in RSSHUB_INSTANCES:
        feed_url = f"{base}{route}"
        try:
            r = requests.get(
                feed_url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                },
                timeout=REQUEST_TIMEOUT,
            )
            # some instances return 200 but with HTML error page
            content = (r.text or "").strip()

            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue

            if not is_probably_xml(content):
                # keep a short hint
                hint = content.lstrip()[:120].replace("\n", " ")
                last_err = f"Non-XML response (likely blocked/HTML). Hint: {hint}"
                continue

            feed = feedparser.parse(content)
            if getattr(feed, "bozo", False):
                err = getattr(feed, "bozo_exception", None)
                last_err = f"Parse error: {str(err)[:160] if err else 'Unknown'}"
                continue

            return feed, feed_url, None

        except Exception as e:
            last_err = str(e)[:180]
            continue

    return None, f"{RSSHUB_INSTANCES[0]}{route}" if RSSHUB_INSTANCES else route, last_err or "Unknown error"


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


def build_error_message(username: str, tried_first_url: str, err: str) -> str:
    instances_text = "\n".join([f"• {x}" for x in RSSHUB_INSTANCES[:4]])  # لا نطوّل
    return (
        "⚠️ رصد منصة X (RSS) — تعذر قراءة التغريدات\n"
        f"🕒 {now_ksa_str()}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🔗 المصدر (المحاولة الأولى): {tried_first_url}\n"
        f"🧾 السبب: {err}\n"
        "════════════════════\n"
        "🛠️ Instances (Fallback):\n"
        f"{instances_text}\n"
        "════════════════════"
    )


def main():
    state = load_state()
    sent_any = False

    for username in ACCOUNTS:
        last_seen = state.get(username)

        feed, used_url, err = fetch_feed_with_fallback(username)

        if err or feed is None:
            telegram_send(build_error_message(username, used_url, err or "Unknown"))
            time.sleep(1.0)
            continue

        entries = list(getattr(feed, "entries", []) or [])

        new_entries = []
        for e in entries[: MAX_ITEMS_PER_ACCOUNT * 6]:
            eid = getattr(e, "id", None) or getattr(e, "link", None)
            if not eid:
                continue
            if last_seen and eid == last_seen:
                break
            new_entries.append(e)

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
