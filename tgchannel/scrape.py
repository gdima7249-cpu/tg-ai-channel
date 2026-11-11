"""Чтение публичных Telegram-каналов через веб-превью https://t.me/s/<username>.

Аккаунт и ключи не нужны, но работает только для публичных каналов, у которых превью включено.
Разметку страницы Telegram может поменять — тогда разбор вернёт пустой список, а не упадёт."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BG_URL = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)")


@dataclass
class Post:
    id: str  # "channel/123"
    channel: str
    kind: str  # news | meme — из sources.txt
    text: str
    images: list[str] = field(default_factory=list)
    date: datetime | None = None
    views: str = ""

    @property
    def url(self) -> str:
        return f"https://t.me/{self.id}"


def parse_channel_html(html: str, channel: str, kind: str = "news") -> list[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        pid = msg["data-post"]
        text_el = msg.select_one("div.tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        images = []
        for el in msg.select("a.tgme_widget_message_photo_wrap, i.tgme_widget_message_video_thumb"):
            if "video" in " ".join(el.get("class", [])):
                continue  # видео не берём: в превью только обложка
            m = BG_URL.search(el.get("style", ""))
            if m:
                images.append(m.group(1))
        time_el = msg.select_one("time[datetime]")
        date = None
        if time_el:
            try:
                date = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        views_el = msg.select_one("span.tgme_widget_message_views")
        if not text and not images:
            continue
        posts.append(Post(pid, channel, kind, text, images, date, views_el.get_text(strip=True) if views_el else ""))
    return posts


def fetch_channel(channel: str, kind: str = "news", timeout: float = 20) -> list[Post]:
    try:
        resp = requests.get(f"https://t.me/s/{channel}", headers={"User-Agent": UA}, timeout=timeout)
    except requests.RequestException as e:
        log.warning("Канал %s: не загрузился (%s)", channel, type(e).__name__)
        return []
    if resp.status_code != 200 or "tgme_widget_message" not in resp.text:
        # редирект на t.me/<channel> без /s/ = превью выключено или канал приватный
        log.warning("Канал %s: нет публичного превью (HTTP %s), пропускаю", channel, resp.status_code)
        return []
    return parse_channel_html(resp.text, channel, kind)


def normalize_name(raw: str) -> str:
    """@name, t.me/name, https://t.me/s/name → name."""
    name = raw.strip().lstrip("@")
    for prefix in ("https://", "http://", "t.me/", "s/"):  # str.removeprefix есть только с Python 3.9
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.strip("/")


def read_sources(path: str) -> list[tuple[str, str]]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            name = normalize_name(parts[0])
            kind = parts[1] if len(parts) > 1 and parts[1] in ("news", "meme") else "news"
            out.append((name, kind))
    return out


def download(url: str, timeout: float = 30) -> bytes | None:
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except requests.RequestException:
        pass
    return None
