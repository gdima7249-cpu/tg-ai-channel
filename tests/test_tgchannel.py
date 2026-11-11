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
    f.write_text("# коммент\n@one news\nhttps://t.me/two meme\nthree  # хвост\nfour bogus\n", encoding="utf-8")
    assert scrape.read_sources(str(f)) == [("one", "news"), ("two", "meme"), ("three", "news"), ("four", "news")]


def test_wanted_type():
    st = lambda *t: {"published": [{"type": x} for x in t]}
    assert cli.wanted_type(st()) == ""
    assert cli.wanted_type(st("news", "news")) == "meme"
    assert cli.wanted_type(st("news", "meme")) == "news"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "STATE", tmp_path / "data" / "state.json")
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
    monkeypatch.setattr(telegram, "publish", lambda t, c, text, img: sent.append((c, text, img)) or 42)
    return sent


def _args(**kw):
    return type("A", (), {"hours": 24, "dry_run": False, **kw})()


def test_post_publishes_with_image_from_other_channel(env, monkeypatch):
    seen = {}

    def fake(posts, topics, style, limit, want):
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
    telegram.publish("t", "c", "x" * 2000, b"img")
    assert calls == ["sendPhoto", "sendMessage"]
    calls.clear()
    telegram.publish("t", "c", "short", b"img")
    assert calls == ["sendPhoto"]
