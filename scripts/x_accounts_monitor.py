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

LOOKBACK_HOURS = 3          # ✅ آخر 3 ساعات فقط
MAX_PER_ACCOUNT = 3
REQUEST_TIMEOUT = 25

# Google News RSS locale (Saudi Arabic)
GN_PARAMS = "hl=ar&gl=SA&ceid=SA:ar"

# Strict "tweet by account" URL pattern
TWEET_URL_RE = re.compile(r"^https?://(x\.com|twitter\.com)/{acc}/status/\d+/?($|\?)", re.I)


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
    # feedparser gives published_parsed in UTC struct_time
    pp = getattr(entry, "published_parsed", None)
    if not pp:
        pp = getattr(entry, "updated_parsed", None)
    if not pp:
        return None
    try:
        dt_utc = datetime(pp.tm_year, pp.tm_mon, pp.tm_mday, pp.tm_hour, pp.tm_min, pp.tm_sec, tzinfo=ZoneInfo("UTC"))
        return dt_utc.astimezone(KSA_TZ)
    except Exception:
        return None


def is_recent(dt_ksa: datetime | None) -> bool:
    if not dt_ksa:
        return False  # إذا ما نعرف الوقت، لا نرسل (عشان ما يجيك قديم)
    return dt_ksa >= (now_ksa() - timedelta(hours=LOOKBACK_HOURS))


def resolve_final_url(google_news_url: str) -> str:
    """
    Google News RSS links are redirects.
    We resolve to final URL so we can enforce x.com/<account>/status/<id>
    """
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
    # Search only tweets from this account (status URLs)
    # This reduces replies/mentions massively.
    q = f"site:x.com/{account}/status"
    return f"https://news.google.com/rss/search?q={quote_plus(q)}&{GN_PARAMS}"


def build_msg(account: str, text: str, x_link: str, dt_ksa: datetime, source_note: str) -> str:
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {dt_ksa.strftime('%Y-%m-%d | %H:%M')} KSA\n"
        "════════════════════\n"
        f"📌 الحساب: @{account}\n"
        f"🧩 المصدر: Google News RSS (Fallback) — {source_note}\n\n"
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
        sent_acc = 0
        scanned = 0

        try:
            rss_url = build_google_news_rss_url(acc)
            feed = feedparser.parse(rss_url)
            entries = list(getattr(feed, "entries", []) or [])

            # Sort by published time (newest first) if available
            # (Google usually already returns newest first)
            last_seen = state.get(acc)

            for entry in entries[:30]:
                scanned += 1

                dt_ksa = entry_time_ksa(entry)
                if not is_recent(dt_ksa):
                    continue  # ✅ يمنع القديم

                gn_link = (getattr(entry, "link", "") or "").strip()
                if not gn_link:
                    continue

                # dedup key: google link
                if last_seen and gn_link == last_seen:
                    break

                final_url = resolve_final_url(gn_link)
                if not final_url:
                    continue

                # ✅ لازم يكون tweet من نفس الحساب (مو رد/mention)
                if not TWEET_URL_RE.pattern:
                    pass
                if not re.match(TWEET_URL_RE.pattern.format(acc=re.escape(acc)), final_url, flags=re.I):
                    continue

                title = (getattr(entry, "title", "") or "").strip()
                if not title:
                    title = f"(تغريدة جديدة من @{acc})"

                telegram_send(build_msg(acc, title, final_url, dt_ksa, "x.com direct"), preview=True)
                sent_acc += 1
                total_sent += 1
                time.sleep(0.8)

                if sent_acc >= MAX_PER_ACCOUNT:
                    break

            # Warmup: لا نرسل شيء أول مرة (لكن هنا نرسل فقط آخر 3 ساعات أصلاً، فآمن)
            # ومع ذلك نخلي warmup على آخر عنصر لتثبيت نقطة البداية.
            if first_run and entries:
                # store newest link
                newest_link = (getattr(entries[0], "link", "") or "").strip()
                if newest_link:
                    state[acc] = newest_link

            # Normal: update state to newest link to avoid duplicates
            if not first_run and entries:
                newest_link = (getattr(entries[0], "link", "") or "").strip()
                if newest_link:
                    state[acc] = newest_link

            lines.append(f"@{acc}: scanned={scanned} sent={sent_acc}")

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
        "════════════════════\n"
        + "\n".join(lines),
        preview=False
    )


if __name__ == "__main__":
    main()
