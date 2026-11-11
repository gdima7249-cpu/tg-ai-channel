"""Публикация через Telegram Bot API."""

from __future__ import annotations

import json

import requests

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class TelegramError(RuntimeError):
    pass


def _call(token: str, method: str, data: dict, files: dict | None = None, timeout: float = 60) -> dict:
    resp = requests.post(API.format(token=token, method=method), data=data, files=files, timeout=timeout)
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


def publish(token: str, chat: str, text: str, image: bytes | None = None,
            buttons: list[list[dict]] | None = None) -> list[int]:
    """Пост с картинкой (если есть) и текстом. Возвращает message_id всех отправленных сообщений
    (два, если текст не влез в подпись к картинке). Кнопки вешаются на последнее сообщение."""
    text = text.strip()
    markup = {"reply_markup": json.dumps({"inline_keyboard": buttons})} if buttons else {}
    ids = []
    if image:
        caption = text if len(text) <= CAPTION_LIMIT else ""
        last = bool(caption or not text)
        msg = _call(token, "sendPhoto", {"chat_id": chat, "caption": caption, **(markup if last else {})},
                    files={"photo": ("image.jpg", image)})
        ids.append(msg["message_id"])
        if last:
            return ids
    msg = _call(token, "sendMessage", {"chat_id": chat, "text": text[:TEXT_LIMIT],
                                       "link_preview_options": '{"is_disabled": true}', **markup})
    ids.append(msg["message_id"])
    return ids


def copy_message(token: str, to_chat: str, from_chat: str | int, message_id: int) -> int:
    """Копия сообщения (с картинкой и подписью, без пометки «переслано»), без кнопок."""
    return _call(token, "copyMessage", {"chat_id": to_chat, "from_chat_id": from_chat, "message_id": message_id,
                                        "reply_markup": json.dumps({"inline_keyboard": []})})["message_id"]


def get_updates(token: str, offset: int, timeout: int = 50) -> list[dict]:
    return _call(token, "getUpdates", {"offset": offset, "timeout": timeout,
                                       "allowed_updates": json.dumps(["message", "callback_query"])},
                 timeout=timeout + 15)


def answer_callback(token: str, callback_id: str, text: str = "") -> None:
    _call(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def set_buttons(token: str, chat: str | int, message_id: int, buttons: list[list[dict]]) -> None:
    _call(token, "editMessageReplyMarkup", {"chat_id": chat, "message_id": message_id,
                                            "reply_markup": json.dumps({"inline_keyboard": buttons})})


def send_text(token: str, chat: str | int, text: str, reply_to: int | None = None) -> None:
    data = {"chat_id": chat, "text": text}
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    _call(token, "sendMessage", data)
