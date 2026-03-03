import os
import json
import time
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import feedparser

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

MAX_PER_ACCOUNT = 3
REQUEST_TIMEOUT = 25

# RSSHub instances (rotate if one blocks/slow)
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.kenshinji.me",
    "https://rsshub.liumingye.cn",
]

# Extract status id from various url forms
STATUS_ID_RE = re.compile(r"(?:/status/|/i/web/status/)(\d{8,25})")


def now_ksa_str() -> str:
    return datetime.now(tz=KSA_TZ).strftime("%Y-%m-%d | %H:%M") + " KSA"


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


def extract_status_id(s: str) -> str:
    if not s:
        return ""
    m = STATUS_ID_RE.search(s)
    return m.group(1) if m else ""


def fetch_feed(username: str):
    last_err = None
    for base in RSSHUB_INSTANCES:
        url = f"{base}/twitter/user/{username}"
        try:
            # feedparser can fetch itself, but we do requests to control UA/timeouts
            r = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (X_news RSSHub)"},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code != 200:
                last_err = f"{base} HTTP {r.status_code}"
                continue
            feed = feedparser.parse(r.text)
            if getattr(feed, "bozo", False):
                last_err = f"{base} parse bozo"
                continue
            return feed, base
        except Exception as e:
            last_err = f"{base} EXC {str(e)[:80]}"
            continue
    raise RuntimeError(last_err or "All RSSHub instances failed")


def build_msg(username: str, text: str, x_link: str) -> str:
    return (
        "🚨 رصد منصة X — حسابات محددة\n"
        f"🕒 {now_ksa_str()}\n"
        "════════════════════\n"
        f"📌 الحساب: @{username}\n\n"
        "📝 التغريدة:\n"
        f"{text}\n\n"
        "🔗 رابط التغريدة:\n"
        f"{x_link}\n"
        "════════════════════"
    )


def main():
    state = load_state()
    first_run = (state.get("_initialized") != True)

    total_sent = 0
    lines = []

    for username in ACCOUNTS:
        try:
            feed, src = fetch_feed(username)
            entries = list(getattr(feed, "entries", []) or [])
            found = len(entries)

            if found == 0:
                lines.append(f"@{username}: found=0")
                continue

            # newest status id in feed
            newest_id = extract_status_id(entries[0].get("link", "") or entries[0].get("id", ""))
            if not newest_id:
                # fallback: search any entry
                for e in entries[:5]:
                    newest_id = extract_status_id(e.get("link", "") or e.get("id", ""))
                    if newest_id:
                        break

            if not newest_id:
                lines.append(f"@{username}: found={found} (no_status_id)")
                continue

            last_seen = state.get(username)

            # ✅ First run: warm-up only (don’t spam old tweets)
            if first_run and not last_seen:
                state[username] = newest_id
                lines.append(f"@{username}: found={found} warmup")
                continue

            # build list of new entries until last_seen
            new_items = []
            for e in entries[:50]:
                sid = extract_status_id(e.get("link", "") or e.get("id", ""))
                if not sid:
                    continue
                if last_seen and sid == last_seen:
                    break
                new_items.append((sid, e))

            # send oldest -> newest, limit
            new_items = list(reversed(new_items))[:MAX_PER_ACCOUNT]

            sent = 0
            for sid, e in new_items:
                title = (e.get("title") or "").strip()
                summary = (e.get("summary") or "").strip()

                # RSSHub often puts tweet text in title; pick a decent fallback
                text = title if len(title) >= 5 else summary
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    text = f"(تغريدة جديدة من @{username})"

                x_link = f"https://x.com/{username}/status/{sid}"
                telegram_send(build_msg(username, text, x_link), preview=True)
                sent += 1
                total_sent += 1
                time.sleep(0.8)

            # update state to newest id always (after processing)
            state[username] = newest_id

            lines.append(f"@{username}: found={found} new={len(new_items)} sent={sent}")

        except Exception:
            lines.append(f"@{username}: fail")

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
