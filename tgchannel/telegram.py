"""Публикация через Telegram Bot API."""

from __future__ import annotations

import requests

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class TelegramError(RuntimeError):
    pass


def _call(token: str, method: str, data: dict, files: dict | None = None) -> dict:
    resp = requests.post(API.format(token=token, method=method), data=data, files=files, timeout=60)
    try:
        payload = resp.json()
    except ValueError:
        raise TelegramError(f"{method}: HTTP {resp.status_code}")
    if not payload.get("ok"):
        raise TelegramError(f"{method}: {payload.get('description', resp.status_code)}")
    return payload["result"]


def get_me(token: str) -> dict:
    return _call(token, "getMe", {})


def get_chat(token: str, chat: str) -> dict:
    return _call(token, "getChat", {"chat_id": chat})


def publish(token: str, chat: str, text: str, image: bytes | None = None) -> int:
    """Пост с картинкой (если есть) и текстом. Возвращает message_id."""
    text = text.strip()
    if image:
        caption = text if len(text) <= CAPTION_LIMIT else ""
        msg = _call(token, "sendPhoto", {"chat_id": chat, "caption": caption},
                    files={"photo": ("image.jpg", image)})
        if caption or not text:
            return msg["message_id"]
    msg = _call(token, "sendMessage", {"chat_id": chat, "text": text[:TEXT_LIMIT],
                                       "link_preview_options": '{"is_disabled": true}'})
    return msg["message_id"]
