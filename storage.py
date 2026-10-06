"""Хранение постов.

posts.csv  — контент и одобрение: правит человек (Excel/любой редактор). Разделитель «;» и UTF-8 с BOM —
             так русский Excel открывает файл без танцев с импортом.
state.json — что уже опубликовано: правит только бот. Отдельно от CSV, чтобы пост не ушёл в канал дважды,
             даже если CSV в этот момент открыт в Excel и заблокирован на запись.
"""
import csv
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
POSTS_CSV = ROOT / "posts.csv"
STATE_JSON = ROOT / "state.json"
TZ = ZoneInfo(os.getenv("TZ_NAME") or "Asia/Novosibirsk")

FIELDS = ["id", "date", "time", "rubric", "title", "text", "status"]
# draft — черновик, не публикуется; approved — одобрен, уйдёт по расписанию; skip — не публиковать
STATUSES = ("draft", "approved", "skip")


def load_posts() -> list[dict]:
    if not POSTS_CSV.exists():
        return []
    with POSTS_CSV.open(encoding="utf-8-sig", newline="") as f:
        posts = list(csv.DictReader(f, delimiter=";"))
    for p in posts:
        p["status"] = (p.get("status") or "draft").strip().lower()
    return posts


def save_posts(posts: list[dict]) -> bool:
    """Атомарная запись. False — файл занят (открыт в Excel): не страшно, источник правды о публикациях — state.json."""
    tmp = POSTS_CSV.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(posts)
    try:
        tmp.replace(POSTS_CSV)
        return True
    except PermissionError:
        tmp.unlink(missing_ok=True)
        return False


def scheduled_at(post: dict) -> datetime:
    return datetime.strptime(f"{post['date']} {post['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def load_state() -> dict:
    if STATE_JSON.exists():
        return json.loads(STATE_JSON.read_text(encoding="utf-8"))
    return {"channel_id": None, "published": {}, "missed_notified": []}


def save_state(state: dict) -> None:
    tmp = STATE_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_JSON)
