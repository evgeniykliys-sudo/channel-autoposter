"""Контент-план и посты на N дней по брифу → posts.csv (все посты — черновики, статус draft).

    python generate.py                  # brief.json, старт с завтрашнего дня
    python generate.py мой_бриф.json

Два шага: сначала план (темы и рубрики на все дни сразу — чтобы не повторяться),
потом тексты пачками по 6 постов, каждой пачке передаются уже написанные заголовки.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import storage  # noqa: E402

MODEL = os.getenv("CONTENT_MODEL") or "claude-sonnet-5"
BATCH = 6

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

RULES = """Ты пишешь посты для Telegram-канала по брифу. Жёсткие правила:
- Используй только факты из брифа (раздел facts). Не выдумывай клиентов, кейсы, цифры экономии и выручки.
  Если нужен пример — либо факт из брифа, либо явно гипотетический («допустим, у вас салон…») без цифр результата.
- Пиши для людей, которые не программисты: без жаргона, а если термин нужен — объясни в одной фразе.
- Каждый пост — одна мысль, польза в первых двух строках, без воды и вступлений «Друзья, сегодня поговорим…».
- Обычный текст без разметки Markdown и HTML. Абзацы через пустую строку. Эмодзи — не больше 2-3 на пост.
- Соблюдай раздел avoid брифа."""


def call_tool(prompt: str, tool: dict) -> dict:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=RULES,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise ValueError("Модель не вернула результат")


PLAN_TOOL = {
    "name": "save_plan",
    "description": "Сохранить контент-план",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "integer"},
                        "rubric": {"type": "string", "description": "Одна из рубрик брифа"},
                        "title": {"type": "string", "description": "Цепляющий заголовок поста, до 70 символов"},
                        "idea": {"type": "string", "description": "О чём пост и какой факт из брифа использовать, 1-2 предложения"},
                    },
                    "required": ["day", "rubric", "title", "idea"],
                },
            }
        },
        "required": ["items"],
    },
}

POSTS_TOOL = {
    "name": "save_posts",
    "description": "Сохранить тексты постов",
    "input_schema": {
        "type": "object",
        "properties": {
            "posts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "integer"},
                        "text": {"type": "string", "description": "Текст поста без заголовка, 500-1100 символов"},
                    },
                    "required": ["day", "text"],
                },
            }
        },
        "required": ["posts"],
    },
}


def main():
    brief_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "brief.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    days = int(brief.get("days", 30))
    brief_txt = json.dumps(brief, ensure_ascii=False, indent=1)

    print(f"План на {days} дней…")
    plan = call_tool(
        f"Бриф канала:\n{brief_txt}\n\nСоставь контент-план на {days} дней: по одному посту в день, "
        f"дни 1..{days}. Чередуй рубрики.\n"
        "Против повторов (фактов в брифе меньше, чем дней):\n"
        "- каждый факт из брифа — главная тема только ОДНОГО поста; ещё раз его можно упомянуть мимоходом как пример, "
        "но не делать второй пост о том же (два поста про один и тот же бот = повтор);\n"
        "- остальные дни — полезные посты, не привязанные к фактам: чек-листы, как подготовить задачу для разработчика, "
        "как понять, окупится ли автоматизация, мифы о нейросетях, что спросить подрядчика перед заказом, "
        "с чего начать автоматизацию в конкретной нише из аудитории брифа, и т.п.;\n"
        "- никакие два поста не должны отвечать на один и тот же вопрос читателя.",
        PLAN_TOOL,
    )["items"]
    plan = sorted(plan, key=lambda x: x["day"])[:days]

    texts: dict[int, str] = {}
    for i in range(0, len(plan), BATCH):
        batch = plan[i : i + BATCH]
        done_titles = [p["title"] for p in plan[:i]]
        print(f"Посты {batch[0]['day']}-{batch[-1]['day']}…")
        result = call_tool(
            f"Бриф канала:\n{brief_txt}\n\nУже написаны посты с заголовками (не повторяй их мысли):\n"
            + "\n".join(f"- {t}" for t in done_titles)
            + "\n\nНапиши тексты для этих пунктов плана:\n"
            + json.dumps(batch, ensure_ascii=False, indent=1),
            POSTS_TOOL,
        )
        for p in result["posts"]:
            texts[p["day"]] = p["text"].strip()

    start = datetime.now(storage.TZ).date() + timedelta(days=1)
    posts = [
        {
            "id": str(item["day"]),
            "date": (start + timedelta(days=item["day"] - 1)).isoformat(),
            "time": brief.get("post_time", "10:00"),
            "rubric": item["rubric"],
            "title": item["title"],
            "text": texts.get(item["day"], ""),
            "status": "draft",
        }
        for item in plan
    ]
    if storage.POSTS_CSV.exists():
        backup = storage.POSTS_CSV.with_name(f"posts.backup-{datetime.now():%Y%m%d-%H%M%S}.csv")
        storage.POSTS_CSV.replace(backup)
        print(f"Старый план сохранён в {backup.name}")
    storage.save_posts(posts)
    missing = [p["id"] for p in posts if not p["text"]]
    print(f"Готово: {len(posts)} постов в {storage.POSTS_CSV.name}" + (f", БЕЗ ТЕКСТА: {missing}" if missing else ""))


if __name__ == "__main__":
    main()
