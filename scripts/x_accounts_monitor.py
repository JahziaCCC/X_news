import os
import json
import time
import html
import re
from pathlib import Path
from datetime import datetime, timedelta
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

PREFERRED_RSSHUB = (os.getenv("RSSHUB_BASE") or "").strip().rstrip("/")

RSSHUB_INSTANCES = [
    PREFERRED_RSSHUB,
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.kenshinji.me",
    "https://rsshub.liumingye.cn",
]
_seen = set()
RSSHUB_INSTANCES = [x for x in RSSHUB_INSTANCES if x and (x not in _seen and not _seen.add(x))]

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

STATE_PATH = Path("state_x_accounts.json")
KSA_TZ = ZoneInfo("Asia/Riyadh")

MAX_ITEMS_PER_ACCOUNT = 3
SEND_NO_NEW_SUMMARY = False
REQUEST_TIMEOUT = 35

MAX_AGE_HOURS = 3  # آخر 3 ساعات فقط
STRICT_AUTHOR_ONLY = True  # ✅ لا يرسل إلا تغريدة "صادرة من الحساب نفسه"

UA = "Mozilla/5.0 (compatible; X_news/4.0; +https://github.com/)"
X_STATUS_RE = re.compile(r"https?://(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)/status/(\d+)")
X_ANY_URL_RE = re.compile(r"https?://(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/status/\d+")


def now_ksa() -> datetime:
    return datetime.now(tz=KSA_TZ)


def now_ksa_str() -> str:
    dt = now_ksa()
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


def is_probably_xml(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:400].lower()
    if "<html" in head or "<!doctype html" in head:
        return False
    return ("<?xml" in head) or ("<rss" in head) or ("<feed" in head)


def extract_any_x_link(*chunks: str) -> str:
    for c in chunks:
        if not c:
            continue
        m = X_ANY_URL_RE.search(c)
        if m:
            return m.group(0)
    return ""


def is_link_from_account(x_link: str, username: str) -> bool:
    """
    ✅ يتحقق أن الرابط نفسه صادر من الحساب:
    https://x.com/<username>/status/<id>
    """
    if not x_link:
        return False
    m = X_STATUS_RE.search(x_link)
    if not m:
        return False
    author = m.group(1)
    return author.lower() == username.lower()


def resolve_final_url(url: str) -> str:
    """
    يحاول يحوّل رابط Google News إلى الوجهة النهائية (ويستخرج x.com/status إن وجد).
    """
    if not url or not url.startswith("http"):
        return ""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": UA, "Accept": "*/*"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        final = (r.url or "").strip()
        if X_ANY_URL_RE.search(final):
            return final

        txt = (r.text or "")[:8000]
        found = extract_any_x_link(txt)
        return found or ""
    except Exception:
        return ""


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
        return False  # بدون وقت = نرفضه عشان ما يجيب قديم
    return t >= (now_ksa() - timedelta(hours=MAX_AGE_HOURS))


def fetch_rsshub_feed(username: str):
    route = f"/twitter/user/{username}"
    last_err = None

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
                last_err = f"Non-XML response (blocked/HTML). Hint: {hint}"
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

    first_url = f"{RSSHUB_INSTANCES[0]}{route}" if RSSHUB_INSTANCES else route
    return None, first_url, last_err or "Unknown error"


def google_news_feed_url_for_user(username: str) -> str:
    # ✅ نخلي البحث أدق: الروابط تكون داخل مسار الحساب نفسه
    # هذا يقلل mentions كثير
    q = f'site:x.com/{username}/status when:1d'
    return "https://news.google.com/rss/search?q=" + quote_plus(q) + "&hl=ar&gl=SA&ceid=SA:ar"


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


def build_message(username: str, text: str, link: str, source_tag: str, entry_dt: datetime | None) -> str:
    time_line = entry_dt.strftime("%Y-%m-%d | %H:%M") + " KSA" if entry_dt else now_ksa_str()
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {time_line}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🧩 المصدر: {source_tag}\n\n"
        "📝 التغريدة:\n"
        f"{text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{link}\n"
        "════════════════════"
    )


def entries_new_since(entries, last_seen, limit):
    collected = []
    for e in entries[: limit * 12]:
        eid = getattr(e, "id", None) or getattr(e, "link", None)
        if not eid:
            continue
        if last_seen and eid == last_seen:
            break
        if is_recent(e):
            collected.append(e)
    return list(reversed(collected))[:limit]


def main():
    state = load_state()
    sent_any = False

    for username in ACCOUNTS:
        last_seen = state.get(username)

        # 1) RSSHub
        feed, used_url, err = fetch_rsshub_feed(username)
        source_tag = "RSSHub"

        # 2) Google fallback
        if err or feed is None:
            feed, used_url_g, err_g = fetch_google_news_feed(username)
            source_tag = "Google News RSS (Fallback)"
            if err_g or feed is None:
                # لا نرسل أخطاء كثيرة — نكمل بهدوء
                continue
            used_url = used_url_g

        entries = list(getattr(feed, "entries", []) or [])
        new_entries = entries_new_since(entries, last_seen, MAX_ITEMS_PER_ACCOUNT)

        # تحديث state دائمًا لمنع تكرار مزعج
        if entries:
            newest = entries[0]
            newest_id = getattr(newest, "id", None) or getattr(newest, "link", None)
            if newest_id:
                state[username] = newest_id

        for e in new_entries:
            raw_link = (getattr(e, "link", "") or "").strip()
            title = clean_text(getattr(e, "title", "") or "")
            summary_raw = getattr(e, "summary", "") or ""
            summary = clean_text(summary_raw)

            # استخرج رابط X
            x_link = extract_any_x_link(raw_link, title, summary_raw, summary)

            # لو Google News: حاول resolve
            if (not x_link) and ("news.google.com" in raw_link):
                x_link = resolve_final_url(raw_link)

            # ✅ فلترة صارمة: لازم الرابط نفسه يكون من الحساب
            if STRICT_AUTHOR_ONLY:
                if not is_link_from_account(x_link, username):
                    # هذا غالبًا mention/رد على الحساب — نرفضه
                    continue

            final_link = x_link if x_link else raw_link
            text = title if len(title) >= 10 else summary
            if not text:
                text = "(لم يظهر نص واضح من المصدر)"

            dt_entry = entry_time_ksa(e)
            telegram_send(build_message(username, text, final_link, source_tag, dt_entry))
            sent_any = True
            time.sleep(1.2)

    save_state(state)

    if (not sent_any) and SEND_NO_NEW_SUMMARY:
        telegram_send(
            "✅ رصد منصة X — حسابات محددة\n"
            f"🕒 {now_ksa_str()}\n"
            "════════════════════\n"
            f"لا توجد تغريدات جديدة (آخر {MAX_AGE_HOURS} ساعات) من الحسابات المحددة.\n"
            "════════════════════"
        )


if __name__ == "__main__":
    main()
