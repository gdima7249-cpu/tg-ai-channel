# tg-ai-channel — ИИ ведёт Telegram-канал (игры + мемы)

> **Ничего ещё не устанавливали? Пошаговая инструкция с нуля — [INSTALL.md](INSTALL.md).**

Как работает каждый запуск (по умолчанию 10 раз в день):

1. Читает свежие посты (за 24 ч) из публичных каналов из `sources.txt` через `https://t.me/s/<канал>`. Аккаунт не нужен.
2. Отдаёт их Gemini. Модель выбирает **одну** тему: лучше новость, о которой пишут несколько каналов, либо мем.
3. Gemini пишет пост своими словами, сводя факты из всех источников. Картинку можно взять из поста **другого** канала про ту же новость.
4. Бот публикует пост в ваш канал. Использованные посты запоминаются (`data/state.json`) и второй раз не берутся.
5. Посты чередуются: после двух новостей просит мем, после мема — новость. Если ничего стоящего нет, пропускает запуск.

## Что нужно

1. **Бот**: @BotFather → `/newbot` → токен. Добавить бота в свой канал **администратором** с правом публиковать.
2. **Ключ Gemini API**: https://aistudio.google.com/apikey. Подписка Gemini в приложении/Google One — это
   другое, API-ключ в неё не входит. У API есть бесплатный тариф с лимитами.
   ⚠ Gemini API не работает с IP-адресов неподдерживаемых стран (в т.ч. России) — сервер должен быть за рубежом.
3. **Источники**: `cp sources.example.txt sources.txt`, вписать username публичных каналов, пометить `news` или `meme`.
   Готовый список кандидатов (непроверенных) — `sources.candidates.txt`.
   Проверить, что канал читается: открыть в браузере `https://t.me/s/<username>`.

## Установка на сервер (Ubuntu/Debian)

```bash
sudo useradd -r -m -d /opt/tgchannel tgchannel
sudo -u tgchannel git clone https://github.com/gdima7249-cpu/tg-ai-channel.git /opt/tgchannel/app
cd /opt/tgchannel/app
sudo -u tgchannel python3 -m venv .venv
sudo -u tgchannel .venv/bin/pip install -r requirements.txt
sudo -u tgchannel cp .env.example .env
sudo -u tgchannel cp sources.example.txt sources.txt
sudo -u tgchannel nano .env          # токен, канал, ключ Gemini
sudo -u tgchannel nano sources.txt   # каналы-источники
```

Проверка и первый пост:

```bash
sudo -u tgchannel .venv/bin/python -m tgchannel check          # всё ли настроено
sudo -u tgchannel .venv/bin/python -m tgchannel sources        # что читается из источников
sudo -u tgchannel .venv/bin/python -m tgchannel post --dry-run # показать пост, не публикуя
sudo -u tgchannel .venv/bin/python -m tgchannel post           # опубликовать один пост
```

Режим одобрения: впишите в `.env` `TG_PREVIEW_CHAT=<ваш chat id>` — посты будут приходить вам в личку
черновиками с кнопками «✅ Опубликовать» / «❌ Отклонить» (сначала напишите боту `/start` и включите службу
`deploy/tgchannel-bot.service`, см. INSTALL.md, шаг 5.2). Очистите строку — посты пойдут в канал сразу.

Автозапуск 10 раз в день (каждые ~1,5 ч с 08:30 до 22:00 МСК + случайно до 20 мин):

```bash
sudo cp deploy/tgchannel.service deploy/tgchannel.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tgchannel.timer
systemctl list-timers tgchannel.timer   # когда следующий запуск
journalctl -u tgchannel -n 50           # логи
```

Время и количество постов меняются в `deploy/tgchannel.timer` (строки `OnCalendar`).

## Несколько каналов

Одна установка ведёт сколько угодно каналов: `python -m tgchannel new-channel news --preset news`,
настройки в `channels/news/`, расписание `tgchannel@news.timer`. Подробно — INSTALL.md, часть 6.

## Ограничения (честно)

- Читаются только **публичные** каналы с включённым веб-превью. Если Telegram поменяет разметку страницы,
  чтение перестанет находить посты (`check` покажет 0) — разбор придётся поправить.
- Модель может ошибиться в фактах, хотя ей запрещено выдумывать. Режим `TG_PREVIEW_CHAT` — способ это контролировать.
- Картинки и мемы из чужих каналов — чужой контент. Пересказ новости своими словами обычно не проблема,
  а вот на картинки могут пожаловаться правообладатели; Telegram по жалобе может удалить пост.

## Разработка

```bash
pip install -r requirements-dev.txt
python -m pytest
```
