import os
import re
import json
import time
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

STATE_PATH = Path("state_x_accounts.json")

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

KSA_TZ = ZoneInfo("Asia/Riyadh")

# ✅ جرّب 12 ساعة الآن عشان Google News يتأخر
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "12"))

MAX_PER_ACCOUNT = int(os.getenv("MAX_PER_ACCOUNT", "3"))
REQUEST_TIMEOUT = 25

GN_PARAMS = "hl=ar&gl=SA&ceid=SA:ar"

# ✅ فلتر الكلمات (تقدر تطفيه من Secrets/Env)
USE_KEYWORDS = os.getenv("USE_KEYWORDS", "1") == "1"
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

# ✅ وضع اختبار: يرسل أحدث نتيجة “مرة واحدة” حتى لو ما تطابق الكلمات
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

def tweet_url_pattern(acc: str) -> re.Pattern:
    return re.compile(rf"^https?://(x\.com|twitter\.com)/{re.escape(acc)}/status/\d+/?($|\?)", re.I)

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
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def entry_time_ksa(entry) -> datetime | None:
    pp = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not pp:
        return None
    try:
        dt_utc = datetime(pp.tm_year, pp.tm_mon, pp.tm_mday, pp.tm_hour, pp.tm_min, pp.tm_sec, tzinfo=ZoneInfo("UTC"))
        return dt_utc.astimezone(KSA_TZ)
    except Exception:
        return None

def is_recent(dt_ksa: datetime | None) -> bool:
    if not dt_ksa:
        return False
    return dt_ksa >= (now_ksa() - timedelta(hours=LOOKBACK_HOURS))

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
    # نبحث عن روابط status التابعة للحساب
    q = f"site:x.com/{account}/status"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&{GN_PARAMS}"

def keyword_match(text: str) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in KEYWORDS)

def build_msg(account: str, text: str, x_link: str, dt_ksa: datetime, note: str = "") -> str:
    note_line = f"\n🧩 ملاحظة: {note}\n" if note else "\n"
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {dt_ksa.strftime('%Y-%m-%d | %H:%M')} KSA\n"
        "════════════════════\n"
        f"📌 الحساب: @{account}\n"
        "🧩 المصدر: Google News RSS (Fallback)"
        f"{note_line}"
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

        rejected_old = 0
        rejected_bad_url = 0
        rejected_no_kw = 0
        resolved_fail = 0

        try:
            rss_url = build_google_news_rss_url(acc)
            feed = feedparser.parse(rss_url)
            entries = list(getattr(feed, "entries", []) or [])

            last_seen = state.get(acc)

            # warmup: خزّن نقطة البداية فقط (بدون إرسال)
            if first_run and entries:
                newest_link = (getattr(entries[0], "link", "") or "").strip()
                if newest_link:
                    state[acc] = newest_link
                lines.append(f"@{acc}: warmup scanned={min(30,len(entries))} sent=0")
                continue

            # TEST_MODE: أرسل أول عنصر “الآن” للتأكد من المسار كامل
            if TEST_MODE and entries:
                e = entries[0]
                dt_ksa = entry_time_ksa(e) or now_ksa()
                title = (getattr(e, "title", "") or "").strip() or f"(TEST) @{acc}"
                gn_link = (getattr(e, "link", "") or "").strip()
                final_url = resolve_final_url(gn_link) if gn_link else ""
                if not final_url:
                    final_url = gn_link or "(no link)"
                telegram_send(build_msg(acc, title, final_url, dt_ksa, note="TEST_MODE=1"), preview=True)
                total_sent += 1
                sent_acc += 1
                # حدّث state وطلّع ملخص سريع
                newest_link = (getattr(entries[0], "link", "") or "").strip()
                if newest_link:
                    state[acc] = newest_link
                lines.append(f"@{acc}: TEST sent=1")
                continue

            for entry in entries[:30]:
                scanned += 1

                dt_ksa = entry_time_ksa(entry)
                if not is_recent(dt_ksa):
                    rejected_old += 1
                    continue

                gn_link = (getattr(entry, "link", "") or "").strip()
                if not gn_link:
                    continue

                if last_seen and gn_link == last_seen:
                    break

                final_url = resolve_final_url(gn_link)
                if not final_url:
                    resolved_fail += 1
                    continue

                if not pat.match(final_url):
                    rejected_bad_url += 1
                    continue

                title = (getattr(entry, "title", "") or "").strip()
                if not title:
                    continue

                if USE_KEYWORDS and (not keyword_match(title)):
                    rejected_no_kw += 1
                    continue

                telegram_send(build_msg(acc, title, final_url, dt_ksa), preview=True)
                sent_acc += 1
                total_sent += 1
                time.sleep(0.8)

                if sent_acc >= MAX_PER_ACCOUNT:
                    break

            # تحديث state لأحدث رابط لمنع التكرار
            if entries:
                newest_link = (getattr(entries[0], "link", "") or "").strip()
                if newest_link:
                    state[acc] = newest_link

            lines.append(
                f"@{acc}: scanned={scanned} sent={sent_acc} "
                f"old={rejected_old} bad_url={rejected_bad_url} no_kw={rejected_no_kw} resolve_fail={resolved_fail}"
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
        f"Lookback: {LOOKBACK_HOURS}h | Keywords: {'ON' if USE_KEYWORDS else 'OFF'}\n"
        "════════════════════\n"
        + "\n".join(lines),
        preview=False
    )

if __name__ == "__main__":
    main()
