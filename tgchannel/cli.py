"""python -m tgchannel post | check | sources

post     — собрать посты из источников, выбрать тему, написать и опубликовать один пост
           (--dry-run: только показать текст, ничего не отправлять)
check    — проверить настройки: токен бота, доступ к каналу, ключ Gemini, источники
sources  — показать, что удалось прочитать из каждого источника
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ai, scrape, telegram

ROOT = Path(__file__).resolve().parent.parent  # корень проекта: .env, sources.txt, data/
STATE = ROOT / "data" / "state.json"
log = logging.getLogger("tgchannel")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"used": [], "published": []}


def save_state(state: dict) -> None:
    state["used"] = state["used"][-3000:]
    state["published"] = state["published"][-300:]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def collect(max_age_hours: float) -> list[scrape.Post]:
    path = ROOT / "sources.txt"
    if not path.exists():
        raise SystemExit("Нет sources.txt — скопируйте sources.example.txt и впишите каналы")
    sources = scrape.read_sources(str(path))
    if not sources:
        raise SystemExit("В sources.txt нет ни одного канала")
    since = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    posts = []
    for name, kind in sources:
        got = [p for p in scrape.fetch_channel(name, kind) if p.date is None or p.date >= since]
        log.info("%s (%s): %d свежих постов", name, kind, len(got))
        posts += got
    return posts


def wanted_type(state: dict) -> str:
    """Чередование: после двух новостей подряд просим мем, после мема — новость."""
    last = [p.get("type") for p in state["published"][-2:]]
    if last == ["news", "news"]:
        return "meme"
    if last and last[-1] == "meme":
        return "news"
    return ""


def cmd_post(args) -> int:
    state = load_state()
    used = set(state["used"])
    posts = [p for p in collect(args.hours) if p.id not in used]
    if not posts:
        log.info("Нет новых постов в источниках — пропускаю")
        return 0
    posts.sort(key=lambda p: p.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    posts = posts[:120]  # чтобы запрос к модели не раздувался
    by_id = {p.id: p for p in posts}

    style = os.environ.get("CHANNEL_STYLE", "Канал про игры и мемы, пишем по-русски, живо и коротко.")
    sig = os.environ.get("CHANNEL_SIGNATURE", "").strip()
    limit = (telegram.CAPTION_LIMIT - len(sig) - 10) if sig else 950
    res = ai.choose_and_write(posts, [p["topic"] for p in state["published"]], style, limit, wanted_type(state))

    if res.get("type") == "skip" or not res.get("text", "").strip():
        log.info("Модель решила пропустить: ничего подходящего")
        return 0
    sources = [i for i in res.get("source_ids", []) if i in by_id]
    if not sources:
        log.warning("Модель сослалась на несуществующие посты: %s — пропускаю", res.get("source_ids"))
        return 0

    img_post = by_id.get(res.get("image_id", ""))
    if not (img_post and img_post.images):
        img_post = next((by_id[i] for i in sources if by_id[i].images), None)
    if res["type"] == "meme" and not img_post:
        log.warning("Мем без картинки — пропускаю")
        return 0

    text = res["text"].strip() + (f"\n\n{sig}" if sig else "")
    log.info("Тип: %s | тема: %s | источники: %s | картинка: %s", res["type"], res.get("topic"),
             ", ".join(sources), img_post.id if img_post else "нет")
    if args.dry_run:
        print("\n" + text + "\n")
        if img_post:
            print("Картинка:", img_post.images[0])
        return 0

    image = scrape.download(img_post.images[0]) if img_post else None
    if img_post and not image:
        if res["type"] == "meme":
            log.warning("Картинка мема не скачалась — пропускаю")
            return 0
        log.warning("Картинка не скачалась, публикую без неё")

    token = os.environ["TG_BOT_TOKEN"]
    chat = os.environ.get("TG_PREVIEW_CHAT") or os.environ["TG_CHANNEL"]
    msg_id = telegram.publish(token, chat, text, image)
    log.info("Опубликовано в %s, message_id=%s", chat, msg_id)

    state["used"] += sources + ([img_post.id] if img_post else [])
    state["published"].append({"at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                               "type": res["type"], "topic": res.get("topic", ""), "sources": sources,
                               "chat": chat, "message_id": msg_id})
    save_state(state)
    return 0


def cmd_check(args) -> int:
    ok = True

    def bad(msg):
        nonlocal ok
        ok = False
        print("✗", msg)

    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        bad("TG_BOT_TOKEN не задан")
    else:
        try:
            me = telegram.get_me(token)
            print(f"✓ бот @{me['username']}")
            for name in ("TG_CHANNEL", "TG_PREVIEW_CHAT"):
                if os.environ.get(name):
                    try:
                        chat = telegram.get_chat(token, os.environ[name])
                        print(f"✓ {name}: {chat.get('title') or chat.get('username') or chat['id']}")
                    except telegram.TelegramError as e:
                        bad(f"{name}: {e} (бот добавлен в канал администратором?)")
            if not os.environ.get("TG_CHANNEL"):
                bad("TG_CHANNEL не задан")
        except telegram.TelegramError as e:
            bad(f"токен бота не работает: {e}")

    if not os.environ.get("GEMINI_API_KEY"):
        bad("GEMINI_API_KEY не задан")
    else:
        try:
            ai.gemini_json({"contents": [{"role": "user", "parts": [{"text": 'Ответь JSON {"ok": true}'}]}],
                            "generationConfig": {"responseMimeType": "application/json"}})
            print("✓ Gemini отвечает")
        except RuntimeError as e:
            bad(str(e))

    try:
        n = len(collect(args.hours))
        print(f"{'✓' if n else '✗'} свежих постов в источниках: {n}")
        ok = ok and n > 0
    except SystemExit as e:
        bad(str(e))
    return 0 if ok else 1


def cmd_sources(args) -> int:
    for p in collect(args.hours):
        when = p.date.strftime("%d.%m %H:%M") if p.date else "?"
        print(f"[{when}] {p.id} {'🖼' if p.images else '  '} {p.text[:90]!r}")
    return 0


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    load_env(ROOT / ".env")
    ap = argparse.ArgumentParser(prog="python -m tgchannel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("post", cmd_post), ("check", cmd_check), ("sources", cmd_sources)):
        p = sub.add_parser(name)
        p.add_argument("--hours", type=float, default=24, help="брать посты не старше N часов (по умолчанию 24)")
        p.set_defaults(fn=fn)
        if name == "post":
            p.add_argument("--dry-run", action="store_true", help="показать пост, ничего не публиковать")
    args = ap.parse_args(argv)
    try:
        sys.exit(args.fn(args))
    except (RuntimeError, telegram.TelegramError) as e:
        log.error("%s", e)
        sys.exit(1)
