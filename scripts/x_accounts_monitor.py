import os
import json
import time
import html
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

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

# Preferred RSSHub instance (optional secret). If empty, we'll still try fallbacks.
PREFERRED_RSSHUB = (os.getenv("RSSHUB_BASE") or "").strip().rstrip("/")

# Try multiple RSSHub instances (some get blocked)
RSSHUB_INSTANCES = [
    PREFERRED_RSSHUB,
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.kenshinji.me",
    "https://rsshub.liumingye.cn",
]

# remove empties + duplicates, keep order
_seen = set()
RSSHUB_INSTANCES = [x for x in RSSHUB_INSTANCES if x and (x not in _seen and not _seen.add(x))]

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

STATE_PATH = Path("state_x_accounts.json")
KSA_TZ = ZoneInfo("Asia/Riyadh")

MAX_ITEMS_PER_ACCOUNT = 3
SEND_NO_NEW_SUMMARY = False  # True = يرسل "لا يوجد جديد"
REQUEST_TIMEOUT = 35

UA = "Mozilla/5.0 (compatible; X_news/2.0; +https://github.com/)"
X_URL_RE = re.compile(r"https?://(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/status/\d+")


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
    return ("<?xml" in head) or ("<rss" in head) or ("<feed" in head)


def extract_x_link(*chunks: str) -> str:
    for c in chunks:
        if not c:
            continue
        m = X_URL_RE.search(c)
        if m:
            return m.group(0)
    return ""


def fetch_rsshub_feed(username: str):
    """
    Tries multiple RSSHub instances.
    Returns: (feed, used_url, err_or_none)
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

            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue

            content = (r.text or "").strip()
            if not is_probably_xml(content):
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

    # If we got here, RSSHub failed
    first_url = f"{RSSHUB_INSTANCES[0]}{route}" if RSSHUB_INSTANCES else route
    return None, first_url, last_err or "Unknown error"


def google_news_feed_url_for_user(username: str) -> str:
    # Google News RSS search (Arabic/Saudi)
    # We search for tweets from that account; then we extract x.com/status from the RSS item content.
    q = f'site:x.com "{username}" (status OR /status/)'
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(q)
        + "&hl=ar&gl=SA&ceid=SA:ar"
    )


def fetch_google_news_feed(username: str):
    url = google_news_feed_url_for_user(username)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None, url, f"HTTP {r.status_code}"
        feed = feedparser.parse(r.text)
        if getattr(feed, "bozo", False):
            err = getattr(feed, "bozo_exception", None)
            return None, url, f"Parse error: {str(err)[:160] if err else 'Unknown'}"
        return feed, url, None
    except Exception as e:
        return None, url, str(e)[:180]


def build_message(username: str, text: str, link: str, source_tag: str) -> str:
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {now_ksa_str()}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🧩 المصدر: {source_tag}\n\n"
        "📝 التغريدة/المحتوى:\n"
        f"{text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{link}\n"
        "════════════════════"
    )


def build_error_message(username: str, where: str, err: str) -> str:
    return (
        "⚠️ رصد منصة X — تعذر قراءة التغريدات\n"
        f"🕒 {now_ksa_str()}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🔗 المصدر: {where}\n"
        f"🧾 السبب: {err}\n"
        "════════════════════"
    )


def entries_new_since(entries, last_seen, limit):
    new_entries = []
    for e in entries[: limit * 8]:
        eid = getattr(e, "id", None) or getattr(e, "link", None)
        if not eid:
            continue
        if last_seen and eid == last_seen:
            break
        new_entries.append(e)
    return list(reversed(new_entries))[:limit]


def main():
    state = load_state()
    sent_any = False

    for username in ACCOUNTS:
        last_seen = state.get(username)

        # 1) Try RSSHub first
        feed, used_url, err = fetch_rsshub_feed(username)

        source_tag = "RSSHub"
        if err or feed is None:
            # 2) Fallback to Google News RSS
            feed, used_url_g, err_g = fetch_google_news_feed(username)
            source_tag = "Google News RSS (Fallback)"
            if err_g or feed is None:
                # If both failed
                telegram_send(build_error_message(username, used_url, f"RSSHub failed: {err} | Google failed: {err_g}"))
                time.sleep(1.0)
                continue
            used_url = used_url_g  # for state context if needed

        entries = list(getattr(feed, "entries", []) or [])
        new_entries = entries_new_since(entries, last_seen, MAX_ITEMS_PER_ACCOUNT)

        for e in new_entries:
            link = (getattr(e, "link", "") or "").strip()
            title = clean_text(getattr(e, "title", "") or "")
            summary = clean_text(getattr(e, "summary", "") or "")

            # Try to extract the real x.com tweet link (especially for Google News RSS)
            x_link = extract_x_link(link, title, summary)

            # If we couldn't extract x.com link, we still send the available link
            final_link = x_link if x_link else link

            text = title if len(title) >= 10 else summary
            if not text:
                text = "(لم يظهر نص واضح من المصدر)"

            telegram_send(build_message(username, text, final_link, source_tag))
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
            "لا توجد تحديثات جديدة منذ آخر فحص.\n"
            "════════════════════"
        )


if __name__ == "__main__":
    main()
