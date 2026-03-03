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
# ACCOUNTS
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
MAX_AGE_HOURS = 24  # خلها 24 الآن للتأكد

# ✅ Debug دائم: يرسل تقرير واحد كل تشغيل
DEBUG_ALWAYS = True

UA_FALLBACK = "Mozilla/5.0 (compatible; X_news/DEBUG)"
X_STATUS_RE = re.compile(r"https?://(?:x\.com|twitter\.com|xcancel\.com|twitt\.re|nitter\.[^/]+)/([A-Za-z0-9_]+)/status/(\d+)")

# =========================
# RSS FRONTENDS (ROTATION)
# =========================
FEED_SOURCES = [
    {"name": "xcancel.com (mistique UA)", "base": "https://xcancel.com", "ua": "mistique", "path": "/{user}/rss"},
    {"name": "xcancel.com (browser UA)", "base": "https://xcancel.com", "ua": "Mozilla/5.0", "path": "/{user}/rss"},
    {"name": "twitt.re", "base": "https://twitt.re", "ua": "Mozilla/5.0", "path": "/{user}/rss"},
    {"name": "nitter.privacydev.net", "base": "https://nitter.privacydev.net", "ua": "Mozilla/5.0", "path": "/{user}/rss"},
    {"name": "nitter.poast.org", "base": "https://nitter.poast.org", "ua": "Mozilla/5.0", "path": "/{user}/rss"},
]

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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True}
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
        return True
    return t >= (now_ksa() - timedelta(hours=MAX_AGE_HOURS))

def normalize_to_x(link: str) -> str:
    if not link:
        return link
    link = link.replace("twitter.com", "x.com")
    link = re.sub(r"https?://xcancel\.com/", "https://x.com/", link)
    link = re.sub(r"https?://twitt\.re/", "https://x.com/", link)
    link = re.sub(r"https?://nitter\.[^/]+/", "https://x.com/", link)
    return link

def looks_like_rss(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:300].lower()
    if "<html" in head or "<!doctype html" in head:
        return False
    return ("<rss" in head) or ("<feed" in head) or ("<?xml" in head)

def fetch_user_rss(username: str, debug_lines: list[str]):
    """
    Returns (feed, used_source_name, used_url) or (None, "", "")
    """
    for src in FEED_SOURCES:
        base = src["base"].rstrip("/")
        ua = (src.get("ua") or UA_FALLBACK).strip()
        path = (src.get("path") or "/{user}/rss").format(user=username)
        url = f"{base}{path}"

        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": ua,
                    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                    "Accept-Language": "ar,en;q=0.8",
                    "Referer": base + "/",
                },
                timeout=REQUEST_TIMEOUT,
            )

            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            body = r.text or ""
            rss_flag = "RSS" if looks_like_rss(body) else "HTML/OTHER"
            debug_lines.append(f"  - {src['name']}: HTTP {r.status_code} | {ctype or 'no-ctype'} | {rss_flag} | len={len(body)}")

            if r.status_code != 200:
                continue
            if not looks_like_rss(body):
                continue
            if len(body) < 300:
                continue

            feed = feedparser.parse(body)
            if getattr(feed, "bozo", False):
                err = getattr(feed, "bozo_exception", None)
                debug_lines.append(f"    ↳ parse bozo: {str(err)[:120] if err else 'Unknown'}")
                continue

            return feed, src["name"], url

        except Exception as e:
            debug_lines.append(f"  - {src['name']}: EXC {str(e)[:120]}")
            continue

    return None, "", ""

def build_tweet_msg(username: str, text: str, link: str, src_name: str, entry_dt: datetime | None) -> str:
    time_line = entry_dt.strftime("%Y-%m-%d | %H:%M") + " KSA" if entry_dt else now_ksa_str()
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {time_line}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🧩 المصدر: {src_name}\n\n"
        "📝 التغريدة:\n"
        f"{text}\n\n"
        "🔗 الرابط:\n"
        f"{link}\n"
        "════════════════════"
    )

def main():
    state = load_state()
    sent_total = 0

    debug = [f"🧪 Debug X_news — {now_ksa_str()}", "════════════════════"]

    for username in ACCOUNTS:
        debug.append(f"@{username}:")
        feed, src_name, used_url = fetch_user_rss(username, debug)

        if not feed:
            debug.append("  => RESULT: ❌ no working RSS source")
            continue

        entries = list(getattr(feed, "entries", []) or [])
        debug.append(f"  => RESULT: ✅ {src_name} | entries={len(entries)}")
        last_seen = state.get(username)

        newest_id = None
        to_send = []
        scanned = 0

        for e in entries[: 60]:
            scanned += 1
            eid = getattr(e, "id", None) or getattr(e, "link", None)
            if not eid:
                continue
            if newest_id is None:
                newest_id = eid
            if last_seen and eid == last_seen:
                break

            link = (getattr(e, "link", "") or "").strip()
            m = X_STATUS_RE.search(link)
            # لازم الرابط يكون من نفس الحساب (منع الردود/المنشن)
            if not m or m.group(1).lower() != username.lower():
                continue

            if not is_recent(e):
                continue

            title = clean_text(getattr(e, "title", "") or "")
            summary = clean_text(getattr(e, "summary", "") or "")
            text = title if len(title) >= 5 else summary
            if not text:
                text = "(بدون نص واضح)"

            to_send.append((e, text, normalize_to_x(link)))

            if len(to_send) >= MAX_ITEMS_PER_ACCOUNT:
                break

        debug.append(f"  scanned={scanned} candidates={len(to_send)}")

        # إرسال من الأقدم للأحدث
        for e, text, link in reversed(to_send):
            telegram_send(build_tweet_msg(username, text, link, src_name, entry_time_ksa(e)))
            sent_total += 1
            time.sleep(1.0)

        if newest_id:
            state[username] = newest_id

    save_state(state)

    debug.append("════════════════════")
    debug.append(f"sent_total={sent_total}")

    if DEBUG_ALWAYS:
        telegram_send("\n".join(debug))

if __name__ == "__main__":
    main()
