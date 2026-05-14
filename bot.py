import asyncio
import os
import random
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
    "/unsubscribe - Отписаться от уведомлений\n\n"
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
BASE_DELAY = int(os.getenv("BASE_DELAY", 30))      # секунд между обычными проверками
MAX_DELAY = int(os.getenv("MAX_DELAY", 300))       # максимум при 429 (5 минут)

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
prev_available = None      # предыдущее количество билетов
current_delay = BASE_DELAY # текущая задержка между проверками (может расти при 429)

# Заголовки, чтобы имитировать браузер
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
    "Referer": "https://afisha.yandex.ru/",
    "Origin": "https://afisha.yandex.ru"
}

 # Счётчики запросов
request_count = 0          # всего запросов
success_count = 0          # успешных (с данными)
error_429_count = 0        # ошибок 429
error_other_count = 0      # других ошибок
command_start = 0
command_status = 0
command_delay = 0
command_check = 0
command_help = 0
command_stats =0
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
            await send_telegram_message(f"🎟️ Начальный мониторинг билетов.\nДоступно: {available}")
        else:
            if available > prev_available:
                diff = available - prev_available
                if diff > 100:
                    msg = (f"🎫 **НОВЫЕ БИЛЕТЫ!**\n"
                        f"Было: {prev_available}\n"
                        f"Стало: {available}\n"
                        f"Прибавилось: +{diff}\n"
                        f"Скорее сюда: [Купить билет](https://afisha.yandex.ru/moscow/sport/football-superfinal-fonbet-kubka-rossii)")
                    await send_telegram_message(msg)
                    print(f"[{datetime.now()}] УВЕДОМЛЕНИЕ: {prev_available} -> {available} (+{diff})")
                elif available < prev_available:
                    print(f"[{datetime.now()}] Билетов стало меньше: {prev_available} -> {available} (-{prev_available - available})")
                else:
                    print(f"[{datetime.now()}] Без изменений: {available}")

        prev_available = available

        # Небольшая случайная задержка, чтобы не быть роботом
        jitter = random.uniform(0.8, 1.2)
        sleep_time = current_delay * jitter
        await asyncio.sleep(sleep_time)

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
    global command_start, command_status, command_delay, command_check, command_help, command_stats
    command_stats += 1
    total_commands = command_start + command_status + command_delay + command_check + command_help + command_stats
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
    global subscribers
    subscribers = load_subscribers()  # ← загружаем при старте
    print(f"✅ Загружено {len(subscribers)} подписчиков")

    asyncio.create_task(periodic_check())
    # Запускаем бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())