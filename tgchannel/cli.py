"""python -m tgchannel [--channel ИМЯ] post | check | sources | bot | probe | new-channel | channels

post     — собрать посты из источников, выбрать тему, написать и опубликовать один пост
           (--dry-run: только показать текст, ничего не отправлять)
check    — проверить настройки: токен бота, доступ к каналу, ключ Gemini, источники
sources  — показать, что удалось прочитать из каждого источника
bot      — постоянно слушать кнопки «Опубликовать/Отклонить» под черновиками (режим TG_PREVIEW_CHAT)
probe    — проверить любые каналы до добавления в sources.txt: python -m tgchannel probe имя1 имя2 ...
new-channel ИМЯ — создать ещё один канал: папку channels/ИМЯ с .env и sources.txt
channels — список настроенных каналов

Несколько каналов: каждый живёт в channels/ИМЯ/ (свой .env с TG_CHANNEL и стилем, свой sources.txt,
своя память data/). Общие настройки (токен бота, ключ Gemini, TG_PREVIEW_CHAT) — в корневом .env.
Без --channel работает «основной» канал из корня проекта.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ai, scrape, telegram

ROOT = Path(__file__).resolve().parent.parent  # корень проекта: .env, sources.txt, data/
STATE = ROOT / "data" / "state.json"
SOURCES = ROOT / "sources.txt"
CHANNEL = ""  # "" — основной канал из корня, иначе имя папки в channels/
NAME_RE = re.compile(r"^[a-z0-9_]{1,20}$")
OFF = ("off", "0", "нет", "no")
log = logging.getLogger("tgchannel")


def read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_env(path: Path) -> None:
    for k, v in read_env(path).items():
        os.environ.setdefault(k, v)


def select_channel(name: str) -> None:
    """Переключить пути (источники, память) и настройки на канал channels/<name>.
    Настройки канала важнее общих: его .env загружается первым."""
    global CHANNEL, STATE, SOURCES
    if name:
        d = ROOT / "channels" / name
        if not d.is_dir():
            raise SystemExit(f"Нет канала {name}: создайте его командой  python -m tgchannel new-channel {name}")
        load_env(d / ".env")
        STATE, SOURCES, CHANNEL = d / "data" / "state.json", d / "sources.txt", name
    load_env(ROOT / ".env")


def preview_chat() -> str:
    v = os.environ.get("TG_PREVIEW_CHAT", "").strip()
    return "" if v.lower() in OFF else v


def list_channels() -> dict:
    """Имя → TG_CHANNEL для всех каналов: "" — основной (корень), остальные из channels/*/.env."""
    out = {}
    root_ch = read_env(ROOT / ".env").get("TG_CHANNEL", "")
    if root_ch:
        out[""] = root_ch
    base = ROOT / "channels"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            ch = read_env(d / ".env").get("TG_CHANNEL", "")
            if d.is_dir() and NAME_RE.match(d.name) and ch:
                out[d.name] = ch
    return out


DRAFTS = lambda: ROOT / "data" / "drafts.json"  # noqa: E731 — message_id черновика → канал (для ответа «+»)


def remember_drafts(msg_ids: list[int]) -> None:
    try:
        drafts = json.loads(DRAFTS().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        drafts = {}
    for mid in msg_ids:
        drafts[str(mid)] = CHANNEL
    drafts = dict(list(drafts.items())[-2000:])
    DRAFTS().parent.mkdir(parents=True, exist_ok=True)
    DRAFTS().write_text(json.dumps(drafts), encoding="utf-8")


def draft_channel(message_id: int) -> str | None:
    try:
        return json.loads(DRAFTS().read_text(encoding="utf-8")).get(str(message_id))
    except (FileNotFoundError, ValueError):
        return None


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
    path = SOURCES
    if not path.exists():
        raise SystemExit(f"Нет {path} — впишите туда каналы-источники (образец: sources.example.txt)")
    sources = scrape.read_sources(str(path))
    if not sources:
        raise SystemExit(f"В {path} нет ни одного канала")
    since = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    posts = []
    for name, kind in sources:
        got = [p for p in scrape.fetch_channel(name, kind) if p.date is None or p.date >= since]
        log.info("%s (%s): %d свежих постов", name, kind, len(got))
        posts += got
    return posts


def wanted_type(state: dict, has_memes: bool = True) -> str:
    """Чередование: после двух новостей подряд просим мем, после мема — новость.
    Если мем-источников нет (например, чисто новостной канал), мемы не просим."""
    if not has_memes:
        return "news"
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

    style = os.environ.get("CHANNEL_STYLE", "Пишем по-русски, живо и коротко.")
    sig = os.environ.get("CHANNEL_SIGNATURE", "").strip()
    limit = (telegram.CAPTION_LIMIT - len(sig) - 10) if sig else 950
    has_memes = any(p.kind == "meme" for p in posts)
    res = ai.choose_and_write(posts, [p["topic"] for p in state["published"]], style, limit,
                              wanted_type(state, has_memes), os.environ.get("CHANNEL_TOPIC", ""))

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
    preview = preview_chat()
    chat = preview or os.environ["TG_CHANNEL"]
    if preview:  # черновик: подписываем, для какого канала, чтобы при нескольких каналах не путаться
        label = f"[{CHANNEL or 'основной'} → {os.environ['TG_CHANNEL']}]"
        if list_channels().keys() - {CHANNEL}:
            telegram.send_text(token, chat, label)
    msg_ids = telegram.publish(token, chat, text, image)
    if preview:  # кнопки на последнем сообщении, в callback — канал и какие сообщения копировать
        telegram.set_buttons(token, chat, msg_ids[-1], approve_buttons(msg_ids, CHANNEL))
        remember_drafts(msg_ids)
        log.info("Черновик отправлен на одобрение в %s, message_id=%s", chat, msg_ids)
    else:
        log.info("Опубликовано в %s, message_id=%s", chat, msg_ids)
    msg_id = msg_ids

    state["used"] += sources + ([img_post.id] if img_post else [])
    state.setdefault("published", []).append({"at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
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
            if CHANNEL:
                print(f"  канал: channels/{CHANNEL}")
            for name in ("TG_CHANNEL", "TG_PREVIEW_CHAT"):
                value = os.environ.get(name, "") if name == "TG_CHANNEL" else preview_chat()
                if value:
                    try:
                        chat = telegram.get_chat(token, value)
                        print(f"✓ {name}: {chat.get('title') or chat.get('username') or chat['id']}")
                    except telegram.TelegramError as e:
                        bad(f"{name}: {e} — для канала нужен @username (например @my_channel) или id -100..., и бот должен быть администратором канала")
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


def approve_buttons(msg_ids: list[int], channel: str = "") -> list[list[dict]]:
    ids = f"{channel}:{msg_ids[0]}:{msg_ids[-1]}" if channel else f"{msg_ids[0]}:{msg_ids[-1]}"
    return [[{"text": "✅ Опубликовать", "callback_data": f"pub:{ids}"},
             {"text": "❌ Отклонить", "callback_data": f"rej:{ids}"}]]


APPROVE_WORDS = {"+", "да", "ок", "ok", "/pub", "опубликовать", "публикуй", "го"}


def handle_update(upd: dict, token: str, admin: str, channels: dict) -> None:
    """Одно обновление от Telegram: нажатие кнопки под черновиком или ответ «+» на любое сообщение бота.
    channels: имя канала → TG_CHANNEL ("" — основной)."""
    cb = upd.get("callback_query")
    if cb:
        msg = cb.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        if str(cb["from"]["id"]) != admin or str(chat_id) != admin:
            telegram.answer_callback(token, cb["id"], "Нет доступа")
            return
        parts = cb.get("data", "").split(":")
        if len(parts) == 3:  # старый формат pub:first:last — основной канал
            parts.insert(1, "")
        action, name, first, last = (parts + ["", "", "", ""])[:4]
        if action not in ("pub", "rej") or not first.isdigit() or not last.isdigit():
            telegram.answer_callback(token, cb["id"])
            return
        channel = channels.get(name)
        if action == "pub" and not channel:
            telegram.answer_callback(token, cb["id"], f"Канал {name or 'основной'} не настроен")
            return
        if action == "pub":
            try:
                for mid in range(int(first), int(last) + 1):
                    telegram.copy_message(token, channel, chat_id, mid)
            except telegram.TelegramError as e:
                telegram.answer_callback(token, cb["id"], f"Ошибка: {e}"[:190])
                return
            done = [[{"text": "✅ Опубликовано", "callback_data": "noop"}]]
            log.info("Одобрено и опубликовано: сообщения %s–%s → %s", first, last, channel)
            telegram.answer_callback(token, cb["id"], "Опубликовано в канал")
        else:
            done = [[{"text": "❌ Отклонено", "callback_data": "noop"}]]
            log.info("Отклонено: сообщения %s–%s", first, last)
            telegram.answer_callback(token, cb["id"], "Отклонено")
        telegram.set_buttons(token, chat_id, msg["message_id"], done)
        return

    m = upd.get("message") or {}
    if str(m.get("chat", {}).get("id")) != admin:
        return
    reply = m.get("reply_to_message")
    if reply and (m.get("text") or "").strip().lower() in APPROVE_WORDS:
        name = draft_channel(reply["message_id"])
        if name is None:  # сообщение не из учёта черновиков: годится, только если канал один
            name = next(iter(channels)) if len(channels) == 1 else None
        channel = channels.get(name) if name is not None else None
        if not channel:
            telegram.send_text(token, admin, "Не знаю, в какой канал это публиковать: каналов несколько. "
                                             "Используйте кнопки под черновиком.")
            return
        try:
            telegram.copy_message(token, channel, admin, reply["message_id"])
            telegram.send_text(token, admin, "✅ Опубликовано в канал", reply_to=reply["message_id"])
            log.info("Одобрено ответом: сообщение %s → %s", reply["message_id"], channel)
        except telegram.TelegramError as e:
            telegram.send_text(token, admin, f"Не получилось: {e}")
    elif m.get("text", "").startswith("/start"):
        telegram.send_text(token, admin, "Черновики будут приходить сюда с кнопками «Опубликовать» и «Отклонить». "
                                         "Чтобы опубликовать старое сообщение без кнопок, ответьте на него «+».")


def cmd_bot(args) -> int:
    token = os.environ["TG_BOT_TOKEN"]
    admin = preview_chat()
    if not admin:
        raise RuntimeError("TG_PREVIEW_CHAT не задан: одобрять некому")
    channels = list_channels()
    if not channels:
        raise RuntimeError("Нет ни одного канала с TG_CHANNEL")
    log.info("Жду одобрений от %s, каналы: %s", admin,
             ", ".join(f"{k or 'основной'} → {v}" for k, v in channels.items()))
    offset = 0
    while True:
        try:
            updates = telegram.get_updates(token, offset)
        except (telegram.TelegramError, OSError) as e:  # сеть моргнула — подождать и продолжить
            log.warning("getUpdates: %s", e)
            import time
            time.sleep(10)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                handle_update(upd, token, admin, list_channels())  # перечитываем: каналы могли добавить
            except Exception as e:  # одно сломанное обновление не должно ронять бота
                log.exception("Ошибка обработки обновления: %s", e)
        if args.once:
            return 0


def cmd_probe(args) -> int:
    """Живой ли канал: сколько постов на странице, сколько с картинками, когда был последний."""
    now = datetime.now(timezone.utc)
    for raw in args.channels:
        name = scrape.normalize_name(raw)
        posts = scrape.fetch_channel(name)
        if not posts:
            print(f"✗ {name}: не читается (нет канала, закрыт или превью выключено)")
            continue
        dates = [p.date for p in posts if p.date]
        last = max(dates) if dates else None
        age = f"{(now - last).total_seconds() / 3600:.0f} ч назад" if last else "дата неизвестна"
        imgs = sum(1 for p in posts if p.images)
        fresh = sum(1 for d in dates if now - d <= timedelta(hours=args.hours))
        mark = "✓" if fresh else "~"
        print(f"{mark} {name}: постов на странице {len(posts)}, с картинкой {imgs}, "
              f"за {args.hours:.0f} ч — {fresh}, последний {age}")
    return 0


CHANNEL_ENV = """# Настройки канала «{name}». Общие (токен бота, ключ Gemini, TG_PREVIEW_CHAT) берутся из корневого .env.
# Бот тот же: добавьте его администратором и в этот канал.

# Канал: @username или id -100...
TG_CHANNEL=
# О чём канал — модель выбирает только подходящие темы
CHANNEL_TOPIC=
# Тон и подача
CHANNEL_STYLE=Пишем по-русски, понятно и коротко, без канцелярита. Эмодзи — не больше 1–2.
# Подпись в конце поста, например @my_channel
CHANNEL_SIGNATURE=
# Чтобы этот канал публиковал сразу, без одобрения в личке, раскомментируйте:
# TG_PREVIEW_CHAT=off
"""


def cmd_new_channel(args) -> int:
    name = args.name.lower()
    if not NAME_RE.match(name):
        raise SystemExit("Имя канала: латинские буквы, цифры и _, до 20 символов (например news)")
    d = ROOT / "channels" / name
    if d.exists():
        raise SystemExit(f"channels/{name} уже есть")
    d.mkdir(parents=True)
    (d / ".env").write_text(CHANNEL_ENV.format(name=name), encoding="utf-8")
    src = ROOT / "presets" / f"{args.preset}.txt" if args.preset else None
    if src and src.exists():
        (d / "sources.txt").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        (d / "sources.txt").write_text((ROOT / "sources.example.txt").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Создан канал channels/{name}. Дальше:\n"
          f"  1) nano channels/{name}/.env        — TG_CHANNEL, тема, стиль\n"
          f"  2) nano channels/{name}/sources.txt — каналы-источники\n"
          f"  3) python -m tgchannel --channel {name} check")
    return 0


def cmd_channels(args) -> int:
    chans = list_channels()
    if not chans:
        print("Каналов нет. Создайте: python -m tgchannel new-channel ИМЯ")
    for name, ch in chans.items():
        print(f"{name or '(основной)'}: {ch}")
    return 0


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(prog="python -m tgchannel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="", help="имя канала из channels/ (без него — основной канал)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("post", cmd_post), ("check", cmd_check), ("sources", cmd_sources)):
        p = sub.add_parser(name)
        p.add_argument("--hours", type=float, default=24, help="брать посты не старше N часов (по умолчанию 24)")
        p.set_defaults(fn=fn)
        if name == "post":
            p.add_argument("--dry-run", action="store_true", help="показать пост, ничего не публиковать")
    p = sub.add_parser("bot")
    p.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_bot)
    p = sub.add_parser("probe")
    p.add_argument("channels", nargs="+", help="username каналов (можно с @ или ссылкой t.me)")
    p.add_argument("--hours", type=float, default=24)
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("new-channel")
    p.add_argument("name")
    p.add_argument("--preset", default="", help="готовый список источников из presets/ (например news)")
    p.set_defaults(fn=cmd_new_channel)
    sub.add_parser("channels").set_defaults(fn=cmd_channels)
    args = ap.parse_args(argv)
    select_channel(args.channel.lower())
    try:
        sys.exit(args.fn(args))
    except (RuntimeError, telegram.TelegramError) as e:
        log.error("%s", e)
        sys.exit(1)
