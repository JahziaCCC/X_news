import os
import json
import time
import re
import html as htmlmod
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
MAX_AGE_HOURS = 3  # آخر 3 ساعات (إذا قدرنا نقرأ وقت التغريدة)

# ✅ لا ترسل رسائل أخطاء إلا إذا فشل كل شيء للحساب
SEND_ERROR_ONLY_IF_ALL_FAIL = True

# =========================
# FRONTENDS (ROTATION)
# =========================
# ملاحظة: xcancel يحتاج UA=mistique غالبًا
FRONTENDS = [
    {"name": "xcancel", "base": "https://xcancel.com", "ua": "mistique"},
    {"name": "xcancel-browser", "base": "https://xcancel.com", "ua": "Mozilla/5.0"},
    {"name": "nitter.poast", "base": "https://nitter.poast.org", "ua": "Mozilla/5.0"},
    {"name": "nitter.privacydev", "base": "https://nitter.privacydev.net", "ua": "Mozilla/5.0"},
    {"name": "nitter.dashy", "base": "https://nitter.dashy.a3x.dn.nyx.im", "ua": "Mozilla/5.0"},
]

STATUS_ID_RE = re.compile(r"/status/(\d{8,25})")
TWEET_TEXT_PATTERNS = [
    re.compile(r'(?s)<div[^>]+class="tweet-content"[^>]*>(.*?)</div>'),
    re.compile(r'(?s)<div[^>]+class="main-tweet"[^>]*>.*?<div[^>]+class="tweet-content"[^>]*>(.*?)</div>'),
    re.compile(r'(?s)<div[^>]+class="tweet-body"[^>]*>.*?<div[^>]+class="tweet-content"[^>]*>(.*?)</div>'),
]
TIME_DT_RE = re.compile(r'datetime="([^"]+)"')


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


def fetch_html(path: str):
    """
    Try multiple frontends; return first working HTML.
    Returns: (html_text, frontend_name)
    """
    errors = []
    for f in FRONTENDS:
        base = f["base"].rstrip("/")
        url = f"{base}{path}"
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": f["ua"],
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ar,en;q=0.8",
                    "Referer": base + "/",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code != 200:
                errors.append(f"{f['name']} HTTP {r.status_code}")
                continue

            body = r.text or ""
            if len(body) < 200:
                errors.append(f"{f['name']} empty/short")
                continue

            return body, f["name"], errors

        except Exception as e:
            errors.append(f"{f['name']} EXC {str(e)[:90]}")
            continue

    raise RuntimeError(" | ".join(errors[:3]) if errors else "All frontends failed")


def parse_datetime_from_html(body: str) -> datetime | None:
    m = TIME_DT_RE.search(body or "")
    if not m:
        return None
    raw = (m.group(1) or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(KSA_TZ)
    except Exception:
        return None


def is_recent(tweet_time: datetime | None) -> bool:
    if not tweet_time:
        return True
    return tweet_time >= (now_ksa() - timedelta(hours=MAX_AGE_HOURS))


def extract_tweet_text(body: str) -> str:
    for pat in TWEET_TEXT_PATTERNS:
        m = pat.search(body or "")
        if m:
            txt = clean_html_to_text(m.group(1))
            txt = txt.replace("Show this thread", "").strip()
            if txt:
                return txt
    return ""


def fetch_account_status_ids(username: str, limit: int):
    body, src, errs = fetch_html(f"/{username}")
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

    return uniq, src


def fetch_tweet_page(username: str, status_id: str):
    body, src, errs = fetch_html(f"/{username}/status/{status_id}")
    return body, src


def build_msg(username: str, tweet_text: str, x_link: str, tweet_time: datetime | None, src_name: str) -> str:
    time_line = tweet_time.strftime("%Y-%m-%d | %H:%M") + " KSA" if tweet_time else now_ksa_str()
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {time_line}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🧩 المصدر: {src_name}\n\n"
        "📝 التغريدة:\n"
        f"{tweet_text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{x_link}\n"
        "════════════════════"
    )


def main():
    state = load_state()

    for username in ACCOUNTS:
        try:
            last_seen = state.get(username)

            ids, account_src = fetch_account_status_ids(username, MAX_ITEMS_PER_ACCOUNT)
            if not ids:
                # لا شيء ظاهر
                continue

            newest_id = ids[0]

            new_ids = []
            for sid in ids:
                if last_seen and sid == last_seen:
                    break
                new_ids.append(sid)

            new_ids = list(reversed(new_ids))[:MAX_ITEMS_PER_ACCOUNT]

            for sid in new_ids:
                x_link = to_x_link(username, sid)

                tweet_html, tweet_src = fetch_tweet_page(username, sid)
                tweet_time = parse_datetime_from_html(tweet_html)

                if tweet_time and not is_recent(tweet_time):
                    continue

                tweet_text = extract_tweet_text(tweet_html)
                if not tweet_text:
                    tweet_text = f"(تغريدة جديدة من @{username})"

                telegram_send(build_msg(username, tweet_text, x_link, tweet_time, f"{tweet_src} (author-only)"))
                time.sleep(1.2)

            state[username] = newest_id

        except Exception as e:
            # ✅ لا ترسل خطأ إلا إذا تبغى
            if SEND_ERROR_ONLY_IF_ALL_FAIL:
                try:
                    telegram_send(
                        "⚠️ رصد منصة X — تعذر الرصد لحساب\n"
                        f"🕒 {now_ksa_str()}\n"
                        "════════════════════\n"
                        f"📌 الحساب: @{username}\n"
                        f"🧾 السبب: {str(e)[:180]}\n"
                        "════════════════════"
                    )
                except Exception:
                    pass
            continue

    save_state(state)


if __name__ == "__main__":
    main()
