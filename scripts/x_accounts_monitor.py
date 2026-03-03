import os
import json
import time
import html
import re
from pathlib import Path
from datetime import datetime, timedelta
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

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

STATE_PATH = Path("state_x_accounts.json")
KSA_TZ = ZoneInfo("Asia/Riyadh")

MAX_ITEMS_PER_ACCOUNT = 3
REQUEST_TIMEOUT = 35
MAX_AGE_HOURS = 24  # تقدر ترجعها 3 بعد ما نتأكد أنه صار يرسل

# RSS frontends (timeline RSS)
FEED_SOURCES = [
    # xcancel is commonly used as a Twitter frontend fallback  [oai_citation:2‡Reddit](https://www.reddit.com/r/rss/comments/1lzqm9w/what_is_the_status_of_x_twitter_to_rss/)
    {"name": "XCancel", "base": "https://xcancel.com", "ua": "mistique"},
    # nitter alternative (may or may not work depending on bot checks)
    {"name": "Nitter(poast)", "base": "https://nitter.poast.org", "ua": "Mozilla/5.0"},
]

UA_DEFAULT = "Mozilla/5.0 (compatible; X_news/6.0)"
X_STATUS_RE = re.compile(r"https?://(?:x\.com|twitter\.com|xcancel\.com|nitter\.[^/]+)/([A-Za-z0-9_]+)/status/(\d+)")


def now_ksa() -> datetime:
    return datetime.now(tz=KSA_TZ)


def now_ksa_str() -> str:
    return now_ksa().strftime("%Y-%m-%d | %H:%M") + " KSA"


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
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def telegram_send(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False}
    r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()


def entry_time_ksa(entry) -> datetime | None:
    dt_utc = None
    if getattr(entry, "published_parsed", None):
        dt_utc = datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo("UTC"))
    elif getattr(entry, "updated_parsed", None):
        dt_utc = datetime(*entry.updated_parsed[:6], tzinfo=ZoneInfo("UTC"))
    if not dt_utc:
        return None
    return dt_utc.astimezone(KSA_TZ)


def is_recent(entry) -> bool:
    t = entry_time_ksa(entry)
    if not t:
        # إذا RSS ما أعطى وقت، نسمح مؤقتًا (لأننا نقرأ تايملاين مباشر)
        return True
    return t >= (now_ksa() - timedelta(hours=MAX_AGE_HOURS))


def normalize_to_x(link: str) -> str:
    """
    Convert xcancel/nitter links to x.com for clean output.
    """
    if not link:
        return link
    link = link.replace("twitter.com", "x.com")
    # replace xcancel domain with x.com
    link = re.sub(r"https?://xcancel\.com/", "https://x.com/", link)
    # replace any nitter.<domain> with x.com
    link = re.sub(r"https?://nitter\.[^/]+/", "https://x.com/", link)
    return link


def fetch_user_rss(username: str):
    """
    Try multiple RSS frontends: xcancel then nitter.
    Returns: (feed, used_url, source_name, err_or_none)
    """
    last_err = None

    for src in FEED_SOURCES:
        base = src["base"].rstrip("/")
        ua = src.get("ua") or UA_DEFAULT
        url = f"{base}/{username}/rss"  # common pattern for these frontends

        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": ua,  # xcancel sometimes needs special UA  [oai_citation:3‡GitHub](https://github.com/zedeus/nitter/issues/1353?utm_source=chatgpt.com)
                    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                },
                timeout=REQUEST_TIMEOUT,
            )

            if r.status_code != 200:
                last_err = f"{src['name']} HTTP {r.status_code}"
                continue

            feed = feedparser.parse(r.text)
            if getattr(feed, "bozo", False):
                err = getattr(feed, "bozo_exception", None)
                last_err = f"{src['name']} parse error: {str(err)[:160] if err else 'Unknown'}"
                continue

            return feed, url, src["name"], None

        except Exception as e:
            last_err = f"{src['name']} exception: {str(e)[:180]}"
            continue

    return None, "", "", last_err or "Unknown error"


def build_message(username: str, text: str, link: str, src_name: str, entry_dt: datetime | None) -> str:
    time_line = entry_dt.strftime("%Y-%m-%d | %H:%M") + " KSA" if entry_dt else now_ksa_str()
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {time_line}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🧩 المصدر: {src_name}\n\n"
        "📝 التغريدة:\n"
        f"{text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{link}\n"
        "════════════════════"
    )


def main():
    state = load_state()

    for username in ACCOUNTS:
        last_seen = state.get(username)

        feed, used_url, src_name, err = fetch_user_rss(username)
        if err or feed is None:
            # رسالة خطأ قصيرة (بدون تفاصيل كثيرة)
            telegram_send(
                "⚠️ رصد منصة X — تعذر جلب RSS للحساب\n"
                f"🕒 {now_ksa_str()}\n"
                "════════════════════\n"
                f"📌 الحساب: @{username}\n"
                f"🧾 السبب: {err}\n"
                "════════════════════"
            )
            time.sleep(1.0)
            continue

        entries = list(getattr(feed, "entries", []) or [])
        if not entries:
            continue

        # Collect new entries until last_seen
        new_entries = []
        newest_id = None

        for e in entries[: MAX_ITEMS_PER_ACCOUNT * 12]:
            eid = getattr(e, "id", None) or getattr(e, "link", None)
            if not eid:
                continue

            if newest_id is None:
                newest_id = eid

            if last_seen and eid == last_seen:
                break

            if not is_recent(e):
                continue

            new_entries.append(e)

        # Send oldest -> newest
        new_entries = list(reversed(new_entries))[:MAX_ITEMS_PER_ACCOUNT]

        for e in new_entries:
            link = (getattr(e, "link", "") or "").strip()
            title = clean_text(getattr(e, "title", "") or "")
            summary = clean_text(getattr(e, "summary", "") or "")

            # Ensure it's actually from the same author in the URL path
            # (timeline RSS should already ensure this, but keep it strict)
            m = X_STATUS_RE.search(link)
            if m and m.group(1).lower() != username.lower():
                continue

            text = title if len(title) >= 5 else summary
            if not text:
                text = "(بدون نص واضح)"

            dt_entry = entry_time_ksa(e)
            telegram_send(build_message(username, text, normalize_to_x(link), src_name, dt_entry))
            time.sleep(1.2)

        # Update state to newest observed (even if no send, prevents repeats)
        if newest_id:
            state[username] = newest_id

    save_state(state)


if __name__ == "__main__":
    main()
