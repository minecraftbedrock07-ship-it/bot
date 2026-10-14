"""Telegram-бот: считает дни до Нового года.

Поведение:
  * отвечает только на команду /days (и /start, /ny, /newyear);
  * на все остальные сообщения молчит;
  * каждый день в 00:00 по Москве присылает отсчёт туда, где вызывали команду.

Чаты и темы для рассылки лежат в chats.json рядом с файлом. Формат:

    [
      {"chat_id": -1004396358982, "thread_id": 5},   <- группа + конкретная тема
      {"chat_id": -1009876543210},                   <- группа без темы
      {"chat_id": 42}                                <- личка
    ]

Можно просто написать /days внутри нужной темы — бот запомнит и чат, и тему.
Либо вписать thread_id руками (номер темы видно в ссылке на сообщение:
t.me/c/<группа>/<thread_id>/<номер сообщения>).


from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command
from aiogram.types import BotCommand, Message

TOKEN = os.getenv("BOT_TOKEN", "8312049847:AAEYqmAcRpvcr1sdpRM6o8o75iNdfeWsfuA")
HAPP_PROXY = "socks5://127.0.0.1:10808"  # локальный прокси Happ
MSK = timezone(timedelta(hours=3), "MSK")  # Москва = UTC+3, летнего времени нет
CHATS_FILE = Path(__file__).with_name("chats.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("newyear")

dp = Dispatcher()


def plural(number: int, one: str, few: str, many: str) -> str:
    n = abs(number) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def countdown_text(now: datetime | None = None) -> str:
    now = now or datetime.now(MSK)
    target = now.replace(year=now.year + 1, month=1, day=1,
                         hour=0, minute=0, second=0, microsecond=0)
    total = max(0, int((target - now).total_seconds()))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)

    if days == 0 and hours == 0:
        left = f"{minutes} мин {seconds} с"
    elif days == 0:
        left = f"{hours} ч {minutes} мин"
    else:
        left = f"{days} д {hours:02d}:{minutes:02d}:{seconds:02d}"

    # 31 декабря — праздник уже сегодня
    if (now.month, now.day) == (12, 31):
        hours_left = days * 24 + hours
        return (
            f"🎄 <b>Новый год сегодня!</b>\n\n"
            f"      <b>До полуночи {hours_left} ч {minutes:02d} мин</b>\n\n"
            f"🥂 Всё самое доброе — впереди!"
        )

    # 1 января — поздравляем и начинаем отсчёт до следующего
    if (now.month, now.day) == (1, 1):
        return (
            f"🎉 <b>С Новым годом!</b>\n\n"
            f"Пусть год будет добрым и спокойным.\n\n"
            f"⏱ До следующего: {days} {plural(days, 'день', 'дня', 'дней')}"
        )

    return (
        f"🎄 <b>До Нового года {target.year} осталось</b>\n\n"
        f"      <b>{days} {plural(days, 'день', 'дня', 'дней')}</b>\n\n"
        f"📅 {target:%d.%m.%Y}\n"
        f"⏱ {left}"
    )


# ─────────────────── чаты и темы для рассылки ────────────────────
def chats() -> list[tuple[int, int | None]]:
    """Читает chats.json -> список пар (chat_id, thread_id | None).

    Понимает и старый формат [-100123, 42], и новый со словарями.
    """
    try:
        raw = json.loads(CHATS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    result: list[tuple[int, int | None]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            if "chat_id" not in item:
                continue
            thread = item.get("thread_id")
            result.append((int(item["chat_id"]), int(thread) if thread is not None else None))
        else:
            result.append((int(item), None))
    return result


def save_chats(pairs: set[tuple[int, int | None]]) -> None:
    data = [
        {"chat_id": chat_id, **({"thread_id": thread} if thread is not None else {})}
        for chat_id, thread in sorted(pairs, key=lambda p: (p[0], p[1] or 0))
    ]
    CHATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def remember(chat_id: int, thread_id: int | None = None) -> None:
    """Запомнить чат и тему. Проблема с файлом не должна мешать ответу."""
    try:
        known = set(chats())
        if (chat_id, thread_id) not in known:
            known.add((chat_id, thread_id))
            save_chats(known)
    except OSError as exc:
        log.warning("Не смог сохранить %s: %s", CHATS_FILE, exc)


@dp.message(Command("start", "days", "ny", "newyear"))
async def cmd_days(message: Message) -> None:
    """Единственная реакция на сообщения — команда."""
    log.info("Команда из чата %s, тема %s", message.chat.id, message.message_thread_id or "нет")
    await message.answer(countdown_text())
    remember(message.chat.id, message.message_thread_id)


# ─────────────────────────── расписание ──────────────────────────
def next_midnight(now: datetime | None = None) -> datetime:
    """Ближайшие 00:00 по Москве."""
    now = now or datetime.now(MSK)
    target = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


async def push(bot: Bot) -> None:
    """Отправляет отсчёт во все чаты и темы из chats.json."""
    targets = chats()
    if not targets:
        log.warning("chats.json пуст: напиши боту /days или добавь чат вручную")
        return
    for chat_id, thread_id in targets:
        extra = {"message_thread_id": thread_id} if thread_id is not None else {}
        try:
            await bot.send_message(chat_id, countdown_text(), **extra)
            log.info("Отсчёт отправлен в %s, тема %s", chat_id, thread_id or "нет")
        except Exception as exc:
            log.warning("Не смог отправить в %s (тема %s): %s", chat_id, thread_id, exc)


# ──────────────────────── подключение к API ──────────────────────
def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Слушает ли кто-нибудь порт (для 10808 — запущен ли Happ)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def proxy_candidates() -> list[str | None]:
    """Как пробовать подключаться: сначала вероятный вариант, потом запасной."""
    if "SOCKS_PROXY" in os.environ:
        return [os.environ["SOCKS_PROXY"].strip() or None]

    parsed = urlparse(HAPP_PROXY)
    if port_open(parsed.hostname or "127.0.0.1", parsed.port or 1080):
        return [HAPP_PROXY, None]
    return [None, HAPP_PROXY]


async def connect() -> Bot:
    """Перебирает варианты подключения и возвращает рабочий Bot."""
    problems: list[str] = []

    for proxy in proxy_candidates():
        how = f"через прокси {proxy}" if proxy else "напрямую, без прокси"

        if proxy is None and not port_open("api.telegram.org", 443, timeout=5):
            problems.append("напрямую: соединение с api.telegram.org не проходит (режет провайдер)")
            continue

        session = AiohttpSession(proxy=proxy) if proxy else None
        bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            await bot.session.close()
            raise SystemExit(
                "Telegram отверг токен. Проверь TOKEN в начале файла — взять можно у @BotFather."
            )
        except Exception as exc:  # бывает ProxyConnectionError у socks-прокси
            problems.append(f"{how}: {type(exc).__name__}")
            await bot.session.close()
            continue

        log.info("Подключился %s как @%s", how, me.username)
        return bot

    raise SystemExit(
        "Не смог достучаться до Telegram:\n  " + "\n  ".join(problems) +
        "\n\nДома включи Happ (он даёт прокси 127.0.0.1:10808) и запусти снова."
    )

async def ban_user_by_reply(message:Message, bot:Bot):
    chat_id = message.chat.id
    target_user = message.reply_to_message.from_user
    target_id = target_user.id
    bot_user = await bot.get_me()
    if target_id == message.froun_user.id:
        await message.reply("ты чо дурачок сам себя банить")
        return
    elif target_id == bot_user.id:
        await message.reply("я не буду банить себя")
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
        await message.answer(
            f"Пользователь {target_user.full_name} (ID: {target_id}), успешно забанен"
        )
    except TelegramBadRequest as e:
        if "can't remove chat owner" in e.message:
            await message.reply("❌ Невозможно забанить создателя чата!")
        elif "not enough rights" in e.message:
            await message.reply("❌ У бота нет прав на блокировку пользователей или этот пользователь является администратором!")
        else:
            await message.reply(f"❌ Ошибка Telegram: {e.message}")
            
    except Exception as e:
        await message.reply(f"❌ Произошла непредвиденная ошибка: {e}")
        
async def main() -> None:
    bot = await connect()
    await bot.set_my_commands(
        [BotCommand(command="days", description="Сколько дней до Нового года")]
    )
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен. Рассылка в 00:00 МСК по списку: %s", chats() or "пока пусто")

    async def midnight_loop() -> None:
        while True:
            target = next_midnight()
            await asyncio.sleep(max((target - datetime.now(MSK)).total_seconds(), 0))
            await push(bot)
            await asyncio.sleep(60)  # чтобы не отправить дважды в одну минуту

    try:
        await asyncio.gather(dp.start_polling(bot), midnight_loop())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено")
