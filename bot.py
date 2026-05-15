import asyncio
import os
import random
import json
from datetime import datetime

from aiogram.client.session import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message
from aiogram.filters import Command

HELP_TEXT = (
    "🤖 *Доступные команды бота:*\n\n"
    "/start - Запустить бота и получить приветствие\n"
    "/status - Показать последнее известное количество билетов (из кэша)\n"
    "/check - Запросить актуальное количество билетов прямо сейчас (живой запрос)\n"
    "/delay - Показать текущую паузу между автоматическими проверками\n"
    "/help - Показать это сообщение\n"
    "/subscribe - Подписаться на уведомления о новых билетах\n"
    "/unsubscribe - Отписаться от уведомлений\n"
    "/set\\_threshold <число>  - Установить свой порог разницы (по умолчанию 100) \n"
    "/show\\_threshold - Показать текущий порог\n"
    "/id - Показывает id пользовтеля\n\n"
)

# Загружаем переменные из .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID"))
SESSION_TOKEN = os.getenv("SESSION_TOKEN")
CLIENT_KEY = os.getenv("CLIENT_KEY")

PROXY_URL = os.getenv("PROXY_URL")

# Проверка обязательных переменных
if not all([BOT_TOKEN, CHAT_ID, SESSION_TOKEN, CLIENT_KEY]):
    raise ValueError("❌ Проверьте .env: нужны BOT_TOKEN, CHAT_ID, SESSION_TOKEN, CLIENT_KEY")

# Настройки задержек (можно переопределить через .env)
BASE_DELAY = int(os.getenv("BASE_DELAY", 30))  # секунд между обычными проверками
MAX_DELAY = int(os.getenv("MAX_DELAY", 300))  # максимум при 429 (5 минут)

# Собираем URL для API виджета
API_URL = f"https://widget.afisha.yandex.ru/api/tickets/v1/sessions/{SESSION_TOKEN}/hallplan/async?clientKey={CLIENT_KEY}"

# Создаём бота и диспетчер
# bot = Bot(token=BOT_TOKEN)


# --- НАСТРОЙКА ПРОКСИ ДЛЯ БОТА ---
if PROXY_URL:
    print(f"Использую прокси: {PROXY_URL}")
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=BOT_TOKEN, session=session)
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()

# Глобальные переменные для состояния
prev_available = None  # предыдущее количество билетов
current_delay = BASE_DELAY  # текущая задержка между проверками (может расти при 429)

# Заголовки, чтобы имитировать браузер
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
    "Referer": "https://afisha.yandex.ru/",
    "Origin": "https://afisha.yandex.ru"
}

# Счётчики запросов
request_count = 0  # всего запросов
success_count = 0  # успешных (с данными)
error_429_count = 0  # ошибок 429
error_other_count = 0  # других ошибок
command_start = 0
command_status = 0
command_delay = 0
command_check = 0
command_help = 0
command_stats = 0
command_set_threshold = 0
command_show_threshold = 0
last_log_time = datetime.now()


async def get_available_tickets():
    global request_count, success_count, error_429_count, error_other_count
    request_count += 1

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL, headers=HEADERS) as resp:
                if resp.status == 429:
                    error_429_count += 1
                    return 429
                if resp.status != 200:
                    error_other_count += 1
                    print(f"[{datetime.now()}] HTTP {resp.status}")
                    return None
                data = await resp.json()
                # Проверяем статус ответа
                if data.get("status") != "success":
                    error_other_count += 1
                    print(f"[{datetime.now()}] API status not success: {data}")
                    return None
                result = data.get("result", {})
                # Если есть hallplan - берём availableSeatCount
                if "hallplan" in result:
                    success_count += 1
                    return result["hallplan"].get("availableSeatCount", 0)
                # Если saleStatus == "no-seats" - билетов нет
                if result.get("saleStatus") == "no-seats":
                    success_count += 1
                    return 0
                error_other_count += 1
                # Другие случаи (например, "not_available") - тоже 0
                print(f"[{datetime.now()}] Неизвестная структура ответа: {data}")
                return 0
    except Exception as e:
        error_other_count += 1
        print(f"[{datetime.now()}] Ошибка запроса: {e}")
        return None


async def send_telegram_message(msg_text: str):
    """Отправляет сообщение всем подписчикам"""
    failed_ids = []
    for chat_id in subscribers:
        try:
            await bot.send_message(chat_id, msg_text, parse_mode="Markdown")
        except Exception as e:
            print(f"[{datetime.now()}] Не удалось отправить {chat_id}: {e}")


async def notify_new_tickets(diff: int, prev_available: int, available: int):
    """Отправляет уведомление о новых билетах только тем подписчикам, у кого порог <= diff"""
    if not subscribers:
        return
    msg = (f"🎫 **НОВЫЕ БИЛЕТЫ!**\n"
           f"Было: {prev_available}\n"
           f"Стало: {available}\n"
           f"Прибавилось: +{diff}\n"
           f"👉 [Купить билет](https://afisha.yandex.ru/moscow/sport/football-superfinal-fonbet-kubka-rossii)")
    sent = 0
    for chat_id in subscribers:
        threshold = get_user_threshold(chat_id)
        if diff > threshold:
            try:
                await bot.send_message(chat_id, msg, parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.2)  # небольшая задержка, чтобы не спамить
            except Exception as e:
                print(f"Не удалось отправить {chat_id}: {e}")
    if sent:
        print(f"[{datetime.now()}] Уведомления отправлены {sent} подписчикам (новых билетов +{diff})")
    else:
        print(f"[{datetime.now()}] Новых билетов +{diff}, но ни один подписчик не превысил порог")


async def periodic_check():
    """Основной цикл проверки с адаптивной задержкой"""
    global prev_available, current_delay

    print(f"[{datetime.now()}] Бот запущен. API URL: {API_URL}")
    while True:
        available = await get_available_tickets()

        if available == 429:
            # Сервер говорит, что мы слишком быстрые – увеличиваем задержку
            current_delay = min(current_delay * 2, MAX_DELAY)
            print(f"[{datetime.now()}] Ошибка 429. Увеличиваю паузу до {current_delay} сек.")
            await asyncio.sleep(current_delay)
            continue
        elif available is None:
            # Другая ошибка – тоже увеличим паузу, но не так агрессивно
            current_delay = min(current_delay + 10, MAX_DELAY)
            print(f"[{datetime.now()}] Ошибка запроса. Пауза {current_delay} сек.")
            await asyncio.sleep(current_delay)
            continue

        # Успешный ответ – можно сбросить задержку к базовой (плавно)
        if current_delay > BASE_DELAY:
            current_delay = max(BASE_DELAY, current_delay // 2)
            print(f"[{datetime.now()}] Сброс задержки до {current_delay} сек.")

        # Инициализация предыдущего значения
        if prev_available is None:
            prev_available = available
            print(f"[{datetime.now()}] Начальное количество билетов: {available}")
        else:
            if available > prev_available:
                diff = available - prev_available
                if diff > 0:  # уведомления отправляем при любом положительном diff, но с проверкой порога внутри
                    msg = (f"🎫 **НОВЫЕ БИЛЕТЫ!**\n"
                           f"Было: {prev_available}\n"
                           f"Стало: {available}\n"
                           f"Прибавилось: +{diff}\n"
                           f"Скорее сюда: [Купить билет](https://afisha.yandex.ru/moscow/sport/football-superfinal-fonbet-kubka-rossii)")
                    await notify_new_tickets(diff, prev_available, available)
                    print(f"[{datetime.now()}] УВЕДОМЛЕНИЕ: {prev_available} -> {available} (+{diff})")
                elif available < prev_available:
                    print(
                        f"[{datetime.now()}] Билетов стало меньше: {prev_available} -> {available} (-{prev_available - available})")
                else:
                    print(f"[{datetime.now()}] Без изменений: {available}")

        prev_available = available

        # Небольшая случайная задержка, чтобы не быть роботом
        jitter = random.uniform(0.8, 1.2)
        sleep_time = current_delay * jitter
        await asyncio.sleep(sleep_time)


# --- Хранение порогов уведомлений ---
THRESHOLDS_FILE = "user_thresholds.json"
user_thresholds = {}  # {chat_id: threshold}


def load_thresholds():
    global user_thresholds
    if not os.path.exists(THRESHOLDS_FILE):
        user_thresholds = {}
        return
    try:
        with open(THRESHOLDS_FILE, "r", encoding="utf-8") as f:
            user_thresholds = {int(k): v for k, v in json.load(f).items()}
        print(f"✅ Загружены пороги для {len(user_thresholds)} пользователей")
    except Exception as e:
        print(f"Ошибка загрузки порогов: {e}")
        user_thresholds = {}


def save_thresholds():
    try:
        with open(THRESHOLDS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_thresholds, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения порогов: {e}")


def get_user_threshold(chat_id: int) -> int:
    """Возвращает порог пользователя, по умолчанию 100"""
    return user_thresholds.get(chat_id, 100)


def set_user_threshold(chat_id: int, value: int):
    user_thresholds[chat_id] = value
    save_thresholds()


SUBSCRIBERS_FILE = "subscribers.txt"
subscribers = set()


def load_subscribers():
    """Загружает список chat_id из файла"""
    if not os.path.exists(SUBSCRIBERS_FILE):
        return set()
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            ids = [line.strip() for line in f if line.strip().isdigit()]
            return set(map(int, ids))
    except Exception as e:
        print(f"Ошибка загрузки подписчиков: {e}")
        return set()


def save_subscribers():
    """Сохраняет список chat_id в файл"""
    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            for chat_id in subscribers:
                f.write(f"{chat_id}\n")
    except Exception as e:
        print(f"Ошибка сохранения подписчиков: {e}")


# ---------- Telegram команды ----------

@dp.message(Command("set_threshold"))
async def cmd_set_threshold(message: Message):
    global command_set_threshold
    command_set_threshold += 1

    await message.answer(
        "🔢 Отправьте команду в формате:\n"
        "`/set_threshold 50`\n\n"
        "Где `50` — нужное вам количество новых билетов",
        parse_mode="MarkdownV2"
    )

    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Использование: `/set_threshold <число>`\nПример: `/set_threshold 50`",
                             parse_mode="Markdown")
        print(f"[{datetime.now()}] Пользователь {message.chat.id} вызвал /set_threshold без аргумента")
        return
    try:
        new_threshold = int(args[1])
        if new_threshold < 1:
            await message.answer("⚠️ Порог должен быть больше 0.")
            print(f"[{datetime.now()}] Пользователь {message.chat.id} попытался установить порог <= 0")
            return
        chat_id = message.chat.id
        set_user_threshold(chat_id, new_threshold)
        await message.answer(
            f"✅ Ваш порог уведомлений установлен на **{new_threshold}**.\nУведомления будут приходить, когда новых билетов станет больше чем на {new_threshold}.",
            parse_mode="Markdown")
        print(f"[{datetime.now()}] Пользователь {message.chat.id} установил порог: {new_threshold}")
    except ValueError:
        await message.answer("❌ Нужно ввести целое число. Пример: `/set_threshold 50`", parse_mode="Markdown")
        print(f"[{datetime.now()}] Пользователь {message.chat.id} ввёл нечисловое значение в /set_threshold")


@dp.message(Command("show_threshold"))
async def cmd_show_threshold(message: Message):
    global command_show_threshold
    command_show_threshold += 1

    chat_id = message.chat.id
    threshold = get_user_threshold(chat_id)
    await message.answer(
        f"📊 Ваш текущий порог уведомлений: **{threshold}**\nУведомления приходят, когда новых билетов появляется больше чем на {threshold}.",
        parse_mode="Markdown")
    print(f"[{datetime.now()}] Пользователь {message.chat.id} проверил свой порог: {threshold}")


@dp.message(Command("id"))
async def cmd_myid(message: Message):
    await message.answer(
        f"🆔 Ваш Telegram ID:\n"
        f"`{message.chat.id}`",
        parse_mode="Markdown"
    )
    print(f"[{datetime.now()}] Пользователь {message.chat.id} вызвал /id")


@dp.message(Command("subscribe"))
async def subscribe(message: Message):
    if message.chat.id not in subscribers:
        subscribers.add(message.chat.id)
        save_subscribers()
        await message.answer("✅ Вы успешно подписаны на уведомления о новых билетах!")
    else:
        await message.answer("ℹ️ Вы уже подписаны!")


@dp.message(Command("unsubscribe"))
async def unsubscribe(message: Message):
    if message.chat.id in subscribers:
        subscribers.discard(message.chat.id)
        save_subscribers()
        await message.answer("❌ Вы отписаны от уведомлений.")
    else:
        await message.answer("ℹ️ Вы не были подписаны.")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    global request_count, success_count, error_429_count, error_other_count
    global command_start, command_status, command_delay, command_check, command_help, command_stats, command_set_threshold, command_show_threshold
    command_stats += 1
    total_commands = command_start + command_status + command_delay + command_check + command_help + command_stats + command_set_threshold + command_show_threshold
    success_rate = (
        f"{success_count / request_count * 100:.1f}%" if request_count > 0 else "Нет данных"
    )

    stats_text = (
        f"📊 *Статистика запросов к API:*\n\n"
        f"• Всего запросов: `{request_count}`\n"
        f"• Успешных: `{success_count}`\n"
        f"• Ошибок 429: `{error_429_count}`\n"
        f"• Других ошибок: `{error_other_count}`\n\n"
        f"• Успешность: `{success_rate}`\n\n"
        f"🎯 *Команды от пользователей:*\n\n"
        f"• /start: `{command_start}`\n"
        f"• /status: `{command_status}`\n"
        f"• /delay: `{command_delay}`\n"
        f"• /check: `{command_check}`\n"
        f"• /help: `{command_help}`\n"
        f"• /stats: `{command_stats}`\n"
        f"• /set_threshold: `{command_set_threshold}`\n"
        f"• /show_threshold: `{command_show_threshold}`\n"
        f"• Всего команд: `{total_commands}`"
    )
    await message.answer(stats_text, parse_mode="Markdown")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    global command_start
    command_start += 1
    await message.answer(HELP_TEXT, parse_mode="Markdown")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    global command_status
    command_status += 1
    if prev_available is None:
        await message.answer("ℹ️ Данные ещё не получены. Подождите немного.")
    else:
        await message.answer(f"📊 В данный момент доступно билетов: **{prev_available}**", parse_mode="Markdown")


@dp.message(Command("delay"))
async def cmd_delay(message: Message):
    global command_delay
    command_delay += 1
    await message.answer(f"⏱️ Текущая пауза между проверками: {current_delay:.1f} секунд")


@dp.message(Command("check"))
async def cmd_check(message: Message):
    global command_check
    command_check += 1
    await message.answer("🔄 Проверяю актуальное количество билетов...")
    available = await get_available_tickets()
    if available is None:
        await message.answer("❌ Не удалось получить данные. Возможно, ошибка сети или API.")
    elif available == 429:
        await message.answer("⚠️ Слишком частые запросы. Попробуйте позже.")
    else:
        # Обновляем глобальную переменную, чтобы кэш тоже обновился
        global prev_available
        prev_available = available
        await message.answer(f"🎟️ Прямо сейчас доступно билетов: **{available}**", parse_mode="Markdown")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    global command_help
    command_help += 1
    await message.answer(HELP_TEXT, parse_mode="Markdown")


# ---------- Запуск ----------
async def main():
    global subscribers, user_thresholds
    subscribers = load_subscribers()
    load_thresholds()
    print(f"✅ Загружено {len(subscribers)} подписчиков")

    asyncio.create_task(periodic_check())
    # Запускаем бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
