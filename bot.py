"""Автопостинг в Telegram-канал из posts.csv по расписанию + управление из лички с ботом.

Публикуются только посты со статусом approved, когда наступило их время.
Канал бот находит сам: добавьте его администратором канала — он запомнит канал и напишет админу.
"""
import asyncio
import html
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatMemberUpdated, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import storage  # noqa: E402

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
# Пост, время которого прошло больше чем на столько часов (бот был выключен), сам не публикуется —
# иначе после простоя в канал вывалится пачка старых постов. Админ получает уведомление.
MISSED_HOURS = int(os.getenv("MISSED_HOURS") or 12)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)
_lock = asyncio.Lock()  # тик планировщика и ручные команды не должны публиковать одновременно

HELP = (
    "Команды:\n"
    "/plan — ближайшие посты\n"
    "/preview N — показать пост №N так, как он выйдет в канале\n"
    "/approve N или /approve all — одобрить (уйдёт по расписанию)\n"
    "/skip N — не публиковать\n"
    "/publish N — опубликовать сейчас\n"
    "/status — сколько постов в каком статусе\n\n"
    "Тексты, даты и время правьте в posts.csv (Excel) — бот перечитывает файл сам."
)


def render(post: dict) -> str:
    return f"<b>{html.escape(post['title'])}</b>\n\n{html.escape(post['text'])}"


def channel_id() -> int | None:
    env = os.getenv("CHANNEL_ID")
    return int(env) if env else storage.load_state().get("channel_id")


def is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == ADMIN_ID


async def publish(post: dict) -> int:
    """Публикует и записывает в state.json. Повторно тот же id не публикуется никогда."""
    state = storage.load_state()
    if post["id"] in state["published"]:
        raise RuntimeError(f"Пост №{post['id']} уже опубликован")
    cid = channel_id()
    if not cid:
        raise RuntimeError("Канал не привязан — добавьте бота администратором канала")
    msg = await bot.send_message(cid, render(post), parse_mode="HTML")
    state["published"][post["id"]] = {"message_id": msg.message_id, "at": datetime.now(storage.TZ).isoformat()}
    storage.save_state(state)
    return msg.message_id


async def tick():
    async with _lock:
        now = datetime.now(storage.TZ)
        for post in storage.load_posts():
            # Состояние перечитываем на каждом посте: publish() пишет в state.json сам, и сохранение
            # устаревшей копии затёрло бы отметку о публикации → пост ушёл бы в канал повторно.
            state = storage.load_state()
            if post["status"] != "approved" or post["id"] in state["published"]:
                continue
            try:
                at = storage.scheduled_at(post)
            except ValueError:
                continue  # дату/время в таблице записали не в том формате — пропускаем, не падаем
            if at > now:
                continue
            if now - at > timedelta(hours=MISSED_HOURS):
                if post["id"] not in state["missed_notified"]:
                    state["missed_notified"].append(post["id"])
                    storage.save_state(state)
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ Пост №{post['id']} «{post['title']}» должен был выйти {at:%d.%m %H:%M}, "
                        f"но бот был выключен. Сам публиковать не стал. /publish {post['id']} — выложить сейчас, "
                        "или перенесите дату в posts.csv.",
                    )
                continue
            try:
                await publish(post)
                logging.info("Опубликован пост %s", post["id"])
            except Exception:
                logging.exception("Не удалось опубликовать пост %s", post["id"])
                return  # остальное — на следующем тике


def find(n: str) -> dict | None:
    return next((p for p in storage.load_posts() if p["id"] == n.strip()), None)


def set_status(ids: set[str] | None, status: str) -> tuple[int, bool]:
    posts = storage.load_posts()
    published = storage.load_state()["published"]
    changed = 0
    for p in posts:
        if (ids is None or p["id"] in ids) and p["id"] not in published:
            p["status"] = status
            changed += 1
    return changed, storage.save_posts(posts)


@dp.my_chat_member()
async def on_added(event: ChatMemberUpdated):
    if event.chat.type != "channel":
        return
    state = storage.load_state()
    if event.new_chat_member.status == "administrator":
        state["channel_id"] = event.chat.id
        storage.save_state(state)
        await bot.send_message(ADMIN_ID, f"✅ Канал «{event.chat.title}» привязан. Посты будут выходить туда.")
    elif event.new_chat_member.status in ("left", "kicked") and state.get("channel_id") == event.chat.id:
        state["channel_id"] = None
        storage.save_state(state)
        await bot.send_message(ADMIN_ID, f"Бота убрали из канала «{event.chat.title}» — публикация остановлена.")


@dp.message(Command("start", "help"), F.func(is_admin))
async def cmd_help(message: Message):
    cid = channel_id()
    head = "Канал привязан ✅" if cid else "Канал не привязан — добавьте бота администратором канала."
    await message.answer(f"{head}\n\n{HELP}")


@dp.message(Command("plan"), F.func(is_admin))
async def cmd_plan(message: Message):
    published = storage.load_state()["published"]
    upcoming = [p for p in storage.load_posts() if p["id"] not in published and p["status"] != "skip"][:7]
    if not upcoming:
        await message.answer("Неопубликованных постов нет.")
        return
    mark = {"approved": "✅", "draft": "📝"}
    lines = [f"{mark.get(p['status'], '•')} №{p['id']} · {p['date'][8:10]}.{p['date'][5:7]} {p['time']} · {p['title']}" for p in upcoming]
    await message.answer("Ближайшие посты (✅ одобрен, 📝 черновик):\n\n" + "\n".join(lines))


@dp.message(Command("preview"), F.func(is_admin))
async def cmd_preview(message: Message, command: CommandObject):
    post = find(command.args or "")
    if not post:
        await message.answer("Укажите номер: /preview 3")
        return
    await message.answer(render(post), parse_mode="HTML")
    await message.answer(f"№{post['id']} · {post['date']} {post['time']} · статус: {post['status']}")


@dp.message(Command("approve", "skip"), F.func(is_admin))
async def cmd_status_change(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(f"Укажите номер: /{command.command} 3" + (" или /approve all" if command.command == "approve" else ""))
        return
    status = "approved" if command.command == "approve" else "skip"
    ids = None if arg == "all" else {x.strip() for x in arg.replace(",", " ").split()}
    changed, saved = set_status(ids, status)
    if not saved:
        await message.answer("Не получилось сохранить: posts.csv открыт в Excel. Закройте файл и повторите.")
        return
    await message.answer(f"Готово, постов изменено: {changed} → {status}")


@dp.message(Command("publish"), F.func(is_admin))
async def cmd_publish(message: Message, command: CommandObject):
    post = find(command.args or "")
    if not post:
        await message.answer("Укажите номер: /publish 3")
        return
    async with _lock:
        try:
            await publish(post)
        except Exception as e:
            await message.answer(f"Не опубликовано: {e}")
            return
    await message.answer(f"Пост №{post['id']} опубликован.")


@dp.message(Command("status"), F.func(is_admin))
async def cmd_stat(message: Message):
    posts = storage.load_posts()
    published = storage.load_state()["published"]
    counts = {"опубликовано": 0, "одобрено": 0, "черновики": 0, "пропустить": 0}
    for p in posts:
        if p["id"] in published:
            counts["опубликовано"] += 1
        else:
            counts[{"approved": "одобрено", "skip": "пропустить"}.get(p["status"], "черновики")] += 1
    await message.answer("\n".join(f"{k}: {v}" for k, v in counts.items()))


async def main():
    scheduler = AsyncIOScheduler(timezone=storage.TZ)
    scheduler.add_job(tick, "interval", seconds=30)
    scheduler.start()
    await dp.start_polling(bot, allowed_updates=["message", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
