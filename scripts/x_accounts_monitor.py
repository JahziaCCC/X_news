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
MAX_AGE_HOURS = 24  # بعد ما يشتغل عندك رجّعها 3

# =========================
# RSS FRONTENDS (ROTATION)
# =========================
# ملاحظة: xcancel/xcancel RSS قد يحتاج User-Agent = "mistique" عشان ما يطلع فاضي/محجوب.  [oai_citation:2‡GitHub](https://github.com/zedeus/nitter/issues/1353?utm_source=chatgpt.com)
# وبعض الـ instances “تجي وتمشي”، فحطّيت مجموعة معروفة + من قائمة instances.  [oai_citation:3‡Gist](https://gist.github.com/cmj/7dace466c983e07d4e3b13be4b786c29?utm_source=chatgpt.com)
FEED_SOURCES = [
    {"name": "xcancel.com", "base": "https://xcancel.com", "ua": "mistique"},
    {"name": "xcancel.com (browser UA)", "base": "https://xcancel.com", "ua": "Mozilla/5.0"},
    {"name": "xcancel.com (alt)", "base": "https://xcancel.com", "ua": "mistique", "path": "/{user}/rss"},
    {"name": "twitt.re", "base": "https://twitt.re", "ua": "Mozilla/5.0"},
    {"name": "nitter.privacydev.net", "base": "https://nitter.privacydev.net", "ua": "Mozilla/5.0"},
    {"name": "nitter.poast.org", "base": "https://nitter.poast.org", "ua": "Mozilla/5.0"},
    {"name": "nitter.dashy.a3x.dn.nyx.im", "base": "https://nitter.dashy.a3x.dn.nyx.im", "ua": "Mozilla/5.0"},
]

UA_FALLBACK = "Mozilla/5.0 (compatible; X_news/7.0)"
X_STATUS_RE = re.compile(r"https?://(?:x\.com|twitter\.com|xcancel\.com|twitt\.re|nitter\.[^/]+)/([A-Za-z0-9_]+)/status/(\d+)")


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
        # بعض الـ RSS ما يعطون وقت؛ نسمح مؤقتًا
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


def build_msg(username: str, text: str, link: str, src: str, entry_dt: datetime | None) -> str:
    time_line = entry_dt.strftime("%Y-%m-%d | %H:%M") + " KSA" if entry_dt else now_ksa_str()
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {time_line}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n"
        f"🧩 المصدر: {src}\n\n"
        "📝 التغريدة:\n"
        f"{text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{link}\n"
        "════════════════════"
    )


def fetch_user_rss(username: str):
    """
    Try multiple frontends; return (feed, used_url, source_name, errors_list)
    """
    errors = []
    for src in FEED_SOURCES:
        base = src["base"].rstrip("/")
        ua = (src.get("ua") or UA_FALLBACK).strip()
        path_tpl = src.get("path") or "/{user}/rss"
        url = f"{base}{path_tpl.format(user=username)}"

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

            if r.status_code != 200:
                errors.append(f"{src['name']} HTTP {r.status_code}")
                continue

            body = (r.text or "").strip()
            # بعضهم يرجّع صفحة فاضية/قصيرة جداً
            if len(body) < 200:
                errors.append(f"{src['name']} empty/short body")
                continue

            feed = feedparser.parse(body)
            if getattr(feed, "bozo", False):
                err = getattr(feed, "bozo_exception", None)
                errors.append(f"{src['name']} parse error: {str(err)[:120] if err else 'Unknown'}")
                continue

            return feed, url, src["name"], errors

        except Exception as e:
            errors.append(f"{src['name']} exception: {str(e)[:120]}")
            continue

    return None, "", "", errors


def main():
    state = load_state()

    for username in ACCOUNTS:
        last_seen = state.get(username)

        feed, used_url, src_name, errors = fetch_user_rss(username)

        if not feed:
            # رسالة خطأ مختصرة + أول 3 أخطاء فقط (بدون إزعاج)
            short_err = " | ".join(errors[:3]) if errors else "Unknown"
            telegram_send(
                "⚠️ رصد منصة X — تعذر جلب RSS للحساب\n"
                f"🕒 {now_ksa_str()}\n"
                "════════════════════\n"
                f"📌 الحساب: @{username}\n"
                f"🧾 السبب: {short_err}\n"
                "════════════════════"
            )
            time.sleep(1.0)
            continue

        entries = list(getattr(feed, "entries", []) or [])
        if not entries:
            continue

        newest_id = None
        to_send = []

        for e in entries[: MAX_ITEMS_PER_ACCOUNT * 12]:
            eid = getattr(e, "id", None) or getattr(e, "link", None)
            if not eid:
                continue
            if newest_id is None:
                newest_id = eid
            if last_seen and eid == last_seen:
                break

            link = (getattr(e, "link", "") or "").strip()
            # ✅ فلترة صارمة: الرابط لازم يكون من نفس الحساب
            m = X_STATUS_RE.search(link)
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

        # إرسال من الأقدم للأحدث
        to_send = list(reversed(to_send))[:MAX_ITEMS_PER_ACCOUNT]
        for e, text, link in to_send:
            telegram_send(build_msg(username, text, link, src_name, entry_time_ksa(e)))
            time.sleep(1.2)

        if newest_id:
            state[username] = newest_id

    save_state(state)


if __name__ == "__main__":
    main()
