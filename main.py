import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder



logging.basicConfig(level=logging.INFO)
router = Router()

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# Все основные настройки находятся прямо в этом файле.
# При первом запуске рядом с main.py автоматически создаётся config.json.
# Заполните его и перезапустите бота.

CONFIG_FILE = Path("config.json")

DEFAULT_CONFIG = {
    "BOT_TOKEN": "8874041800:AAHQ4xne8zQE_9EClNIgnc8ig8IsIrLN9uc",
    "ADMIN_IDS": [1940800577],
    "REVIEWS_CHANNEL": "@your_reviews_channel",
    "CONTACT_USERNAME": "@Weretyol",
}


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            "\nСоздан config.json.\n"
            "Откройте его, укажите BOT_TOKEN и ADMIN_IDS, "
            "затем снова запустите бота.\n"
        )
        return DEFAULT_CONFIG.copy()

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"Не удалось прочитать config.json: {e}")

    config = DEFAULT_CONFIG.copy()
    config.update(data)
    return config


CONFIG = load_config()

BOT_TOKEN = str(CONFIG["BOT_TOKEN"])
ADMIN_IDS = [int(x) for x in CONFIG.get("ADMIN_IDS", [])]
REVIEWS_CHANNEL = str(CONFIG.get("REVIEWS_CHANNEL", ""))
CONTACT_USERNAME = str(CONFIG.get("CONTACT_USERNAME", "@Weretyol"))
DB_PATH = Path("data/database.json")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# JSON DATABASE
# ---------------------------------------------------------------------------

DEFAULT_DB = {
    "users": {},
    "orders": {},
    "reviews": [],
    "support": [],
    "next_order_id": 1,
}


def load_db() -> dict[str, Any]:
    if not DB_PATH.exists():
        save_db(DEFAULT_DB)
        return json.loads(json.dumps(DEFAULT_DB))
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = json.loads(json.dumps(DEFAULT_DB))
    for key, value in DEFAULT_DB.items():
        data.setdefault(key, json.loads(json.dumps(value)))
    return data


DB = load_db()


def save_db() -> None:
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(DB, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(DB_PATH)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_user_record(user_id: int, username: str | None = None, full_name: str | None = None) -> dict[str, Any]:
    key = str(user_id)
    if key not in DB["users"]:
        DB["users"][key] = {
            "id": user_id,
            "username": username or "",
            "full_name": full_name or "",
            "purchases": 0,
            "notifications": True,
            "ban_until": None,
            "ban_permanent": False,
        }
    else:
        if username is not None:
            DB["users"][key]["username"] = username
        if full_name is not None:
            DB["users"][key]["full_name"] = full_name
    save_db()
    return DB["users"][key]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def username_of(user_id: int) -> str:
    u = DB["users"].get(str(user_id), {})
    return ("@" + u.get("username", "")).strip("@") if u.get("username") else f"id{user_id}"


def display_username(user_id: int) -> str:
    u = DB["users"].get(str(user_id), {})
    if u.get("username"):
        return "@" + u["username"]
    return u.get("full_name") or f"id{user_id}"


def is_banned(user_id: int) -> bool:
    if is_admin(user_id):
        return False
    u = DB["users"].get(str(user_id))
    if not u:
        return False
    if u.get("ban_permanent"):
        return True
    value = u.get("ban_until")
    if not value:
        return False
    try:
        until = datetime.fromisoformat(value)
    except ValueError:
        return False
    if datetime.now(timezone.utc) < until:
        return True
    u["ban_until"] = None
    save_db()
    return False


def ban_text(user_id: int) -> str:
    u = DB["users"].get(str(user_id), {})
    if u.get("ban_permanent"):
        return "навсегда"
    if u.get("ban_until"):
        try:
            return datetime.fromisoformat(u["ban_until"]).strftime("%d.%m.%Y %H:%M UTC")
        except ValueError:
            pass
    return "нет"


# ---------------------------------------------------------------------------
# UI
# Все пользовательские меню сделаны inline-кнопками: они отображаются прямо
# под сообщением бота и не создают отдельную клавиатуру Telegram.

def _inline_menu(rows: list[tuple[str, str]], width: int = 2) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, data in rows:
        kb.button(text=text, callback_data=data)
    kb.adjust(width)
    return kb.as_markup()


def main_menu() -> InlineKeyboardMarkup:
    return _inline_menu([
        ("👤 Профиль", "menu:profile"),
        ("🛒 Заказать", "menu:order"),
        ("✨ Отзывы", "menu:reviews"),
        ("📋 Список заказов", "menu:orders"),
        ("💸 Ценники", "menu:prices"),
        ("🆘 Тех. поддержка", "menu:support"),
        ("⚙️ Настройки", "menu:settings"),
    ], 2)


def back_menu() -> InlineKeyboardMarkup:
    return _inline_menu([("⬅️ Главное меню", "menu:back")], 1)


def order_types_menu() -> InlineKeyboardMarkup:
    return _inline_menu([
        ("🤖 Бот", "order_type:bot"),
        ("🌐 Сайт", "order_type:site"),
        ("🎮 Игра", "order_type:game"),
        ("📱 Приложение", "order_type:app"),
        ("⬅️ Назад", "menu:back"),
    ], 2)


def settings_menu(user_id: int) -> InlineKeyboardMarkup:
    u = DB["users"].get(str(user_id), {})
    status = "✅ Вкл." if u.get("notifications", True) else "❌ Выкл."
    return _inline_menu([
        (f"🔔 Уведомления: {status}", "settings:notifications"),
        ("⬅️ Главное меню", "menu:back"),
    ], 1)


def donate_menu() -> InlineKeyboardMarkup:
    return _inline_menu([
        ("💎 Имеется", "donate:yes"),
        ("🚫 Не имеется", "donate:no"),
        ("⬅️ Назад", "menu:back"),
    ], 2)


def hosting_menu() -> InlineKeyboardMarkup:
    return _inline_menu([
        ("🛜 Нужен", "hosting:yes"),
        ("🚫 Не нужен", "hosting:no"),
        ("⬅️ Назад", "menu:back"),
    ], 2)


def platform_menu() -> InlineKeyboardMarkup:
    return _inline_menu([
        ("📱 Telegram", "platform:telegram"),
        ("💬 MAX", "platform:max"),
        ("🎮 Discord", "platform:discord"),
        ("🌐 VK", "platform:vk"),
        ("⬅️ Назад", "menu:back"),
    ], 2)


def admin_menu() -> InlineKeyboardMarkup:
    return _inline_menu([
        ("📦 Заказы", "admin:orders"),
        ("👥 Пользователи", "admin:users"),
        ("📊 Статистика", "admin:stats"),
        ("🛠 Команды", "admin:commands"),
        ("⬅️ Назад", "admin:back"),
    ], 2)


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять заказ", callback_data=f"order_accept:{order_id}")
    kb.button(text="❌ Отказать", callback_data=f"order_reject:{order_id}")
    kb.adjust(2)
    return kb.as_markup()


def admin_support_kb(ticket_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Ответить", callback_data=f"support_reply:{ticket_id}")
    kb.button(text="❌ Игнорировать", callback_data=f"support_ignore:{ticket_id}")
    kb.adjust(2)
    return kb.as_markup()


def complete_order_kb(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнил заказ", callback_data=f"order_done:{order_id}")
    return kb.as_markup()


def rating_kb(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in range(1, 6):
        kb.button(text="⭐️" * n, callback_data=f"rate:{order_id}:{n}")
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------------------
# STATES
# ---------------------------------------------------------------------------

class OrderForm(StatesGroup):
    name = State()
    description = State()
    theme = State()
    purpose = State()
    donate = State()
    hosting = State()
    platform = State()


class AdminAcceptForm(StatesGroup):
    project_name = State()
    price = State()


class AdminRejectForm(StatesGroup):
    reason = State()


class ReviewForm(StatesGroup):
    text = State()


class SupportForm(StatesGroup):
    text = State()


class SupportReplyForm(StatesGroup):
    text = State()


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

TYPE_DATA = {
    "bot": {
        "button": "🤖 Заказать Бота",
        "title": "Бота",
        "emoji": "🤖",
        "desc": "бота",
        "extra": ["donate", "hosting", "platform"],
    },
    "site": {
        "button": "🌐 Заказать Сайт",
        "title": "Сайта",
        "emoji": "🌐",
        "desc": "сайта",
        "extra": ["donate", "hosting"],
    },
    "game": {
        "button": "🎮 Заказать Игру",
        "title": "Игры",
        "emoji": "🎮",
        "desc": "игры",
        "extra": ["donate", "hosting"],
    },
    "app": {
        "button": "📱 Заказать приложение",
        "title": "Приложения",
        "emoji": "📱",
        "desc": "приложения",
        "extra": ["donate"],
    },
}


def order_header(order: dict[str, Any]) -> str:
    t = TYPE_DATA[order["type"]]
    return f"{t['emoji']} НОВЫЙ ЗАКАЗ #{order['id']}"


def format_order_for_admin(order: dict[str, Any]) -> str:
    t = TYPE_DATA[order["type"]]
    lines = [
        order_header(order),
        "",
        f"👤 Пользователь — {display_username(order['user_id'])}",
        f"🆔 ID: <code>{order['user_id']}</code>",
        "",
        f"✏️ Название: {escape_html(order['name'])}",
        f"✨ Описание {t['desc']}: {escape_html(order['description'])}",
        "",
        f"🪄 Тематика: {escape_html(order['theme'])}",
        f"💎 Смысл {t['desc']}: {escape_html(order['purpose'])}",
        "",
        f"💸 Донат: {escape_html(order['donate'])}",
        f"🛜 Хостинг: {escape_html(order['hosting'])}",
    ]
    if order["type"] == "bot":
        lines.append(f"📱 Платформа: {escape_html(order['platform'])}")
    lines += [
        "",
        f"📌 Статус: {escape_html(order['status'])}",
    ]
    return "\n".join(lines)


def escape_html(value: Any) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def send_to_admins(bot: Bot, text: str, reply_markup: Any = None) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:
            logging.warning("Cannot message admin %s: %s", admin_id, e)


async def notify_user(bot: Bot, user_id: int, text: str, reply_markup: Any = None) -> None:
    u = DB["users"].get(str(user_id), {})
    if not u.get("notifications", True):
        return
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
    except Exception as e:
        logging.warning("Cannot notify user %s: %s", user_id, e)


def create_order(user_id: int, order_type: str, data: dict[str, Any]) -> dict[str, Any]:
    order_id = int(DB["next_order_id"])
    DB["next_order_id"] = order_id + 1
    order = {
        "id": order_id,
        "user_id": user_id,
        "type": order_type,
        "name": data["name"],
        "description": data["description"],
        "theme": data["theme"],
        "purpose": data["purpose"],
        "donate": data.get("donate", "Не указано"),
        "hosting": data.get("hosting", "Не указано"),
        "platform": data.get("platform", "Не указано"),
        "status": "Ожидание принятия заказа",
        "project_name": "",
        "price": "",
        "reject_reason": "",
        "created_at": now_iso(),
        "completed_at": None,
        "review": "",
        "rating": None,
    }
    DB["orders"][str(order_id)] = order
    save_db()
    return order


def get_order(order_id: int) -> dict[str, Any] | None:
    return DB["orders"].get(str(order_id))


def order_position(order_id: int, user_id: int) -> int:
    orders = [
        o for o in DB["orders"].values()
        if o["user_id"] == user_id and o["status"] not in ("Отклонён",)
    ]
    orders.sort(key=lambda x: x["id"])
    for i, o in enumerate(orders, 1):
        if o["id"] == order_id:
            return i
    return 0


def find_user_target(raw: str) -> int | None:
    raw = raw.strip()
    if raw.isdigit():
        uid = int(raw)
        return uid if str(uid) in DB["users"] else None
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.lower()
    for uid, user in DB["users"].items():
        if user.get("username", "").lower() == raw:
            return int(uid)
    return None


# ---------------------------------------------------------------------------
# START / COMMON
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    get_user_record(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    if is_banned(message.from_user.id):
        await message.answer(
            f"⛔ Доступ к боту ограничен.\nСрок блокировки: {ban_text(message.from_user.id)}"
        )
        return
    await message.answer(
        "╭── ✦ WERET | PRODUCTION ✦ ──╮\n\n"
        "👋 Добро пожаловать в официальный бот компании!\n\n"
        "💻 Разработка ботов, сайтов, игр и приложений\n"
        "📋 Оформление и отслеживание заказов\n"
        "🆘 Поддержка и связь с администрацией\n"
        "✨ Отзывы клиентов и рейтинг компании\n\n"
        "Выберите нужный раздел ниже.\n"
        "╰────────────────────────╯",
        reply_markup=main_menu(),
    )


@router.message(Command("cancel"))
@router.message(F.text == "⬅️ Назад")
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_banned(message.from_user.id):
        await message.answer("⛔ Доступ ограничен.", reply_markup=main_menu())
        return
    await message.answer("🏠 <b>Главное меню Weret | Production</b>", reply_markup=main_menu())


# ---------------------------------------------------------------------------
# INLINE MENU CALLBACKS
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:back")
async def cb_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await cancel(callback.message, state)


@router.callback_query(F.data == "menu:profile")
async def cb_profile(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await profile(callback.message, state)


@router.callback_query(F.data == "menu:order")
async def cb_order_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await order_menu(callback.message, state)


@router.callback_query(F.data == "menu:orders")
async def cb_order_list(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await order_list(callback.message, state)


@router.callback_query(F.data == "menu:reviews")
async def cb_reviews(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await reviews(callback.message, state)


@router.callback_query(F.data == "menu:prices")
async def cb_prices(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await prices(callback.message, state)


@router.callback_query(F.data == "menu:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await settings(callback.message, state)


@router.callback_query(F.data == "menu:support")
async def cb_support(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await support_start(callback.message, state)


@router.callback_query(F.data.startswith("order_type:"))
async def cb_order_type(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    order_type = callback.data.split(":", 1)[1]
    if order_type not in TYPE_DATA:
        return
    await begin_order(callback.message, state, order_type)


@router.callback_query(F.data.startswith("donate:"))
async def cb_donate(callback: CallbackQuery, state: FSMContext) -> None:
    text = callback.data.split(":", 1)[1]
    if text not in {"yes", "no"}:
        await callback.answer("Выберите вариант кнопкой.", show_alert=True)
        return
    donate = "Имеется" if text == "yes" else "Не имеется"
    await state.update_data(donate=donate)
    data = await state.get_data()
    if data.get("order_type") not in TYPE_DATA:
        await callback.answer("Сессия заказа устарела.", show_alert=True)
        return
    await state.set_state(OrderForm.hosting)
    if data["order_type"] == "app":
        await state.update_data(hosting="Не указано")
        await finish_order(callback.message, state)
    else:
        await callback.message.answer(
            "🛜 Нужен ли хостинг?\n\nВыберите вариант:",
            reply_markup=hosting_menu(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("hosting:"))
async def cb_hosting(callback: CallbackQuery, state: FSMContext) -> None:
    text = callback.data.split(":", 1)[1]
    if text not in {"yes", "no"}:
        await callback.answer("Выберите вариант кнопкой.", show_alert=True)
        return
    await state.update_data(hosting="Нужен" if text == "yes" else "Не нужен")
    data = await state.get_data()
    if data.get("order_type") == "bot":
        await state.set_state(OrderForm.platform)
        await callback.message.answer(
            "📱 Выберите платформу для бота:",
            reply_markup=platform_menu(),
        )
    else:
        await finish_order(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith("platform:"))
async def cb_platform(callback: CallbackQuery, state: FSMContext) -> None:
    values = {
        "telegram": "Telegram",
        "max": "MAX",
        "discord": "Discord",
        "vk": "VK",
    }
    value = values.get(callback.data.split(":", 1)[1])
    if not value:
        await callback.answer("Выберите платформу кнопкой.", show_alert=True)
        return
    await state.update_data(platform=value)
    await finish_order(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "settings:notifications")
async def cb_toggle_notifications(callback: CallbackQuery) -> None:
    await toggle_notifications(callback.message)
    await callback.answer()


@router.callback_query(F.data == "admin:back")
async def cb_admin_back(callback: CallbackQuery) -> None:
    await callback.answer()
    await admin_help(callback.message)


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    await admin_stats(callback.message)


@router.callback_query(F.data == "admin:users")
async def cb_admin_users(callback: CallbackQuery) -> None:
    await callback.answer()
    await admin_users(callback.message)


@router.callback_query(F.data == "admin:orders")
async def cb_admin_orders(callback: CallbackQuery) -> None:
    await callback.answer()
    await admin_orders(callback.message)


@router.callback_query(F.data == "admin:commands")
async def cb_admin_commands(callback: CallbackQuery) -> None:
    await callback.answer()
    await admin_commands(callback.message)


# ---------------------------------------------------------------------------
# PROFILE
# ---------------------------------------------------------------------------

@router.message(F.text == "👤 Профиль")
async def profile(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_banned(message.from_user.id):
        return
    u = get_user_record(message.from_user.id, message.from_user.username, message.from_user.full_name)
    tg = f"@{u['username']}" if u.get("username") else "не указан"
    text = (
        "╭── 👤 ПРОФИЛЬ ──╮\n\n"
        f"👥 Ник в ТГ: {escape_html(u.get('full_name') or 'не указан')}\n"
        f"📱 Telegram: {escape_html(tg)}\n"
        f"🆔 ID: <code>{u['id']}</code>\n"
        f"📊 Покупок оформлено: {u.get('purchases', 0)}\n"
        f"🔔 Уведомления: {'включены' if u.get('notifications', True) else 'выключены'}\n"
        f"⛔ Бан: {escape_html(ban_text(message.from_user.id))}\n\n"
        "╰────────────╯"
    )
    await message.answer(text, reply_markup=main_menu())


# ---------------------------------------------------------------------------
# ORDER CREATION
# ---------------------------------------------------------------------------

async def begin_order(message: Message, state: FSMContext, order_type: str) -> None:
    if is_banned(message.from_user.id):
        return
    await state.clear()
    await state.update_data(order_type=order_type)
    t = TYPE_DATA[order_type]
    await state.set_state(OrderForm.name)
    await message.answer(
        f"{t['emoji']} Заказ {t['title']}\n\n"
        "Шаг 1 из 5–7.\n"
        f"✏️ Напишите название {t['desc']}.\n\n"
        "Для отмены нажмите «⬅️ Назад»."
    , reply_markup=back_menu())


@router.message(F.text == "🛒 Заказать")
async def order_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_banned(message.from_user.id):
        return
    await message.answer("🛒 <b>Заказать</b>\n\nВыберите направление разработки:", reply_markup=order_types_menu())


@router.message(F.text == "🤖 Заказать Бота")
async def order_bot(message: Message, state: FSMContext) -> None:
    await begin_order(message, state, "bot")


@router.message(F.text == "🌐 Заказать Сайт")
async def order_site(message: Message, state: FSMContext) -> None:
    await begin_order(message, state, "site")


@router.message(F.text == "🎮 Заказать Игру")
async def order_game(message: Message, state: FSMContext) -> None:
    await begin_order(message, state, "game")


@router.message(F.text == "📱 Заказать приложение")
async def order_app(message: Message, state: FSMContext) -> None:
    await begin_order(message, state, "app")


@router.message(OrderForm.name)
async def order_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text[:500])
    await state.set_state(OrderForm.description)
    t = (await state.get_data())["order_type"]
    await message.answer(f"✨ Опишите идею и основные функции {TYPE_DATA[t]['desc']}.", reply_markup=back_menu())


@router.message(OrderForm.description)
async def order_description(message: Message, state: FSMContext) -> None:
    await state.update_data(description=message.text[:3000])
    await state.set_state(OrderForm.theme)
    await message.answer("🪄 Какая тематика проекта?", reply_markup=back_menu())


@router.message(OrderForm.theme)
async def order_theme(message: Message, state: FSMContext) -> None:
    await state.update_data(theme=message.text[:1000])
    await state.set_state(OrderForm.purpose)
    t = (await state.get_data())["order_type"]
    await message.answer(f"💎 Какой основной смысл/цель {TYPE_DATA[t]['desc']}?", reply_markup=back_menu())


@router.message(OrderForm.purpose)
async def order_purpose(message: Message, state: FSMContext) -> None:
    await state.update_data(purpose=message.text[:2000])
    data = await state.get_data()
    t = data["order_type"]

    await state.set_state(OrderForm.donate)
    await message.answer(
        "💸 Нужен ли донат / монетизация?\n\nВыберите вариант:",
        reply_markup=donate_menu(),
    )


@router.message(OrderForm.donate)
async def order_donate(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    if text not in {"💎 имеется", "🚫 не имеется"}:
        await message.answer("Пожалуйста, выберите вариант кнопкой ниже.", reply_markup=donate_menu())
        return
    donate = "Имеется" if text == "💎 имеется" else "Не имеется"
    await state.update_data(donate=donate)
    data = await state.get_data()
    t = data["order_type"]

    await state.set_state(OrderForm.hosting)
    if t == "app":
        await state.update_data(hosting="Не указано")
        await finish_order(message, state)
        return
    await message.answer("🛜 Нужен ли хостинг?\n\nВыберите вариант:", reply_markup=hosting_menu())


@router.message(OrderForm.hosting)
async def order_hosting(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    if text not in {"🛜 нужен", "🚫 не нужен"}:
        await message.answer("Пожалуйста, выберите вариант кнопкой ниже.", reply_markup=hosting_menu())
        return
    hosting = "Нужен" if text == "🛜 нужен" else "Не нужен"
    await state.update_data(hosting=hosting)
    data = await state.get_data()

    if data["order_type"] == "bot":
        await state.set_state(OrderForm.platform)
        await message.answer("📱 Выберите платформу для бота:", reply_markup=platform_menu())
        return

    await finish_order(message, state)


@router.message(OrderForm.platform)
async def order_platform(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    allowed = {
        "📱 telegram": "Tg",
        "💬 max": "Max",
        "🎮 discord": "Discord",
        "🌐 vk": "VK",
    }
    platform = allowed.get(text)
    if not platform:
        await message.answer("Пожалуйста, выберите платформу кнопкой ниже.", reply_markup=platform_menu())
        return
    await state.update_data(platform=platform)
    await finish_order(message, state)


async def finish_order(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order = create_order(message.from_user.id, data["order_type"], data)
    DB["users"][str(message.from_user.id)]["purchases"] = (
        DB["users"][str(message.from_user.id)].get("purchases", 0) + 1
    )
    save_db()

    await state.clear()
    t = TYPE_DATA[order["type"]]
    await message.answer(
        f"╭── ✦ ЗАКАЗ #{order['id']} ✦ ──╮\n\n"
        f"{t['emoji']} Направление: {t['title']}\n"
        f"✏️ Название: {escape_html(order['name'])}\n"
        f"📌 Статус: ⏳ Ожидает принятия\n\n"
        "Заявка отправлена администрации.\n"
        "Следить за статусом можно в разделе «📋 Список заказов».\n\n"
        "╰────────────────────────╯",
        reply_markup=main_menu(),
    )
    await send_to_admins(
        message.bot,
        format_order_for_admin(order),
        admin_order_kb(order["id"]),
    )


# ---------------------------------------------------------------------------
# ORDERS LIST
# ---------------------------------------------------------------------------

@router.message(F.text == "📋 Список заказов")
async def order_list(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_banned(message.from_user.id):
        return
    orders = [
        o for o in DB["orders"].values()
        if o["user_id"] == message.from_user.id and o["status"] != "Отклонён"
    ]
    orders.sort(key=lambda x: x["id"])

    if not orders:
        await message.answer(
            "📋 У вас пока нет заказов.\n\n"
            "Оформите первый заказ через «🛒 Заказать».",
            reply_markup=back_menu(),
        )
        return

    lines = [
        "📋 <b>Ваши заказы</b>",
        "",
        "Статусы:",
        "⏳ — ожидание принятия заказа",
        "⚙️ — заказ выполняется",
        "🏁 — выполнен",
        "",
    ]
    for i, o in enumerate(orders, 1):
        t = TYPE_DATA[o["type"]]
        status_icon = {
            "Ожидание принятия заказа": "⏳",
            "Выполняется": "⚙️",
            "Выполнен": "🏁",
        }.get(o["status"], "•")
        lines.append(
            f"{i}. #{o['id']} | {t['title']} | "
            f"{escape_html(o['description'][:90])} | {status_icon}"
        )
    active = [o for o in orders if o["status"] in {"Ожидание принятия заказа", "Выполняется"}]
    if active:
        o = active[0]
        lines += [
            "",
            f"📌 Ваш ближайший активный заказ — позиция №{order_position(o['id'], message.from_user.id)}.",
            f"Статус: {o['status']}",
        ]
    await message.answer("\n".join(lines), reply_markup=back_menu())


# ---------------------------------------------------------------------------
# ADMIN ACCEPT / REJECT
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("order_accept:"))
async def order_accept_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    order = get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if order["status"] != "Ожидание принятия заказа":
        await callback.answer("Заказ уже обработан.", show_alert=True)
        return

    await state.clear()
    await state.update_data(order_id=order_id)
    await state.set_state(AdminAcceptForm.project_name)
    await callback.message.answer(
        f"✅ Принятие заказа #{order_id}\n\n"
        "Напишите название проекта, которое будет опубликовано в итоговом сообщении."
    )
    await callback.answer()


@router.message(AdminAcceptForm.project_name)
async def admin_accept_project(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.update_data(project_name=message.text[:500])
    await state.set_state(AdminAcceptForm.price)
    await message.answer("💸 Укажите стоимость проекта (например: 250 ⭐️).")


@router.message(AdminAcceptForm.price)
async def admin_accept_price(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    order = get_order(int(data["order_id"]))
    if not order:
        await state.clear()
        await message.answer("Заказ не найден.")
        return

    order["project_name"] = data["project_name"]
    order["price"] = message.text[:100]
    order["status"] = "Выполняется"
    order["reject_reason"] = ""
    save_db()
    await state.clear()

    await notify_user(
        message.bot,
        order["user_id"],
        "✅ Ваш заказ принят!\n\n"
        "Заявка рассмотрена и передана в работу. "
        "Если в проекте предусмотрен донат, около 50% дохода может "
        "быть направлено вам при наличии реальных вложений пользователей.\n\n"
        f"💬 Связь для согласования проекта и цены: {CONTACT_USERNAME}\n\n"
        f"📋 Номер заказа: #{order['id']}\n"
        "Когда работа будет завершена, вы получите уведомление.",
    )
    await message.answer(f"✅ Заказ #{order['id']} принят и переведён в работу.")


@router.callback_query(F.data.startswith("order_reject:"))
async def order_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    order = get_order(order_id)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if order["status"] != "Ожидание принятия заказа":
        await callback.answer("Заказ уже обработан.", show_alert=True)
        return

    await state.clear()
    await state.update_data(order_id=order_id)
    await state.set_state(AdminRejectForm.reason)
    await callback.message.answer(f"❌ Отклонение заказа #{order_id}\n\nНапишите причину отказа.")
    await callback.answer()


@router.message(AdminRejectForm.reason)
async def admin_reject_finish(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    order = get_order(int(data["order_id"]))
    if not order:
        await state.clear()
        await message.answer("Заказ не найден.")
        return

    order["status"] = "Отклонён"
    order["reject_reason"] = message.text[:2000]
    save_db()
    await state.clear()

    t = TYPE_DATA[order["type"]]
    await notify_user(
        message.bot,
        order["user_id"],
        f"❌ Ваш заказ ({t['desc']}) отклонён.\n\n"
        f"✏️ Причина: {escape_html(order['reject_reason'])}\n\n"
        "Извините. Ждём ваших следующих идей! Спасибо, что вы с нами. "
        "Следите за новостями.",
    )
    await message.answer(f"❌ Заказ #{order['id']} отклонён.")


# ---------------------------------------------------------------------------
# COMPLETE + REVIEW
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("order_done:"))
async def order_done(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.split(":")[1])
    order = get_order(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if order["status"] != "Выполняется":
        await callback.answer("Этот заказ сейчас нельзя завершить.", show_alert=True)
        return

    order["status"] = "Выполнен"
    order["completed_at"] = now_iso()
    save_db()

    await state.clear()
    await callback.message.answer(
        "✅ Ваш заказ отмечен как выполненный!\n\n"
        "Спасибо, что пользуетесь нашей компанией. "
        "Нам будет очень полезен ваш отзыв.\n\n"
        "✍️ Напишите отзыв одним сообщением."
    )
    await state.set_state(ReviewForm.text)
    await callback.answer()


@router.message(ReviewForm.text)
async def review_text(message: Message, state: FSMContext) -> None:
    orders = [
        o for o in DB["orders"].values()
        if o["user_id"] == message.from_user.id
        and o["status"] == "Выполнен"
        and not o.get("review")
    ]
    if not orders:
        await state.clear()
        await message.answer("Не найден выполненный заказ для отзыва.", reply_markup=main_menu())
        return
    orders.sort(key=lambda x: x["id"], reverse=True)
    order = orders[0]
    order["review"] = message.text[:3000]
    save_db()
    await state.clear()

    await message.answer(
        "⭐️ Оцените работу над вашим проектом от 1 до 5 звёзд:",
        reply_markup=rating_kb(order["id"]),
    )


@router.callback_query(F.data.startswith("rate:"))
async def rate_order(callback: CallbackQuery) -> None:
    _, order_id_s, rating_s = callback.data.split(":")
    order = get_order(int(order_id_s))
    rating = int(rating_s)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if order.get("rating"):
        await callback.answer("Оценка уже сохранена.", show_alert=True)
        return

    order["rating"] = rating
    save_db()

    DB["reviews"].append({
        "order_id": order["id"],
        "user_id": order["user_id"],
        "type": order["type"],
        "project_name": order.get("project_name") or order.get("name"),
        "rating": rating,
        "review": order.get("review", ""),
        "created_at": now_iso(),
    })
    save_db()

    await callback.message.answer(
        "Спасибо, что заказали у нас! Желаем удачи вам и вашему проекту 👋",
        reply_markup=main_menu(),
    )
    await publish_review(callback.bot, order)
    await callback.answer("Оценка сохранена!")


async def publish_review(bot: Bot, order: dict[str, Any]) -> None:
    t = TYPE_DATA[order["type"]]
    rating = "⭐️" * int(order["rating"] or 0)
    text = (
        "🏁 <b>ЗАКАЗ ВЫПОЛНЕН ‼️</b>\n\n"
        f"{t['title'].replace('Бота', 'Бот').replace('Сайта', 'Сайт').replace('Игры', 'Игра').replace('Приложения', 'Приложение')}: "
        f"{escape_html(order.get('project_name') or order['name'])}\n"
        f"Стоимость: {escape_html(order.get('price') or 'не указана')}\n"
        f"Оценка от заказчика: {rating}\n"
        f"Отзыв: {escape_html(order.get('review') or 'Без текста')}\n"
    )
    try:
        await bot.send_message(REVIEWS_CHANNEL, text)
    except Exception as e:
        logging.warning("Cannot publish review to channel: %s", e)


# ---------------------------------------------------------------------------
# REVIEWS
# ---------------------------------------------------------------------------

@router.message(F.text == "✨ Отзывы")
async def reviews(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_banned(message.from_user.id):
        return

    reviews = DB["reviews"]
    if not reviews:
        await message.answer(
            "✨ <b>Отзывы</b>\n\n"
            "СРЕДНИЙ РЕЙТИНГ 0 | 5\n\n"
            "Пока отзывов нет. Будьте первым!",
            reply_markup=back_menu(),
        )
        return

    avg = sum(int(r["rating"]) for r in reviews) / len(reviews)
    lines = [
        "✨ <b>Отзывы клиентов</b>",
        "",
        f"СРЕДНИЙ РЕЙТИНГ {avg:.2f} | 5",
        "",
    ]
    for r in reversed(reviews[-20:]):
        stars = "⭐️" * int(r["rating"])
        user = display_username(int(r["user_id"]))
        kind = TYPE_DATA.get(r["type"], {}).get("title", r["type"])
        kind = kind.replace("Бота", "Бот").replace("Сайта", "Сайт").replace("Игры", "Игра").replace("Приложения", "Приложение")
        lines.append(
            f"{stars}\n{escape_html(user)}: {escape_html(r['review'])} | {kind}\n"
        )
    await message.answer("\n".join(lines), reply_markup=back_menu())


# ---------------------------------------------------------------------------
# PRICES
# ---------------------------------------------------------------------------

@router.message(F.text == "💸 Ценники")
async def prices(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_banned(message.from_user.id):
        return
    await message.answer(
        "💸 <b>Ценники дополнительных услуг</b>\n\n"
        "🤖 <b>Боты</b>\n"
        "Хостинг — 1 месяц: 25 ⭐️\n"
        "Обновление — 25–100 ⭐️ в зависимости от сложности\n\n"
        "🌐 <b>Сайты</b>\n"
        "Хостинг — 1 месяц: 50 ⭐️\n"
        "Обновление — 25–100 ⭐️ в зависимости от сложности\n\n"
        "🎮 <b>Игры</b>\n"
        "Онлайн-сервер — 1 месяц: 75 ⭐️\n"
        "Обновление — 50–100 ⭐️ в зависимости от сложности\n\n"
        "📱 <b>Приложения</b>\n"
        "Обновление — 50–150 ⭐️ в зависимости от сложности\n\n"
        "💬 Точную стоимость проекта определяет администрация после рассмотрения заявки.\n"
        f"Связь: {CONTACT_USERNAME}",
        reply_markup=back_menu(),
    )


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

@router.message(F.text == "⚙️ Настройка")
async def settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_banned(message.from_user.id):
        return
    await message.answer(
        "⚙️ <b>Настройки Weret | Production</b>\n\n"
        "💬 Уведомления — получать сообщения о принятии, "
        "отклонении и завершении заказов.",
        reply_markup=settings_menu(message.from_user.id),
    )


@router.message(F.text.startswith("💬 Уведомления:"))
async def toggle_notifications(message: Message) -> None:
    u = get_user_record(message.from_user.id, message.from_user.username, message.from_user.full_name)
    u["notifications"] = not u.get("notifications", True)
    save_db()
    await message.answer(
        f"🔔 Уведомления теперь: {'ON' if u['notifications'] else 'OFF'}",
        reply_markup=settings_menu(message.from_user.id),
    )


# ---------------------------------------------------------------------------
# SUPPORT
# ---------------------------------------------------------------------------

@router.message(F.text == "🆘 Тех.поддержка")
async def support_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_banned(message.from_user.id):
        return
    await state.set_state(SupportForm.text)
    await message.answer(
        "🆘 <b>Техподдержка</b>\n\n"
        "Опишите вопрос или проблему одним сообщением. "
        "Не отправляйте пароли, коды подтверждения и другие секретные данные.",
        reply_markup=back_menu(),
    )


@router.message(SupportForm.text)
async def support_send(message: Message, state: FSMContext) -> None:
    ticket_id = max(
        [int(x.get("id", 0)) for x in DB.get("support", [])] + [0]
    ) + 1
    DB.setdefault("support", []).append({
        "id": ticket_id,
        "user_id": message.from_user.id,
        "text": message.text[:3000],
        "created_at": now_iso(),
        "status": "open",
    })
    save_db()
    await state.clear()

    await message.answer(
        "✅ Обращение отправлено администрации. "
        "Ответ появится в этом чате.",
        reply_markup=main_menu(),
    )
    text = (
        "‼️ <b>ОБРАЩЕНИЕ В ТЕХПОДДЕРЖКУ</b>\n\n"
        f"Пишет — {escape_html(display_username(message.from_user.id))}\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n\n"
        f"Причина обращения:\n{escape_html(message.text)}\n\n"
        f"Номер обращения: #{ticket_id}"
    )
    await send_to_admins(message.bot, text, admin_support_kb(ticket_id))


@router.callback_query(F.data.startswith("support_reply:"))
async def support_reply_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":")[1])
    ticket = next((x for x in DB.get("support", []) if x["id"] == ticket_id), None)
    if not ticket:
        await callback.answer("Обращение не найдено.", show_alert=True)
        return
    await state.clear()
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(SupportReplyForm.text)
    await callback.message.answer(f"‼️ Ответ на обращение #{ticket_id}\n\nНапишите текст ответа пользователю.")
    await callback.answer()


@router.message(SupportReplyForm.text)
async def support_reply_finish(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    ticket_id = int(data["ticket_id"])
    ticket = next((x for x in DB.get("support", []) if x["id"] == ticket_id), None)
    await state.clear()
    if not ticket:
        await message.answer("Обращение не найдено.")
        return
    ticket["status"] = "answered"
    ticket["answer"] = message.text[:3000]
    save_db()
    await notify_user(
        message.bot,
        ticket["user_id"],
        "‼️ <b>Ответ техподдержки</b>\n\n"
        f"Текст: {escape_html(message.text)}\n\n"
        "Удачи вам и следите за новостями.",
    )
    await message.answer(f"✅ Ответ по обращению #{ticket_id} отправлен.")


@router.callback_query(F.data.startswith("support_ignore:"))
async def support_ignore(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    ticket_id = int(callback.data.split(":")[1])
    ticket = next((x for x in DB.get("support", []) if x["id"] == ticket_id), None)
    if not ticket:
        await callback.answer("Обращение не найдено.", show_alert=True)
        return
    ticket["status"] = "ignored"
    save_db()
    await notify_user(
        callback.bot,
        ticket["user_id"],
        "❌ Ваше обращение было проигнорировано.\n\n"
        "Пожалуйста, сформулируйте обращение подробно и корректно "
        "и попробуйте обратиться в поддержку позже.\n\n"
        "Спасибо, что вы с нами. Следите за новостями.",
    )
    await callback.message.answer(f"❌ Обращение #{ticket_id} проигнорировано.")
    await callback.answer()


# ---------------------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------------------

@router.message(Command("ban"))
async def cmd_ban(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or parts[2].lower() not in {"7d", "14d", "28d", "owers"}:
        await message.answer(
            "Использование:\n"
            "/ban id/@Username 7d\n"
            "/ban id/@Username 14d\n"
            "/ban id/@Username 28d\n"
            "/ban id/@Username Owers"
        )
        return
    uid = find_user_target(parts[1])
    if not uid:
        await message.answer("Пользователь не найден в базе бота.")
        return
    if uid in ADMIN_IDS:
        await message.answer("Нельзя заблокировать администратора.")
        return

    u = DB["users"][str(uid)]
    mode = parts[2].lower()
    if mode == "owers":
        u["ban_permanent"] = True
        u["ban_until"] = None
        text = "навсегда"
    else:
        days = int(mode[:-1])
        u["ban_permanent"] = False
        u["ban_until"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        text = f"на {days} дней"
    save_db()
    await message.answer(f"⛔ {display_username(uid)} заблокирован {text}.")


@router.message(Command("unban"))
async def cmd_unban(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /unban id/@Username")
        return
    uid = find_user_target(parts[1])
    if not uid:
        await message.answer("Пользователь не найден в базе бота.")
        return
    u = DB["users"][str(uid)]
    u["ban_permanent"] = False
    u["ban_until"] = None
    save_db()
    await message.answer(f"✅ {display_username(uid)} разблокирован.")


def profile_text_for(uid: int) -> str:
    u = DB["users"].get(str(uid))
    if not u:
        return "Пользователь не найден в базе."
    orders = [o for o in DB["orders"].values() if o["user_id"] == uid]
    completed = sum(1 for o in orders if o["status"] == "Выполнен")
    return (
        "╭── 👤 ПРОФИЛЬ ──╮\n\n"
        f"👥 Ник: {escape_html(u.get('full_name') or 'не указан')}\n"
        f"📱 Telegram: {escape_html('@' + u['username'] if u.get('username') else 'не указан')}\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📊 Заказов: {len(orders)}\n"
        f"🏁 Выполнено: {completed}\n"
        f"⛔ Бан: {escape_html(ban_text(uid))}\n\n"
        "╰────────────╯"
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    if not is_admin(message.from_user.id):
        # Разрешаем пользователю смотреть только свой профиль.
        parts = message.text.split()
        if len(parts) > 1:
            return
        await message.answer(profile_text_for(message.from_user.id), reply_markup=main_menu())
        return

    parts = message.text.split(maxsplit=1)
    uid = message.from_user.id if len(parts) == 1 else find_user_target(parts[1])
    if not uid:
        await message.answer("Пользователь не найден.")
        return
    await message.answer(profile_text_for(uid))


@router.message(Command("prim_zakaz"))
async def cmd_accept_order(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /prim_zakaz 123")
        return
    order = get_order(int(parts[1]))
    if not order:
        await message.answer("Заказ не найден.")
        return
    if order["status"] != "Ожидание принятия заказа":
        await message.answer("Этот заказ уже обработан.")
        return
    await state.clear()
    await state.update_data(order_id=order["id"])
    await state.set_state(AdminAcceptForm.project_name)
    await message.answer(f"Введите название проекта для заказа #{order['id']}.")


@router.message(Command("otkaz_zakaz"))
async def cmd_reject_order(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /otkaz_zakaz 123")
        return
    order = get_order(int(parts[1]))
    if not order:
        await message.answer("Заказ не найден.")
        return
    if order["status"] != "Ожидание принятия заказа":
        await message.answer("Этот заказ уже обработан.")
        return
    await state.clear()
    await state.update_data(order_id=order["id"])
    await state.set_state(AdminRejectForm.reason)
    await message.answer(f"Введите причину отказа по заказу #{order['id']}.")


# ---------------------------------------------------------------------------
# ADMIN /start and fallback
# ---------------------------------------------------------------------------

@router.message(Command("admin"))
async def admin_help(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    orders = list(DB["orders"].values())
    open_count = sum(o["status"] == "Ожидание принятия заказа" for o in orders)
    work_count = sum(o["status"] == "Выполняется" for o in orders)
    done_count = sum(o["status"] == "Выполнен" for o in orders)
    await message.answer(
        "╭── 🛡 WERET | ADMIN ──╮\n\n"
        f"📦 Всего заказов: {len(orders)}\n"
        f"⏳ На рассмотрении: {open_count}\n"
        f"⚙️ В работе: {work_count}\n"
        f"🏁 Выполнено: {done_count}\n"
        f"👥 Пользователей: {len(DB['users'])}\n\n"
        "Команды администратора доступны через /команды ниже.\n"
        "╰────────────────────╯",
        reply_markup=admin_menu(),
    )


@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    orders = list(DB["orders"].values())
    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {len(DB['users'])}\n"
        f"📦 Заказов: {len(orders)}\n"
        f"⏳ Ожидают: {sum(o['status']=='Ожидание принятия заказа' for o in orders)}\n"
        f"⚙️ В работе: {sum(o['status']=='Выполняется' for o in orders)}\n"
        f"🏁 Выполнено: {sum(o['status']=='Выполнен' for o in orders)}\n"
        f"❌ Отклонено: {sum(o['status']=='Отклонён' for o in orders)}\n"
        f"✨ Отзывов: {len(DB['reviews'])}",
        reply_markup=admin_menu(),
    )


@router.message(F.text == "👥 Пользователи")
async def admin_users(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        f"👥 <b>Пользователи</b>\n\nВсего зарегистрировано: {len(DB['users'])}\n\n"
        "Для просмотра конкретного профиля используйте /profile id/@Username.",
        reply_markup=admin_menu(),
    )


@router.message(F.text == "📦 Заказы")
async def admin_orders(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    orders = sorted(DB["orders"].values(), key=lambda x: x["id"], reverse=True)[:15]
    if not orders:
        await message.answer("📦 Заказов пока нет.", reply_markup=admin_menu())
        return
    lines = ["📦 <b>Последние заказы</b>", ""]
    icons = {"Ожидание принятия заказа":"⏳", "Выполняется":"⚙️", "Выполнен":"🏁", "Отклонён":"❌"}
    for o in orders:
        t = TYPE_DATA[o["type"]]
        lines.append(f"#{o['id']} · {t['emoji']} {o['name'][:35]} · {icons.get(o['status'],'•')}")
    await message.answer("\n".join(lines), reply_markup=admin_menu())


@router.message(F.text == "🛠 Команды")
async def admin_commands(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 <b>Команды администратора</b>\n\n"
        "/ban id/@Username 7d|14d|28d|Owers\n"
        "/unban id/@Username\n"
        "/profile [id/@Username]\n"
        "/prim_zakaz номер\n"
        "/otkaz_zakaz номер",
        reply_markup=admin_menu(),
    )


@router.message()
async def fallback(message: Message) -> None:
    if is_banned(message.from_user.id):
        return
    await message.answer(
        "Не понял команду. Используйте кнопки главного меню.",
        reply_markup=main_menu(),
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Укажите BOT_TOKEN и ADMIN_IDS в автоматически созданном config.json, "
            "затем перезапустите бота."
        )
    if not ADMIN_IDS or ADMIN_IDS == [123456789]:
        raise RuntimeError(
            "Укажите реальные Telegram ID администраторов в config.json, "
            "затем перезапустите бота."
        )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    me = await bot.get_me()
    logging.info("Bot started: @%s", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
