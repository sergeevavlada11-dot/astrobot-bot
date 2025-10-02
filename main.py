import os
import logging
import sqlite3
from datetime import datetime
import swisseph as swe
import pytz
from timezonefinder import TimezoneFinder
from geopy.geocoders import Nominatim
from datetime import timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.executor import start_webhook
from openai import OpenAI, OpenAIError

# ---------------------------------
# Logging
# ---------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astrobot-final")

# ---------------------------------
# Env
# ---------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # e.g. https://astrobot-xxx.onrender.com
WEBHOOK_PATH = "/"
WEBHOOK_URL = WEBHOOK_HOST
UNLOCK_CODE = os.getenv("UNLOCK_CODE", "ASTROVIP")
DB_PATH = os.getenv("DB_PATH", "astrobot.sqlite3")
PAY_URL = os.getenv("PAY_URL", "https://pay.example.com")

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 10000))

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not WEBHOOK_HOST:
    raise RuntimeError("Set TELEGRAM_TOKEN, OPENAI_API_KEY, WEBHOOK_HOST")

# ---------------------------------
# Init
# ---------------------------------
bot = Bot(token=TELEGRAM_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------
# DB helpers
# ---------------------------------
def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                paid INTEGER DEFAULT 0,
                free_used INTEGER DEFAULT 0,
                city TEXT,
                birth_date TEXT,
                birth_time TEXT,
                created_at TEXT
            );
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                sphere TEXT,
                subtopic TEXT,
                prompt TEXT,
                answer TEXT,
                created_at TEXT
            );
        """)
        con.commit()

def get_user(uid: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT user_id, paid, free_used, city, birth_date, birth_time FROM users WHERE user_id=?",
            (uid,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "paid": bool(row[1]),
            "free_used": bool(row[2]),
            "city": row[3],
            "birth_date": row[4],
            "birth_time": row[5],
        }

def ensure_user(uid: int):
    u = get_user(uid)
    if u:
        return u
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)",
            (uid, datetime.utcnow().isoformat())
        )
        con.commit()
    return get_user(uid)

def update_user(uid: int, **fields):
    if not fields:
        return
    cols = ",".join([f"{k}=?" for k in fields.keys()])
    vals = list(fields.values()) + [uid]
    with sqlite3.connect(DB_PATH) as con:
        con.execute(f"UPDATE users SET {cols} WHERE user_id=?", vals)
        con.commit()

def save_reading(uid: int, sphere: str, sub: str, prompt: str, answer: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO readings (user_id, sphere, subtopic, prompt, answer, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, sphere, sub, prompt, answer, datetime.utcnow().isoformat())
        )
        con.commit()

def delete_history(uid: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM readings WHERE user_id=?", (uid,))
        con.commit()

# ---------------------------------
# UI
# ---------------------------------
sphere_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🧬 Личность"), KeyboardButton("💰 Деньги")],
        [KeyboardButton("💼 Карьера"), KeyboardButton("❤️ Отношения")],
        [KeyboardButton("🌟 Предназначение")]
    ],
    resize_keyboard=True
)

sub_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("📖 Общее описание")],
        [KeyboardButton("🔮 Прогноз на 5 лет")],
        [KeyboardButton("🪷 Советы по гармонизации")],
        [KeyboardButton("⬅️ Назад к сферам")]
    ],
    resize_keyboard=True
)

help_kb = InlineKeyboardMarkup().add(
    InlineKeyboardButton(text="💳 Оформить доступ", url=PAY_URL)
)

# ---------------------------------
# State (in-memory)
# ---------------------------------
STATE_WAIT_CITY = "wait_city"
STATE_WAIT_DATE = "wait_date"
STATE_WAIT_TIME = "wait_time"
STATE_READY = "ready"

user_state = {}  # per-user ephemeral state

def set_state(uid, state): user_state[uid] = state
def get_state(uid): return user_state.get(uid)

def fmt_profile(u):
    return (
        f"📍 Город: {u.get('city','—')}\n"
        f"📅 Дата: {u.get('birth_date','—')}\n"
        f"⏰ Время: {u.get('birth_time','—')}"
    )

SPHERE_MAP = {
    "🧬 Личность": "Личность (сильные/ слабые стороны, описание человека)",
    "💰 Деньги": "Деньги (отношение к финансам и зоны максимального дохода)",
    "💼 Карьера": "Карьера (где лучше реализоваться и на что делать акцент)",
    "❤️ Отношения": "Отношения (какой человек в отношениях и возможная динамика пары)",
    "🌟 Предназначение": "Предназначение (в чем преуспеть и сильные таланты)"
}

SUB_MAP = {
    "📖 Общее описание": "общее описание",
    "🔮 Прогноз на 5 лет": "прогноз на 5 лет",
    "🪷 Советы по гармонизации": "советы по гармонизации"
}

main_topics = {
    "🧬 Личность": [
        "Основные архетипы личности",
        "Эмоциональная природа",
        "Сильные и слабые стороны",
        "Механизмы роста",
        "Практические рекомендации"
    ],
    "💰 Деньги": [
        "Денежное мышление",
        "Источник дохода",
        "Стиль обращения с ресурсами",
        "Кармические задачи денег",
        "Практические шаги"
    ],
    "💼 Карьера": [
        "Природный стиль работы",
        "Сильные стороны в карьере",
        "Оптимальные направления",
        "Кармические задачи работы",
        "Практические советы"
    ],
    "❤️ Отношения": [
        "Энергия любви",
        "Образ идеального партнёра",
        "Сценарий отношений",
        "Этапы эволюции любви",
        "Практические рекомендации"
    ],
    "🌟 Предназначение": [
        "Путь души",
        "Главные дары и таланты",
        "Уроки судьбы",
        "Векторы развития",
        "Практические шаги"
    ]
}

# ---------------------------------
# Helpers
# ---------------------------------
def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s.strip(), "%d.%m.%Y")
        return True
    except Exception:
        return False

def _valid_time(s: str) -> bool:
    try:
        datetime.strptime(s.strip(), "%H:%M")
        return True
    except Exception:
        return False

def is_blocked(u) -> bool:
    return (not u.get("paid")) and u.get("free_used")

async def guard_access(message: types.Message, u) -> bool:
    if is_blocked(u):
        await message.answer(
            "🔒 <b>Доступ ограничен</b>\n\n"
            "Ты уже использовала бесплатную консультацию.\n"
            "Чтобы открыть все разделы — введи <b>секретный код</b> для разблокировки."
        )
        return False
    return True

VALID_CODES = {
    "ASTRO-1F9A-2025",
    "ASTRO-2X4M-2025",
    "ASTRO-3L7P-2025",
    "ASTRO-4V2Q-2025",
    "ASTRO-5R8D-2025",
    "ASTRO-6H1Z-2025",
    "ASTRO-7T5B-2025",
    "ASTRO-8W3C-2025",
    "ASTRO-9N6J-2025",
    "ASTRO-10K2U-2025"
}
async def try_unlock(message):
    code = (message.text or "").strip()
    uid = message.from_user.id
    log.info(f"🔑 Проверка одноразового кода: {code} от пользователя {uid}")

    if code in VALID_CODES:
        VALID_CODES.remove(code)  # ❗️ Код сразу вычеркивается
        update_user(uid, paid=1, free_used=0)
        await message.answer(
            "✅ Доступ открыт! Теперь ты можешь пользоваться всеми разделами без ограничений 🎉",
            reply_markup=sphere_kb
        )
        log.info(f"🔓 Пользователь {uid} разблокирован одноразовым кодом {code}")
        return True

    elif code.upper() == "ASTROVIP":
        await message.answer("⚠️ Этот код устарел или уже использован. Обратись в поддержку 💁‍♀️")
        log.info(f"❌ Пользователь {uid} ввёл устаревший код ASTROVIP")
        return True

    return False

# =========================
# 🔭 Астрология: геокодинг, TZ, расчёт карты
# =========================

SIGN_NAMES = ["Овен", "Телец", "Близнецы", "Рак", "Лев", "Дева", "Весы", "Скорпион", "Стрелец", "Козерог", "Водолей", "Рыбы"]

# Инициализируем геокодер один раз (важно для Render)
_geolocator = Nominatim(user_agent="astrobot_v1")

def geocode_city(city: str):
    """
    Возвращает (lat, lon, display_name). Если не нашли — None.
    """
    try:
        loc = _geolocator.geocode(city, language="ru")
        if not loc:
            return None
        return (float(loc.latitude), float(loc.longitude), loc.address)
    except Exception:
        return None

def get_timezone_offset_hours(lat: float, lon: float, dt_naive_local_str: str, fmt="%d.%m.%Y %H:%M"):
    """
    Возвращает (смещение_в_часах, tzname) для координат и ЛОКАЛЬНОЙ даты/времени рождения.
    dt_naive_local_str: '20.05.1995 14:30'
    """
    tf = TimezoneFinder()
    tzname = tf.timezone_at(lat=lat, lng=lon)
    if not tzname:
        tzname = "Europe/Moscow"
    tz = pytz.timezone(tzname)

    # парсим локальную дату/время без TZ
    dt_local = datetime.strptime(dt_naive_local_str, fmt)
    # локализуем (как будто это местное время)
    dt_localized = tz.localize(dt_local, is_dst=None)
    # смещение от UTC в секундах
    offset_sec = dt_localized.utcoffset().total_seconds()
    return offset_sec / 3600.0, tzname

def _lon_to_sign(lon_deg: float):
    sign_index = int(lon_deg // 30) % 12
    return SIGN_NAMES[sign_index]

def calculate_chart_ddmmyyyy(city: str, date_str_ddmmyyyy: str, time_str_hhmm: str):
    """
    По городу + дате 'dd.mm.yyyy' + времени 'HH:MM' возвращает словарь:
    планеты (тропически), асцендент, MC, куспиды домов (Плацидус).
    """
    # 1) Гео
    geo = geocode_city(city)
    if not geo:
        lat, lon, display = 55.7558, 37.6173, "Москва, Россия (fallback)"
    else:
        lat, lon, display = geo

    # 2) TZ смещение для локального времени рождения
    dt_local_str = f"{date_str_ddmmyyyy} {time_str_hhmm}"
    offset_hours, tzname = get_timezone_offset_hours(lat, lon, dt_local_str)

    # 3) Переводим локальное время рождения в UTC
    dt_local = datetime.strptime(dt_local_str, "%d.%m.%Y %H:%M")
    dt_utc = dt_local - timedelta(hours=offset_hours)

    # 4) Юлианская дата по UTC
    jd = swe.julday(dt_utc.year, dt_utc.month, dt_utc.day, dt_utc.hour + dt_utc.minute/60.0)

    # 5) Планеты (тропически)
    planet_map = {
        swe.SUN: "Солнце",
        swe.MOON: "Луна",
        swe.MERCURY: "Меркурий",
        swe.VENUS: "Венера",
        swe.MARS: "Марс",
        swe.JUPITER: "Юпитер",
        swe.SATURN: "Сатурн",
        swe.TRUE_NODE: "Раху",
    }

    planets = {}
    for pl_id, name in planet_map.items():
        lon, latp, dist, speed = swe.calc_ut(jd, pl_id)  # тропика по умолчанию
        planets[name] = {"lon": lon, "sign": _lon_to_sign(lon)}

    # Кету = Раху + 180°
    if "Раху" in planets:
        ketu_lon = (planets["Раху"]["lon"] + 180.0) % 360.0
        planets["Кету"] = {"lon": ketu_lon, "sign": _lon_to_sign(ketu_lon)}

    # 6) Дома (Плацидус)
    houses, ascmc = swe.houses(jd, lat, lon)
    asc = ascmc[0]
    mc = ascmc[1]

    asc_sign = _lon_to_sign(asc)
    house_cusps = {f"Дом {i+1}": {"lon": houses[i], "sign": _lon_to_sign(houses[i])} for i in range(12)}

    return {
        "city_resolved": display,
        "tzname": tzname,
        "utc_offset_hours": offset_hours,
        "ascendant": {"lon": asc, "sign": asc_sign},
        "midheaven": {"lon": mc, "sign": _lon_to_sign(mc)},
        "houses": house_cusps,
        "planets": planets,
    }

def chart_to_text(chart: dict) -> str:
    """
    Текст для промпта: кратко и по делу.
    """
    parts = []
    parts.append(f"Город (геокод): {chart['city_resolved']}")
    parts.append(f"Часовой пояс: {chart['tzname']} (UTC{chart['utc_offset_hours']:+.0f})")
    parts.append(f"Асцендент: {chart['ascendant']['sign']} ({chart['ascendant']['lon']:.2f}°)")
    parts.append(f"MC: {chart['midheaven']['sign']} ({chart['midheaven']['lon']:.2f}°)")

    parts.append("\nПланеты:")
    for name, data in chart["planets"].items():
        parts.append(f"- {name}: {data['sign']} ({data['lon']:.2f}°)")

    parts.append("\nКуспиды домов:")
    for hname, data in chart["houses"].items():
        parts.append(f"- {hname}: {data['sign']} ({data['lon']:.2f}°)")

    return "\n".join(parts)

# ---------------------------------
# Commands
# ---------------------------------
@dp.message_handler(commands=["help"])
async def cmd_help(message: types.Message):
    db_init(); ensure_user(message.from_user.id)
    text = (
        "✨ <b>Что я умею</b>\n"
        "• Сохраняю твои данные рождения (город, дата, время)\n"
        "• Помогаю разобраться в сферах: Личность, Деньги, Карьера, Отношения, Предназначение\n"
        "• В каждой сфере: общее описание, прогноз на 5 лет, советы по гармонизации\n\n"
        "🔎 После ввода данных жми нужную сферу — и я подготовлю персональный разбор 💫"
    )
    await message.answer(text, reply_markup=help_kb)

@dp.message_handler(commands=["reset"])
async def cmd_reset(message: types.Message):
    db_init()
    u = ensure_user(message.from_user.id)
    if not u.get("paid"):
        await message.answer("🔒 Команда доступна только пользователям с полным доступом.")
        return
    delete_history(u["user_id"])  # частичный сброс — только история
    await message.answer("🧹 История очищена ✅")
    await message.answer("Выбери сферу ⤵️", reply_markup=sphere_kb)

@dp.message_handler(commands=["start", "restart"])
async def cmd_start(message: types.Message):
    db_init()
    u = ensure_user(message.from_user.id)
    set_state(u["user_id"], STATE_WAIT_CITY)
    await message.answer(
        "Привет 🌌 Я твой астробот-подруга (@TheAstrology_bot)!\n"
        "Сначала соберём данные рождения.\n\n"
        "🧭 Напиши <b>город</b> рождения (например: Москва)\n\n"
        "ℹ️ В любой момент можно ввести секретный код для разблокировки (если у тебя есть)."
    )

# ---------------------------------
# Data collection
# ---------------------------------
@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_WAIT_CITY)
async def ask_date(message: types.Message):
    if await try_unlock(message): 
        return

    city = (message.text or "").strip()
    if len(city) < 2:
        await message.answer("Хм, коротко. Напиши город полностью 🏙️")
        return

    u = ensure_user(message.from_user.id)
    update_user(u["user_id"], city=city)

    set_state(u["user_id"], STATE_WAIT_DATE)
    log.info(f"📍 STATE_WAIT_DATE установлен для {u['user_id']}")

    await message.answer("Отлично! ✨ Теперь пришли дату рождения <b>дд.мм.гггг</b>\nНапример: 15.07.1995")

@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_WAIT_DATE)
async def ask_time(message: types.Message):
    log.info(f"📆 Вошёл в ask_time. Текущее состояние: {get_state(message.from_user.id)}")

    if await try_unlock(message): 
        return

    date = (message.text or "").strip()
    if not _valid_date(date):
        await message.answer("Формат другой 🤔 Нужно: <b>дд.мм.гггг</b>\nПример: 03.11.1998")
        return

    u = ensure_user(message.from_user.id)
    update_user(u["user_id"], birth_date=date)

    set_state(u["user_id"], STATE_WAIT_TIME)
    log.info(f"⏱ STATE_WAIT_TIME установлен для {u['user_id']}")

    await message.answer("Супер! 🕰️ Теперь пришли время рождения <b>чч:мм</b>\nЕсли не знаешь — напиши <i>не знаю</i>")

# -----------------------
# 🔧 Форматирование ответа
# -----------------------
def format_answer(text: str) -> str:
    """
    Форматирует текст ответа:
    - убирает ### заголовки
    - заменяет их на жирный стиль
    """
    import re
    # ### Заголовки → жирный текст
    text = re.sub(r"^### (.+)$", r"**\1**", text, flags=re.MULTILINE)
    return text.strip()
    
@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_WAIT_TIME)
async def ready_menu(message: types.Message):
    if await try_unlock(message): 
        return

    t = (message.text or "").strip().lower()
    u = ensure_user(message.from_user.id)

    if t == "не знаю":
        update_user(u["user_id"], birth_time="неизвестно")
    else:
        if not _valid_time(t):
            await message.answer("Нужно <b>чч:мм</b> (например, 14:30) ⏰\nИли напиши <i>не знаю</i>")
            return
        update_user(u["user_id"], birth_time=t)

    set_state(u["user_id"], STATE_READY)
    log.info(f"✅ Пользователь {u['user_id']} готов, состояние: STATE_READY")
    u = get_user(u["user_id"])

    # Вариант 2: сначала резюме, потом меню
    await message.answer(
        "Отлично! Данные сохранены ✅\n\n"
        f"{fmt_profile(u)}\n\n"
        "Теперь выбери сферу ⤵️",
        reply_markup=sphere_kb
    )

# ---------------------------------
# Flow: pick sphere/subtopic
# ---------------------------------
@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_READY and m.text in SPHERE_MAP.keys())
async def pick_subtopic(message: types.Message):
    # 🔑 Проверка на код
    if await try_unlock(message):
        return

    u = ensure_user(message.from_user.id)
    if not await guard_access(message, u):
        return

    user_state[f"last_sphere_{message.from_user.id}"] = message.text
    await message.answer(
        f"Ты выбрала: <b>{message.text}</b> 💫\nТеперь выбери формат разбора:",
        reply_markup=sub_kb
    )

@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_READY and m.text == "⬅️ Назад к сферам")
async def back_to_spheres(message: types.Message):
    u = ensure_user(message.from_user.id)
    if await try_unlock(message): 
        return
    if not await guard_access(message, u): 
        return
    await message.answer("Выбери сферу ⤵️", reply_markup=sphere_kb)

# ---------------------------------
# Final generate (GPT-4)
# ---------------------------------
@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_READY and m.text in SUB_MAP.keys())
async def final_generate(message: types.Message):
    # 🔑 Разблокировка кодом (если ввели код вместо кнопки)
    if await try_unlock(message):
        return

    uid = message.from_user.id
    u = ensure_user(uid)
    if not await guard_access(message, u):
        return

    # Какая сфера выбрана ранее (сохранялась в pick_subtopic)
    sphere = user_state.get(f"last_sphere_{uid}")
    if not sphere:
        await message.answer("Сначала выбери сферу ⤵️", reply_markup=sphere_kb)
        return

    # Текущая подтема — это текущий текст сообщения (кнопка из SUB_MAP)
    sub = message.text

    # Профиль пользователя для подстановки в промпт
    birth = get_user(uid) or {}
    birth_text = (
        f"📍 Город: {birth.get('city', '—')}\n"
        f"📅 Дата: {birth.get('birth_date', '—')}\n"
        f"⏰ Время: {birth.get('birth_time', '—')}"
    )

    sphere_text = SPHERE_MAP.get(sphere, sphere)
    sub_text = SUB_MAP.get(sub, sub)

    # ====== 🪐 АСТРО-КАРТА из введённых данных ======
    city_for_calc = birth.get("city") or "Москва"
    date_for_calc = birth.get("birth_date") or "01.01.2000"
    time_for_calc = birth.get("birth_time") or "12:00"
    if "неизвест" in time_for_calc.lower():
        time_for_calc = "12:00"  # разумный дефолт при неизвестном времени

    try:
        chart = calculate_chart_ddmmyyyy(city_for_calc, date_for_calc, time_for_calc)
        astro_block = chart_to_text(chart)

        # ✅ Вот здесь вставляем лог, чтобы проверить, что карта реально сформировалась:
        log.info(f"🪐 AstroBlock:\n{astro_block}")

    except Exception:
        log.exception("Astro calc error")
        astro_block = "Астрологические расчёты недоступны. Используй общий психологический анализ по данным пользователя."

    # -----------------------
    # 🔮 Выбираем PROMPT по сфере
    # -----------------------
    if sphere == "🧬 Личность":
        prompt = (
            "Ты — опытный ведический астролог-консультант (джйотиш), делающий глубокие персональные разборы. "
            "Твоя задача — подготовить развёрнутую, детализированную консультацию по личности человека, как на индивидуальной встрече.\n\n"
            "📌 Формат и стиль:\n"
            "- Пиши подробно и глубоко, с уважительным, тёплым тоном.\n"
            "- Используй термины ведической астрологии (Лагна, грахи, накшатры, даша и т.д.), но поясняй их простыми словами.\n"
            "- Структурируй ответ по пунктам и подзаголовкам, как в профессиональном астрологическом отчёте.\n"
            "- Эмодзи можно использовать, но не больше 1–2 на раздел.\n"
            "- Каждую часть обязательно заверши практическими рекомендациями (ритуалы, мантры, дни недели, привычки и т.д.).\n"
            "- Избегай фатализма. Всегда подчёркивай свободу воли и потенциал для роста.\n\n"
            "📊 Структура ответа (обязательно соблюдай):\n"
            "1. Основные архетипы личности — Лагна и управитель: проявление личности, стиль самовыражения, главные качества.\n"
            "2. Эмоциональная природа — положение Луны: эмоциональные реакции, внутренние потребности, привязанности.\n"
            "3. Сильные и слабые стороны — что поддерживает развитие, а что мешает.\n"
            "4. Механизмы роста — через какие задачи и испытания происходит эволюция.\n"
            "5. Практические рекомендации — конкретные шаги, мантры, советы для гармонизации.\n"
            "6. Итог — собери всё в единый вывод, который даёт целостное понимание потенциала человека.\n\n"
            "📜 Исходные данные:\n"
            f"{birth_text}\n\n"
            "📊 Индивидуальные астрологические данные:\n"
            f"{astro_block}\n\n"
            f"Сфера анализа: {sphere_text}\n"
            f"Подтема: {sub_text}\n\n"
            "⚠️ ВАЖНО: Ты обязан использовать именно приведённые положения планет, асцендента, домов и управителей из блока выше. "
            "Не используй общие формулировки и не пиши, что данных не хватает. "
            "Делай анализ строго по этим данным: указывай, в каком знаке стоит каждая планета, какие дома активны, "
            "как их управители влияют на сферу жизни. "
            "Структурируй текст как консультацию: отдельные разделы, конкретные выводы, прогнозы, советы. "
            "Не используй форматирование с `**` или `###`."
            "Не используй заголовки с решётками или звёздочками, форматируй текст простыми разделами с подзаголовками и абзацами."
            "📌 Цель: Подготовь глубокий индивидуальный разбор объёмом не менее 3000 символов. "
            "Каждый раздел должен содержать конкретную интерпретацию именно для этого человека на основе его натальной карты. "
            "Не используй общих фраз и теоретических описаний. Пиши так, как если бы ты давал консультацию клиенту лично, анализируя его реальные положения."
        )

    elif sphere == "💰 Деньги":
        prompt = (
            "Ты — опытный ведический астролог-консультант (джйотиш), делающий глубокие персональные финансовые разборы. "
            "Твоя задача — подготовить развёрнутый и детализированный анализ денежного потенциала человека.\n\n"
            "📌 Формат и стиль:\n"
            "- Пиши глубоко и развёрнуто, как на личной консультации, но понятным языком.\n"
            "- Используй ведические термины (2-й дом, 11-й дом, Дхана йога, Лакшми-йога и т.д.), объясняя их простыми словами.\n"
            "- Минимум эмодзи, максимум смысла. Стиль — профессиональный, но тёплый.\n"
            "- Обязательно добавляй практические рекомендации (финансовые ритуалы, мантры, дни силы, советы по привычкам).\n"
            "- Не давай фаталистичных прогнозов. Всегда подчёркивай потенциал и возможности роста.\n\n"
            "📊 Структура разбора:\n"
            "1. Денежное мышление — что показывает карта о вашем отношении к деньгам, установках и глубинных убеждениях.\n"
            "2. Источник дохода — через какие сферы, способности и стратегии вы зарабатываете лучше всего.\n"
            "3. Управление ресурсами — стиль обращения с деньгами, вложения, накопления, подход к рискам.\n"
            "4. Кармические задачи и уроки денег — чему вы учитесь через материю, какие паттерны важно осознать.\n"
            "5. Возможности роста и финансовые стратегии — как раскрыть денежный потенциал, какие шаги помогут увеличить поток.\n"
            "6. Практические рекомендации — мантры, периоды, ритуалы, дни недели и конкретные шаги.\n"
            "7. Итог — собери всё в целостную картину финансовой реализации.\n\n"
           "📜 Исходные данные:\n"
            f"{birth_text}\n\n"
            "📊 Индивидуальные астрологические данные:\n"
            f"{astro_block}\n\n"
            f"Сфера анализа: {sphere_text}\n"
            f"Подтема: {sub_text}\n\n"
            "⚠️ ВАЖНО: Ты обязан использовать именно приведённые положения планет, асцендента, домов и управителей из блока выше. "
            "Не используй общие формулировки и не пиши, что данных не хватает. "
            "Делай анализ строго по этим данным: указывай, в каком знаке стоит каждая планета, какие дома активны, "
            "как их управители влияют на сферу жизни. "
            "Структурируй текст как консультацию: отдельные разделы, конкретные выводы, прогнозы, советы. "
            "Не используй форматирование с `**` или `###`."
            "Не используй заголовки с решётками или звёздочками, форматируй текст простыми разделами с подзаголовками и абзацами."
            "📌 Цель: Подготовь глубокий индивидуальный разбор объёмом не менее 3000 символов. "
            "Каждый раздел должен содержать конкретную интерпретацию именно для этого человека на основе его натальной карты. "
            "Не используй общих фраз и теоретических описаний. Пиши так, как если бы ты давал консультацию клиенту лично, анализируя его реальные положения."
        )

    elif sphere == "❤️ Отношения":
        prompt = (
            "Ты — опытный ведический астролог-консультант (джйотиш), делающий глубокие консультации по отношениям и партнёрству. "
            "Твоя задача — провести развёрнутый анализ эмоциональной и романтической сферы жизни человека.\n\n"
            "📌 Формат и стиль:\n"
            "- Пиши мягко и уважительно, но глубоко и профессионально.\n"
            "- Используй термины ведической астрологии (7-й дом, Венера, Луна, Карака отношений и т.д.), поясняя их простыми словами.\n"
            "- Структурируй текст по пунктам, как личный психологический и астрологический разбор.\n"
            "- Эмодзи можно использовать, но не больше 1–2 на раздел.\n"
            "- Каждую часть заверши практическими советами (ритуалы, способы гармонизации, внутренние практики).\n"
            "- Избегай фатализма. Всегда подчёркивай возможность развития и осознанного выбора.\n\n"
            "📊 Структура ответа (обязательно соблюдай):\n"
            "1. Энергия любви и стиль проявления чувств — через Венеру, Луну и 5-й дом.\n"
            "2. Образ идеального партнёра — качества, к которым вы стремитесь в союзе.\n"
            "3. Сценарий отношений — как вы строите связи, как проявляются привязанности и ожидания.\n"
            "4. Этапы эволюции любви — как будут развиваться отношения со временем.\n"
            "5. Кармические уроки партнёрства — чему вы учитесь через любовь и взаимодействие.\n"
            "6. Практические рекомендации — советы для гармонизации, мантры, периоды благоприятных отношений.\n"
            "7. Итог — общее понимание вашей любовной динамики и потенциала партнёрства.\n\n"
          "📜 Исходные данные:\n"
            f"{birth_text}\n\n"
            "📊 Индивидуальные астрологические данные:\n"
            f"{astro_block}\n\n"
            f"Сфера анализа: {sphere_text}\n"
            f"Подтема: {sub_text}\n\n"
            "⚠️ ВАЖНО: Ты обязан использовать именно приведённые положения планет, асцендента, домов и управителей из блока выше. "
            "Не используй общие формулировки и не пиши, что данных не хватает. "
            "Делай анализ строго по этим данным: указывай, в каком знаке стоит каждая планета, какие дома активны, "
            "как их управители влияют на сферу жизни. "
            "Структурируй текст как консультацию: отдельные разделы, конкретные выводы, прогнозы, советы. "
            "Не используй форматирование с `**` или `###`."
            "Не используй заголовки с решётками или звёздочками, форматируй текст простыми разделами с подзаголовками и абзацами."
            "📌 Цель: Подготовь глубокий индивидуальный разбор объёмом не менее 3000 символов. "
            "Каждый раздел должен содержать конкретную интерпретацию именно для этого человека на основе его натальной карты. "
            "Не используй общих фраз и теоретических описаний. Пиши так, как если бы ты давал консультацию клиенту лично, анализируя его реальные положения."
        )

    elif sphere == "💼 Карьера":
        prompt = (
            "Ты — опытный ведический астролог-консультант (джйотиш), делающий глубокие профессиональные разборы. "
            "Твоя задача — подготовить развёрнутую консультацию о профессиональном пути человека, его потенциале, задачах и стратегиях успеха.\n\n"
            "📌 Формат и стиль:\n"
            "- Пиши подробно, как на личной консультации, с тёплым и уважительным тоном.\n"
            "- Используй термины ведической астрологии (10-й дом, 6-й дом, даши, йоги, караки и т.д.), но поясняй их простыми словами.\n"
            "- Структурируй текст чётко и логично, с подзаголовками и анализом.\n"
            "- Эмодзи можно использовать, но не больше 1–2 на раздел.\n"
            "- Каждую часть заверши практическими рекомендациями (стратегии развития, периоды активности, ритуалы и т.д.).\n"
            "- Избегай фатализма. Покажи не только предрасположенности, но и возможности роста.\n\n"
            "📊 Структура ответа (обязательно соблюдай):\n"
            "1. Природный стиль работы — через Лагну, Солнце и 10-й дом: как человек проявляется в профессии.\n"
            "2. Сильные стороны и таланты — какие качества и способности поддерживают профессиональный рост.\n"
            "3. Оптимальные направления — в каких сферах человек реализуется лучше всего.\n"
            "4. Кармические задачи и вызовы — какие уроки связаны с работой и служением.\n"
            "5. Этапы карьерной эволюции — как будет меняться путь во времени (например, через махадаши).\n"
            "6. Практические рекомендации — стратегии успеха, подходящие периоды, действия для раскрытия потенциала.\n"
            "7. Итог — общий вывод о предназначении в карьере и миссии через работу.\n\n"
          "📜 Исходные данные:\n"
            f"{birth_text}\n\n"
            "📊 Индивидуальные астрологические данные:\n"
            f"{astro_block}\n\n"
            f"Сфера анализа: {sphere_text}\n"
            f"Подтема: {sub_text}\n\n"
            "⚠️ ВАЖНО: Ты обязан использовать именно приведённые положения планет, асцендента, домов и управителей из блока выше. "
            "Не используй общие формулировки и не пиши, что данных не хватает. "
            "Делай анализ строго по этим данным: указывай, в каком знаке стоит каждая планета, какие дома активны, "
            "как их управители влияют на сферу жизни. "
            "Структурируй текст как консультацию: отдельные разделы, конкретные выводы, прогнозы, советы. "
            "Не используй форматирование с `**` или `###`."
            "Не используй заголовки с решётками или звёздочками, форматируй текст простыми разделами с подзаголовками и абзацами."
            "📌 Цель: Подготовь глубокий индивидуальный разбор объёмом не менее 3000 символов. "
            "Каждый раздел должен содержать конкретную интерпретацию именно для этого человека на основе его натальной карты. "
            "Не используй общих фраз и теоретических описаний. Пиши так, как если бы ты давал консультацию клиенту лично, анализируя его реальные положения."
        )
        
    elif sphere == "🌟 Предназначение":
        prompt = (
            "Ты — опытный ведический астролог-консультант (джйотиш), делающий глубокие консультации о миссии души и пути развития. "
            "Твоя задача — подготовить развёрнутый анализ предназначения человека, его высших целей и кармических задач.\n\n"
            "📌 Формат и стиль:\n"
            "- Пиши вдохновляюще, философски и глубоко, но ясно и доступно.\n"
            "- Используй термины ведической астрологии (Раху, Кету, 9-й дом, 12-й дом, караки и т.д.), объясняя их простыми словами.\n"
            "- Структурируй текст как целостный путь развития души.\n"
            "- Эмодзи можно использовать, но не больше 1–2 на раздел.\n"
            "- Заверши каждую часть практическими советами и рекомендациями для раскрытия потенциала.\n"
            "- Подчёркивай свободу выбора и возможности роста.\n\n"
            "📊 Структура ответа (обязательно соблюдай):\n"
            "1. Путь души — глобальные задачи и смысл воплощения.\n"
            "2. Главные дары и таланты — потенциал, с которым человек пришёл в этот мир.\n"
            "3. Кармические уроки — задачи, которые предстоит пройти для роста.\n"
            "4. Векторы развития — направления, которые приведут к реализации миссии.\n"
            "5. Практические шаги — конкретные действия, практики, мантры, рекомендации.\n"
            "6. Итог — целостный вывод о предназначении и пути развития.\n\n"
          "📜 Исходные данные:\n"
            f"{birth_text}\n\n"
            "📊 Индивидуальные астрологические данные:\n"
            f"{astro_block}\n\n"
            f"Сфера анализа: {sphere_text}\n"
            f"Подтема: {sub_text}\n\n"
            "⚠️ ВАЖНО: Ты обязан использовать именно приведённые положения планет, асцендента, домов и управителей из блока выше. "
            "Не используй общие формулировки и не пиши, что данных не хватает. "
            "Делай анализ строго по этим данным: указывай, в каком знаке стоит каждая планета, какие дома активны, "
            "как их управители влияют на сферу жизни. "
            "Структурируй текст как консультацию: отдельные разделы, конкретные выводы, прогнозы, советы. "
            "Не используй форматирование с `**` или `###`."
            "Не используй заголовки с решётками или звёздочками, форматируй текст простыми разделами с подзаголовками и абзацами."
            "📌 Цель: Подготовь глубокий индивидуальный разбор объёмом не менее 3000 символов. "
            "Каждый раздел должен содержать конкретную интерпретацию именно для этого человека на основе его натальной карты. "
            "Не используй общих фраз и теоретических описаний. Пиши так, как если бы ты давал консультацию клиенту лично, анализируя его реальные положения."
        )

    else:
        # На всякий случай — дефолт
        prompt = (
            f"Сформируй развёрнутый анализ по сфере: {sphere_text} / {sub_text}.\n"
            f"Данные: {birth_text}\n"
            "Объём 3000+ символов, структурировано, с практическими рекомендациями."
        )

    # -----------------------
    # 📡 GPT-запрос
    # -----------------------
    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Ты опытный ведический астролог-консультант."},
                {"role": "user", "content": prompt},
            ]
        )

        raw_answer = completion.choices[0].message.content
        answer = format_answer(raw_answer)

        # 🔧 Разбиваем длинный текст на части
        MAX_LEN = 4000
        for i in range(0, len(answer), MAX_LEN):
            part = answer[i:i + MAX_LEN]
            await message.answer(part)

        # 💾 Сохраняем полный ответ
        save_reading(uid, sphere, sub, prompt, answer)

        if not u.get("paid") and not u.get("free_used"):
            update_user(uid, free_used=1)
            await message.answer(
                "🔒 Ты использовала бесплатную консультацию. "
                "Чтобы открыть все разделы — введи секретный код разблокировки."
            )

    except OpenAIError:
        log.exception("OpenAI error")
        await message.answer("⚠️ Сейчас ИИ недоступен. Давай попробуем позже.")

    except Exception:
        log.exception("Unexpected error")
        await message.answer("❌ Что-то пошло не так. Попробуем ещё раз.")
        
# ----------------------
# Webhook lifecycle
# ----------------------
async def on_startup(dp):
    db_init()  # ✅ Инициализация базы данных, если используешь её
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    log.info(f"✅ Webhook успешно установлен: {WEBHOOK_URL}")

async def on_shutdown(dp):
    await bot.delete_webhook()
    log.info("🧹 Webhook удалён (бот остановлен)")


if __name__ == "__main__":
    # Render / Railway webhook runner
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )
