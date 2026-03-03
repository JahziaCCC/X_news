import os
import json
import time
import re
import html as htmlmod
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests

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
MAX_AGE_HOURS = 3   # ✅ رجعناها 3 ساعات

XCANCEL_BASE = "https://xcancel.com"
XCANCEL_UA = "mistique"

STATUS_ID_RE = re.compile(r"/status/(\d{8,25})")
# محاولة استخراج نص التغريدة من HTML (مرنة)
TEXT_BLOCK_RE = re.compile(r'(?s)<div[^>]+class="tweet-content"[^>]*>(.*?)</div>')
# الوقت عادة داخل <span class="tweet-date"> ... </span> أو time datetime
TIME_DT_RE = re.compile(r'datetime="([^"]+)"')
TIME_FALLBACK_RE = re.compile(r'class="tweet-date"[^>]*>.*?(?:title="([^"]+)")?.*?</', re.S)

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

def telegram_send(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False}
    r = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

def clean_html_to_text(s: str) -> str:
    if not s:
        return ""
    s = htmlmod.unescape(s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def to_x_link(username: str, status_id: str) -> str:
    return f"https://x.com/{username}/status/{status_id}"

def fetch_html(url: str) -> str:
    r = requests.get(
        url,
        headers={
            "User-Agent": XCANCEL_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": XCANCEL_BASE + "/",
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.text or ""

def fetch_account_status_ids(username: str, limit: int):
    url = f"{XCANCEL_BASE}/{username}"
    body = fetch_html(url)
    ids = STATUS_ID_RE.findall(body)

    seen = set()
    uniq = []
    for sid in ids:
        if sid in seen:
            continue
        seen.add(sid)
        uniq.append(sid)
        if len(uniq) >= limit * 10:
            break
    return uniq

def parse_datetime_from_html(body: str) -> datetime | None:
    # Best: <time datetime="2026-03-03T22:10:00+00:00">
    m = TIME_DT_RE.search(body)
    if m:
        dt_raw = m.group(1).strip()
        try:
            # Python fromisoformat supports offsets like +00:00
            dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt.astimezone(KSA_TZ)
        except Exception:
            pass
    # fallback: sometimes title="Mar 3, 2026 · 10:10 PM UTC"
    m2 = TIME_FALLBACK_RE.search(body)
    if m2 and m2.group(1):
        # Not reliable to parse across locales → skip
        return None
    return None

def extract_tweet_text_from_html(body: str) -> str:
    m = TEXT_BLOCK_RE.search(body)
    if not m:
        return ""
    raw = m.group(1)
    text = clean_html_to_text(raw)
    # تنظيف إضافي
    text = text.replace("Show this thread", "").strip()
    return text

def build_msg(username: str, tweet_text: str, x_link: str, tweet_time: datetime | None) -> str:
    time_line = tweet_time.strftime("%Y-%m-%d | %H:%M") + " KSA" if tweet_time else now_ksa_str()
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {time_line}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n\n"
        "📝 التغريدة:\n"
        f"{tweet_text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{x_link}\n"
        "════════════════════"
    )

def main():
    state = load_state()

    for username in ACCOUNTS:
        last_seen = state.get(username)

        ids = fetch_account_status_ids(username, MAX_ITEMS_PER_ACCOUNT)
        if not ids:
            continue

        newest_id = ids[0]

        # IDs الجديدة فقط
        new_ids = []
        for sid in ids:
            if last_seen and sid == last_seen:
                break
            new_ids.append(sid)

        # إرسال من الأقدم للأحدث
        new_ids = list(reversed(new_ids))[:MAX_ITEMS_PER_ACCOUNT]

        for sid in new_ids:
            x_link = to_x_link(username, sid)

            tweet_url = f"{XCANCEL_BASE}/{username}/status/{sid}"
            body = fetch_html(tweet_url)

            tweet_time = parse_datetime_from_html(body)
            # فلترة آخر 3 ساعات إذا قدرنا نقرأ الوقت
            if tweet_time and tweet_time < (now_ksa() - timedelta(hours=MAX_AGE_HOURS)):
                continue

            tweet_text = extract_tweet_text_from_html(body)
            if not tweet_text:
                tweet_text = f"(تغريدة جديدة من @{username})"

            telegram_send(build_msg(username, tweet_text, x_link, tweet_time))
            time.sleep(1.2)

        state[username] = newest_id

    save_state(state)

if __name__ == "__main__":
    main()
