import os
import re
import json
import time
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

STATE_PATH = Path("state_x_accounts.json")

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

KSA_TZ = ZoneInfo("Asia/Riyadh")
REQUEST_TIMEOUT = 25
MAX_PER_ACCOUNT = 3

# Google News RSS locale
GN_PARAMS = "hl=ar&gl=SA&ceid=SA:ar"

# فلتر الكلمات المهمة
USE_KEYWORDS = True
KEYWORDS = [
    "بيان",
    "عاجل",
    "تحذير",
    "تنويه",
    "إعلان",
    "alert",
    "warning",
    "emergency",
]

# وضع التهيئة الأولية:
# أول تشغيل فقط يخزن آخر نقطة بداية ولا يرسل القديم
WARMUP_ON_FIRST_RUN = True


def tweet_url_pattern(acc: str) -> re.Pattern:
    return re.compile(
        rf"^https?://(x\.com|twitter\.com)/{re.escape(acc)}/status/\d+/?($|\?)",
        re.I,
    )


def now_ksa() -> datetime:
    return datetime.now(tz=KSA_TZ)


def now_ksa_str() -> str:
    return now_ksa().strftime("%Y-%m-%d | %H:%M") + " KSA"


def telegram_send(text: str, preview: bool = False) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": (not preview),
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def resolve_final_url(google_news_url: str) -> str:
    try:
        r = requests.get(
            google_news_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        return (r.url or "").strip()
    except Exception:
        return ""


def build_google_news_rss_url(account: str) -> str:
    # نبحث عن روابط تغريدات تخص الحساب
    q = f"site:x.com/{account}"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&{GN_PARAMS}"


def keyword_match(text: str) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in KEYWORDS)


def build_msg(account: str, text: str, x_link: str, note: str = "") -> str:
    note_line = f"🧩 ملاحظة: {note}\n" if note else ""
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {now_ksa_str()}\n"
        "════════════════════\n"
        f"📌 الحساب: @{account}\n"
        "🧩 المصدر: Google News RSS (Fallback)\n"
        f"{note_line}\n"
        "📝 التغريدة/المحتوى:\n"
        f"{text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{x_link}\n"
        "════════════════════"
    )


def main():
    state = load_state()
    first_run = (state.get("_initialized") is not True)

    total_sent = 0
    lines = []

    for acc in ACCOUNTS:
        pat = tweet_url_pattern(acc)
        scanned = 0
        sent_acc = 0
        rejected_bad_url = 0
        rejected_no_kw = 0
        resolved_fail = 0

        try:
            rss_url = build_google_news_rss_url(acc)
            feed = feedparser.parse(rss_url)
            entries = list(getattr(feed, "entries", []) or [])

            last_seen = state.get(acc)

            # أول تشغيل: warmup فقط
            if first_run and WARMUP_ON_FIRST_RUN and entries:
                newest_link = (getattr(entries[0], "link", "") or "").strip()
                if newest_link:
                    state[acc] = newest_link
                lines.append(f"@{acc}: warmup scanned={min(30, len(entries))} sent=0")
                continue

            for entry in entries[:30]:
                scanned += 1

                gn_link = (getattr(entry, "link", "") or "").strip()
                if not gn_link:
                    continue

                # وقف عند آخر عنصر سبق إرساله/تسجيله
                if last_seen and gn_link == last_seen:
                    break

                final_url = resolve_final_url(gn_link)
                if not final_url:
                    resolved_fail += 1
                    continue

                # لازم يكون الرابط النهائي تغريدة من نفس الحساب
                if not pat.match(final_url):
                    rejected_bad_url += 1
                    continue

                title = (getattr(entry, "title", "") or "").strip()
                if not title:
                    continue

                # فلتر الكلمات
                if USE_KEYWORDS and (not keyword_match(title)):
                    rejected_no_kw += 1
                    continue

                telegram_send(build_msg(acc, title, final_url), preview=True)
                sent_acc += 1
                total_sent += 1
                time.sleep(0.8)

                if sent_acc >= MAX_PER_ACCOUNT:
                    break

            # تحديث state إلى أحدث رابط ظهر
            if entries:
                newest_link = (getattr(entries[0], "link", "") or "").strip()
                if newest_link:
                    state[acc] = newest_link

            lines.append(
                f"@{acc}: scanned={scanned} sent={sent_acc} "
                f"bad_url={rejected_bad_url} no_kw={rejected_no_kw} resolve_fail={resolved_fail}"
            )

        except Exception:
            lines.append(f"@{acc}: fail")

    state["_initialized"] = True
    save_state(state)

    telegram_send(
        "🧪 ملخص تشغيل X_news\n"
        f"🕒 {now_ksa_str()}\n"
        "════════════════════\n"
        f"Checked: {len(ACCOUNTS)}\n"
        f"Sent: {total_sent}\n"
        f"Mode: {'warmup' if first_run else 'normal'}\n"
        f"Keywords: {'ON' if USE_KEYWORDS else 'OFF'}\n"
        "════════════════════\n"
        + "\n".join(lines),
        preview=False
    )


if __name__ == "__main__":
    main()
