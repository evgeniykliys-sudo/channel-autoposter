"""Офлайн-проверка логики публикации, без Telegram (отправка подменена).

Сценарии:
1. черновик, время наступило        → НЕ публикуется
2. одобрен, время наступило          → публикуется ровно один раз
3. повторный тик (как после перезапуска) → повтора нет
4. одобрен, время прошло > 12 ч       → НЕ публикуется, админ получает одно уведомление
5. одобрен, время в будущем           → НЕ публикуется
6. CSV «открыт в Excel» (запись запрещена) → публикация всё равно не дублируется
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["ADMIN_ID"] = "1"
os.environ["CHANNEL_ID"] = "-100500"
sys.path.insert(0, str(Path(__file__).parent.parent))

import bot  # noqa: E402
import storage  # noqa: E402

sent = []  # (chat_id, text)


async def fake_send(chat_id, text, **kw):
    sent.append((chat_id, text))
    return SimpleNamespace(message_id=len(sent))


bot.bot.send_message = fake_send


def post(pid, when: datetime, status):
    return {"id": pid, "date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
            "rubric": "r", "title": f"Пост {pid}", "text": "текст", "status": status}


def channel_posts():
    return [t for c, t in sent if c == -100500]


async def main():
    tmp = Path(tempfile.mkdtemp())
    storage.POSTS_CSV, storage.STATE_JSON = tmp / "posts.csv", tmp / "state.json"
    now = datetime.now(storage.TZ)
    storage.save_posts([
        post("1", now - timedelta(minutes=5), "draft"),
        post("2", now - timedelta(minutes=5), "approved"),
        post("4", now - timedelta(hours=13), "approved"),
        post("5", now + timedelta(hours=1), "approved"),
    ])

    await bot.tick()
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= cond
        print(("✅" if cond else "❌"), name)

    pub = storage.load_state()["published"]
    check("1. черновик не опубликован", "1" not in pub)
    check("2. одобренный опубликован", "2" in pub and len(channel_posts()) == 1)
    admin_msgs = [t for c, t in sent if c == 1]
    check("4. пропущенный (>12 ч) не опубликован, админ уведомлён", "4" not in pub and len(admin_msgs) == 1)
    check("5. будущий не опубликован", "5" not in pub)

    await bot.tick()
    check("3. повторный тик: без повторной публикации и повторного уведомления",
          len(channel_posts()) == 1 and len([1 for c, _ in sent if c == 1]) == 1)

    # 6: «Excel держит файл» — save_posts не может записать; публикация опирается на state.json
    orig_replace = Path.replace
    Path.replace = lambda self, target: (_ for _ in ()).throw(PermissionError()) if Path(target) == storage.POSTS_CSV else orig_replace(self, target)
    try:
        await bot.tick()
    finally:
        Path.replace = orig_replace
    check("6. при заблокированном CSV повтора нет", len(channel_posts()) == 1)

    print("\nИТОГ:", "все сценарии пройдены" if ok else "ЕСТЬ ОШИБКИ")
    sys.exit(0 if ok else 1)


asyncio.run(main())
