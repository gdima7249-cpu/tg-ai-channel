"""Тесты tgchannel без сети."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tgchannel import cli, scrape, telegram

NOW = datetime.now(timezone.utc)


def _msg(pid, text, img=None, video=False, when=NOW):
    media = ""
    if img:
        media = f'<a class="tgme_widget_message_photo_wrap" style="width:100px;background-image:url(\'{img}\')"></a>'
    if video:
        media += '<i class="tgme_widget_message_video_thumb" style="background-image:url(\'https://cdn/v.jpg\')"></i>'
    return f'''<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="{pid}">
      {media}<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>
      <span class="tgme_widget_message_views">1.2K</span>
      <a class="tgme_widget_message_date"><time datetime="{when.isoformat()}" class="time">12:00</time></a>
    </div></div>'''


def test_parse_channel_html():
    html = "<html><body>" + _msg("chan/1", "Анонс <b>игры</b><br/>вторая строка", "https://cdn/1.jpg") \
        + _msg("chan/2", "только видео", video=True) + _msg("chan/3", "") + "</body></html>"
    posts = scrape.parse_channel_html(html, "chan", "news")
    assert [p.id for p in posts] == ["chan/1", "chan/2"]
    assert posts[0].images == ["https://cdn/1.jpg"]
    assert "Анонс" in posts[0].text and "вторая строка" in posts[0].text
    assert posts[1].images == []
    assert posts[0].views == "1.2K" and posts[0].date is not None


def test_read_sources(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("# коммент\n@one news\nhttps://t.me/two meme\nthree  # хвост\nfour bogus\nhttps://t.me/s/five meme\n", encoding="utf-8")
    assert scrape.read_sources(str(f)) == [("one", "news"), ("two", "meme"), ("three", "news"), ("four", "news"), ("five", "meme")]


def test_wanted_type():
    st = lambda *t: {"published": [{"type": x} for x in t]}
    assert cli.wanted_type(st()) == ""
    assert cli.wanted_type(st("news", "news")) == "meme"
    assert cli.wanted_type(st("news", "meme")) == "news"
    assert cli.wanted_type(st("news", "news"), has_memes=False) == "news"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "STATE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(cli, "SOURCES", tmp_path / "sources.txt")
    monkeypatch.setattr(cli, "CHANNEL", "")
    (tmp_path / "sources.txt").write_text("a news\nb news\n", encoding="utf-8")
    monkeypatch.setenv("TG_BOT_TOKEN", "x")
    monkeypatch.setenv("TG_CHANNEL", "@mine")
    monkeypatch.delenv("TG_PREVIEW_CHAT", raising=False)
    monkeypatch.setenv("CHANNEL_SIGNATURE", "@mine")
    feeds = {
        "a": [scrape.Post("a/1", "a", "news", "Вышел трейлер X", [], NOW),
              scrape.Post("a/0", "a", "news", "старьё", [], NOW - timedelta(days=3))],
        "b": [scrape.Post("b/5", "b", "news", "Трейлер X уже в сети", ["https://cdn/x.jpg"], NOW)],
    }
    monkeypatch.setattr(scrape, "fetch_channel", lambda name, kind: feeds[name])
    monkeypatch.setattr(scrape, "download", lambda url: b"IMG")
    sent = []
    monkeypatch.setattr(telegram, "publish", lambda t, c, text, img: sent.append((c, text, img)) or [42])
    monkeypatch.setattr(telegram, "set_buttons", lambda *a: sent.append(("buttons",) + a[1:]))
    return sent


def _args(**kw):
    return type("A", (), {"hours": 24, "dry_run": False, **kw})()


def test_post_publishes_with_image_from_other_channel(env, monkeypatch):
    seen = {}

    def fake(posts, topics, style, limit, want, topic=""):
        seen["ids"] = [p.id for p in posts]
        return {"type": "news", "source_ids": ["a/1", "b/5"], "image_id": "", "text": "Трейлер X!", "topic": "трейлер X"}

    monkeypatch.setattr(cli.ai, "choose_and_write", fake)
    assert cli.cmd_post(_args()) == 0
    assert sorted(seen["ids"]) == ["a/1", "b/5"]  # старый пост отфильтрован по возрасту
    assert env == [("@mine", "Трейлер X!\n\n@mine", b"IMG")]
    state = json.loads(cli.STATE.read_text(encoding="utf-8"))
    assert set(state["used"]) >= {"a/1", "b/5"}

    # второй запуск: те же посты уже использованы — к модели ничего не уходит
    monkeypatch.setattr(cli.ai, "choose_and_write", lambda *a: pytest.fail("не должно вызываться"))
    assert cli.cmd_post(_args()) == 0


def test_post_skip_and_bad_ids(env, monkeypatch):
    monkeypatch.setattr(cli.ai, "choose_and_write", lambda *a: {"type": "skip", "source_ids": [], "image_id": "", "text": "", "topic": ""})
    assert cli.cmd_post(_args()) == 0
    monkeypatch.setattr(cli.ai, "choose_and_write", lambda *a: {"type": "news", "source_ids": ["zzz/1"], "image_id": "", "text": "t", "topic": "t"})
    assert cli.cmd_post(_args()) == 0
    assert env == [] and not cli.STATE.exists()


def test_meme_without_image_is_skipped(env, monkeypatch):
    monkeypatch.setattr(cli.ai, "choose_and_write", lambda *a: {"type": "meme", "source_ids": ["a/1"], "image_id": "", "text": "лол", "topic": "мем"})
    assert cli.cmd_post(_args()) == 0
    assert env == []


def test_publish_long_text_splits(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, "_call", lambda tok, m, data, files=None: calls.append(m) or {"message_id": 1})
    assert telegram.publish("t", "c", "x" * 2000, b"img") == [1, 1]
    assert calls == ["sendPhoto", "sendMessage"]
    calls.clear()
    telegram.publish("t", "c", "short", b"img")
    assert calls == ["sendPhoto"]


def test_normalize_name():
    for raw in ("stalcraft", "@stalcraft", "t.me/stalcraft", "https://t.me/s/stalcraft/", "http://t.me/stalcraft"):
        assert scrape.normalize_name(raw) == "stalcraft"


def test_probe(monkeypatch, capsys):
    feeds = {"live": [scrape.Post("live/1", "live", "news", "мем", ["https://cdn/1.jpg"], NOW)], "dead": []}
    monkeypatch.setattr(scrape, "fetch_channel", lambda name, kind="news": feeds[name])
    args = type("A", (), {"channels": ["@live", "https://t.me/s/dead"], "hours": 24})()
    assert cli.cmd_probe(args) == 0
    out = capsys.readouterr().out
    assert "✓ live" in out and "с картинкой 1" in out and "✗ dead" in out


def test_preview_mode_adds_approve_buttons(env, monkeypatch):
    monkeypatch.setenv("TG_PREVIEW_CHAT", "777")
    monkeypatch.setattr(cli.ai, "choose_and_write", lambda *a: {"type": "news", "source_ids": ["a/1"], "image_id": "", "text": "t", "topic": "t"})
    assert cli.cmd_post(_args()) == 0
    assert env[0][0] == "777"
    assert env[1] == ("buttons", "777", 42, cli.approve_buttons([42]))


@pytest.fixture
def tg(monkeypatch):
    calls = []
    for name in ("copy_message", "answer_callback", "set_buttons", "send_text"):
        monkeypatch.setattr(telegram, name, lambda *a, _n=name, **k: calls.append((_n,) + a[1:]) or 1)
    return calls


def _cb(data, user=777, chat=777, mid=11):
    return {"update_id": 1, "callback_query": {"id": "q", "data": data, "from": {"id": user},
                                               "message": {"message_id": mid, "chat": {"id": chat}}}}


def test_approve_button_copies_all_parts(tg):
    cli.handle_update(_cb("pub:10:11"), "t", "777", {"": "@mine"})
    assert ("copy_message", "@mine", 777, 10) in tg and ("copy_message", "@mine", 777, 11) in tg
    assert tg[-1][0] == "set_buttons" and "Опубликовано" in tg[-1][3][0][0]["text"]


def test_reject_and_foreign_user(tg):
    cli.handle_update(_cb("rej:10:10"), "t", "777", {"": "@mine"})
    assert not any(c[0] == "copy_message" for c in tg)
    tg.clear()
    cli.handle_update(_cb("pub:10:10", user=666, chat=666), "t", "777", {"": "@mine"})
    assert tg == [("answer_callback", "q", "Нет доступа")]


def test_reply_plus_publishes_old_message(tg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    upd = {"update_id": 2, "message": {"chat": {"id": 777}, "text": "+", "reply_to_message": {"message_id": 5}}}
    cli.handle_update(upd, "t", "777", {"": "@mine"})
    assert tg[0] == ("copy_message", "@mine", "777", 5)


def test_multichannel(tmp_path, monkeypatch, tg):
    """Два канала: callback с именем канала публикует в нужный, ответ «+» находит канал черновика."""
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("TG_CHANNEL=@games\nTG_BOT_TOKEN=x\n", encoding="utf-8")
    monkeypatch.setattr(cli, "SOURCES", tmp_path / "sources.example.txt")
    (tmp_path / "sources.example.txt").write_text("# пример\n", encoding="utf-8")
    args = type("A", (), {"name": "news", "preset": ""})()
    assert cli.cmd_new_channel(args) == 0
    env = (tmp_path / "channels" / "news" / ".env")
    env.write_text(env.read_text(encoding="utf-8").replace("TG_CHANNEL=\n", "TG_CHANNEL=@news\n"), encoding="utf-8")
    chans = cli.list_channels()
    assert chans == {"": "@games", "news": "@news"}

    assert cli.approve_buttons([3, 4], "news")[0][0]["callback_data"] == "pub:news:3:4"
    cli.handle_update(_cb("pub:news:3:4"), "t", "777", chans)
    assert [c for c in tg if c[0] == "copy_message"] == [("copy_message", "@news", 777, 3), ("copy_message", "@news", 777, 4)]

    tg.clear()
    monkeypatch.setattr(cli, "CHANNEL", "news")
    cli.remember_drafts([9])
    upd = {"update_id": 3, "message": {"chat": {"id": 777}, "text": "+", "reply_to_message": {"message_id": 9}}}
    cli.handle_update(upd, "t", "777", chans)
    assert tg[0] == ("copy_message", "@news", "777", 9)

    tg.clear()  # неизвестное сообщение при двух каналах — не угадываем
    upd["message"]["reply_to_message"]["message_id"] = 100
    cli.handle_update(upd, "t", "777", chans)
    assert not any(c[0] == "copy_message" for c in tg)


def test_select_channel_env_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    import os
    monkeypatch.setattr(os, "environ", {})  # не засорять настоящее окружение других тестов
    (tmp_path / ".env").write_text("TG_CHANNEL=@games\nCHANNEL_STYLE=игры\nGEMINI_API_KEY=k\n", encoding="utf-8")
    d = tmp_path / "channels" / "news"
    d.mkdir(parents=True)
    (d / ".env").write_text("TG_CHANNEL=@news\nCHANNEL_STYLE=новости\n", encoding="utf-8")
    cli.select_channel("news")
    assert (os.environ["TG_CHANNEL"], os.environ["CHANNEL_STYLE"], os.environ["GEMINI_API_KEY"]) == ("@news", "новости", "k")
    assert cli.SOURCES == d / "sources.txt" and cli.STATE == d / "data" / "state.json"
    monkeypatch.setattr(cli, "SOURCES", tmp_path / "sources.txt")
    monkeypatch.setattr(cli, "STATE", tmp_path / "data" / "state.json")
    monkeypatch.setattr(cli, "CHANNEL", "")
