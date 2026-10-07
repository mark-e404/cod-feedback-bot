"""
Телеграм-бот: вопросы и предложения с антиспамом, опросы, голосования и викторины.

- Обращения приходят всем админам; ответ (реплаем) уходит автору.
- Управление ботом (меню, анонсы, модерация, статистика, обновление) — кнопками в /start.
- Роли: ADMIN_IDS (админы) и SUPER_ADMIN_IDS (ещё и обновление бота с GitHub).

Запуск:
    pip install -r requirements.txt
    cp .env.example .env && chmod 600 .env   # впишите токен и ID в .env
    python bot.py
"""
import asyncio
import json
import logging
import os
import random
import re
import socket
import sqlite3
import sys
import time
from datetime import datetime
from html import escape
from math import ceil

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendPoll
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    PollAnswer,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ───────────────────────── Настройки ─────────────────────────
# Секреты читаются из файла .env рядом с ботом (или из переменных окружения).
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Создайте файл .env (см. .env.example)")
def _parse_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


# Роли (ID через запятую):
#   ADMIN_IDS="111,222"     — админы: ответы, баны, статистика, меню, анонсы
#   SUPER_ADMIN_IDS="333"   — супер-админы: всё то же + обновление бота из панели
# Старая переменная OWNER_ID читается как ADMIN_IDS.
# Роли из .env — «корневые»: из панели их изменить нельзя. Остальных админов
# супер-админы добавляют/повышают/понижают/удаляют кнопками (хранится в БД).
ENV_SUPER_IDS = _parse_ids(os.environ.get("SUPER_ADMIN_IDS", ""))
ENV_ADMIN_IDS = _parse_ids(os.environ.get("ADMIN_IDS") or os.environ.get("OWNER_ID", ""))
# Рабочие наборы (меняются «на лету» через rebuild_admin_sets, объекты не подменяются).
# ADMIN_IDS = все, у кого есть доступ к панели (включая супер-админов).
SUPER_ADMIN_IDS = set(ENV_SUPER_IDS)
ADMIN_IDS = ENV_ADMIN_IDS | ENV_SUPER_IDS
if not ADMIN_IDS:
    raise SystemExit("Укажите ADMIN_IDS и/или SUPER_ADMIN_IDS (ID через запятую)")
DB_PATH = os.getenv("DB_PATH", "bot.db")

COOLDOWN_SEC = 60        # пауза между сообщениями одного пользователя
MAX_PER_HOUR = 3         # лимит в час
MAX_PER_DAY = 10         # лимит в сутки
MIN_LEN = 8             # минимальная длина текста
MAX_LEN = 2000           # максимальная длина текста
MAX_LINKS = 2            # максимум ссылок в одном сообщении

# Типы пунктов меню: «вопрос» (админ должен ответить), «предложение» (ответ по желанию)
# и три интерактивных: опрос, голосование, викторина (пользователь отвечает на готовый опрос).
POLL_KINDS = {
    "poll": "Опрос",
    "vote": "Голосование",
    "quiz": "Викторина",
}
POLL_HINTS = {
    "poll": "Опрос — можно выбрать несколько вариантов ответа.",
    "vote": "Голосование — можно выбрать только один вариант.",
    "quiz": "Викторина — один вариант, один из них правильный.",
}
MODES = {
    "question": "Вопрос",
    "suggestion": "Предложение",
    **POLL_KINDS,
}
DEFAULT_PROMPTS = {
    "question": "Напишите ваш вопрос одним сообщением:",
    "suggestion": "Напишите ваше предложение одним сообщением:",
}
MAX_ITEMS = 12          # максимум пунктов в меню
MAX_TITLE = 40          # максимальная длина названия кнопки
MAX_PROMPT = 500        # максимальная длина текста-подсказки
MAX_POLL_QUESTION = 300  # лимиты Telegram для опросов
MAX_POLL_OPTION = 100
MAX_POLL_OPTIONS = 10
MAX_POLL_EXPLAIN = 200
BROADCAST_DELAY = 0.05  # пауза между сообщениями рассылки (~20 в секунду)
PAGE_SIZE = 8           # пользователей на странице в списках

# Лимиты, которые супер-админ меняет из панели: имя -> (название, минимум, максимум).
# Значения выше — стандартные; изменённые хранятся в БД и переживают перезапуск.
LIMITS = {
    "COOLDOWN_SEC": ("Пауза между сообщениями, сек", 0, 3600),
    "MAX_PER_HOUR": ("Сообщений в час", 1, 100),
    "MAX_PER_DAY": ("Сообщений в сутки", 1, 500),
    "MIN_LEN": ("Минимальная длина текста", 1, 500),
    "MAX_LEN": ("Максимальная длина текста", 10, 3500),
    "MAX_LINKS": ("Максимум ссылок и @упоминаний", 0, 20),
}
DEFAULT_LIMITS = {name: globals()[name] for name in LIMITS}

# ───────────────────────── База данных ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
_menu_is_new = not db.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='menu_items'"
).fetchone()
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        verified INTEGER NOT NULL DEFAULT 0,
        banned INTEGER NOT NULL DEFAULT 0,
        created INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        created INTEGER NOT NULL,
        owner_msg_id INTEGER,
        answered INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id, created);
    CREATE TABLE IF NOT EXISTS admin_msgs (
        chat_id INTEGER NOT NULL,
        msg_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        PRIMARY KEY (chat_id, msg_id)
    );
    CREATE TABLE IF NOT EXISTS menu_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        mode TEXT NOT NULL,
        prompt TEXT,
        position INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL,
        correct INTEGER,
        explanation TEXT,
        created INTEGER NOT NULL,
        created_by INTEGER
    );
    CREATE TABLE IF NOT EXISTS poll_sent (
        poll_id TEXT PRIMARY KEY,
        poll_ref INTEGER NOT NULL,
        user_id INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS poll_votes (
        poll_ref INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        options TEXT NOT NULL,
        created INTEGER NOT NULL,
        PRIMARY KEY (poll_ref, user_id)
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY,
        role TEXT NOT NULL,
        added_by INTEGER,
        added_at INTEGER NOT NULL
    );
    """
)


def _add_column(table: str, column: str, ddl: str) -> None:
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# Миграции для уже существующей базы
_add_column("users", "blocked", "INTEGER NOT NULL DEFAULT 0")
_add_column("users", "username", "TEXT")
_add_column("users", "full_name", "TEXT")
_add_column("users", "last_seen", "INTEGER")
_add_column("messages", "item_title", "TEXT")
_add_column("menu_items", "poll_ref", "INTEGER")

# Стандартное меню — только при создании таблицы (чтобы удалённые пункты не возвращались)
if _menu_is_new:
    db.executemany(
        "INSERT INTO menu_items (title, mode, prompt, position) VALUES (?, ?, ?, ?)",
        [
            ("Задать вопрос", "question", None, 1),
            ("Предложить идею", "suggestion", None, 2),
        ],
    )
db.commit()


def ensure_user(uid: int) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (id, created) VALUES (?, ?)", (uid, int(time.time()))
    )
    # Если пользователь снова написал боту — он больше не «заблокировал» его
    db.execute("UPDATE users SET blocked=0 WHERE id=? AND blocked=1", (uid,))
    db.commit()


def is_banned(uid: int) -> bool:
    row = db.execute("SELECT banned FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row[0])


def is_verified(uid: int) -> bool:
    row = db.execute("SELECT verified FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row and row[0])


def set_flag(uid: int, field: str, value: int) -> None:
    assert field in ("verified", "banned", "blocked")
    ensure_user(uid)
    db.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))
    db.commit()


def touch_user(u) -> None:
    """Запоминает имя, username и время последней активности пользователя."""
    now = int(time.time())
    db.execute("INSERT OR IGNORE INTO users (id, created) VALUES (?, ?)", (u.id, now))
    db.execute(
        "UPDATE users SET username=?, full_name=?, last_seen=?, blocked=0 WHERE id=?",
        (u.username, u.full_name, now, u.id),
    )
    db.commit()


def get_setting(key: str):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    db.commit()


def del_setting(key: str) -> None:
    db.execute("DELETE FROM settings WHERE key=?", (key,))
    db.commit()


def set_limit(name: str, value: int) -> None:
    globals()[name] = value  # check_text читает значения в момент вызова — действует сразу
    set_setting(f"limit:{name}", str(value))


def load_limits() -> None:
    for name, (_title, lo, hi) in LIMITS.items():
        raw = get_setting(f"limit:{name}")
        if raw is not None and raw.isdigit() and lo <= int(raw) <= hi:
            globals()[name] = int(raw)


def reset_limits() -> None:
    for name, default in DEFAULT_LIMITS.items():
        globals()[name] = default
        del_setting(f"limit:{name}")


def validate_limit(name: str, value: int) -> str | None:
    _title, lo, hi = LIMITS[name]
    if not lo <= value <= hi:
        return f"Допустимо от {lo} до {hi}."
    new = {n: globals()[n] for n in LIMITS}
    new[name] = value
    if new["MIN_LEN"] > new["MAX_LEN"]:
        return "Минимальная длина не может быть больше максимальной."
    if new["MAX_PER_HOUR"] > new["MAX_PER_DAY"]:
        return "Лимит в час не может быть больше лимита в сутки."
    return None


load_limits()


# ───────────────────────── Роли администраторов ─────────────────────────
def role_of(uid: int):
    if uid in SUPER_ADMIN_IDS:
        return "super"
    return "admin" if uid in ADMIN_IDS else None


def env_role(uid: int):
    if uid in ENV_SUPER_IDS:
        return "super"
    return "admin" if uid in ENV_ADMIN_IDS else None


def rebuild_admin_sets() -> None:
    """Пересобирает ADMIN_IDS / SUPER_ADMIN_IDS из .env и таблицы admins (на месте)."""
    rows = db.execute("SELECT id, role FROM admins").fetchall()
    ADMIN_IDS.clear()
    ADMIN_IDS.update(ENV_ADMIN_IDS | ENV_SUPER_IDS | {r[0] for r in rows})
    SUPER_ADMIN_IDS.clear()
    SUPER_ADMIN_IDS.update(ENV_SUPER_IDS | {r[0] for r in rows if r[1] == "super"})


def _upsert_admin(uid: int, role: str, actor: int) -> None:
    db.execute(
        "INSERT OR IGNORE INTO admins (id, role, added_by, added_at) VALUES (?, ?, ?, ?)",
        (uid, role, actor, int(time.time())),
    )
    db.execute("UPDATE admins SET role=? WHERE id=?", (role, uid))
    db.commit()


def _delete_admin(uid: int) -> None:
    db.execute("DELETE FROM admins WHERE id=?", (uid,))
    db.commit()


def add_admin(actor: int, uid: int, role: str) -> str | None:
    """Добавляет админа. Возвращает текст ошибки или None."""
    if role not in ("admin", "super"):
        return "Неизвестная роль."
    if uid <= 0:
        return "Некорректный ID."
    if role_of(uid):
        return "Этот пользователь уже администратор."
    _upsert_admin(uid, role, actor)
    rebuild_admin_sets()
    return None


def change_role(actor: int, action: str, uid: int) -> str | None:
    """action: up (повысить), down (понизить), del (удалить). Возвращает ошибку или None."""
    if uid == actor:
        return "Свою роль изменить нельзя."
    cur = role_of(uid)
    if cur is None:
        return "Этот пользователь не администратор."
    env = env_role(uid)
    if env == "super":
        return "Роль задана в .env — изменить её можно только там."
    if action == "up":
        if cur == "super":
            return "Уже супер-админ."
        _upsert_admin(uid, "super", actor)
    elif action == "down":
        if cur != "super":
            return "Это не супер-админ."
        if env == "admin":
            _delete_admin(uid)  # останется админом из .env
        else:
            _upsert_admin(uid, "admin", actor)
    elif action == "del":
        if env == "admin":
            return "Админ задан в .env — удалить можно только там."
        _delete_admin(uid)
    else:
        return "Неизвестное действие."
    rebuild_admin_sets()
    return None


rebuild_admin_sets()


def ago(ts: int) -> str:
    d = max(0, int(time.time()) - ts)
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} дн назад"


def fmt_ts(ts) -> str:
    if not ts:
        return "нет данных"
    return f"{datetime.fromtimestamp(ts):%d.%m.%Y %H:%M} ({ago(ts)})"


# ───────────────────────── Антиспам ─────────────────────────
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|@\w{4,})", re.IGNORECASE)


def check_text(uid: int, text: str) -> str | None:
    """Возвращает текст ошибки или None, если всё в порядке."""
    text = text.strip()
    if len(text) < MIN_LEN:
        return f"Слишком короткое сообщение, минимум {MIN_LEN} символов."
    if len(text) > MAX_LEN:
        return f"Слишком длинное сообщение, максимум {MAX_LEN} символов (у вас {len(text)})."
    if len(LINK_RE.findall(text)) > MAX_LINKS:
        return f"Слишком много ссылок/упоминаний (максимум {MAX_LINKS})."
    if len(set(text.lower())) < 3:
        return "Сообщение похоже на спам. Опишите мысль словами."

    now = int(time.time())
    rows = db.execute(
        "SELECT created, text FROM messages WHERE user_id=? AND created>?",
        (uid, now - 86400),
    ).fetchall()

    if rows:
        wait = COOLDOWN_SEC - (now - max(r[0] for r in rows))
        if wait > 0:
            return f"Не так быстро! Подождите ещё {wait} сек."
    if sum(1 for r in rows if r[0] > now - 3600) >= MAX_PER_HOUR:
        return "Достигнут лимит сообщений в час. Попробуйте позже."
    if len(rows) >= MAX_PER_DAY:
        return "Достигнут лимит сообщений на сегодня. Возвращайтесь завтра."
    norm = text.lower()
    if any(r[1].strip().lower() == norm for r in rows):
        return "Такое сообщение вы уже отправляли."
    return None


# ───────────────────────── Клавиатуры и состояния ─────────────────────────
class Form(StatesGroup):
    waiting_text = State()


def get_items():
    return db.execute(
        "SELECT id, title, mode, prompt FROM menu_items ORDER BY position, id"
    ).fetchall()


def get_item(item_id):
    if not str(item_id).isdigit():
        return None
    return db.execute(
        "SELECT id, title, mode, prompt FROM menu_items WHERE id=?", (int(item_id),)
    ).fetchone()


# ---- опросы, голосования, викторины ----
# В новых версиях aiogram к вопросу опроса тоже применяется parse_mode по умолчанию (HTML),
# в старых — только к пояснению. Экранируем ровно там, где разметка реально разбирается.
_Q_PARSED = "question_parse_mode" in SendPoll.model_fields


def poll_q(text: str) -> str:
    return escape(text, quote=False) if _Q_PARSED else text


def create_poll(kind: str, question: str, options: list, correct, explanation, uid: int) -> int:
    cur = db.execute(
        "INSERT INTO polls (kind, question, options, correct, explanation, created, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, question, json.dumps(options, ensure_ascii=False), correct, explanation,
         int(time.time()), uid),
    )
    db.commit()
    return cur.lastrowid


def get_poll(ref):
    row = db.execute(
        "SELECT id, kind, question, options, correct, explanation FROM polls WHERE id=?",
        (ref,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "kind": row[1], "question": row[2],
        "options": json.loads(row[3]), "correct": row[4], "explanation": row[5],
    }


def item_poll_ref(item_id):
    row = db.execute("SELECT poll_ref FROM menu_items WHERE id=?", (item_id,)).fetchone()
    return row[0] if row else None


async def send_poll_to(bot: Bot, uid: int, poll_ref: int) -> None:
    """Отправляет пользователю опрос/голосование/викторину и запоминает его poll_id."""
    p = get_poll(poll_ref)
    if not p:
        raise TelegramAPIError(method=None, message="опрос не найден")
    kwargs = dict(
        chat_id=uid,
        question=poll_q(p["question"]),
        options=p["options"],
        is_anonymous=False,  # иначе Telegram не присылает, кто что выбрал
    )
    if p["kind"] == "quiz":
        kwargs.update(type="quiz", correct_option_id=p["correct"])
        if p["explanation"]:
            kwargs["explanation"] = escape(p["explanation"], quote=False)
    else:
        kwargs["allows_multiple_answers"] = p["kind"] == "poll"
    try:
        sent = await bot.send_poll(**kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        sent = await bot.send_poll(**kwargs)
    db.execute(
        "INSERT OR REPLACE INTO poll_sent (poll_id, poll_ref, user_id) VALUES (?, ?, ?)",
        (sent.poll.id, p["id"], uid),
    )
    db.commit()


def menu_kb():
    kb = InlineKeyboardBuilder()
    for item_id, title, _mode, _prompt in get_items():
        kb.button(text=title, callback_data=f"kind:{item_id}")
    kb.adjust(1)
    return kb.as_markup()


def cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data="cancel")
    return kb.as_markup()


WELCOME = (
    "Здесь можно задать вопрос, отправить предложение или принять участие "
    "в опросах и викторинах.\n\n"
    "На вопросы ответят прямо в этом чате."
)

# ───────────────────────── Общие команды ─────────────────────────
common = Router()


@common.message(Command("id"))
async def show_id(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")


@common.poll_answer()
async def on_poll_answer(a: PollAnswer):
    """Запоминает, что выбрал пользователь в опросе, голосовании или викторине."""
    if a.user is None or is_banned(a.user.id):
        return
    row = db.execute(
        "SELECT poll_ref FROM poll_sent WHERE poll_id=? AND user_id=?", (a.poll_id, a.user.id)
    ).fetchone()
    if not row:
        return
    if a.option_ids:
        db.execute(
            "INSERT OR REPLACE INTO poll_votes (poll_ref, user_id, options, created) "
            "VALUES (?, ?, ?, ?)",
            (row[0], a.user.id, json.dumps(list(a.option_ids)), int(time.time())),
        )
    else:  # человек отозвал голос
        db.execute(
            "DELETE FROM poll_votes WHERE poll_ref=? AND user_id=?", (row[0], a.user.id)
        )
    db.commit()


# ───────────────────────── Роутер администраторов ─────────────────────────
# Всё управление — кнопками. /start у админа открывает панель.
owner = Router()
# Права проверяются в момент события, поэтому смена ролей действует сразу.
async def is_admin_event(event) -> bool:
    return event.from_user is not None and event.from_user.id in ADMIN_IDS


async def is_super_event(event) -> bool:
    return event.from_user is not None and event.from_user.id in SUPER_ADMIN_IDS


owner.message.filter(is_admin_event)
owner.callback_query.filter(is_admin_event)

# Фильтр «только супер-админы» (обновление, лимиты, управление админами)
SUPER = is_super_event

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class AdminForm(StatesGroup):
    admin_target = State()
    admin_role = State()
    limit_value = State()
    item_title = State()
    item_mode = State()
    item_prompt = State()
    announce = State()
    ban_target = State()
    poll_question = State()
    poll_options = State()
    poll_correct = State()
    poll_explain = State()


broadcast_running = False
background_tasks: set = set()
update_lock = asyncio.Lock()


def is_super(uid: int) -> bool:
    return uid in SUPER_ADMIN_IDS


def rows_kb(*rows):
    # rows: списки кнопок, кнопка = (текст, callback_data)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
            for row in rows
        ]
    )


async def safe_edit(msg: Message, text: str, markup=None) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass  # например, «message is not modified»


# ---- главная панель ----
def panel_text(uid: int) -> str:
    role = "супер-админ" if is_super(uid) else "админ"
    return (
        "<b>Панель администратора</b>\n"
        f"Ваша роль: {role}\n\n"
        "Чтобы ответить на вопрос или предложение, нажмите «Ответить» (Reply) "
        "на сообщении с ним."
    )


def panel_kb(uid: int):
    rows = [
        [("Статистика", "st:home")],
        [("Пункты меню", "adm:menu")],
        [("Анонс пользователям", "adm:announce")],
        [("Опросы и викторины", "pl:home")],
        [("Модерация", "mod:home")],
    ]
    if is_super(uid):
        rows.append([("Администраторы", "rl:home")])
        rows.append([("Лимиты антиспама", "lim:home")])
        rows.append([("Обновление бота", "upd:home")])
    return rows_kb(*rows)


@owner.message(CommandStart())
@owner.message(Command("menu"))
async def owner_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(panel_text(m.from_user.id), reply_markup=panel_kb(m.from_user.id))


@owner.callback_query(F.data == "adm:home")
async def adm_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, panel_text(c.from_user.id), panel_kb(c.from_user.id))
    await c.answer()


# ───────────── Статистика ─────────────
def _admin_excl(col: str = "u.id"):
    ids = sorted(ADMIN_IDS)
    return f"{col} NOT IN ({','.join('?' * len(ids))})", ids


def _count(where: str, extra: tuple = ()) -> int:
    excl, ids = _admin_excl("id")
    return db.execute(
        f"SELECT COUNT(*) FROM users WHERE {where} AND {excl}", (*extra, *ids)
    ).fetchone()[0]


def basic_stats_text() -> str:
    day_ago = int(time.time()) - 86400
    users = _count("verified=1")
    banned = _count("banned=1")
    active = _count("verified=1 AND banned=0 AND last_seen>?", (day_ago,))
    left = _count("verified=1 AND banned=0 AND blocked=1")
    q = db.execute("SELECT COUNT(*) FROM messages WHERE kind='question'").fetchone()[0]
    qa = db.execute(
        "SELECT COUNT(*) FROM messages WHERE kind='question' AND answered=1"
    ).fetchone()[0]
    s = db.execute("SELECT COUNT(*) FROM messages WHERE kind='suggestion'").fetchone()[0]
    polls = db.execute("SELECT COUNT(*) FROM polls").fetchone()[0]
    return (
        "<b>Статистика</b>\n\n"
        f"Пользователей: {users} (в бане: {banned})\n"
        f"Активны за 24 часа: {active}\n"
        f"Заблокировали бота: {left}\n"
        f"Вопросов: {q} (отвечено: {qa})\n"
        f"Предложений: {s}\n"
        f"Опросов, голосований и викторин: {polls}"
    )


@owner.callback_query(F.data == "st:home")
async def st_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        c.message,
        basic_stats_text(),
        rows_kb(
            [("Активные пользователи", "st:act:0")],
            [("Пользователи в бане", "st:ban:0")],
            [("Назад", "adm:home")],
        ),
    )
    await c.answer()


def fetch_users(banned: bool, page: int):
    cond = "u.banned=1" if banned else "u.verified=1 AND u.banned=0"
    excl, ids = _admin_excl("u.id")
    total = db.execute(
        f"SELECT COUNT(*) FROM users u WHERE {cond} AND {excl}", ids
    ).fetchone()[0]
    pages = max(1, ceil(total / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    rows = db.execute(
        "SELECT u.id, u.full_name, u.username, u.last_seen, u.blocked, "
        "(SELECT COUNT(*) FROM messages m WHERE m.user_id = u.id) "
        f"FROM users u WHERE {cond} AND {excl} "
        "ORDER BY (u.last_seen IS NULL), u.last_seen DESC, u.id DESC "
        "LIMIT ? OFFSET ?",
        (*ids, PAGE_SIZE, page * PAGE_SIZE),
    ).fetchall()
    return rows, page, pages, total


async def backfill_profiles(bot: Bot, rows) -> bool:
    # Для старых пользователей имя/username ещё неизвестны — подтягиваем у Telegram
    changed = False
    for uid, name, _uname, *_rest in rows:
        if name is not None:
            continue
        try:
            chat = await bot.get_chat(uid)
        except TelegramAPIError:
            continue
        full = " ".join(filter(None, [chat.first_name, chat.last_name])) or chat.title or ""
        db.execute(
            "UPDATE users SET full_name=?, username=? WHERE id=?",
            (full, chat.username, uid),
        )
        changed = True
    if changed:
        db.commit()
    return changed


def render_users(title: str, rows, page: int, pages: int, total: int) -> str:
    lines = [f"<b>{title}</b>: всего {total}, страница {page + 1}/{pages}\n"]
    if not rows:
        lines.append("Список пуст.")
    for i, (uid, name, uname, last_seen, left, cnt) in enumerate(
        rows, page * PAGE_SIZE + 1
    ):
        shown = escape(name) if name else "без имени"
        tag = f"@{escape(uname)}" if uname else "нет username"
        extra = ", заблокировал бота" if left else ""
        lines.append(
            f'{i}. <a href="tg://user?id={uid}">{shown}</a> · {tag} · <code>{uid}</code>\n'
            f"    был: {fmt_ts(last_seen)} · обращений: {cnt}{extra}"
        )
    return "\n".join(lines)


async def show_users(c: CallbackQuery, bot: Bot, banned: bool, page: int) -> None:
    rows, page, pages, total = fetch_users(banned, page)
    if await backfill_profiles(bot, rows):
        rows, page, pages, total = fetch_users(banned, page)
    text = render_users(
        "Пользователи в бане" if banned else "Активные пользователи",
        rows, page, pages, total,
    )
    kind = "ban" if banned else "act"
    kb_rows = []
    if banned:
        buttons = [
            (f"Разбанить №{page * PAGE_SIZE + i}", f"mod:unban:{r[0]}:{page}")
            for i, r in enumerate(rows, 1)
        ]
        kb_rows += [buttons[j:j + 2] for j in range(0, len(buttons), 2)]
    nav = []
    if page > 0:
        nav.append(("← Назад", f"st:{kind}:{page - 1}"))
    if page < pages - 1:
        nav.append(("Вперёд →", f"st:{kind}:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([("К статистике", "st:home")])
    await safe_edit(c.message, text, rows_kb(*kb_rows))


@owner.callback_query(F.data.startswith("st:act:"))
async def st_act(c: CallbackQuery, bot: Bot):
    await c.answer()
    await show_users(c, bot, False, int(c.data.split(":")[2]))


@owner.callback_query(F.data.startswith("st:ban:"))
async def st_ban(c: CallbackQuery, bot: Bot):
    await c.answer()
    await show_users(c, bot, True, int(c.data.split(":")[2]))


# ───────────── Модерация ─────────────
@owner.callback_query(F.data == "mod:home")
async def mod_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(
        c.message,
        f"<b>Модерация</b>\n\nСейчас в бане: {_count('banned=1')}",
        rows_kb(
            [("Список заблокированных", "st:ban:0")],
            [("Забанить по ID или @username", "mod:askban")],
            [("Назад", "adm:home")],
        ),
    )
    await c.answer()


@owner.callback_query(F.data == "mod:askban")
async def mod_askban(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.ban_target)
    await safe_edit(
        c.message,
        "Отправьте <b>числовой ID</b> или <b>@username</b> пользователя, "
        "которого нужно забанить.\n"
        "(@username находится только среди тех, кто уже писал боту.)",
        rows_kb([("Отмена", "mod:home")]),
    )
    await c.answer()


@owner.message(AdminForm.ban_target, F.text)
async def mod_ban_target(m: Message, state: FSMContext):
    raw = m.text.strip()
    uid = None
    if raw.isdigit():
        uid = int(raw)
    else:
        row = db.execute(
            "SELECT id FROM users WHERE LOWER(username)=?", (raw.lstrip("@").lower(),)
        ).fetchone()
        if row:
            uid = row[0]
    if uid is None:
        return await m.answer(
            "Не нашёл такого пользователя. Отправьте числовой ID или @username "
            "того, кто уже писал боту.",
            reply_markup=rows_kb([("Отмена", "mod:home")]),
        )
    if uid in ADMIN_IDS:
        return await m.answer(
            "Администратора забанить нельзя.", reply_markup=rows_kb([("Отмена", "mod:home")])
        )
    set_flag(uid, "banned", 1)
    await state.clear()
    await m.answer(
        f"Пользователь <code>{uid}</code> забанен.",
        reply_markup=rows_kb(
            [("Разбанить", f"mod:unban:{uid}:-1")], [("В панель", "adm:home")]
        ),
    )


@owner.callback_query(F.data.startswith("mod:unban:"))
async def mod_unban(c: CallbackQuery, bot: Bot):
    _, _, uid, page = c.data.split(":")
    set_flag(int(uid), "banned", 0)
    await c.answer("Пользователь разбанен.")
    if int(page) >= 0:
        await show_users(c, bot, True, int(page))
    else:
        await safe_edit(
            c.message,
            f"Пользователь <code>{uid}</code> разбанен.",
            rows_kb([("В панель", "adm:home")]),
        )


# Кнопка «Забанить автора» под каждым обращением
def author_kb(uid: int):
    if is_banned(uid):
        return rows_kb([("Разбанить автора", f"mb:unban:{uid}")])
    return rows_kb([("Забанить автора", f"mb:ban:{uid}")])


@owner.callback_query(F.data.startswith("mb:"))
async def mb_toggle(c: CallbackQuery):
    _, action, uid = c.data.split(":")
    uid = int(uid)
    if action == "ban":
        if uid in ADMIN_IDS:
            return await c.answer("Администратора забанить нельзя.", show_alert=True)
        set_flag(uid, "banned", 1)
        await c.answer("Автор забанен.")
    else:
        set_flag(uid, "banned", 0)
        await c.answer("Автор разбанен.")
    try:
        await c.message.edit_reply_markup(reply_markup=author_kb(uid))
    except TelegramBadRequest:
        pass


# ───────────── Лимиты антиспама (только супер-админы) ─────────────
def limits_text() -> str:
    lines = ["<b>Лимиты антиспама</b>\n"]
    for name, (title, _lo, _hi) in LIMITS.items():
        value = globals()[name]
        mark = "" if value == DEFAULT_LIMITS[name] else " (изменено)"
        lines.append(f"{title}: <b>{value}</b>{mark}")
    lines.append("\nИзменения действуют сразу и сохраняются после перезапуска.")
    return "\n".join(lines)


def limits_kb():
    rows = [
        [(f"{title}: {globals()[name]}", f"lim:edit:{name}")]
        for name, (title, _lo, _hi) in LIMITS.items()
    ]
    rows.append([("Сбросить к стандартным", "lim:reset")])
    rows.append([("Назад", "adm:home")])
    return rows_kb(*rows)


@owner.callback_query(F.data == "lim:home", SUPER)
async def lim_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, limits_text(), limits_kb())
    await c.answer()


@owner.callback_query(F.data.startswith("lim:edit:"), SUPER)
async def lim_edit(c: CallbackQuery, state: FSMContext):
    name = c.data.split(":")[2]
    if name not in LIMITS:
        return await c.answer()
    title, lo, hi = LIMITS[name]
    await state.set_state(AdminForm.limit_value)
    await state.update_data(limit=name)
    await safe_edit(
        c.message,
        f"<b>{title}</b>\n"
        f"Сейчас: <b>{globals()[name]}</b> (стандартно: {DEFAULT_LIMITS[name]})\n\n"
        f"Отправьте новое число от {lo} до {hi}.",
        rows_kb([("Отмена", "lim:home")]),
    )
    await c.answer()


@owner.message(AdminForm.limit_value, F.text, SUPER)
async def lim_value(m: Message, state: FSMContext):
    name = (await state.get_data()).get("limit")
    if name not in LIMITS:
        await state.clear()
        return await m.answer(
            "Что-то пошло не так, начните заново.",
            reply_markup=rows_kb([("К лимитам", "lim:home")]),
        )
    raw = m.text.strip()
    error = "Нужно целое число." if not raw.isdigit() else validate_limit(name, int(raw))
    if error:
        return await m.answer(error, reply_markup=rows_kb([("Отмена", "lim:home")]))
    set_limit(name, int(raw))
    await state.clear()
    await m.answer(
        f"Готово: {LIMITS[name][0].lower()} = {int(raw)}.\n\n" + limits_text(),
        reply_markup=limits_kb(),
    )


@owner.callback_query(F.data == "lim:reset", SUPER)
async def lim_reset_ask(c: CallbackQuery):
    await safe_edit(
        c.message,
        "Вернуть все лимиты к стандартным значениям?",
        rows_kb([("Да, сбросить", "lim:resetok")], [("Отмена", "lim:home")]),
    )
    await c.answer()


@owner.callback_query(F.data == "lim:resetok", SUPER)
async def lim_reset_ok(c: CallbackQuery):
    reset_limits()
    await c.answer("Лимиты сброшены.")
    await safe_edit(c.message, limits_text(), limits_kb())


@owner.callback_query(F.data.startswith("lim:"))
async def lim_denied(c: CallbackQuery):
    # сюда попадают только обычные админы
    await c.answer("Лимиты доступны только супер-админам.", show_alert=True)


# ───────────── Администраторы (только супер-админы) ─────────────
ROLE_NAMES = {"admin": "админ", "super": "супер-админ"}
MAX_ADMINS_SHOWN = 25
ASK_TEXT = {
    "up": "Повысить {who} до супер-админа?\n\n"
          "Супер-админ может обновлять бота, менять лимиты и управлять админами.",
    "down": "Понизить {who} до обычного админа?\n\n"
            "Доступ к обновлению, лимитам и управлению админами пропадёт.",
    "del": "Удалить {who} из админов?",
}
NOTIFY_TEXT = {
    "up": "Ваша роль повышена: вы супер-админ. Нажмите /start, чтобы открыть панель.",
    "down": "Ваша роль изменена: теперь вы админ. Нажмите /start, чтобы обновить панель.",
    "del": "Вы больше не администратор бота.",
}


async def profile_of(bot: Bot, uid: int):
    """Имя и username пользователя: из БД, а если нет — спрашиваем у Telegram."""
    row = db.execute("SELECT full_name, username FROM users WHERE id=?", (uid,)).fetchone()
    if row and row[0] is not None:
        return row[0], row[1]
    try:
        chat = await bot.get_chat(uid)
    except TelegramAPIError:
        return None, None
    full = " ".join(filter(None, [chat.first_name, chat.last_name])) or chat.title or ""
    db.execute(
        "INSERT OR IGNORE INTO users (id, created) VALUES (?, ?)", (uid, int(time.time()))
    )
    db.execute(
        "UPDATE users SET full_name=?, username=? WHERE id=?", (full, chat.username, uid)
    )
    db.commit()
    return full, chat.username


async def notify(bot: Bot, uid: int, text: str) -> bool:
    try:
        await bot.send_message(uid, text)
        return True
    except TelegramAPIError:
        return False


def role_actions(actor: int, uid: int) -> list[tuple[str, str]]:
    """Какие действия доступны над этим админом: [(подпись кнопки, action)]."""
    if uid == actor or env_role(uid) == "super":
        return []
    acts = []
    cur = role_of(uid)
    if cur == "admin":
        acts.append(("Повысить до супер-админа", "up"))
    if cur == "super":
        acts.append(("Понизить до админа", "down"))
    if env_role(uid) is None:
        acts.append(("Удалить из админов", "del"))
    return acts


def role_note(actor: int, uid: int) -> str:
    env = env_role(uid)
    if uid == actor:
        return "Это вы — свою роль менять нельзя."
    if env == "super":
        return "Роль задана в .env — изменить можно только там."
    if env == "admin":
        return "Админ задан в .env — удалить можно только там."
    return ""


def who_html(uid: int, name, uname) -> str:
    shown = escape(name) if name else "без имени"
    return f'<a href="tg://user?id={uid}">{shown}</a>'


async def admins_screen(bot: Bot, actor: int):
    ids = sorted(ADMIN_IDS, key=lambda x: (0 if x in SUPER_ADMIN_IDS else 1, x))
    shown = ids[:MAX_ADMINS_SHOWN]
    lines = [f"<b>Администраторы</b>: {len(ids)}\n"]
    buttons = []
    for i, uid in enumerate(shown, 1):
        name, uname = await profile_of(bot, uid)
        tag = f"@{escape(uname)}" if uname else "нет username"
        src = ", .env" if env_role(uid) else ""
        you = ", это вы" if uid == actor else ""
        lines.append(
            f"{i}. {who_html(uid, name, uname)} · {tag} · <code>{uid}</code>\n"
            f"    {ROLE_NAMES[role_of(uid)]}{src}{you}"
        )
        buttons.append((f"№{i}", f"rl:view:{uid}"))
    if len(ids) > len(shown):
        lines.append(f"\n…и ещё {len(ids) - len(shown)}")
    kb_rows = [buttons[j:j + 4] for j in range(0, len(buttons), 4)]
    kb_rows.append([("Добавить админа", "rl:add")])
    kb_rows.append([("Назад", "adm:home")])
    return "\n".join(lines), rows_kb(*kb_rows)


@owner.callback_query(F.data == "rl:home", SUPER)
async def rl_home(c: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await c.answer()
    text, markup = await admins_screen(bot, c.from_user.id)
    await safe_edit(c.message, text, markup)


@owner.callback_query(F.data.startswith("rl:view:"), SUPER)
async def rl_view(c: CallbackQuery, bot: Bot):
    await c.answer()
    uid = int(c.data.split(":")[2])
    role = role_of(uid)
    if role is None:  # список устарел
        text, markup = await admins_screen(bot, c.from_user.id)
        return await safe_edit(c.message, text, markup)
    name, uname = await profile_of(bot, uid)
    tag = f"@{escape(uname)}" if uname else "нет username"
    row = db.execute("SELECT added_by, added_at FROM admins WHERE id=?", (uid,)).fetchone()
    if env_role(uid):
        source = "Источник: файл .env"
    elif row:
        source = (
            f"Добавлен через панель: {datetime.fromtimestamp(row[1]):%d.%m.%Y %H:%M}, "
            f"пользователем <code>{row[0]}</code>"
        )
    else:
        source = ""
    note = role_note(c.from_user.id, uid)
    text = (
        f"<b>{who_html(uid, name, uname)}</b>\n{tag} · <code>{uid}</code>\n\n"
        f"Роль: {ROLE_NAMES[role]}\n{source}" + (f"\n\n{note}" if note else "")
    )
    rows = [[(label, f"rl:ask:{act}:{uid}")] for label, act in role_actions(c.from_user.id, uid)]
    rows.append([("К списку админов", "rl:home")])
    await safe_edit(c.message, text, rows_kb(*rows))


@owner.callback_query(F.data.startswith("rl:ask:"), SUPER)
async def rl_ask(c: CallbackQuery, bot: Bot):
    _, _, act, uid_s = c.data.split(":")
    uid = int(uid_s)
    if act not in ASK_TEXT or act not in [a for _, a in role_actions(c.from_user.id, uid)]:
        await c.answer("Это действие сейчас недоступно.", show_alert=True)
        text, markup = await admins_screen(bot, c.from_user.id)
        return await safe_edit(c.message, text, markup)
    name, uname = await profile_of(bot, uid)
    await safe_edit(
        c.message,
        ASK_TEXT[act].format(who=who_html(uid, name, uname)),
        rows_kb([("Да", f"rl:do:{act}:{uid}")], [("Отмена", f"rl:view:{uid}")]),
    )
    await c.answer()


@owner.callback_query(F.data.startswith("rl:do:"), SUPER)
async def rl_do(c: CallbackQuery, bot: Bot):
    _, _, act, uid_s = c.data.split(":")
    uid = int(uid_s)
    error = change_role(c.from_user.id, act, uid)
    if error:
        await c.answer(error, show_alert=True)
    else:
        sent = await notify(bot, uid, NOTIFY_TEXT[act])
        await c.answer("Готово." + ("" if sent else " Уведомить человека не удалось."))
    text, markup = await admins_screen(bot, c.from_user.id)
    await safe_edit(c.message, text, markup)


@owner.callback_query(F.data == "rl:add", SUPER)
async def rl_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.admin_target)
    await safe_edit(
        c.message,
        "Отправьте <b>числовой ID</b> или <b>@username</b> нового админа.\n\n"
        "Человек должен сначала открыть бота и нажать Start, иначе бот не сможет "
        "ему написать. Свой ID он может узнать командой /id.",
        rows_kb([("Отмена", "rl:home")]),
    )
    await c.answer()


@owner.message(AdminForm.admin_target, F.text, SUPER)
async def rl_target(m: Message, state: FSMContext):
    raw = m.text.strip()
    uid = None
    if raw.isdigit():
        uid = int(raw)
    else:
        row = db.execute(
            "SELECT id FROM users WHERE LOWER(username)=?", (raw.lstrip("@").lower(),)
        ).fetchone()
        if row:
            uid = row[0]
    cancel = rows_kb([("Отмена", "rl:home")])
    if uid is None or uid <= 0:
        return await m.answer(
            "Не нашёл такого пользователя. Отправьте числовой ID или @username "
            "того, кто уже писал боту.",
            reply_markup=cancel,
        )
    if role_of(uid):
        return await m.answer(
            "Этот пользователь уже администратор. Изменить роль можно в списке админов.",
            reply_markup=cancel,
        )
    await state.update_data(target=uid)
    await state.set_state(AdminForm.admin_role)
    await m.answer(
        f"Какую роль выдать пользователю <code>{uid}</code>?\n\n"
        "<b>Админ</b> — ответы, баны, статистика, меню, анонсы.\n"
        "<b>Супер-админ</b> — всё то же + обновление бота, лимиты и управление админами.",
        reply_markup=rows_kb(
            [("Админ", "rl:addrole:admin")],
            [("Супер-админ", "rl:addrole:super")],
            [("Отмена", "rl:home")],
        ),
    )


@owner.callback_query(AdminForm.admin_role, F.data.startswith("rl:addrole:"), SUPER)
async def rl_addrole(c: CallbackQuery, state: FSMContext, bot: Bot):
    role = c.data.split(":")[2]
    uid = (await state.get_data()).get("target")
    await state.clear()
    if role not in ROLE_NAMES or uid is None:
        return await c.answer("Что-то пошло не так, начните заново.", show_alert=True)
    error = add_admin(c.from_user.id, uid, role)
    if error:
        await c.answer(error, show_alert=True)
        text, markup = await admins_screen(bot, c.from_user.id)
        return await safe_edit(c.message, text, markup)
    sent = await notify(
        bot, uid,
        f"Вам выдана роль: {ROLE_NAMES[role]}. Нажмите /start, чтобы открыть панель.",
    )
    extra = "" if sent else "\n\nУведомить не удалось: пусть откроет бота и нажмёт Start."
    await c.answer()
    await safe_edit(
        c.message,
        f"Добавлен: <code>{uid}</code> — {ROLE_NAMES[role]}.{extra}",
        rows_kb([("К списку админов", "rl:home")]),
    )


@owner.callback_query(F.data.startswith("rl:"))
async def rl_denied(c: CallbackQuery):
    if c.from_user.id in SUPER_ADMIN_IDS:
        # устаревшая кнопка (например, после перезапуска бота сбросился шаг ввода)
        return await c.answer("Кнопка устарела. Откройте раздел заново.", show_alert=True)
    await c.answer("Управление админами доступно только супер-админам.", show_alert=True)


# ───────────── Редактор меню ─────────────
def menu_editor_text() -> str:
    items = get_items()
    lines = ["<b>Пункты меню</b> (так их видят пользователи):\n"]
    for i, (_id, title, mode, _prompt) in enumerate(items, 1):
        kind = MODES.get(mode, mode).lower()
        lines.append(f"{i}. {escape(title)} — {kind}")
    lines.append(f"\nВсего: {len(items)} из {MAX_ITEMS}.")
    return "\n".join(lines)


def menu_editor_kb():
    rows = [[(f"Удалить: {t}", f"adm:del:{i}")] for i, t, _m, _p in get_items()]
    rows.append([("Добавить пункт", "adm:add")])
    rows.append([("Назад", "adm:home")])
    return rows_kb(*rows)


@owner.callback_query(F.data == "adm:menu")
async def adm_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, menu_editor_text(), menu_editor_kb())
    await c.answer()


@owner.callback_query(F.data.startswith("adm:del:"))
async def adm_del_ask(c: CallbackQuery):
    item = get_item(c.data.split(":")[2])
    if not item:
        await c.answer("Пункт уже удалён.", show_alert=True)
        return await safe_edit(c.message, menu_editor_text(), menu_editor_kb())
    if len(get_items()) <= 1:
        return await c.answer("Нельзя удалить последний пункт меню.", show_alert=True)
    await safe_edit(
        c.message,
        f"Удалить пункт «{escape(item[1])}»?",
        rows_kb([("Да, удалить", f"adm:delok:{item[0]}")], [("Отмена", "adm:menu")]),
    )
    await c.answer()


@owner.callback_query(F.data.startswith("adm:delok:"))
async def adm_del_ok(c: CallbackQuery):
    item_id = c.data.split(":")[2]
    if len(get_items()) <= 1:
        await c.answer("Нельзя удалить последний пункт меню.", show_alert=True)
    elif get_item(item_id):
        db.execute("DELETE FROM menu_items WHERE id=?", (int(item_id),))
        db.commit()
        await c.answer("Пункт удалён.")
    else:
        await c.answer("Пункт уже удалён.")
    await safe_edit(c.message, menu_editor_text(), menu_editor_kb())


@owner.callback_query(F.data == "adm:add")
async def adm_add(c: CallbackQuery, state: FSMContext):
    if len(get_items()) >= MAX_ITEMS:
        return await c.answer(f"Максимум {MAX_ITEMS} пунктов.", show_alert=True)
    await state.set_state(AdminForm.item_title)
    await safe_edit(
        c.message,
        f"Отправьте <b>название кнопки</b> (до {MAX_TITLE} символов).\n"
        "Например: <i>Фильм на киновечер</i>",
        rows_kb([("Отмена", "adm:menu")]),
    )
    await c.answer()


@owner.message(AdminForm.item_title, F.text)
async def adm_item_title(m: Message, state: FSMContext):
    title = m.text.strip()
    if not title or len(title) > MAX_TITLE or title.startswith("/"):
        return await m.answer(
            f"Название должно быть от 1 до {MAX_TITLE} символов. Попробуйте ещё раз.",
            reply_markup=rows_kb([("Отмена", "adm:menu")]),
        )
    await state.update_data(title=title)
    await state.set_state(AdminForm.item_mode)
    await m.answer(
        "Какой это тип пункта?\n\n"
        "<b>Вопрос</b> — вы ждёте ответа автору, в сообщении будет подсказка отвечать реплаем.\n"
        "<b>Предложение</b> — отвечать необязательно (например, «какой фильм посмотреть»).\n\n"
        "<b>Опрос</b>, <b>голосование</b> и <b>викторина</b> — готовый опрос: пользователь "
        "нажимает кнопку и получает его. В опросе можно выбрать несколько вариантов, "
        "в голосовании — один, в викторине один из вариантов правильный.",
        reply_markup=rows_kb(
            [("Вопрос (нужно ответить)", "adm:mode:question")],
            [("Предложение (ответ по желанию)", "adm:mode:suggestion")],
            [("Опрос", "adm:mode:poll"), ("Голосование", "adm:mode:vote")],
            [("Викторина", "adm:mode:quiz")],
            [("Отмена", "adm:menu")],
        ),
    )


@owner.callback_query(AdminForm.item_mode, F.data.startswith("adm:mode:"))
async def adm_item_mode(c: CallbackQuery, state: FSMContext):
    mode = c.data.split(":")[2]
    if mode not in MODES:
        return await c.answer()
    if mode in POLL_KINDS:
        await state.update_data(mode=mode, poll_kind=mode, poll_target="menu")
        await state.set_state(AdminForm.poll_question)
        await safe_edit(
            c.message, poll_question_text(mode), rows_kb([("Отмена", "adm:menu")])
        )
        return await c.answer()
    await state.update_data(mode=mode)
    await state.set_state(AdminForm.item_prompt)
    await safe_edit(
        c.message,
        "Отправьте <b>текст-подсказку</b>, который увидит пользователь после нажатия кнопки.\n"
        "Например: <i>Напишите название фильма и пару слов, почему он подойдёт</i>.\n\n"
        "Или нажмите «Пропустить» — будет стандартный текст.",
        rows_kb([("Пропустить", "adm:skipprompt")], [("Отмена", "adm:menu")]),
    )
    await c.answer()


async def finish_item(msg: Message, state: FSMContext, prompt) -> None:
    data = await state.get_data()
    await state.clear()
    if "title" not in data or "mode" not in data:
        return await msg.answer(
            "Что-то пошло не так, начните заново.",
            reply_markup=rows_kb([("К меню", "adm:menu")]),
        )
    if len(get_items()) >= MAX_ITEMS:
        return await msg.answer(f"Достигнут лимит: {MAX_ITEMS} пунктов.")
    pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM menu_items").fetchone()[0]
    db.execute(
        "INSERT INTO menu_items (title, mode, prompt, position) VALUES (?, ?, ?, ?)",
        (data["title"], data["mode"], prompt, pos),
    )
    db.commit()
    await msg.answer(
        "Пункт добавлен.\n\n" + menu_editor_text(), reply_markup=menu_editor_kb()
    )


@owner.message(AdminForm.item_prompt, F.text)
async def adm_item_prompt(m: Message, state: FSMContext):
    if len(m.text) > MAX_PROMPT or m.text.startswith("/"):
        return await m.answer(
            f"Подсказка не должна быть длиннее {MAX_PROMPT} символов. Попробуйте ещё раз.",
            reply_markup=rows_kb([("Отмена", "adm:menu")]),
        )
    await finish_item(m, state, m.html_text)  # html_text сохраняет форматирование


@owner.callback_query(AdminForm.item_prompt, F.data == "adm:skipprompt")
async def adm_skip_prompt(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await finish_item(c.message, state, None)


# ───────────── Конструктор опроса / голосования / викторины ─────────────
# Используется и для пунктов меню (poll_target="menu"), и для анонсов ("announce").
def poll_cancel_cb(data: dict) -> str:
    return "adm:menu" if data.get("poll_target") == "menu" else "adm:announce"


def poll_question_text(kind: str) -> str:
    return (
        f"<b>{POLL_KINDS[kind]}</b>\n{POLL_HINTS[kind]}\n\n"
        f"Отправьте <b>текст вопроса</b> (до {MAX_POLL_QUESTION} символов)."
    )


def poll_preview(kind, question, options, correct=None, explanation=None) -> str:
    lines = [f"<b>{POLL_KINDS[kind]}</b>", escape(question), ""]
    for i, opt in enumerate(options):
        mark = " — верный ответ" if kind == "quiz" and i == correct else ""
        lines.append(f"{i + 1}. {escape(opt)}{mark}")
    if explanation:
        lines += ["", f"Пояснение: {escape(explanation)}"]
    return "\n".join(lines)


@owner.message(AdminForm.poll_question, F.text)
async def poll_question_msg(m: Message, state: FSMContext):
    data = await state.get_data()
    q = m.text.strip()
    if not q or len(q) > MAX_POLL_QUESTION or q.startswith("/"):
        return await m.answer(
            f"Вопрос должен быть от 1 до {MAX_POLL_QUESTION} символов. Попробуйте ещё раз.",
            reply_markup=rows_kb([("Отмена", poll_cancel_cb(data))]),
        )
    await state.update_data(poll_q=q)
    await state.set_state(AdminForm.poll_options)
    await m.answer(
        f"Теперь отправьте <b>варианты ответа</b> — каждый с новой строки "
        f"(от 2 до {MAX_POLL_OPTIONS}, до {MAX_POLL_OPTION} символов каждый).\n"
        "Например:\n<i>Да\nНет\nПока не знаю</i>",
        reply_markup=rows_kb([("Отмена", poll_cancel_cb(data))]),
    )


@owner.message(AdminForm.poll_options, F.text)
async def poll_options_msg(m: Message, state: FSMContext):
    data = await state.get_data()
    cancel = rows_kb([("Отмена", poll_cancel_cb(data))])
    opts = [x.strip() for x in m.text.split("\n") if x.strip()]
    if not 2 <= len(opts) <= MAX_POLL_OPTIONS:
        return await m.answer(
            f"Нужно от 2 до {MAX_POLL_OPTIONS} вариантов, каждый с новой строки. "
            "Попробуйте ещё раз.",
            reply_markup=cancel,
        )
    if any(len(x) > MAX_POLL_OPTION for x in opts):
        return await m.answer(
            f"Каждый вариант — не длиннее {MAX_POLL_OPTION} символов. Попробуйте ещё раз.",
            reply_markup=cancel,
        )
    if len({x.lower() for x in opts}) != len(opts):
        return await m.answer(
            "Варианты не должны повторяться. Попробуйте ещё раз.", reply_markup=cancel
        )
    await state.update_data(poll_opts=opts)
    if data.get("poll_kind") == "quiz":
        await state.set_state(AdminForm.poll_correct)
        rows = [[(f"{i + 1}. {o[:40]}", f"adm:pcor:{i}")] for i, o in enumerate(opts)]
        rows.append([("Отмена", poll_cancel_cb(data))])
        return await m.answer("Какой вариант <b>правильный</b>?", reply_markup=rows_kb(*rows))
    await finish_poll(m, state, m.from_user.id)


@owner.callback_query(AdminForm.poll_correct, F.data.startswith("adm:pcor:"))
async def poll_correct_cb(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    opts = data.get("poll_opts") or []
    idx = c.data.split(":")[2]
    if not idx.isdigit() or int(idx) >= len(opts):
        return await c.answer()
    await state.update_data(poll_correct=int(idx))
    await state.set_state(AdminForm.poll_explain)
    await safe_edit(
        c.message,
        f"Отправьте <b>пояснение</b> к правильному ответу (до {MAX_POLL_EXPLAIN} символов) — "
        "оно появится у человека после ответа. Или нажмите «Пропустить».",
        rows_kb([("Пропустить", "adm:pexp:skip")], [("Отмена", poll_cancel_cb(data))]),
    )
    await c.answer()


@owner.message(AdminForm.poll_explain, F.text)
async def poll_explain_msg(m: Message, state: FSMContext):
    data = await state.get_data()
    text = m.text.strip()
    if not text or len(text) > MAX_POLL_EXPLAIN or text.startswith("/"):
        return await m.answer(
            f"Пояснение должно быть от 1 до {MAX_POLL_EXPLAIN} символов. Попробуйте ещё раз.",
            reply_markup=rows_kb(
                [("Пропустить", "adm:pexp:skip")], [("Отмена", poll_cancel_cb(data))]
            ),
        )
    await finish_poll(m, state, m.from_user.id, text)


@owner.callback_query(AdminForm.poll_explain, F.data == "adm:pexp:skip")
async def poll_explain_skip(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await finish_poll(c.message, state, c.from_user.id, None)


async def finish_poll(msg: Message, state: FSMContext, uid: int, explanation=None) -> None:
    data = await state.get_data()
    kind, q, opts = data.get("poll_kind"), data.get("poll_q"), data.get("poll_opts")
    target, title = data.get("poll_target"), data.get("title")
    correct = data.get("poll_correct") if kind == "quiz" else None
    broken = (
        kind not in POLL_KINDS or not q or not opts
        or (kind == "quiz" and correct is None)
        or target not in ("menu", "announce")
        or (target == "menu" and not title)
    )
    if broken:
        await state.clear()
        return await msg.answer(
            "Что-то пошло не так, начните заново.",
            reply_markup=rows_kb([("В панель", "adm:home")]),
        )

    if target == "menu":
        await state.clear()
        if len(get_items()) >= MAX_ITEMS:
            return await msg.answer(f"Достигнут лимит: {MAX_ITEMS} пунктов.")
        ref = create_poll(kind, q, opts, correct, explanation, uid)
        pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM menu_items").fetchone()[0]
        db.execute(
            "INSERT INTO menu_items (title, mode, prompt, position, poll_ref) "
            "VALUES (?, ?, NULL, ?, ?)",
            (title, kind, pos, ref),
        )
        db.commit()
        return await msg.answer(
            "Пункт добавлен.\n\n" + menu_editor_text(), reply_markup=menu_editor_kb()
        )

    # Анонс: в базу опрос попадёт только при отправке, пока это черновик
    n = len(broadcast_recipients())
    await state.clear()
    if n == 0:
        return await msg.answer(
            "Пока некому отправлять: нет подтверждённых пользователей.",
            reply_markup=rows_kb([("В панель", "adm:home")]),
        )
    await state.set_state(AdminForm.announce)
    await state.update_data(
        draft={"kind": kind, "q": q, "opts": opts, "correct": correct, "explanation": explanation}
    )
    await msg.answer(
        poll_preview(kind, q, opts, correct, explanation)
        + f"\n\nОтправить это {n} пользователям бота?",
        reply_markup=rows_kb(
            [(f"Отправить всем ({n})", "adm:send")], [("Отмена", "adm:home")]
        ),
    )


@owner.message(
    StateFilter(
        AdminForm.item_title,
        AdminForm.item_prompt,
        AdminForm.ban_target,
        AdminForm.limit_value,
        AdminForm.admin_target,
        AdminForm.poll_question,
        AdminForm.poll_options,
        AdminForm.poll_explain,
    )
)
async def adm_need_text(m: Message):
    await m.answer(
        "Нужен обычный текст.", reply_markup=rows_kb([("Отмена", "adm:home")])
    )


# ───────────── Анонсы ─────────────
def broadcast_recipients() -> list[int]:
    rows = db.execute(
        "SELECT id FROM users WHERE verified=1 AND banned=0 AND blocked=0"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in ADMIN_IDS]


@owner.callback_query(F.data == "adm:announce")
async def adm_announce_start(c: CallbackQuery, state: FSMContext):
    n = len(broadcast_recipients())
    await state.set_state(AdminForm.announce)
    await safe_edit(
        c.message,
        "Отправьте анонс <b>одним сообщением</b> — текст, фото, видео, файл, "
        "голосовое (форматирование сохранится).\n"
        "Или создайте опрос, голосование либо викторину кнопками ниже.\n"
        f"Получателей сейчас: <b>{n}</b>.\n\n"
        "Альбомы из нескольких фото не поддерживаются.",
        rows_kb(
            [("Опрос", "adm:apoll:poll"), ("Голосование", "adm:apoll:vote")],
            [("Викторина", "adm:apoll:quiz")],
            [("Отмена", "adm:home")],
        ),
    )
    await c.answer()


@owner.callback_query(F.data.startswith("adm:apoll:"))
async def adm_announce_poll(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[2]
    if kind not in POLL_KINDS:
        return await c.answer()
    if not broadcast_recipients():
        return await c.answer(
            "Пока некому отправлять: нет подтверждённых пользователей.", show_alert=True
        )
    await state.clear()
    await state.update_data(poll_kind=kind, poll_target="announce")
    await state.set_state(AdminForm.poll_question)
    await safe_edit(
        c.message, poll_question_text(kind), rows_kb([("Отмена", "adm:announce")])
    )
    await c.answer()


@owner.message(AdminForm.announce)
async def adm_announce_msg(m: Message, state: FSMContext):
    if m.media_group_id:
        data = await state.get_data()
        if data.get("warned_group") != m.media_group_id:
            await state.update_data(warned_group=m.media_group_id)
            await m.answer("Альбомы не поддерживаются. Отправьте одним сообщением.")
        return
    n = len(broadcast_recipients())
    if n == 0:
        await state.clear()
        return await m.answer(
            "Пока некому отправлять: нет подтверждённых пользователей.",
            reply_markup=rows_kb([("В панель", "adm:home")]),
        )
    await state.update_data(src_chat=m.chat.id, src_msg=m.message_id, draft=None)
    await m.reply(
        f"Отправить это сообщение {n} пользователям бота?",
        reply_markup=rows_kb(
            [(f"Отправить всем ({n})", "adm:send")], [("Отмена", "adm:home")]
        ),
    )


async def copy_with_retry(bot: Bot, uid: int, src_chat: int, src_msg: int) -> None:
    try:
        await bot.copy_message(uid, src_chat, src_msg)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        await bot.copy_message(uid, src_chat, src_msg)


async def run_broadcast(bot: Bot, report_chat: int, src_chat: int, src_msg: int,
                        recipients: list[int], poll_ref: int | None = None) -> None:
    global broadcast_running
    ok = blocked = failed = 0
    try:
        for uid in recipients:
            try:
                if poll_ref is not None:
                    await send_poll_to(bot, uid, poll_ref)
                else:
                    await copy_with_retry(bot, uid, src_chat, src_msg)
                ok += 1
            except TelegramForbiddenError:
                blocked += 1  # пользователь заблокировал бота — больше не шлём
                set_flag(uid, "blocked", 1)
            except TelegramAPIError:
                failed += 1
                logging.exception("Рассылка: не доставлено пользователю %s", uid)
            await asyncio.sleep(BROADCAST_DELAY)
    finally:
        broadcast_running = False
    try:
        await bot.send_message(
            report_chat,
            "<b>Рассылка завершена</b>\n"
            f"Доставлено: {ok}\n"
            f"Заблокировали бота: {blocked}\n"
            f"Другие ошибки: {failed}",
            reply_markup=rows_kb([("В панель", "adm:home")]),
        )
    except TelegramAPIError:
        logging.exception("Не удалось отправить отчёт о рассылке")


@owner.callback_query(AdminForm.announce, F.data == "adm:send")
async def adm_announce_send(c: CallbackQuery, state: FSMContext, bot: Bot):
    global broadcast_running
    data = await state.get_data()
    src_chat, src_msg = data.get("src_chat"), data.get("src_msg")
    draft = data.get("draft")
    if not src_msg and not draft:
        return await c.answer("Сначала отправьте сообщение с анонсом.", show_alert=True)
    if broadcast_running:
        return await c.answer("Другая рассылка ещё идёт, подождите.", show_alert=True)
    recipients = broadcast_recipients()
    poll_ref = None
    if draft:
        poll_ref = create_poll(
            draft["kind"], draft["q"], draft["opts"], draft.get("correct"),
            draft.get("explanation"), c.from_user.id,
        )
    await state.clear()
    broadcast_running = True
    await safe_edit(
        c.message,
        f"Рассылка запущена: {len(recipients)} получателей. Пришлю отчёт, когда закончу.",
    )
    await c.answer()
    task = asyncio.create_task(
        run_broadcast(bot, c.message.chat.id, src_chat, src_msg, recipients, poll_ref)
    )
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


# ───────────── Опросы и викторины: результаты ─────────────
def poll_results_text(p: dict) -> str:
    votes = db.execute("SELECT options FROM poll_votes WHERE poll_ref=?", (p["id"],)).fetchall()
    counts = [0] * len(p["options"])
    for (raw,) in votes:
        for i in json.loads(raw):
            if 0 <= i < len(counts):
                counts[i] += 1
    total = len(votes)
    got = db.execute(
        "SELECT COUNT(DISTINCT user_id) FROM poll_sent WHERE poll_ref=?", (p["id"],)
    ).fetchone()[0]
    lines = [f"<b>{POLL_KINDS[p['kind']]} #{p['id']}</b>", escape(p["question"]), ""]
    for i, (opt, n) in enumerate(zip(p["options"], counts), 1):
        pct = round(n * 100 / total) if total else 0
        mark = " (верный)" if p["kind"] == "quiz" and p["correct"] == i - 1 else ""
        lines.append(f"{i}. {escape(opt)}{mark} — {n} ({pct}%)")
    lines.append(f"\nПолучили: {got}, ответили: {total}")
    if p["kind"] == "quiz" and total:
        lines.append(f"Ответили верно: {counts[p['correct']]} из {total}")
    if p["kind"] == "poll":
        lines.append("Можно было выбрать несколько вариантов, проценты — от числа ответивших.")
    return "\n".join(lines)


@owner.callback_query(F.data == "pl:home")
async def pl_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = db.execute(
        "SELECT p.id, p.kind, p.question, "
        "(SELECT COUNT(*) FROM poll_votes v WHERE v.poll_ref = p.id) "
        "FROM polls p ORDER BY p.id DESC LIMIT 10"
    ).fetchall()
    if rows:
        text = "<b>Опросы и викторины</b>\n\nПоследние 10 (в скобках — сколько ответили):"
    else:
        text = (
            "<b>Опросы и викторины</b>\n\nПока ничего нет. Создайте их через "
            "«Пункты меню» или «Анонс пользователям»."
        )
    kb_rows = [
        [(f"{POLL_KINDS[kind]} #{pid}: {q[:25]} ({n})", f"pl:view:{pid}")]
        for pid, kind, q, n in rows
    ]
    kb_rows.append([("Назад", "adm:home")])
    await safe_edit(c.message, text, rows_kb(*kb_rows))
    await c.answer()


@owner.callback_query(F.data.startswith("pl:view:"))
async def pl_view(c: CallbackQuery):
    ref = c.data.split(":")[2]
    p = get_poll(int(ref)) if ref.isdigit() else None
    if not p:
        return await c.answer("Не нашёл этот опрос.", show_alert=True)
    await safe_edit(
        c.message,
        poll_results_text(p),
        rows_kb([("Обновить", f"pl:view:{ref}")], [("К списку", "pl:home")]),
    )
    await c.answer()


# ───────────── Обновление с GitHub (только супер-админы) ─────────────
def short(text: str, n: int = 700) -> str:
    text = text.strip()
    return escape(text if len(text) <= n else "…" + text[-n:])


async def run_cmd(*args: str, timeout: int = 60) -> tuple[int, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=BASE_DIR,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, f"Команда не найдена: {args[0]}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "Превышено время ожидания."
    return proc.returncode, out.decode(errors="replace").strip()


async def git(*args: str, timeout: int = 60) -> tuple[int, str]:
    return await run_cmd("git", *args, timeout=timeout)


async def update_home_text() -> tuple[str, bool]:
    rc, out = await git("log", "-1", "--format=%h|%cd|%s", "--date=format:%d.%m.%Y %H:%M")
    if rc != 0 or out.count("|") < 2:
        return (
            "<b>Обновление бота</b>\n\n"
            "Не удалось определить версию: папка бота не является git-репозиторием "
            "или git не установлен.\n\n"
            "Чтобы обновления работали, разверните бота командой "
            "<code>git clone &lt;адрес репозитория&gt;</code> и запускайте его из этой папки.\n\n"
            f"<i>{short(out, 300)}</i>"
        ), False
    h, d, subj = out.split("|", 2)
    rc, url = await git("remote", "get-url", "origin")
    url = re.sub(r"//[^/@]+@", "//", url) if rc == 0 else "не задан"  # без токенов в URL
    rc, branch = await git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    branch = branch if rc == 0 else "не задана (нет upstream)"
    return (
        "<b>Обновление бота</b>\n\n"
        f"Версия: <code>{escape(h)}</code> от {escape(d)}\n"
        f"Последнее изменение: {escape(subj)}\n"
        f"Источник: {escape(url)}\n"
        f"Ветка: {escape(branch)}"
    ), True


async def check_updates() -> tuple[str | None, int, list[str], bool]:
    # возвращает (ошибка, сколько коммитов позади, список коммитов, есть ли локальные правки)
    rc, out = await git("fetch", "--prune", timeout=90)
    if rc != 0:
        return f"Не удалось получить обновления:\n<pre>{short(out)}</pre>", 0, [], False
    rc, up = await git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc != 0:
        return (
            "У текущей ветки нет upstream. На сервере выполните, например:\n"
            "<code>git branch --set-upstream-to=origin/main</code>",
            0, [], False,
        )
    rc, cnt = await git("rev-list", "--count", "HEAD..@{u}")
    behind = int(cnt) if rc == 0 and cnt.isdigit() else 0
    commits: list[str] = []
    if behind:
        rc, log = await git("log", "--format=%h %s", "-n", "10", "HEAD..@{u}")
        commits = log.splitlines() if rc == 0 else []
    rc, dirty = await git("status", "--porcelain", "--untracked-files=no")
    return None, behind, commits, bool(rc == 0 and dirty.strip())


async def apply_update(notify_chat: int) -> tuple[str, bool]:
    # возвращает (текст для админа, нужно ли перезапускаться)
    rc, old = await git("rev-parse", "HEAD")
    if rc != 0:
        return f"Это не git-репозиторий:\n<pre>{short(old, 400)}</pre>", False
    rc, out = await git("pull", "--ff-only", timeout=180)
    if rc != 0:
        return f"Не удалось обновить:\n<pre>{short(out)}</pre>", False
    rc, new = await git("rev-parse", "HEAD")
    if rc != 0 or new == old:
        return "Обновлений нет — уже установлена последняя версия.", False

    rc, changed = await git("diff", "--name-only", old, new)
    if rc == 0 and "requirements.txt" in changed.splitlines():
        rc, out = await run_cmd(
            sys.executable, "-m", "pip", "install", "-r", "requirements.txt", timeout=600
        )
        if rc != 0:
            await git("reset", "--keep", old)
            return (
                "Не удалось установить зависимости, изменения откатил.\n"
                f"<pre>{short(out)}</pre>",
                False,
            )

    # Проверяем, что новая версия хотя бы компилируется, иначе бот не поднимется
    rc, out = await run_cmd(sys.executable, "-m", "py_compile", os.path.abspath(__file__))
    if rc != 0:
        await git("reset", "--keep", old)
        return (
            "Новая версия не прошла проверку (ошибка в коде), изменения откатил.\n"
            f"<pre>{short(out)}</pre>",
            False,
        )

    _, short_old = await git("rev-parse", "--short", old)
    _, short_new = await git("rev-parse", "--short", new)
    set_setting("restart_notice", f"{notify_chat}|{short_old}|{short_new}")
    return (
        f"Обновлено: <code>{escape(short_old)}</code> → <code>{escape(short_new)}</code>.\n"
        "Перезапускаюсь…",
        True,
    )


async def restart_later() -> None:
    # Пауза нужна, чтобы Telegram успел подтвердить обработку этого обновления
    await asyncio.sleep(2)
    logging.info("Перезапуск после обновления")
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except OSError:
        logging.exception("Не удалось перезапустить процесс — перезапустите бота вручную")


@owner.callback_query(F.data == "upd:home", SUPER)
async def upd_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    text, is_repo = await update_home_text()
    rows = [[("Проверить обновления", "upd:check")]] if is_repo else []
    rows.append([("Назад", "adm:home")])
    await safe_edit(c.message, text, rows_kb(*rows))


@owner.callback_query(F.data == "upd:check", SUPER)
async def upd_check(c: CallbackQuery):
    await c.answer()
    await safe_edit(c.message, "Проверяю обновления…")
    err, behind, commits, dirty = await check_updates()
    back = [("Назад", "upd:home")]
    if err:
        return await safe_edit(
            c.message, err, rows_kb([("Проверить снова", "upd:check")], back)
        )
    if behind == 0:
        return await safe_edit(
            c.message, "Установлена последняя версия, обновлений нет.", rows_kb(back)
        )
    lines = "\n".join(f"• {escape(x)}" for x in commits)
    more = f"\n…и ещё {behind - len(commits)}" if behind > len(commits) else ""
    warn = (
        "\n\nВнимание: в папке есть локальные правки отслеживаемых файлов. "
        "Если они пересекутся с обновлением, оно не применится."
        if dirty
        else ""
    )
    await safe_edit(
        c.message,
        f"<b>Доступно обновлений: {behind}</b>\n\n{lines}{more}{warn}",
        rows_kb([("Установить и перезапустить", "upd:apply")], back),
    )


@owner.callback_query(F.data == "upd:apply", SUPER)
async def upd_apply(c: CallbackQuery):
    if broadcast_running:
        return await c.answer("Идёт рассылка — дождитесь её окончания.", show_alert=True)
    if update_lock.locked():
        return await c.answer("Обновление уже выполняется.", show_alert=True)
    async with update_lock:
        await c.answer()
        await safe_edit(c.message, "Устанавливаю обновление…")
        text, restart = await apply_update(c.message.chat.id)
        await safe_edit(
            c.message, text, None if restart else rows_kb([("Назад", "upd:home")])
        )
        if restart:
            task = asyncio.create_task(restart_later())
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)


@owner.callback_query(F.data.startswith("upd:"))
async def upd_denied(c: CallbackQuery):
    # сюда попадают только обычные админы (супер-админов перехватили обработчики выше)
    await c.answer("Обновление доступно только супер-админам.", show_alert=True)


@owner.message(F.reply_to_message)
async def owner_reply(m: Message, bot: Bot):
    row = db.execute(
        "SELECT m.id, m.user_id, m.kind, m.text, m.answered "
        "FROM admin_msgs a JOIN messages m ON m.id = a.message_id "
        "WHERE a.chat_id=? AND a.msg_id=?",
        (m.chat.id, m.reply_to_message.message_id),
    ).fetchone()
    if not row:
        return await m.answer("Не нашёл, к какому обращению относится это сообщение.")
    msg_id, user_id, kind, text, was_answered = row
    # Отвечать можно и на вопросы, и на предложения (на предложения — по желанию)
    is_question = kind == "question"
    what = "вопрос" if is_question else "предложение"          # «ответ на вопрос/предложение»
    your = "Ваш вопрос" if is_question else "Ваше предложение"  # цитата в ответе
    reply_title = "Ответ на ваш вопрос" if is_question else "Ответ на ваше предложение"

    preview = text if len(text) <= 300 else text[:300] + "…"
    single = bool(m.text) and len(m.html_text) < 3500  # можно одним сообщением?

    try:
        if single:
            # Единое сообщение: цитата вопроса + ответ
            await bot.send_message(
                user_id,
                f"<b>{your}</b>\n"
                f"<blockquote>{escape(preview)}</blockquote>\n\n"
                f"<b>Ответ</b>\n"
                f"{m.html_text}",
            )
        else:
            # Фото, голосовое, файл или очень длинный текст — двумя сообщениями
            await bot.send_message(
                user_id,
                f"<b>{reply_title}</b>\n<blockquote>{escape(preview)}</blockquote>",
            )
            await bot.copy_message(user_id, m.chat.id, m.message_id)
    except TelegramForbiddenError:
        return await m.answer("Пользователь заблокировал бота — ответ не доставлен.")
    except TelegramAPIError as e:
        return await m.answer(f"Ошибка отправки: {escape(str(e))}")

    db.execute("UPDATE messages SET answered=1 WHERE id=?", (msg_id,))
    db.commit()
    note = " (ранее на него уже отвечали)" if was_answered else ""
    await m.answer(f"Ответ на {what} #{msg_id} отправлен{note}.")

    # Сообщаем остальным администраторам, кто и что ответил
    for aid in ADMIN_IDS - {m.from_user.id}:
        try:
            who = escape(m.from_user.full_name)
            if single:
                await bot.send_message(
                    aid, f"{who} ответил на {what} #{msg_id}:\n\n{m.html_text}"
                )
            else:
                await bot.send_message(aid, f"{who} ответил на {what} #{msg_id}:")
                await bot.copy_message(aid, m.chat.id, m.message_id)
        except TelegramAPIError:
            pass


@owner.message()
async def owner_hint(m: Message):
    await m.answer(
        "Чтобы ответить на вопрос или предложение, нажмите <b>«Ответить» (Reply)</b> "
        "на сообщении с ним и напишите ответ.\n"
        "Обычное сообщение без реплая бот никому не отправляет.",
        reply_markup=rows_kb([("Открыть панель", "adm:home")]),
    )


# ───────────────────────── Роутер пользователей ─────────────────────────
user = Router()


async def is_regular_user(event) -> bool:
    """Пропускает только не-владельцев и не забаненных."""
    uid = event.from_user.id
    return uid not in ADMIN_IDS and not is_banned(uid)


class TouchMiddleware(BaseMiddleware):
    """Обновляет имя, username и время последней активности при любом действии."""

    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u is not None and not u.is_bot and u.id not in ADMIN_IDS:
            try:
                touch_user(u)
            except Exception:
                logging.exception("Не удалось обновить профиль пользователя")
        return await handler(event, data)


user.message.outer_middleware(TouchMiddleware())
user.callback_query.outer_middleware(TouchMiddleware())
user.message.filter(is_regular_user)
user.callback_query.filter(is_regular_user)


async def send_captcha(m: Message, state: FSMContext):
    a, b = random.randint(2, 9), random.randint(2, 9)
    answer = a + b
    options = {answer}
    while len(options) < 4:
        options.add(max(1, answer + random.randint(-5, 5)))
    options = list(options)
    random.shuffle(options)

    await state.update_data(captcha=answer)
    kb = InlineKeyboardBuilder()
    for o in options:
        kb.button(text=str(o), callback_data=f"cap:{o}")
    kb.adjust(4)
    await m.answer(f"Сначала проверка, что вы не бот.\nСколько будет <b>{a} + {b}</b>?",
                   reply_markup=kb.as_markup())


@user.message(CommandStart())
@user.message(Command("menu"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    ensure_user(m.from_user.id)
    if not is_verified(m.from_user.id):
        return await send_captcha(m, state)
    await m.answer(WELCOME, reply_markup=menu_kb())


@user.callback_query(F.data.startswith("cap:"))
async def captcha_answer(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    expected = data.get("captcha")
    if expected is None:
        await c.answer("Нажмите /start", show_alert=True)
        return
    if c.data.split(":")[1] == str(expected):
        set_flag(c.from_user.id, "verified", 1)
        await state.clear()
        await c.message.edit_text("Проверка пройдена!")
        await c.message.answer(WELCOME, reply_markup=menu_kb())
    else:
        await c.message.delete()
        await send_captcha(c.message, state)
    await c.answer()


@user.callback_query(F.data.startswith("kind:"))
async def choose_kind(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_verified(c.from_user.id):
        await c.answer("Сначала нажмите /start и пройдите проверку.", show_alert=True)
        return
    item = get_item(c.data.split(":")[1])
    if not item:
        await c.answer("Этот пункт больше недоступен.", show_alert=True)
        try:
            await c.message.edit_reply_markup(reply_markup=menu_kb())  # обновим меню
        except TelegramAPIError:
            pass
        return
    _id, title, mode, prompt = item
    if mode in POLL_KINDS:
        await state.clear()
        ref = item_poll_ref(_id)
        if not ref or not get_poll(ref):
            return await c.answer("Этот пункт временно недоступен.", show_alert=True)
        if db.execute(
            "SELECT 1 FROM poll_votes WHERE poll_ref=? AND user_id=?", (ref, c.from_user.id)
        ).fetchone():
            return await c.answer("Вы уже участвовали — спасибо!", show_alert=True)
        await c.answer()
        try:
            await send_poll_to(bot, c.from_user.id, ref)
        except TelegramAPIError:
            logging.exception("Не удалось отправить опрос пользователю %s", c.from_user.id)
            await c.message.answer("Не получилось отправить, попробуйте позже.")
        return
    await state.set_state(Form.waiting_text)
    await state.update_data(kind=mode, item_title=title)
    await c.message.answer(prompt or DEFAULT_PROMPTS[mode], reply_markup=cancel_kb())
    await c.answer()


@user.callback_query(F.data == "cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Отменено.")
    await c.message.answer("Выберите действие", reply_markup=menu_kb())
    await c.answer()


@user.message(Form.waiting_text, F.text)
async def receive_text(m: Message, state: FSMContext, bot: Bot):
    uid = m.from_user.id
    if not is_verified(uid):
        await state.clear()
        return await m.answer("Нажмите /start и пройдите проверку.")

    text = m.text.strip()
    error = check_text(uid, text)
    if error:
        return await m.answer(f"{error}", reply_markup=cancel_kb())

    data = await state.get_data()
    kind = data.get("kind", "question")
    title = data.get("item_title") or MODES[kind]

    cur = db.execute(
        "INSERT INTO messages (user_id, kind, text, created, item_title) "
        "VALUES (?, ?, ?, ?, ?)",
        (uid, kind, text, int(time.time()), title),
    )
    msg_id = cur.lastrowid
    db.commit()

    u = m.from_user
    name = escape(u.full_name)
    uname = f"@{u.username}" if u.username else "нет username"
    header = (
        f"<b>{escape(title)} #{msg_id}</b>\n"
        f"От: <a href=\"tg://user?id={uid}\">{name}</a> ({escape(uname)}), "
        f"id <code>{uid}</code>\n\n"
    )
    footer = (
        "\n\n<i>Ответьте реплаем на это сообщение — ответ уйдёт автору.</i>"
        if kind == "question"
        else "\n\n<i>Ответ необязателен. Если хотите ответить автору — "
             "ответьте реплаем на это сообщение.</i>"
    )

    delivered = False
    for aid in list(ADMIN_IDS):
        try:
            sent = await bot.send_message(
                aid, header + escape(text) + footer, reply_markup=author_kb(uid)
            )
            db.execute(
                "INSERT OR REPLACE INTO admin_msgs (chat_id, msg_id, message_id) "
                "VALUES (?, ?, ?)",
                (aid, sent.message_id, msg_id),
            )
            delivered = True
        except TelegramAPIError:
            logging.exception("Не удалось отправить администратору %s", aid)
    db.commit()

    if not delivered:
        db.execute("DELETE FROM messages WHERE id=?", (msg_id,))
        db.commit()
        await state.clear()
        return await m.answer("Не получилось отправить, попробуйте позже.")

    await state.clear()

    done = (
        "Вопрос отправлен! Ответ придёт в этот чат."
        if kind == "question"
        else "Спасибо! Предложение отправлено. Если захотим что-то уточнить или ответить, напишем сюда."
    )
    await m.answer(done, reply_markup=menu_kb())


@user.message(Form.waiting_text)
async def receive_non_text(m: Message):
    await m.answer("Пожалуйста, отправьте <b>текстом</b>.", reply_markup=cancel_kb())


@user.message()
async def fallback(m: Message, state: FSMContext):
    ensure_user(m.from_user.id)
    if not is_verified(m.from_user.id):
        return await send_captcha(m, state)
    await m.answer("Выберите, что хотите отправить:", reply_markup=menu_kb())


# ───────────────────────── Запуск ─────────────────────────
async def main():
    logging.basicConfig(level=logging.INFO)
    # Необязательные обходы проблем с сетью:
    #   PROXY_URL=socks5://127.0.0.1:1080  (или http://host:port)
    #   IPV4_ONLY=1                        (если сломан IPv6)
    proxy = os.getenv("PROXY_URL")
    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    if os.getenv("IPV4_ONLY"):
        session._connector_init["family"] = socket.AF_INET
    bot = Bot(
        BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(common)
    dp.include_router(owner)
    dp.include_router(user)
    await bot.delete_webhook(drop_pending_updates=False)

    # Кнопка «Меню» рядом с полем ввода: показывает список команд бота
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Запустить бота"),
                BotCommand(command="menu", description="Главное меню"),
                BotCommand(command="id", description="Узнать свой Telegram ID"),
            ]
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError:
        logging.exception("Не удалось настроить кнопку меню")

    if not SUPER_ADMIN_IDS:
        logging.warning(
            "SUPER_ADMIN_IDS не задан и в панели нет супер-админов — обновление, "
            "лимиты и управление админами никому не доступны."
        )

    # Если перезапуск был после обновления — сообщим тому, кто его запустил
    notice_chat, notice_text = None, ""
    notice = get_setting("restart_notice")
    if notice:
        del_setting("restart_notice")
        try:
            chat_s, old_s, new_s = notice.split("|")
            notice_chat = int(chat_s)
            notice_text = f"Бот обновлён и перезапущен: {old_s} → {new_s}."
        except ValueError:
            pass

    # Проверяем, что бот может писать каждому администратору
    for aid in list(ADMIN_IDS):
        try:
            await bot.send_message(
                aid,
                notice_text
                if (aid == notice_chat and notice_text)
                else "Бот запущен и видит ваш аккаунт.",
            )
        except TelegramAPIError as e:
            logging.error(
                "НЕ МОГУ НАПИСАТЬ АДМИНИСТРАТОРУ %s: %s. "
                "Пусть он откроет бота, нажмёт Start и проверит ID командой /id.",
                aid,
                e,
            )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
