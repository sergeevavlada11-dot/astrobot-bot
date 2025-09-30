import os
import logging
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.executor import start_webhook
from openai import OpenAI, OpenAIError

# ----------------------
# Logging
# ----------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astrobot-final")

# ----------------------
# Env
# ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # e.g. https://your-bot.onrender.com
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None
UNLOCK_CODE = os.getenv("UNLOCK_CODE", "ASTROVIP")
DB_PATH = os.getenv("DB_PATH", "astrobot.sqlite3")

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not WEBHOOK_HOST:
    raise RuntimeError("Set TELEGRAM_TOKEN, OPENAI_API_KEY, WEBHOOK_HOST")

# ----------------------
# Init
# ----------------------
bot = Bot(token=TELEGRAM_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)
client = OpenAI(api_key=OPENAI_API_KEY)

# ----------------------
# DB
# ----------------------
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

def get_user(uid):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("SELECT user_id, paid, free_used, city, birth_date, birth_time FROM users WHERE user_id=?", (uid,))
        row = cur.fetchone()
        if not row:
            return None
        return {{"user_id": row[0], "paid": bool(row[1]), "free_used": bool(row[2]), "city": row[3], "birth_date": row[4], "birth_time": row[5]}}

def ensure_user(uid):
    u = get_user(uid)
    if u: return u
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)", (uid, datetime.utcnow().isoformat()))
        con.commit()
    return get_user(uid)

def update_user(uid, **fields):
    if not fields: return
    cols = ",".join([f"{{k}}=?" for k in fields.keys()])
    vals = list(fields.values()) + [uid]
    with sqlite3.connect(DB_PATH) as con:
        con.execute(f"UPDATE users SET {{cols}} WHERE user_id=?", vals)
        con.commit()

def save_reading(uid, sphere, sub, prompt, answer):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO readings (user_id, sphere, subtopic, prompt, answer, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, sphere, sub, prompt, answer, datetime.utcnow().isoformat())
        )
        con.commit()

def delete_history(uid):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM readings WHERE user_id=?", (uid,))
        con.commit()

# ----------------------
# UI
# ----------------------
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
    InlineKeyboardButton(text="💳 Оформить доступ", url="{PAY_URL}")
)

# ----------------------
# State (in-memory)
# ----------------------
STATE_WAIT_CITY = "wait_city"
STATE_WAIT_DATE = "wait_date"
STATE_WAIT_TIME = "wait_time"
STATE_READY = "ready"

user_state = {}

def set_state(uid, state): user_state[uid] = state
def get_state(uid): return user_state.get(uid)

def fmt_profile(u):
    return f"📍 Город: {{u.get('city','—')}}\n📅 Дата: {{u.get('birth_date','—')}}\n⏰ Время: {{u.get('birth_time','—')}}"

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

def _valid_date(s):
    try:
        datetime.strptime(s.strip(), "%d.%m.%Y")
        return True
    except:
        return False

def _valid_time(s):
    try:
        datetime.strptime(s.strip(), "%H:%M")
        return True
    except:
        return False

def is_blocked(u):
    return (not u.get("paid")) and u.get("free_used")

async def guard_access(message, u):
    if is_blocked(u):
        await message.answer(
            "🔒 <b>Доступ ограничен</b>\n\n"
            "Ты уже использовала бесплатную консультацию.\n"
            "Чтобы открыть все разделы — введи <b>секретный код разблокировки</b>."
        )
        return False
    return True

async def try_unlock(message):
    text = (message.text or "").strip()
    if text == UNLOCK_CODE:
        update_user(message.from_user.id, paid=1)
        await message.answer("✅ Доступ открыт! Можешь пользоваться всеми разделами без ограничений 🎉", reply_markup=sphere_kb)
        return True
    return False

# ----------------------
# Commands
# ----------------------
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
        "Привет 🌌 Я твой астробот-подруга!\n"
        "Сначала соберём данные рождения.\n\n"
        "🧭 Напиши <b>город</b> рождения (например: Москва)\n\n"
        "ℹ️ В любой момент можно ввести секретный код для разблокировки (если у тебя есть)."
    )

# ----------------------
# Data collection
# ----------------------
@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_WAIT_CITY)
async def ask_date(message: types.Message):
    if await try_unlock(message): return
    city = message.text.strip()
    if len(city) < 2:
        await message.answer("Хм, коротко. Напиши город полностью 🏙️")
        return
    u = ensure_user(message.from_user.id)
    update_user(u["user_id"], city=city)
    set_state(u["user_id"], STATE_WAIT_DATE)
    await message.answer("Отлично! ✨ Теперь пришли дату рождения <b>дд.мм.гггг</b>\nНапример: 15.07.1995")

@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_WAIT_DATE)
async def ask_time(message: types.Message):
    if await try_unlock(message): return
    date = message.text.strip()
    if not _valid_date(date):
        await message.answer("Формат другой 🤔 Нужно: <b>дд.мм.гггг</b>\nПример: 03.11.1998")
        return
    u = ensure_user(message.from_user.id)
    update_user(u["user_id"], birth_date=date)
    set_state(u["user_id"], STATE_WAIT_TIME)
    await message.answer("Супер! 🕰️ Теперь пришли время рождения <b>чч:мм</b>\nЕсли не знаешь — напиши <i>не знаю</i>")

@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_WAIT_TIME)
async def ready_menu(message: types.Message):
    if await try_unlock(message): return
    t = message.text.strip().lower()
    u = ensure_user(message.from_user.id)
    if t == "не знаю":
        update_user(u["user_id"], birth_time="неизвестно")
    else:
        if not _valid_time(t):
            await message.answer("Нужно <b>чч:мм</b> (например, 14:30) ⏰\nИли напиши <i>не знаю</i>")
            return
        update_user(u["user_id"], birth_time=t)
    set_state(u["user_id"], STATE_READY)
    u = get_user(u["user_id"])
    await message.answer(
        "Отлично! Данные сохранены ✅\n\n"
        f"{{fmt_profile(u)}}\n\n"
        "Теперь выбери сферу ⤵️",
        reply_markup=sphere_kb
    )

# ----------------------
# Flow
# ----------------------
SPHERE_MAP = SPHERE_MAP  # keep for clarity

@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_READY and m.text in SPHERE_MAP.keys())
async def pick_subtopic(message: types.Message):
    u = ensure_user(message.from_user.id)
    if await try_unlock(message): return
    if not await guard_access(message, u): return
    # save last chosen sphere
    user_state[f"last_sphere_{{message.from_user.id}}"] = message.text
    await message.answer(
        f"Ты выбрала: <b>{{message.text}}</b> 💫\nТеперь выбери формат разбора:",
        reply_markup=sub_kb
    )

@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_READY and m.text == "⬅️ Назад к сферам")
async def back_to_spheres(message: types.Message):
    u = ensure_user(message.from_user.id)
    if await try_unlock(message): return
    if not await guard_access(message, u): return
    await message.answer("Выбери сферу ⤵️", reply_markup=sphere_kb)

@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_READY and m.text in SUB_MAP.keys())
async def final_generate(message: types.Message):
    uid = message.from_user.id
    u = ensure_user(uid)
    if await try_unlock(message): return
    if not await guard_access(message, u): return

    sphere = user_state.get(f"last_sphere_{{uid}}")
    if not sphere:
        await message.answer("Сначала выбери сферу ⤵️", reply_markup=sphere_kb)
        return

    sub = message.text
    birth = get_user(uid)
    birth_text = f"📍 Город: {{birth.get('city','—')}}\n📅 Дата: {{birth.get('birth_date','—')}}\n⏰ Время: {{birth.get('birth_time','—')}}"
    sphere_text = SPHERE_MAP[sphere]
    sub_text = SUB_MAP[sub]

    prompt = (
        "Ты — дружелюбный и эмпатичный астролог. Пиши по-русски, структурировано, с эмодзи, "
        "без страшилок, с акцентом на потенциал и свободу выбора. "
        "Добавь подзаголовки, списки и 2–3 практических совета в конце.\n\n"
        f"Данные рождения:\n{{birth_text}}\n\n"
        f"Сфера: {{sphere_text}}\nПодраздел: {{sub_text}}"
    )

    try:
        completion = client.chat.completions.create(
            model="gpt-5",
            messages=[
                {{"role": "system", "content": "Ты опытный астролог-консультант. Пиши дружелюбно, с эмодзи и структурой."}},
                {{"role": "user", "content": prompt}},
            ]
        )
        answer = completion.choices[0].message.content
        await message.answer(answer)
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
    db_init()
    await bot.set_webhook(WEBHOOK_URL)
    log.info(f"Webhook set: {{WEBHOOK_URL}}")

async def on_shutdown(dp):
    await bot.delete_webhook()
    log.info("Webhook removed")

if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )
