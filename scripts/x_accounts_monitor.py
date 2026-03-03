import os
import json
import time
import html
import re
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

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

# بعد ما تتأكد أنه صار يرسل، رجعها 3
MAX_AGE_HOURS = 24

# Debug summary per run
DEBUG_ALWAYS = True

# RSS frontends (we mostly rely on xcancel since it responds 200 for you)
FEED_SOURCES = [
    {"name": "xcancel.com (mistique UA)", "base": "https://xcancel.com", "ua": "mistique", "path": "/{user}/rss"},
    {"name": "xcancel.com (browser UA)", "base": "https://xcancel.com", "ua": "Mozilla/5.0", "path": "/{user}/rss"},
]

UA_FALLBACK = "Mozilla/5.0 (compatible; X_news/8.0)"


# Accept many status URL forms (with or without username)
STATUS_ID_RE = re.compile(r"/status/(\d+)")
IWEB_STATUS_ID_RE = re.compile(r"/i/web/status/(\d+)")


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
        # بعض RSS ما يعطي وقت، نسمح مؤقتًا
        return True
    return t >= (now_ksa() - timedelta(hours=MAX_AGE_HOURS))


def looks_like_rss(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:300].lower()
    if "<html" in head or "<!doctype html" in head:
        return False
    return ("<rss" in head) or ("<feed" in head) or ("<?xml" in head)


def sanitize_xml(text: str) -> str:
    # remove BOM + anything before first '<'
    if not text:
        return ""
    text = text.replace("\ufeff", "")
    text = text.lstrip(" \t\r\n")
    idx = text.find("<")
    if idx > 0:
        text = text[idx:]
    return text


def extract_status_id(link: str) -> str:
    if not link:
        return ""
    m = STATUS_ID_RE.search(link)
    if m:
        return m.group(1)
    m = IWEB_STATUS_ID_RE.search(link)
    if m:
        return m.group(1)
    return ""


def to_x_link(username: str, status_id: str) -> str:
    return f"https://x.com/{username}/status/{status_id}"


def fetch_user_rss(username: str, debug_lines: list[str]):
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
            body_raw = r.text or ""
            debug_lines.append(f"  - {src['name']}: HTTP {r.status_code} | {ctype or 'no-ctype'} | len={len(body_raw)}")

            if r.status_code != 200:
                continue
            if not looks_like_rss(body_raw):
                debug_lines.append("    ↳ not RSS (looks like HTML/other)")
                continue

            body = sanitize_xml(body_raw)
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


def build_tweet_msg(username: str, text: str, x_link: str, src_name: str, entry_dt: datetime | None) -> str:
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
        f"{x_link}\n"
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
        candidates = []
        scanned = 0

        for e in entries[: 80]:
            scanned += 1
            eid = getattr(e, "id", None) or getattr(e, "link", None)
            if not eid:
                continue

            if newest_id is None:
                newest_id = eid

            # stop at duplicate barrier
            if last_seen and eid == last_seen:
                break

            if not is_recent(e):
                continue

            link = (getattr(e, "link", "") or "").strip()
            # make absolute if it is relative
            if link.startswith("/"):
                link = urljoin("https://xcancel.com", link)

            status_id = extract_status_id(link)
            if not status_id:
                continue

            # ✅ Build clean official x.com link for THIS account timeline
            x_link = to_x_link(username, status_id)

            title = clean_text(getattr(e, "title", "") or "")
            summary = clean_text(getattr(e, "summary", "") or "")
            text = title if len(title) >= 5 else summary
            if not text:
                text = "(بدون نص واضح)"

            candidates.append((e, text, x_link))

            if len(candidates) >= MAX_ITEMS_PER_ACCOUNT:
                break

        debug.append(f"  scanned={scanned} candidates={len(candidates)}")

        # send oldest -> newest
        for e, text, x_link in reversed(candidates):
            telegram_send(build_tweet_msg(username, text, x_link, src_name, entry_time_ksa(e)))
            sent_total += 1
            time.sleep(1.0)

        # update state
        if newest_id:
            state[username] = newest_id

    save_state(state)

    debug.append("════════════════════")
    debug.append(f"sent_total={sent_total}")

    if DEBUG_ALWAYS:
        telegram_send("\n".join(debug))


if __name__ == "__main__":
    main()
