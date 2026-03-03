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

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

STATE_PATH = Path("state_x_accounts.json")
KSA_TZ = ZoneInfo("Asia/Riyadh")

MAX_ITEMS_PER_ACCOUNT = 3
REQUEST_TIMEOUT = 35

# ✅ مؤقتًا: خله 24 ساعة للتأكد أنه يرسل
MAX_AGE_HOURS = 24

# ✅ فقط تغريدات الحساب نفسه
STRICT_AUTHOR_ONLY = True

# ✅ رسالة تشخيص مرة واحدة بكل تشغيل
DEBUG_SUMMARY = True

UA = "Mozilla/5.0 (compatible; X_news/5.0)"
X_STATUS_RE = re.compile(r"https?://(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)/status/(\d+)")
X_ANY_URL_RE = re.compile(r"https?://(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/status/\d+")


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


def extract_any_x_link(*chunks: str) -> str:
    for c in chunks:
        if not c:
            continue
        m = X_ANY_URL_RE.search(c)
        if m:
            return m.group(0)
    return ""


def is_link_from_account(x_link: str, username: str) -> bool:
    if not x_link:
        return False
    m = X_STATUS_RE.search(x_link)
    if not m:
        return False
    return m.group(1).lower() == username.lower()


def resolve_final_url(url: str) -> str:
    if not url or not url.startswith("http"):
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "*/*"}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        final = (r.url or "").strip()
        if X_ANY_URL_RE.search(final):
            return final
        txt = (r.text or "")[:10000]
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
        # ✅ مهم: لا نرفضه تمامًا، نخليه "مسموح" إذا ما عنده وقت (عشان ما ينقطع كل شيء)
        # لكن نوسم هذا في الديبق.
        return True
    return t >= (now_ksa() - timedelta(hours=MAX_AGE_HOURS))


def google_news_feed_url_for_user(username: str) -> str:
    # أدق بحث: روابط داخل مسار الحساب نفسه
    q = f"site:x.com/{username}/status when:7d"
    return "https://news.google.com/rss/search?q=" + quote_plus(q) + "&hl=ar&gl=SA&ceid=SA:ar"


def fetch_google_news_feed(username: str):
    url = google_news_feed_url_for_user(username)
    r = requests.get(url, headers={"User-Agent": UA}, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        return None, url, f"HTTP {r.status_code}"
    feed = feedparser.parse(r.text)
    if getattr(feed, "bozo", False):
        err = getattr(feed, "bozo_exception", None)
        return None, url, f"Parse error: {str(err)[:160] if err else 'Unknown'}"
    return feed, url, None


def build_message(username: str, text: str, link: str, entry_dt: datetime | None) -> str:
    time_line = entry_dt.strftime("%Y-%m-%d | %H:%M") + " KSA" if entry_dt else now_ksa_str()
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {time_line}\n"
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
    debug_lines = [f"🧪 Debug Summary — {now_ksa_str()}", "════════════════════"]

    for username in ACCOUNTS:
        last_seen = state.get(username)
        feed, src_url, err = fetch_google_news_feed(username)

        if err or feed is None:
            debug_lines.append(f"@{username}: ❌ Google RSS failed ({err})")
            continue

        entries = list(getattr(feed, "entries", []) or [])
        debug_lines.append(f"@{username}: found={len(entries)}")

        sent_for_account = 0
        newest_id_observed = None

        # نقرأ لعدد أكبر ثم نوقف عند last_seen
        scanned = 0
        for e in entries[: 50]:
            scanned += 1
            eid = getattr(e, "id", None) or getattr(e, "link", None)
            if not eid:
                continue

            if newest_id_observed is None:
                newest_id_observed = eid

            if last_seen and eid == last_seen:
                break

            # فلترة الوقت
            if not is_recent(e):
                continue

            raw_link = (getattr(e, "link", "") or "").strip()
            title = clean_text(getattr(e, "title", "") or "")
            summary_raw = getattr(e, "summary", "") or ""
            summary = clean_text(summary_raw)

            x_link = extract_any_x_link(raw_link, title, summary_raw, summary)
            if (not x_link) and ("news.google.com" in raw_link):
                x_link = resolve_final_url(raw_link)

            if STRICT_AUTHOR_ONLY and not is_link_from_account(x_link, username):
                continue

            final_link = x_link if x_link else raw_link
            text = title if len(title) >= 10 else summary
            if not text:
                text = "(لم يظهر نص واضح من المصدر)"

            dt_entry = entry_time_ksa(e)
            telegram_send(build_message(username, text, final_link, dt_entry))
            sent_for_account += 1
            time.sleep(1.2)

            if sent_for_account >= MAX_ITEMS_PER_ACCOUNT:
                break

        # ✅ تحديث state فقط لو رصدنا "newest" فعلاً (لتجنب تكرار)
        # والأهم: لا نحدّثه إذا كان ما فيه شيء أصلاً
        if newest_id_observed:
            state[username] = newest_id_observed

        debug_lines.append(f"@{username}: scanned={scanned} sent={sent_for_account}")

    save_state(state)

    if DEBUG_SUMMARY:
        debug_lines.append("════════════════════")
        telegram_send("\n".join(debug_lines))


if __name__ == "__main__":
    main()
