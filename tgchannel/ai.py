"""Gemini выбирает, о чём написать, и пишет пост. Тот же способ вызова, что в faceless/adapt.py."""

from __future__ import annotations

import json
import logging
import os
import time

import requests

from .scrape import Post

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["news", "meme", "skip"]},
        "source_ids": {"type": "array", "items": {"type": "string"}},
        "image_id": {"type": "string"},
        "text": {"type": "string"},
        "topic": {"type": "string"},
    },
    "required": ["type", "source_ids", "image_id", "text", "topic"],
}

SYSTEM = """Ты — редактор Telegram-канала. {style}

{topic}Тебе дают свежие посты из чужих каналов (новости и, возможно, мемы). Выбери ОДНО, о чём сделать пост:
- type="news": новость по теме канала. Сильно предпочитай новость, о которой пишут НЕСКОЛЬКО разных каналов
  (это и подтверждение, и признак, что тема горячая). В source_ids перечисли ВСЕ посты про эту новость.
  Напиши пост СВОИМИ словами: сведи факты из всех источников, не копируй фразы. Не добавляй фактов,
  которых нет в источниках, — ничего не выдумывай (даты, цены, цитаты, названия).
- type="meme": мем-картинка из постов с картинкой. Придумай свою короткую подпись (можно 1 строку).
  Подпись к мему, который без контекста непонятен, не выдумывай — выбери другой.
- type="skip": если ничего стоящего нет или всё уже было (см. список недавних тем).

image_id — id поста, у которого есть картинка (has_image=true), подходящая к посту. Для новости можно
взять картинку из другого поста про ту же новость. Если подходящей нет — пустая строка.
Не указывай в тексте, откуда взята новость, не упоминай другие каналы и не вставляй ссылки.
Текст — обычный текст без Markdown и HTML, до {limit} символов. topic — тема в 3–6 словах."""


def _post_line(p: Post) -> dict:
    return {
        "id": p.id,
        "channel_kind": p.kind,
        "date": p.date.isoformat(timespec="minutes") if p.date else "",
        "views": p.views,
        "has_image": bool(p.images),
        "text": p.text[:700],
    }


def choose_and_write(posts: list[Post], recent_topics: list[str], style: str, limit: int = 900,
                     want: str = "", topic: str = "") -> dict:
    user = {
        "recent_topics_already_posted": recent_topics[-30:],
        "posts": [_post_line(p) for p in posts],
    }
    if want:
        user["preferred_type_this_time"] = want
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM.format(style=style, limit=limit,
                                                               topic=f"Тема канала: {topic}. Посты не по теме пропускай.\n\n" if topic else "")}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(user, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0.9},
    }
    return gemini_json(body)


def gemini_json(body: dict) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Нет GEMINI_API_KEY в .env")
    models = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-flash-latest").split(",") if m.strip()]
    last = "нет ответа"
    for rnd in range(3):
        for model in models:
            try:
                resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=(10, 90),
                                     headers={"x-goog-api-key": key})
            except requests.RequestException as e:
                last = f"{model}: {type(e).__name__}"
                continue
            if resp.status_code in (429, 500, 503, 404):
                last = f"{model}: {resp.status_code}"
                continue
            if resp.status_code >= 400:
                try:
                    msg = resp.json()["error"]["message"]
                except Exception:
                    msg = resp.text[:200]
                if "location is not supported" in msg:
                    raise RuntimeError("Gemini недоступен с этого сервера: Google не обслуживает страну по IP. "
                                       "Нужен сервер в поддерживаемой стране.")
                raise RuntimeError(f"Gemini {resp.status_code}: {msg}")
            data = resp.json()
            cands = data.get("candidates") or []
            if not cands or not cands[0].get("content", {}).get("parts"):
                last = f"{model}: {(data.get('promptFeedback') or {}).get('blockReason') or 'пустой ответ'}"
                continue
            try:
                return json.loads("".join(p.get("text", "") for p in cands[0]["content"]["parts"]))
            except ValueError:
                last = f"{model}: обрезанный ответ"
        if rnd < 2:
            time.sleep(20 * (rnd + 1))
    raise RuntimeError(f"Gemini недоступен или лимит исчерпан ({last})")
