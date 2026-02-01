import re
import logging
import sqlite3
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)

# ----------------- Настройки -----------------
BOT_TOKEN = "8127030068:AAHt_4mOEuXELizkcJY6OJW2n8_I-NHAUHw"
ADMIN_CODE = "11092001"
DB_FILENAME = "bookings.db"
ENABLE_NAME = True
MAX_DAYS_AHEAD = 30

SERVICES = [
    "Панель приборов (Cluster)",
    "Блок ABS",
    "Рулевое управление EPS",
    "Ремонт 4WD",
    "Ремонт электроручника (EPB)",
    "Блок управления АКПП (TCU)",
    "Климат-контроль (HVAC)",
    "Круиз-контроль (CC)",
    "Радар (RCU)",
    "Ремонт стеклоочистителей",
]

PHONE_RE = re.compile(r'^\+?\d{7,15}$')
WEEKDAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
SERVICE_ADDRESS = "г. Москва, Алтуфьевское шоссе, 31с1, въезд через 31с5\nСервис «ExactLab»."

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOOKINGS: Dict[str, List[Dict[str, Any]]] = {}
DB_CONN: sqlite3.Connection = None


def init_db():
    global DB_CONN
    DB_CONN = sqlite3.connect(DB_FILENAME, check_same_thread=False)
    cur = DB_CONN.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            name TEXT,
            services TEXT NOT NULL,
            date TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    DB_CONN.commit()

    cur.execute("PRAGMA table_info(bookings)")
    cols = [r[1] for r in cur.fetchall()]
    if "status" not in cols:
        cur.execute("ALTER TABLE bookings ADD COLUMN status TEXT DEFAULT 'active'")
        DB_CONN.commit()


def add_booking_db(phone: str, name: str, services: List[str], date_iso: str) -> int:
    cur = DB_CONN.cursor()
    cur.execute(
        "INSERT INTO bookings (phone, name, services, date, created_at, status) VALUES (?, ?, ?, ?, ?, ?)",
        (phone, name, json.dumps(services, ensure_ascii=False), date_iso, datetime.now().isoformat(), "active"),
    )
    DB_CONN.commit()
    return cur.lastrowid


def mark_cancelled_db(booking_id: int) -> None:
    cur = DB_CONN.cursor()
    cur.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
    DB_CONN.commit()
    logger.info(f"Запись ID:{booking_id} помечена как cancelled в БД")


def delete_booking_db(booking_id: int) -> None:
    # legacy name — помечаем как cancelled
    mark_cancelled_db(booking_id)


def get_all_db_bookings() -> List[Tuple]:
    cur = DB_CONN.cursor()
    cur.execute("SELECT id, phone, name, services, date, created_at FROM bookings WHERE IFNULL(status,'active')='active' ORDER BY date")
    return cur.fetchall()


def get_bookings_for_date_db(date_iso: str) -> List[Tuple]:
    cur = DB_CONN.cursor()
    cur.execute(
        "SELECT id, phone, name, services, date, created_at FROM bookings WHERE date = ? AND IFNULL(status,'active')='active' ORDER BY created_at",
        (date_iso,),
    )
    return cur.fetchall()


def get_bookings_by_phone_db(phone: str) -> List[Tuple]:
    cur = DB_CONN.cursor()
    cur.execute(
        "SELECT id, phone, name, services, date, created_at FROM bookings WHERE phone = ? AND IFNULL(status,'active')='active' ORDER BY date",
        (phone,),
    )
    return cur.fetchall()


def count_bookings_by_date_range_db(start_iso: str, end_iso: str) -> Dict[str, int]:
    cur = DB_CONN.cursor()
    cur.execute(
        "SELECT date, COUNT(*) FROM bookings WHERE date BETWEEN ? AND ? AND IFNULL(status,'active')='active' GROUP BY date",
        (start_iso, end_iso),
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def load_bookings_to_memory():
    BOOKINGS.clear()
    rows = get_all_db_bookings()
    for r in rows:
        bid, phone, name, services_json, date_iso, created_at = r
        services = json.loads(services_json)
        BOOKINGS.setdefault(phone, []).append({
            "id": bid, "services": services, "name": name, "date": date_iso, "created_at": created_at
        })


(
    SELECT_SERVICE,
    PHONE,
    NAME,
    DATE,
    CONFIRM,
) = range(5)


def fmt_booking_preview(b: Dict[str, Any]) -> str:
    services = "\n".join(f"- {s}" for s in b["services"])
    name = b.get("name") or "—"
    return (
        f"Услуги:\n{services}\n\n"
        f"Имя: {name}\n"
        f"Дата: {b['date']}\n"
    )


def get_available_dates(now: datetime) -> List[datetime]:
    dates = []
    for d in range(0, MAX_DAYS_AHEAD + 1):
        candidate = (now + timedelta(days=d)).date()
        if candidate.weekday() == 6:  # исключаем воскресенье
            continue
        dates.append(candidate)
    return dates


def make_date_label(dt_obj) -> str:
    wd = WEEKDAY_RU[dt_obj.weekday()]
    return f"{dt_obj.strftime('%d.%m.%y')} {wd}"


def build_dates_keyboard(dates: List[any]) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for i, dt_obj in enumerate(dates, 1):
        label = f"{make_date_label(dt_obj)}"
        row.append(InlineKeyboardButton(label, callback_data=f"date|{dt_obj.isoformat()}"))
        if i % 3 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("◀️ Назад", callback_data="back"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
    ])
    return InlineKeyboardMarkup(buttons)


def build_services_keyboard(selected: List[int]) -> InlineKeyboardMarkup:
    buttons = []
    for i, s in enumerate(SERVICES):
        prefix = "✅ " if i in selected else ""
        buttons.append([InlineKeyboardButton(f"{prefix}{s}", callback_data=f"svc|{i}")])
    buttons.append([InlineKeyboardButton("❗ Уже записаны? Отменить запись", callback_data="start_cancel")])
    buttons.append([
        InlineKeyboardButton("✅ Готово", callback_data="svc_done"),
        InlineKeyboardButton("🧹 Очистить выбор", callback_data="svc_clear"),
    ])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


# ----------------- Обработчики (основной flow) -----------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    context.user_data['selected_services'] = []
    await update.message.reply_text(
        "Здравствуйте! Вас приветствует сервис ExactLab.\n\n"
        "Выберите одну или несколько услуг (нажмите, чтобы пометить/снять пометку). "
        "Когда закончите — нажмите «Готово».\n\n"
        "Если в любой момент хотите начать сначала — отправьте /start. Для отмены — /cancel.\n\n"
        "Если вы уже записаны и хотите отменить запись — нажмите кнопку «❗ Уже записаны? Отменить запись». "
        "Выберите услугу:",
        reply_markup=build_services_keyboard(context.user_data['selected_services'])
    )
    return SELECT_SERVICE


async def svc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "start_cancel":
        logger.info(">>> Начало процесса отмены записи через кнопку")
        context.user_data['in_cancel_flow'] = True
        await query.edit_message_text("Введите номер телефона (в международном формате), по которому хотите отменить записи.\nДля отмены — /cancel.")
        return SELECT_SERVICE

    if data == "svc_done":
        if not context.user_data.get('selected_services'):
            await query.edit_message_text("Вы не выбрали ни одной услуги. Пожалуйста, выберите хотя бы одну.")
            await query.edit_message_reply_markup(build_services_keyboard(context.user_data['selected_services']))
            return SELECT_SERVICE
        context.user_data['step_from'] = SELECT_SERVICE
        await query.edit_message_text("Отлично. Теперь введите номер телефона в международном формате (пример: +79661234567).\n\n"
                                      "Или отправьте /cancel для выхода.")
        return PHONE

    if data == "svc_clear":
        context.user_data['selected_services'] = []
        await query.edit_message_text("Выбор очищен. Выберите услугу(и):")
        await query.edit_message_reply_markup(build_services_keyboard(context.user_data['selected_services']))
        return SELECT_SERVICE

    if data == "cancel":
        await query.edit_message_text("Запись отменена.")
        context.user_data.clear()
        return ConversationHandler.END

    if data.startswith("svc|"):
        try:
            idx = int(data.split("|", 1)[1])
        except Exception:
            return SELECT_SERVICE
        sel = context.user_data.setdefault('selected_services', [])
        if idx in sel:
            sel.remove(idx)
        else:
            sel.append(idx)
        await query.edit_message_reply_markup(build_services_keyboard(sel))
        return SELECT_SERVICE

    if data == "back":
        await query.edit_message_text("Возврат к началу. Для начала заново отправьте /start.")
        context.user_data.clear()
        return ConversationHandler.END

    return SELECT_SERVICE


async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() == "start" or text == "/start":
        return await cmd_start(update, context)
    if text.lower() == "отмена" or text == "/cancel":
        await update.message.reply_text("Отмена записи.")
        context.user_data.clear()
        return ConversationHandler.END

    if not PHONE_RE.match(text):
        await update.message.reply_text(
            "Неверный формат номера. Введите номер в международном формате, например +79661234567.\n"
            "Или нажмите /cancel для выхода."
        )
        return PHONE

    phone = text if text.startswith("+") else "+" + text
    context.user_data['phone'] = phone

    if ENABLE_NAME:
        await update.message.reply_text("Введите имя и фамилию клиента (или отправьте /skip):")
        return NAME
    else:
        context.user_data['name'] = None
        return await ask_date_prompt(update, context)


async def skip_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['name'] = None
    return await ask_date_prompt(update, context)


async def name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() == "start" or text == "/start":
        return await cmd_start(update, context)
    if text.lower() == "отмена" or text == "/cancel":
        await update.message.reply_text("Отмена записи.")
        context.user_data.clear()
        return ConversationHandler.END
    if len(text) < 2:
        await update.message.reply_text("Слишком короткое имя. Введите имя и фамилию полностью или /skip:")
        return NAME
    context.user_data['name'] = text
    return await ask_date_prompt(update, context)


async def ask_date_prompt(update_obj, context: ContextTypes.DEFAULT_TYPE) -> int:
    now = datetime.now()
    dates = get_available_dates(now)
    if not dates:
        text = "К сожалению, нет доступных дат для записи в ближайший месяц."
        if isinstance(update_obj, Update) and update_obj.message:
            await update_obj.message.reply_text(text)
        else:
            await update_obj.callback_query.edit_message_text(text)
        context.user_data.clear()
        return ConversationHandler.END

    text = "Выберите дату для записи (доступно в ближайший месяц, воскресенье недоступно):"
    if isinstance(update_obj, Update) and update_obj.message:
        await update_obj.message.reply_text(text, reply_markup=build_dates_keyboard(dates))
    else:
        await update_obj.callback_query.edit_message_text(text, reply_markup=build_dates_keyboard(dates))
    return DATE


async def date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await query.edit_message_text("Отмена записи.")
        context.user_data.clear()
        return ConversationHandler.END
    if data == "back":
        if ENABLE_NAME:
            await query.edit_message_text("Вернулись назад. Введите имя и фамилию (или /skip):")
            return NAME
        else:
            await query.edit_message_text("Вернулись назад. Введите номер телефона в международном формате:")
            return PHONE

    if data.startswith("date|"):
        sel = data.split("|", 1)[1]
        try:
            dt = datetime.fromisoformat(sel).date()
        except Exception:
            await query.edit_message_text("Неправильная дата. Попробуйте снова.")
            return DATE
        # дополнительная защита: если вдруг пользователь выбрал воскресенье (извне), отвергаем
        if dt.weekday() == 6:
            await query.edit_message_text("Воскресенье недоступно для записи. Выберите другую дату.")
            return DATE

        context.user_data['date'] = dt.isoformat()
        services = [SERVICES[i] for i in context.user_data.get('selected_services', [])]
        booking_preview = {
            "services": services,
            "name": context.user_data.get('name'),
            "date": f"{dt.strftime('%d.%m.%y')} {WEEKDAY_RU[dt.weekday()]}",
        }
        text = "Проверьте данные записи:\n\n" + fmt_booking_preview(booking_preview)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back"), InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ])
        await query.edit_message_text(text, reply_markup=kb)
        return CONFIRM

    return DATE


async def send_route_image_or_text(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    caption = f" "
    local_file = "route.png"
    if os.path.exists(local_file):
        with open(local_file, "rb") as f:
            await context.bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
        return
    await context.bot.send_message(chat_id=chat_id, text=f"{caption}\n\n(Файл route.png не найден в папке скрипта.)")


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        await query.edit_message_text("Отмена записи.")
        context.user_data.clear()
        return ConversationHandler.END

    if data == "back":
        now = datetime.now()
        await query.edit_message_text("Выберите дату:", reply_markup=build_dates_keyboard(get_available_dates(now)))
        return DATE

    if data == "confirm":
        phone = context.user_data['phone']
        services = [SERVICES[i] for i in context.user_data.get('selected_services', [])]
        name = context.user_data.get('name')
        date_iso = context.user_data['date']
        booking_id = add_booking_db(phone, name, services, date_iso)
        BOOKINGS.setdefault(phone, []).append({
            "id": booking_id, "services": services, "name": name, "date": date_iso, "created_at": datetime.now().isoformat()
        })
        dt = datetime.fromisoformat(date_iso).date()
        booking = {
            "services": services,
            "name": name,
            "date": f"{dt.strftime('%d.%m.%y')} {WEEKDAY_RU[dt.weekday()]}",
        }
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📌 Записаться ещё", callback_data="start_again")],
            [InlineKeyboardButton("🏠 На старт (/start)", callback_data="start_again"), InlineKeyboardButton("❌ Выход", callback_data="end_session")],
        ])
        await query.edit_message_text(
            "Готово! Ваша запись подтверждена:\n\n" + fmt_booking_preview(booking),
            reply_markup=kb
        )
        await send_route_image_or_text(query.message.chat_id, context)
        context.user_data.clear()
        return ConversationHandler.END

    return CONFIRM


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отмена. Если захотите записаться — введите /start.")
    context.user_data.clear()
    return ConversationHandler.END


# ----------------- Обработчик ввода телефона для отмены внутри ConversationHandler -----------------

async def handle_cancel_phone_in_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода номера телефона для отмены внутри ConversationHandler"""
    if not context.user_data.get('in_cancel_flow'):
        # Если не в процессе отмены, игнорируем
        logger.info("Получен текст, но не в процессе отмены - игнорируем")
        return SELECT_SERVICE
    
    text = update.message.text.strip()
    
    logger.info(f">>> handle_cancel_phone_in_conv получил текст: {text}")
    
    if text.lower() in ["/cancel", "отмена"]:
        context.user_data.clear()
        await update.message.reply_text("Отменено. Для новой записи нажмите /start")
        return ConversationHandler.END
        
    if not PHONE_RE.match(text):
        await update.message.reply_text("Неверный формат номера. Попробуйте еще раз или /cancel.")
        return SELECT_SERVICE
        
    phone = text if text.startswith("+") else "+" + text
    
    logger.info(f">>> Поиск записей для номера: {phone}")
    
    rows = get_bookings_by_phone_db(phone)
    
    logger.info(f">>> Найдено записей: {len(rows)}")
    
    if not rows:
        context.user_data.clear()
        await update.message.reply_text("Номер отсутствует в записях. Для новой записи нажмите /start")
        return ConversationHandler.END

    found_name = None
    for r in rows:
        _, _phone, name, _, _, _ = r
        if name and str(name).strip():
            found_name = name
            break

    if found_name:
        prompt = f"Найдены записи для номера {phone}.\nИмя в записи: {found_name}\n\nПодтвердите удаление всех записей, связанных с этим номером."
    else:
        prompt = f"Найдены записи для номера {phone}, имя в записях не указано.\n\nПодтвердите удаление всех записей, связанных с этим номером."

    # Очищаем флаг процесса отмены
    context.user_data.pop('in_cancel_flow', None)
    
    # ВАЖНО: Сохраняем номер телефона в user_data
    context.user_data['pending_cancel_phone'] = phone
    
    logger.info(f">>> СОХРАНЁН pending_cancel_phone: {phone}")
    logger.info(f">>> user_data после сохранения: {context.user_data}")
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Подтвердить отмену", callback_data="cancel_confirm")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="cancel_cancel")],
    ])
    await update.message.reply_text(prompt, reply_markup=kb)
    return SELECT_SERVICE  # Остаёмся в SELECT_SERVICE для обработки кнопок


# ----------------- Обработчик кнопок подтверждения отмены -----------------

async def client_cancel_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    data = q.data
    
    logger.info(f"=== client_cancel_confirm_callback ВЫЗВАН ===")
    logger.info(f"Callback data: {data}")
    logger.info(f"User ID: {q.from_user.id}")
    logger.info(f"Текущий user_data: {context.user_data}")
    
    # даём быстрый ответ
    await q.answer(text="Обработка...", show_alert=False)

    if data == "cancel_cancel":
        logger.info(">>> Отмена удаления записи")
        context.user_data.clear()
        try:
            await q.edit_message_text("Отмена удаления. Для новой записи нажмите /start")
        except Exception as e:
            logger.error(f"Ошибка редактирования сообщения: {e}")
            await q.message.reply_text("Отмена удаления. Для новой записи нажмите /start")
        return ConversationHandler.END

    if data == "cancel_confirm":
        logger.info(">>> Подтверждение отмены записи")
        phone = context.user_data.get('pending_cancel_phone')
        logger.info(f">>> Номер телефона из context: '{phone}'")
        
        if not phone:
            logger.warning("!!! ОШИБКА: Номер телефона не найден в context")
            logger.warning(f"!!! user_data был: {context.user_data}")
            try:
                await q.edit_message_text("Время ожидания истекло или номер не найден. Повторите попытку - нажмите /start и выберите 'Отменить запись'.")
            except Exception as e:
                logger.error(f"Ошибка редактирования сообщения: {e}")
                await q.message.reply_text("Время ожидания истекло или номер не найден. Повторите попытку - нажмите /start и выберите 'Отменить запись'.")
            return ConversationHandler.END

        rows = get_bookings_by_phone_db(phone)
        logger.info(f">>> Найдено записей для номера {phone}: {len(rows)}")
        
        if not rows:
            logger.warning(f"!!! Записей для {phone} не найдено в БД")
            context.user_data.clear()
            try:
                await q.edit_message_text("Записей для этого номера уже нет.")
            except Exception as e:
                logger.error(f"Ошибка редактирования сообщения: {e}")
                await q.message.reply_text("Записей для этого номера уже нет.")
            return ConversationHandler.END

        marked = 0
        for r in rows:
            bid = r[0]
            logger.info(f">>> Помечаем запись ID:{bid} как отменённую")
            mark_cancelled_db(bid)
            marked += 1

        logger.info(f">>> УСПЕШНО помечено записей: {marked}")

        # обновляем память, убираем номер
        if phone in BOOKINGS:
            BOOKINGS.pop(phone, None)
            logger.info(f">>> Удалён номер {phone} из памяти BOOKINGS")

        context.user_data.clear()
        try:
            await q.edit_message_text(f"✅ Готово! Отменено {marked} запись(ей) для номера {phone}.\n\nДля новой записи нажмите /start")
            logger.info(">>> УСПЕШНО отправлено подтверждение отмены")
        except Exception as e:
            logger.error(f"Ошибка редактирования сообщения: {e}")
            await q.message.reply_text(f"✅ Готово! Отменено {marked} запись(ей) для номера {phone}.\n\nДля новой записи нажмите /start")
        return ConversationHandler.END

    return SELECT_SERVICE


# ----------------- Отладочные / админские функции -----------------


async def show_bookings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or args[0] != ADMIN_CODE:
        await update.message.reply_text("Эта команда доступна только администратору. Введите: /bookings <код>")
        return

    rows = get_all_db_bookings()
    if not rows:
        await update.message.reply_text("Записей пока нет.")
        return
    text = "Текущие (active) записи в базе:\n\n"
    for r in rows:
        bid, phone, name, services_json, date_iso, created_at = r
        services = ", ".join(json.loads(services_json))
        dt = datetime.fromisoformat(date_iso).date()
        text += f"ID:{bid} {phone} {name or '—'} — {dt.strftime('%d.%m.%y')} {WEEKDAY_RU[dt.weekday()]} — {services}\n"
    await update.message.reply_text(text)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or args[0] != ADMIN_CODE:
        await update.message.reply_text("Неверный админский код.")
        return
    now = datetime.now().date()
    end = now + timedelta(days=MAX_DAYS_AHEAD)
    counts = count_bookings_by_date_range_db(now.isoformat(), end.isoformat())
    text_lines = [f"Сводка записей с {now.strftime('%d.%m.%y')} по {end.strftime('%d.%m.%y')} (воскресенье исключен):\n"]
    dates = get_available_dates(datetime.now())
    kb_buttons = []
    for dt in dates:
        iso = dt.isoformat()
        cnt = counts.get(iso, 0)
        text_lines.append(f"{make_date_label(dt)} — {cnt} записей")
        kb_buttons.append([InlineKeyboardButton(f"{dt.strftime('%d.%m')} — {cnt}", callback_data=f"stats_date|{iso}|{ADMIN_CODE}")])
    kb_buttons.append([InlineKeyboardButton("❌ Закрыть", callback_data="stats_close")])
    await update.message.reply_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(kb_buttons))


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "stats_close":
        await q.edit_message_text("Закрыто.")
        return
    if data.startswith("stats_date|"):
        try:
            _, iso_date, code = data.split("|", 2)
        except Exception:
            await q.edit_message_text("Неправильный формат запроса.")
            return
        if code != ADMIN_CODE:
            await q.edit_message_text("Неверный админский код.")
            return
        rows = get_bookings_for_date_db(iso_date)
        if not rows:
            dt = datetime.fromisoformat(iso_date).date()
            await q.edit_message_text(f"Записей на {make_date_label(dt)} нет.")
            return
        dt = datetime.fromisoformat(iso_date).date()
        header = f"Записи на {make_date_label(dt)}:\n\n"
        text_lines = [header]
        kb = []
        for r in rows:
            bid, phone, name, services_json, date_iso, created_at = r
            services = ", ".join(json.loads(services_json))
            text_lines.append(f"ID:{bid} {phone} {name or '—'} — {services}")
            kb.append([InlineKeyboardButton(f"❌ Пометить отменённым ID:{bid}", callback_data=f"del|{bid}|{ADMIN_CODE}")])
        kb.append([InlineKeyboardButton("◀️ Назад", callback_data=f"stats_back|{ADMIN_CODE}")])
        await q.edit_message_text("\n".join(text_lines), reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("stats_back|"):
        await q.edit_message_text("Назад. Вызовите /stats <код> заново для новой сводки.")
        return


async def delete_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    
    logger.info(f"delete_booking_callback вызван с data={data}")
    
    if data.startswith("del|"):
        try:
            _, bid_str, code = data.split("|", 2)
            bid = int(bid_str)
        except Exception:
            await q.edit_message_text("Неверный формат удаления.")
            return
        if code != ADMIN_CODE:
            await q.edit_message_text("Неверный админский код.")
            return
        delete_booking_db(bid)
        for phone, blist in list(BOOKINGS.items()):
            BOOKINGS[phone] = [b for b in blist if b.get('id') != bid]
            if not BOOKINGS[phone]:
                BOOKINGS.pop(phone, None)
        await q.edit_message_text(f"Запись ID:{bid} помечена как отменённая.")
        return

    if data == "end_session":
        await q.edit_message_text("Если хотите записаться ещё — нажмите /start.")
        return

    if data == "start_again":
        await q.edit_message_text("Хорошо — чтобы записаться ещё, нажмите /start или нажмите кнопку ниже.")
        await q.message.reply_text("Нажмите /start для новой записи.")
        return


def main() -> None:
    # инициализация БД и памяти
    init_db()
    load_bookings_to_memory()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # ConversationHandler с обработкой отмены внутри
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', cmd_start)],
        states={
            SELECT_SERVICE: [
                CallbackQueryHandler(client_cancel_confirm_callback, pattern=r'^(cancel_confirm|cancel_cancel)$'),
                CallbackQueryHandler(svc_callback, pattern=r'^(svc\||svc_done|svc_clear|cancel|back|start_cancel)'),
                MessageHandler(filters.TEXT & (~filters.COMMAND), handle_cancel_phone_in_conv)
            ],
            PHONE: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), phone_handler),
                CommandHandler('cancel', cancel_command),
            ],
            NAME: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), name_handler),
                CommandHandler('skip', skip_name),
                CommandHandler('cancel', cancel_command),
            ],
            DATE: [
                CallbackQueryHandler(date_callback, pattern=r'^(date\||cancel|back)')
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_callback, pattern=r'^(confirm|back|cancel)')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    # admin handlers
    app.add_handler(CommandHandler('bookings', show_bookings_cmd))
    app.add_handler(CommandHandler('stats', stats_cmd))
    app.add_handler(CallbackQueryHandler(stats_callback, pattern=r'^(stats_date\||stats_back\||stats_close)'))
    app.add_handler(CallbackQueryHandler(delete_booking_callback, pattern=r'^(del\||start_again|end_session)'))

    logger.info("Бот запущен и готов к работе")
    app.run_polling()


if __name__ == "__main__":
    main()
