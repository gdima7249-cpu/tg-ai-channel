"""Gemini выбирает, о чём написать, и пишет пост. Тот же способ вызова, что в faceless/adapt.py."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


QUOTA_FILE = Path(__file__).resolve().parent.parent / "data" / "gemini_quota.json"


def next_pacific_midnight(now: datetime) -> datetime:
    """Ближайшая полночь по тихоокеанскому времени (тогда Google сбрасывает дневные лимиты), в UTC.
    Без zoneinfo (его нет в Python 3.8): летнее время — со 2-го воскресенья марта по 1-е воскресенье ноября."""
    def nth_sunday(year, month, n):
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    def offset(utc: datetime) -> timedelta:  # переход в 02:00 местного = 10:00 UTC (март) / 09:00 UTC (ноябрь)
        start = nth_sunday(utc.year, 3, 2) + timedelta(hours=10)
        end = nth_sunday(utc.year, 11, 1) + timedelta(hours=9)
        return timedelta(hours=-7) if start <= utc < end else timedelta(hours=-8)

    local = now + offset(now)
    midnight_local = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    utc = midnight_local - offset(now)
    return utc - (offset(utc) - offset(now))  # если между сейчас и полночью сменилось время


def _load_quota() -> dict:
    try:
        return json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def exhausted_models(now: datetime | None = None) -> dict:
    """Модели, у которых кончился дневной лимит: модель → до какого момента (UTC) не пробовать."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for model, until in _load_quota().items():
        try:
            t = datetime.fromisoformat(until)
        except (TypeError, ValueError):
            continue
        if t > now:
            out[model] = t
    return out


def mark_exhausted(model: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    until = next_pacific_midnight(now)
    data = {m: t.isoformat() for m, t in exhausted_models(now).items()}
    data[model] = until.isoformat()
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUOTA_FILE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(QUOTA_FILE)
    return until


def is_daily_quota(resp) -> bool:
    """429 бывает от лимита в минуту (подождать) и в день (до сброса). Google пишет, какой лимит
    кончился, в error.details[].violations[].quotaId, например ...PerDay... — по нему и отличаем."""
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return False
    text = json.dumps(err).lower()
    return "perday" in text or "per day" in text or "requests_per_day" in text


def gemini_json(body: dict) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Нет GEMINI_API_KEY в .env")
    models = [m.strip() for m in os.environ.get("GEMINI_MODELS", "gemini-flash-latest").split(",") if m.strip()]
    skip = exhausted_models()
    for model in models:
        if model in skip:
            log.info("%s: дневной лимит исчерпан, не пробую до %s UTC", model, skip[model].strftime("%d.%m %H:%M"))
    models = [m for m in models if m not in skip]
    if not models:
        soonest = min(skip.values())
        raise RuntimeError(f"У всех моделей кончился дневной лимит Gemini, сброс около "
                           f"{soonest.strftime('%d.%m %H:%M')} UTC")
    last = "нет ответа"
    for rnd in range(3):
        for model in list(models):
            try:
                resp = requests.post(GEMINI_URL.format(model=model), json=body, timeout=(10, 90),
                                     headers={"x-goog-api-key": key})
            except requests.RequestException as e:
                last = f"{model}: {type(e).__name__}"
                continue
            if resp.status_code == 429 and is_daily_quota(resp):
                until = mark_exhausted(model)
                log.warning("%s: кончился дневной лимит, запомнил до %s UTC", model, until.strftime("%d.%m %H:%M"))
                models.remove(model)
                last = f"{model}: дневной лимит"
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
        if not models:
            break
        if rnd < 2:
            time.sleep(20 * (rnd + 1))
    raise RuntimeError(f"Gemini недоступен или лимит исчерпан ({last})")
