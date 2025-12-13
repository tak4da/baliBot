import os
import asyncio
import logging
from datetime import datetime, date, timedelta
from aiogram import F
from sqlalchemy import text

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Date,
    Text,
    DateTime,
    ForeignKey,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker

# ------------- НАСТРОЙКИ -------------

from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data_simple.db")

# ID чата-конфы магазина (Бализаж), куда слать уведомления
BALIZAG_CHAT_ID = -1002815036494      # правильный ID группы
# ID ветки в Бализаж (если нужна). Пока None — можно потом подставить.
BALIZAG_THREAD_ID = 445

# ID админов, которые могут подтверждать/возвращать замечания
ADMIN_IDS = {377226664, 1705170078}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Base = declarative_base()


class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, nullable=False)
    name = Column(String)


class Inspection(Base):
    __tablename__ = "inspections"
    id = Column(Integer, primary_key=True)
    department_id = Column(Integer, ForeignKey("departments.id"))
    inspector_id = Column(Integer, ForeignKey("users.id"))
    date = Column(Date, default=date.today)
    status = Column(String, default="open")  # open/completed
    created_at = Column(DateTime, default=datetime.utcnow)


class Issue(Base):
    __tablename__ = "issues"
    id = Column(Integer, primary_key=True)
    inspection_id = Column(Integer, ForeignKey("inspections.id"))
    department_id = Column(Integer, ForeignKey("departments.id"))
    photo_url = Column(Text)
    comment = Column(Text)
    status = Column(String, default="open")  # open/pending/fixed
    created_at = Column(DateTime, default=datetime.utcnow)
    fixed_at = Column(DateTime, nullable=True)
    fixed_photo_url = Column(Text)
    fixed_by_tg_id = Column(Integer, nullable=True)  # кто отправлял исправление


# DB
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(bind=engine)

# Добавляем колонку для старой базы, если её ещё нет
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE issues ADD COLUMN fixed_by_tg_id INTEGER"))
except Exception:
    pass

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Отделы
DEPARTMENTS = [
    "Стройка",
    "Столярка",
    "Электрика",
    "Инструменты",
    "Наполка",
    "Плитка",
    "Сантехника",
    "Водянка",
    "Сад",
    "Скобяные",
    "Краски",
    "Декор",
    "Освещение",
    "Хранение",
    "Кухни",
]

# Память процесса: режимы пользователя
# mode: None / 'inspection' / 'fix'
USER_STATE: dict[int, dict] = {}


def get_session():
    return SessionLocal()


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS

def purge_old_data(days: int = 15):
    """
    Удаляет обходы и связанные с ними замечания, которым больше `days` дней.
    """
    cutoff_date = date.today() - timedelta(days=days)

    s = get_session()

    # Находим обходы старше cutoff_date
    old_inspections = (
        s.query(Inspection)
        .filter(Inspection.date < cutoff_date)
        .all()
    )

    if not old_inspections:
        s.close()
        return

    ins_ids = [ins.id for ins in old_inspections]

    # Сначала удаляем связанные замечания
    s.query(Issue).filter(Issue.inspection_id.in_(ins_ids)).delete(
        synchronize_session=False
    )

    # Потом сами обходы
    s.query(Inspection).filter(Inspection.id.in_(ins_ids)).delete(
        synchronize_session=False
    )

    s.commit()
    s.close()


# ---------- КЛАВИАТУРЫ ----------

def main_menu_kb(is_admin_user: bool) -> ReplyKeyboardMarkup:
    """
    Главное меню:
    - обычный пользователь: только 'ИСПРАВИТЬ ЗАМЕЧАНИЯ'
    - админ:
        [СДЕЛАТЬ ОБХОД]
        [ИСТОРИЯ ОБХОДОВ] [ОЧИСТИТЬ ИСТОРИЮ]
        [ИСПРАВИТЬ ЗАМЕЧАНИЯ]
    """
    builder = ReplyKeyboardBuilder()

    if is_admin_user:
        # верхняя строка – одна большая
        builder.button(text="СДЕЛАТЬ ОБХОД")

        # средняя строка – две кнопки
        builder.button(text="ИСТОРИЯ ОБХОДОВ")
        builder.button(text="ОЧИСТИТЬ ИСТОРИЮ")

        # нижняя строка – одна кнопка
        builder.button(text="ИСПРАВИТЬ ЗАМЕЧАНИЯ")

        # раскладка строк: 1 / 2 / 1
        builder.adjust(1, 2, 1)
    else:
        # для обычного пользователя только одна кнопка
        builder.button(text="ИСПРАВИТЬ ЗАМЕЧАНИЯ")
        builder.adjust(1)

    return builder.as_markup(resize_keyboard=True)

def inspection_menu_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="ЗАВЕРШИТЬ ОБХОД")
    builder.button(text="НАЗАД")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def clear_history_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="За 7 дней", callback_data="clear_history:7")
    builder.button(text="За 30 дней", callback_data="clear_history:30")
    builder.button(text="За всё время", callback_data="clear_history:all")
    builder.adjust(2)
    return builder.as_markup()


def departments_kb(prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, d in enumerate(DEPARTMENTS, start=1):
        builder.button(text=d, callback_data=f"{prefix}{i}")
    builder.adjust(3)
    return builder.as_markup()


def fix_issue_kb(issue_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Исправлено", callback_data=f"fix:{issue_id}")
    builder.adjust(1)
    return builder.as_markup()


def admin_review_kb(issue_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ ОК", callback_data=f"approve:{issue_id}")
    builder.button(text="↩️ Вернуть в работу", callback_data=f"return:{issue_id}")
    builder.adjust(2)
    return builder.as_markup()


# ---------- ХЭНДЛЕРЫ ----------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    logger.info("START from %s", message.from_user.id)
    USER_STATE.pop(message.from_user.id, None)

    # авто-очистка старых данных (всё, что старше 15 дней)
    purge_old_data(days=15)

    s = get_session()

    # создаём отделы при первом запуске
    if s.query(Department).count() == 0:
        for name in DEPARTMENTS:
            s.add(Department(name=name))
        s.commit()

    # регистрируем пользователя
    user = s.query(User).filter_by(tg_id=message.from_user.id).first()
    if not user:
        s.add(User(tg_id=message.from_user.id, name=message.from_user.full_name))
        s.commit()

    s.close()

    is_admin_user = is_admin(message.from_user.id)

    await message.answer(
        "Выбери нужное действие",
        reply_markup=main_menu_kb(is_admin_user),
    )



# ===== ОЧИСТКА ИСТОРИИ =====

@dp.message(F.text == "ОЧИСТИТЬ ИСТОРИЮ")
async def ask_clear_history(message: types.Message):
    # только для админов
    if not is_admin(message.from_user.id):
        await message.answer("У тебя нет прав для очистки истории.")
        return

    await message.answer(
        "Выбери период, за который нужно удалить историю обходов и связанных замечаний:",
        reply_markup=clear_history_kb(),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("clear_history:"))
async def clear_history_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У тебя нет прав для этой операции.", show_alert=True)
        return

    _, period = callback.data.split(":")  # "7" / "30" / "all"

    s = get_session()

    if period == "all":
        inspections_q = s.query(Inspection)
        period_text = "за всё время"
    else:
        days = int(period)
        cutoff_date = date.today() - timedelta(days=days)
        inspections_q = s.query(Inspection).filter(
            Inspection.date >= cutoff_date,
            Inspection.date <= date.today(),
        )
        period_text = f"за последние {days} дней"

    inspections = inspections_q.all()

    if not inspections:
        s.close()
        await callback.answer("Под этот период обходов не найдено.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    ins_ids = [i.id for i in inspections]

    issues_deleted = (
        s.query(Issue)
        .filter(Issue.inspection_id.in_(ins_ids))
        .delete(synchronize_session=False)
    )

    inspections_deleted = (
        s.query(Inspection)
        .filter(Inspection.id.in_(ins_ids))
        .delete(synchronize_session=False)
    )

    s.commit()
    s.close()

    await callback.answer("История очищена.", show_alert=True)

    try:
        await callback.message.edit_text(
            f"Очистка истории завершена.\n"
            f"Период: {period_text}.\n"
            f"Удалено обходов: {inspections_deleted}\n"
            f"Удалено замечаний: {issues_deleted}"
        )
    except Exception:
        pass


# ===== ОБХОД =====

@dp.message(F.text == "СДЕЛАТЬ ОБХОД")
async def start_inspection(message: types.Message):
    logger.info("Сделать обход from %s", message.from_user.id)

    if not is_admin(message.from_user.id):
        await message.answer(
            "Сейчас создавать обходы могут только администраторы.\n"
            "Если нужен обход по отделу — напиши своему администратору 👍",
            reply_markup=main_menu_kb(False),
        )
        return

    USER_STATE[message.from_user.id] = {"mode": None}
    await message.answer(
        "Выбери отдел, по которому делаешь обход:",
        reply_markup=departments_kb("ins_dept:"),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("ins_dept:"))
async def choose_inspection_department(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    _, idx = callback.data.split(":")
    idx = int(idx)

    s = get_session()
    dept = s.query(Department).filter_by(id=idx).first()
    user = s.query(User).filter_by(tg_id=user_id).first()
    if not dept or not user:
        s.close()
        await callback.answer("Не удалось найти отдел или пользователя.", show_alert=True)
        return

    ins = Inspection(
        department_id=dept.id,
        inspector_id=user.id,
        date=date.today(),
        status="open",
    )
    s.add(ins)
    s.commit()
    s.refresh(ins)
    inspection_id = ins.id
    s.close()

    USER_STATE[user_id] = {
        "mode": "inspection",
        "inspection_id": inspection_id,
        "department_id": dept.id,
        "last_issue_id": None,
        "last_issue_cleanup": [],
    }

    await callback.message.answer(
        f"Обход по отделу «{dept.name}».\n\n"
        "1️⃣ Сфоткай нарушение\n"
        "2️⃣ Потом отправь короткий комментарий текстом.\n"
        "Повтори для всех замечаний.\n\n"
        "Когда закончишь — нажми «Завершить обход».",
        reply_markup=inspection_menu_kb(),
    )
    await callback.answer()


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    if not state:
        return

    caption = message.caption or ""

    # фото во время обхода
    if state.get("mode") == "inspection":
        photo = message.photo[-1]
        file_id = photo.file_id

        s = get_session()
        issue = Issue(
            inspection_id=state["inspection_id"],
            department_id=state["department_id"],
            photo_url=file_id,
            status="open",
            comment=caption if caption else None,
        )
        s.add(issue)
        s.commit()
        s.refresh(issue)
        issue_id = issue.id
        s.close()

        if caption:
            try:
                await bot.delete_message(chat_id=user_id, message_id=message.message_id)
            except Exception:
                pass

            await bot.send_message(
                chat_id=user_id,
                text=f"Замечание #{issue_id} сохранено. Можешь отправить следующее фото или завершить обход.",
            )

            state["last_issue_id"] = None
            state["last_issue_cleanup"] = []
        else:
            notice_msg = await message.answer(
                f"Замечание #{issue_id} — фото сохранено. Теперь отправь текст: что тут не так?"
            )
            state["last_issue_id"] = issue_id
            state["last_issue_cleanup"] = [message.message_id, notice_msg.message_id]

        return

    # фото при исправлении
    elif state.get("mode") == "fix":
        issue_id = state.get("issue_id")
        if not issue_id:
            return

        photo = message.photo[-1]
        file_id = photo.file_id

        # фото + подпись = комментарий, фото без подписи = "(без комментария)"
        fix_comment = caption if caption else "(без комментария)"

        s = get_session()
        issue = s.query(Issue).filter_by(id=issue_id).first()
        if not issue:
            s.close()
            USER_STATE.pop(user_id, None)
            await message.answer(
                "Не нашёл это замечание. Попробуй ещё раз через меню «Исправить замечания»."
            )
            return

        original_photo_id = issue.photo_url
        dept = s.query(Department).filter_by(id=issue.department_id).first()
        dept_name = dept.name if dept else f"Отдел #{issue.department_id}"
        original_comment = issue.comment or "(без текста)"

        issue.fixed_photo_url = file_id
        issue.fixed_at = datetime.utcnow()
        issue.status = "pending"
        issue.fixed_by_tg_id = message.from_user.id
        s.commit()
        s.close()

        cleanup_ids = state.get("cleanup_ids", [])
        cleanup_ids.append(message.message_id)
        for mid in cleanup_ids:
            try:
                await bot.delete_message(chat_id=user_id, message_id=mid)
            except Exception:
                pass

        USER_STATE.pop(user_id, None)

        await bot.send_message(
            chat_id=user_id,
            text=f"Супер, замечание #{issue_id} отправлено на проверку. Спасибо! 🙌",
        )

        if ADMIN_IDS:
            for admin_id in ADMIN_IDS:
                try:
                    if original_photo_id:
                        await bot.send_photo(
                            admin_id,
                            original_photo_id,
                            caption=(
                                f"До исправления. Замечание #{issue_id} по отделу «{dept_name}».\n"
                                f"{original_comment}"
                            ),
                        )

                    caption_after = (
                        f"После исправления замечания #{issue_id} по отделу «{dept_name}».\n"
                        f"Исправил: {message.from_user.full_name}\n\n"
                        f"Комментарий к исправлению: {fix_comment}"
                    )
                    await bot.send_photo(
                        admin_id,
                        file_id,
                        caption=caption_after,
                        reply_markup=admin_review_kb(issue_id),
                    )
                except Exception as e:
                    logger.exception(
                        "Не удалось отправить уведомление админу %s: %s",
                        admin_id,
                        e,
                    )

@dp.message(
    F.text
    & (~F.text.startswith("/"))
    & (F.text != "СДЕЛАТЬ ОБХОД")
    & (F.text != "ИСТОРИЯ ОБХОДОВ")
    & (F.text != "ОЧИСТИТЬ ИСТОРИЮ")
    & (F.text != "ЗАВЕРШИТЬ ОБХОД")
    & (F.text != "НАЗАД")
    & (F.text != "ИСПРАВИТЬ ЗАМЕЧАНИЯ")
)
async def handle_text_comment(message: types.Message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    if not state:
        return

    # комментарий к исправлению (после фото без подписи)
    if state.get("mode") == "fix":
        issue_id = state.get("issue_id")
        fixed_photo_id = state.get("fixed_photo_id")

        if not issue_id:
            return

        # Вариант "только комментарий"
        if not fixed_photo_id:
            fix_comment = message.text

            s = get_session()
            issue = s.query(Issue).filter_by(id=issue_id).first()
            if not issue:
                s.close()
                USER_STATE.pop(user_id, None)
                await message.answer(
                    "Не нашёл это замечание. Попробуй ещё раз через меню «Исправить замечания»."
                )
                return

            original_photo_id = issue.photo_url
            dept = s.query(Department).filter_by(id=issue.department_id).first()
            dept_name = dept.name if dept else f"Отдел #{issue.department_id}"
            original_comment = issue.comment or "(без текста)"

            issue.fixed_photo_url = None
            issue.fixed_at = datetime.utcnow()
            issue.status = "pending"
            issue.fixed_by_tg_id = message.from_user.id
            s.commit()
            s.close()

            cleanup_ids = state.get("cleanup_ids", [])
            cleanup_ids.append(message.message_id)
            for mid in cleanup_ids:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=mid)
                except Exception:
                    pass

            USER_STATE.pop(user_id, None)

            await bot.send_message(
                chat_id=user_id,
                text=f"Супер, замечание #{issue_id} отправлено на проверку. Спасибо! 🙌",
            )

            if ADMIN_IDS:
                for admin_id in ADMIN_IDS:
                    try:
                        if original_photo_id:
                            await bot.send_photo(
                                admin_id,
                                original_photo_id,
                                caption=(
                                    f"До исправления. Замечание #{issue_id} по отделу «{dept_name}».\n"
                                    f"{original_comment}"
                                ),
                            )

                        await bot.send_message(
                            admin_id,
                            text=(
                                f"После исправления замечания #{issue_id} по отделу «{dept_name}».\n"
                                f"Исправил: {message.from_user.full_name}\n\n"
                                f"Комментарий к исправлению: {fix_comment}\n"
                                f"Фото после исправления: (не приложено)"
                            ),
                            reply_markup=admin_review_kb(issue_id),
                        )
                    except Exception as e:
                        logger.exception(
                            "Не удалось отправить уведомление админу %s: %s",
                            admin_id,
                            e,
                        )

            return

        # Старый режим "сначала фото без подписи -> потом текст" (оставляем, чтобы ничего не ломать)
        fix_comment = message.text

        s = get_session()
        issue = s.query(Issue).filter_by(id=issue_id).first()
        if not issue:
            s.close()
            USER_STATE.pop(user_id, None)
            await message.answer(
                "Не нашёл это замечание. Попробуй ещё раз через меню «Исправить замечания»."
            )
            return

        original_photo_id = issue.photo_url
        dept = s.query(Department).filter_by(id=issue.department_id).first()
        dept_name = dept.name if dept else f"Отдел #{issue.department_id}"
        original_comment = issue.comment or "(без текста)"

        issue.fixed_photo_url = fixed_photo_id
        issue.fixed_at = datetime.utcnow()
        issue.status = "pending"
        issue.fixed_by_tg_id = message.from_user.id
        s.commit()
        s.close()

        cleanup_ids = state.get("cleanup_ids", [])
        cleanup_ids.append(message.message_id)
        for mid in cleanup_ids:
            try:
                await bot.delete_message(chat_id=user_id, message_id=mid)
            except Exception:
                pass

        USER_STATE.pop(user_id, None)

        await bot.send_message(
            chat_id=user_id,
            text=f"Супер, замечание #{issue_id} отправлено на проверку. Спасибо! 🙌",
        )

        if ADMIN_IDS:
            for admin_id in ADMIN_IDS:
                try:
                    if original_photo_id:
                        await bot.send_photo(
                            admin_id,
                            original_photo_id,
                            caption=(
                                f"До исправления. Замечание #{issue_id} по отделу «{dept_name}».\n"
                                f"{original_comment}"
                            ),
                        )

                    caption_after = (
                        f"После исправления замечания #{issue_id} по отделу «{dept_name}».\n"
                        f"Исправил: {message.from_user.full_name}\n\n"
                        f"Комментарий к исправлению: {fix_comment}"
                    )

                    await bot.send_photo(
                        admin_id,
                        fixed_photo_id,
                        caption=caption_after,
                        reply_markup=admin_review_kb(issue_id),
                    )
                except Exception as e:
                    logger.exception(
                        "Не удалось отправить уведомление админу %s: %s",
                        admin_id,
                        e,
                    )
        return

    # комментарий к замечанию при обходе
    if state.get("mode") != "inspection" or not state.get("last_issue_id"):
        return

    issue_id = state["last_issue_id"]
    s = get_session()
    issue = s.query(Issue).filter_by(id=issue_id).first()
    if not issue:
        s.close()
        state["last_issue_id"] = None
        state["last_issue_cleanup"] = []
        await message.answer("Не получилось привязать комментарий к замечанию, попробуй ещё раз.")
        return

    issue.comment = message.text
    s.commit()
    s.close()

    cleanup_ids = state.get("last_issue_cleanup", [])
    for mid in cleanup_ids:
        try:
            await bot.delete_message(chat_id=user_id, message_id=mid)
        except Exception:
            pass

    try:
        await bot.delete_message(chat_id=user_id, message_id=message.message_id)
    except Exception:
        pass

    state["last_issue_id"] = None
    state["last_issue_cleanup"] = []

    await bot.send_message(
        chat_id=user_id,
        text="Комментарий сохранён. Можешь отправить следующее фото или завершить обход.",
    )


@dp.message(F.text == "ЗАВЕРШИТЬ ОБХОД")
async def finish_inspection(message: types.Message):
    user_id = message.from_user.id
    state = USER_STATE.get(user_id)
    if not state or state.get("mode") != "inspection":
        await message.answer(
            "У тебя сейчас нет активного обхода.",
            reply_markup=main_menu_kb(is_admin(user_id)),
        )
        return

    s = get_session()
    ins = s.query(Inspection).filter_by(id=state["inspection_id"]).first()
    dept_name = "неизвестный отдел"
    inspector_name = message.from_user.full_name
    ins_date = date.today()

    if ins:
        ins.status = "completed"
        s.commit()

        dept = s.query(Department).filter_by(id=ins.department_id).first()
        if dept:
            dept_name = dept.name

        inspector = s.query(User).filter_by(id=ins.inspector_id).first()
        if inspector and inspector.name:
            inspector_name = inspector.name

        ins_date = ins.date

    issues_count = (
        s.query(Issue)
        .filter(Issue.inspection_id == state["inspection_id"])
        .count()
    )

    s.close()

    if BALIZAG_CHAT_ID:
        try:
            control_date = ins_date + timedelta(days=7)

            text = (
                f"Завершён обход по бализажу\n"
                f"📌 Отдел: {dept_name}\n"
                f"⚠️ Замечаний: {issues_count}\n"
                f"👷 Аудитор: {inspector_name}\n"
                f"📅 Дата аудита: {ins_date.strftime('%d.%m.%Y')}\n"
                f"📍 Исправить до: {control_date.strftime('%d.%m.%Y')}\n"
                f"🤖 Перейти в бота: @BalisageAudit_bot"
            )

            await bot.send_message(
                chat_id=BALIZAG_CHAT_ID,
                text=text,
                message_thread_id=BALIZAG_THREAD_ID,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.exception(
                "Не удалось отправить уведомление о завершении обхода в BALIZAG_CHAT_ID: %s",
                e,
            )

    USER_STATE.pop(user_id, None)
    await message.answer(
        "Обход завершён. Всё сохранил.",
        reply_markup=main_menu_kb(is_admin(user_id)),
    )


@dp.message(F.text == "Отмена")
async def cancel_any(message: types.Message):
    USER_STATE.pop(message.from_user.id, None)
    await message.answer(
        "Действие отменено.",
        reply_markup=main_menu_kb(is_admin(message.from_user.id)),
    )


# ===== ИСПРАВЛЕНИЕ ЗАМЕЧАНИЙ =====

@dp.message(F.text == "ИСПРАВИТЬ ЗАМЕЧАНИЯ")
async def start_fix_text(message: types.Message):
    # если кто-то вдруг сам напишет текстом
    await start_fix_flow(message)


@dp.callback_query(lambda c: c.data == "menu:fix")
async def start_fix_inline(callback: types.CallbackQuery):
    await start_fix_flow(callback.message)
    await callback.answer()


async def start_fix_flow(message: types.Message):
    USER_STATE[message.from_user.id] = {"mode": None}
    await message.answer(
        "Выбери отдел, в котором будешь исправлять замечания:",
        reply_markup=departments_kb("fix_dept:"),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("fix_dept:"))
async def show_issues_for_fix(callback: types.CallbackQuery):
    _, idx = callback.data.split(":")
    idx = int(idx)
    s = get_session()
    dept = s.query(Department).filter_by(id=idx).first()
    if not dept:
        s.close()
        await callback.message.answer("Отдел не найден.")
        await callback.answer()
        return

    issues = (
        s.query(Issue)
        .filter(
            Issue.department_id == dept.id,
            Issue.status.in_(["open", "pending"]),
        )
        .order_by(Issue.created_at.asc())
        .all()
    )
    s.close()

    if not issues:
        await callback.message.answer(f"По отделу «{dept.name}» открытых замечаний нет.")
        await callback.answer()
        return

    await callback.message.answer(f"Открытые замечания по отделу «{dept.name}»:")

    for it in issues:
        if it.status == "open":
            status_ru = "открыто"
        elif it.status == "pending":
            status_ru = "на проверке"
        else:
            status_ru = it.status

        text = (
            f"#{it.id}\n"
            f"{it.comment or '(без текста)'}\n"
            f"Статус: {status_ru}"
        )
        if it.photo_url:
            try:
                await bot.send_photo(
                    callback.from_user.id,
                    it.photo_url,
                    caption=text,
                    reply_markup=fix_issue_kb(it.id),
                )
            except Exception:
                await bot.send_message(
                    callback.from_user.id,
                    text + "\n(фото недоступно)",
                    reply_markup=fix_issue_kb(it.id),
                )
        else:
            await bot.send_message(
                callback.from_user.id,
                text,
                reply_markup=fix_issue_kb(it.id),
            )

    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("fix:"))
async def mark_issue_fixed(callback: types.CallbackQuery):
    _, issue_id_str = callback.data.split(":")
    issue_id = int(issue_id_str)

    prompt_msg = await callback.message.answer(
        f"Исправление для замечания #{issue_id}.\n"
        "Отправь ЛЮБОЙ вариант:\n"
        "1) фото\n"
        "2) комментарий\n"
        "3) фото + комментарий (в подписи)\n"
    )

    USER_STATE[callback.from_user.id] = {
        "mode": "fix",
        "issue_id": issue_id,
        "cleanup_ids": [callback.message.message_id, prompt_msg.message_id],
        "fixed_photo_id": None,
    }

    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("approve:"))
async def approve_issue(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Эта кнопка только для админов.", show_alert=True)
        return

    _, issue_id_str = callback.data.split(":")
    issue_id = int(issue_id_str)

    s = get_session()
    issue = s.query(Issue).filter_by(id=issue_id).first()
    if not issue:
        s.close()
        await callback.answer("Это замечание уже обработано.")
        try:
            await callback.message.delete()
        except Exception:
            pass
        try:
            await bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id - 1,
            )
        except Exception:
            pass
        return

    issue.status = "fixed"
    s.commit()
    s.close()

    await callback.answer("Замечание закрыто. 👍")

    try:
        await callback.message.delete()
    except Exception:
        pass
    try:
        await bot.delete_message(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id - 1,
        )
    except Exception:
        pass


@dp.callback_query(lambda c: c.data and c.data.startswith("return:"))
async def return_issue_to_work(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Эта кнопка только для админов.", show_alert=True)
        return

    _, issue_id_str = callback.data.split(":")
    issue_id = int(issue_id_str)

    s = get_session()
    issue = s.query(Issue).filter_by(id=issue_id).first()
    if not issue:
        s.close()
        await callback.answer("Это замечание уже обработано.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    fixed_by_tg_id = issue.fixed_by_tg_id
    comment_text = issue.comment or "(без текста)"

    dept = s.query(Department).filter_by(id=issue.department_id).first()
    dept_name = dept.name if dept else f"Отдел #{issue.department_id}"

    issue.status = "open"
    issue.fixed_photo_url = None
    issue.fixed_at = None
    s.commit()
    s.close()

    await callback.answer("Замечание возвращено в работу.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if fixed_by_tg_id:
        try:
            await bot.send_message(
                chat_id=fixed_by_tg_id,
                text=(
                    f"Твоё исправление по замечанию #{issue_id} вернули в работу.\n"
                    f"Отдел: {dept_name}\n"
                    f"Текст замечания: {comment_text}\n\n"
                    "Пожалуйста, проверь ещё раз и исправь️🙂"
                ),
            )
        except Exception as e:
            logger.exception(
                "Не удалось отправить уведомление сотруднику %s: %s",
                fixed_by_tg_id,
                e,
            )


# ===== ИСТОРИЯ ОБХОДОВ =====
@dp.message(F.text == "ИСТОРИЯ ОБХОДОВ")
async def history(message: types.Message):
    # только для админов
    if not is_admin(message.from_user.id):
        await message.answer("У тебя нет прав для просмотра истории.")
        return

    s = get_session()
    inspections = s.query(Inspection).all()
    issues = s.query(Issue).all()

    total_inspections = len(inspections)
    completed = sum(1 for i in inspections if i.status == "completed")
    active = total_inspections - completed

    total_issues = len(issues)
    open_issues = sum(1 for it in issues if it.status in ("open", "pending"))
    closed_issues = sum(1 for it in issues if it.status == "fixed")

    s.close()

    lines = []
    lines.append("*Общая статистика*")
    lines.append(f"Обходов: *{total_inspections}*")
    lines.append(f"✔ Завершено: *{completed}*")
    lines.append(f"🟡 Активных: *{active}*")
    lines.append("")
    lines.append(f"⚠️ Замечаний: *{total_issues}*")
    lines.append(f" В работе: *{open_issues}*")
    lines.append(f"✔ Закрыто: *{closed_issues}*")
    lines.append("")
    lines.append("Чтобы посмотреть детали по конкретному отделу — выбери его ниже 👇")

    text = "\n".join(lines)

    await message.answer(
        text,
        parse_mode="Markdown",
        reply_markup=departments_kb("hist_dept:"),
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("hist_dept:"))
async def history_by_department(callback: types.CallbackQuery):
    _, idx = callback.data.split(":")
    dept_id = int(idx)

    s = get_session()
    dept = s.query(Department).filter_by(id=dept_id).first()
    if not dept:
        s.close()
        await callback.answer("Отдел не найден.", show_alert=True)
        return

    inspections = s.query(Inspection).filter_by(department_id=dept.id).all()
    issues = s.query(Issue).filter_by(department_id=dept.id).all()

    total_inspections = len(inspections)
    completed = sum(1 for i in inspections if i.status == "completed")
    active = total_inspections - completed

    total_issues = len(issues)
    open_issues = sum(1 for it in issues if it.status in ("open", "pending"))
    closed_issues = sum(1 for it in issues if it.status == "fixed")

    s.close()

    lines = []
    lines.append(f"*{dept.name}*")
    lines.append(f"Обходов: *{total_inspections}*")
    lines.append(f"✔ Завершено: *{completed}*")
    lines.append(f"🟡 Активных: *{active}*")
    lines.append("")
    lines.append(f"⚠️ Замечаний: *{total_issues}*")
    lines.append(f" В работе: *{open_issues}*")
    lines.append(f"✔ Закрыто: *{closed_issues}*")

    text = "\n".join(lines)
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


# ===== ЗАПУСК =====

async def main():
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    print("Бот запущен. Нажми Ctrl+C для остановки.")
    asyncio.run(main())
