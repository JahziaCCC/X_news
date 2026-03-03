import os
import json
import time
import html as htmlmod
import re
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

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

# بعد ما تتأكد أنه يرسل، رجعها 3
MAX_AGE_HOURS = 24

DEBUG_ALWAYS = True

# xcancel source
XCANCEL_BASE = "https://xcancel.com"
XCANCEL_UA = "mistique"  # مهم

# Regex to extract tweet IDs from HTML
STATUS_ID_RE = re.compile(r"/status/(\d{8,25})")


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
    s = htmlmod.unescape(s)
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


def is_recent_dt(dt: datetime | None) -> bool:
    if not dt:
        return True
    return dt >= (now_ksa() - timedelta(hours=MAX_AGE_HOURS))


def sanitize_xml(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\ufeff", "")
    text = text.lstrip(" \t\r\n")
    idx = text.find("<")
    if idx > 0:
        text = text[idx:]
    return text


def to_x_link(username: str, status_id: str) -> str:
    return f"https://x.com/{username}/status/{status_id}"


def fetch_xcancel_rss(username: str):
    """
    Try RSS first (may be broken/empty on GH actions).
    Return (entries, debug_note)
    """
    url = f"{XCANCEL_BASE}/{username}/rss"
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": XCANCEL_UA,
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                "Referer": XCANCEL_BASE + "/",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return [], f"RSS HTTP {r.status_code}"
        feed = feedparser.parse(sanitize_xml(r.text or ""))
        if getattr(feed, "bozo", False):
            err = getattr(feed, "bozo_exception", None)
            return [], f"RSS parse error: {str(err)[:120] if err else 'Unknown'}"
        return list(getattr(feed, "entries", []) or []), f"RSS ok entries={len(getattr(feed, 'entries', []) or [])}"
    except Exception as e:
        return [], f"RSS exception: {str(e)[:120]}"


def fetch_xcancel_html_status_ids(username: str, limit: int):
    """
    Scrape xcancel HTML page and extract /status/<id>
    Return list of unique IDs (newest first usually).
    """
    url = f"{XCANCEL_BASE}/{username}"
    r = requests.get(
        url,
        headers={
            "User-Agent": XCANCEL_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": XCANCEL_BASE + "/",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code != 200:
        return [], f"HTML HTTP {r.status_code}"

    body = r.text or ""
    ids = STATUS_ID_RE.findall(body)

    # dedupe keep order
    seen = set()
    uniq = []
    for sid in ids:
        if sid in seen:
            continue
        seen.add(sid)
        uniq.append(sid)
        if len(uniq) >= limit * 6:
            break

    return uniq[: limit * 3], f"HTML ok ids={len(uniq)}"


def build_msg(username: str, text: str, x_link: str, source: str, dt: datetime | None) -> str:
    time_line = dt.strftime("%Y-%m-%d | %H:%M") + " KSA" if dt else now_ksa_str()
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {time_line}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🧩 المصدر: {source}\n\n"
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
        last_seen = state.get(username)

        # 1) Try RSS (mostly for title/summary if it ever works)
        rss_entries, rss_note = fetch_xcancel_rss(username)
        debug.append(f"@{username}: {rss_note}")

        # 2) Always scrape HTML for status IDs (this is the reliable part)
        ids, html_note = fetch_xcancel_html_status_ids(username, MAX_ITEMS_PER_ACCOUNT)
        debug.append(f"  - {html_note}")

        if not ids:
            debug.append("  => RESULT: ❌ no status ids")
            continue

        # Use first ID as state barrier (newest)
        newest_id = ids[0]

        # Build list of new IDs until last_seen
        new_ids = []
        for sid in ids:
            if last_seen and sid == last_seen:
                break
            new_ids.append(sid)

        # Send oldest -> newest, limit
        new_ids = list(reversed(new_ids))[:MAX_ITEMS_PER_ACCOUNT]

        # Try to map titles from RSS if possible (best-effort)
        # We'll create a dict status_id->text if RSS contains status links.
        rss_text_by_id = {}
        for e in rss_entries[:30]:
            link = (getattr(e, "link", "") or "").strip()
            if link.startswith("/"):
                link = urljoin(XCANCEL_BASE, link)
            m = STATUS_ID_RE.search(link)
            if not m:
                # sometimes in id field
                eid = str(getattr(e, "id", "") or "")
                m = STATUS_ID_RE.search(eid)
            if not m:
                continue
            sid = m.group(1)
            title = clean_text(getattr(e, "title", "") or "")
            summary = clean_text(getattr(e, "summary", "") or "")
            text = title if len(title) >= 5 else summary
            if text:
                rss_text_by_id[sid] = text

        for sid in new_ids:
            x_link = to_x_link(username, sid)
            text = rss_text_by_id.get(sid, f"(تغريدة جديدة من @{username})")
            telegram_send(build_msg(username, text, x_link, "xcancel HTML (Author-only)", None))
            sent_total += 1
            time.sleep(1.0)

        # Update state always to newest id (prevents repeats)
        state[username] = newest_id
        debug.append(f"  => RESULT: sent={len(new_ids)} newest_id={newest_id}")

    save_state(state)

    debug.append("════════════════════")
    debug.append(f"sent_total={sent_total}")

    if DEBUG_ALWAYS:
        telegram_send("\n".join(debug))


if __name__ == "__main__":
    main()
